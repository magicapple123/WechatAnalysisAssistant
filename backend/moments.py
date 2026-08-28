"""朋友圈读取与导出领域服务。

本模块只依赖调用方传入的已解密 SQLite 连接。导入模块不会探测微信、读取配置、
访问网络或创建文件；只有显式调用 :meth:`MomentsExporter.export` 且传入
``download_media=True`` 时，才会尝试下载朋友圈图片。
"""

from __future__ import annotations

import base64
import binascii
import csv
import html
import http.client
import io
import ipaddress
import json
import re
import socket
import sqlite3
import ssl
import tempfile
import time
import warnings
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .image_service import normalize_wechat_image_payload
from .sns_media_crypto import SnsMediaCryptoError, decrypt_sns_image

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - capability is checked at parse time
    zstd = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is part of the application deps
    Image = None


SNS_XML_MAX_LENGTH = 200_000
MEDIA_MAX_FILE_BYTES = 8 * 1024 * 1024
MEDIA_MAX_TOTAL_BYTES = 32 * 1024 * 1024
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 10.0
MEDIA_MAX_TOTAL_SECONDS = 120.0
MEDIA_MAX_PIXELS = 40_000_000
MEDIA_MAX_FRAMES = 200
MEDIA_MAX_ATTEMPTS = 100

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
_UNSAFE_XML_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_INVALID_XML_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_BARE_AMP_RE = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)"
)
_TEXT_ONLY_NODE_NAMES = (
    "content",
    "title",
    "description",
    "nickname",
    "contentDesc",
    "appname",
    "sourceName",
    "sourcename",
    "poiName",
    "displayName",
    "feeddesc",
)
_TEXT_ONLY_NODE_RE = re.compile(
    r"(<(" + "|".join(_TEXT_ONLY_NODE_NAMES) + r")\b[^>]*>)(.*?)(</\2>)",
    re.DOTALL,
)

_CONTENT_TYPE_NAMES = {
    1: "图文",
    2: "纯文本",
    3: "链接",
    5: "视频链接",
    7: "位置",
    15: "视频",
    28: "短视频",
    30: "音乐",
    34: "笔记",
    42: "小程序",
    54: "直播",
}

_ALLOWED_MEDIA_DOMAINS = (
    "shmmsns.qpic.cn",
    "szmmsns.qpic.cn",
    "mmsns.qpic.cn",
    "mmbiz.qpic.cn",
    "wxapp.tc.qq.com",
)
_IMAGE_MEDIA_TYPES = {"", "2"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class MomentsError(Exception):
    """朋友圈功能的基础异常。"""


class MomentsSchemaError(MomentsError):
    """朋友圈数据库缺少必需表或字段。"""


class MomentsSelectionError(MomentsError):
    """导出范围不是明确、有效的联系人选择。"""


class MomentsExportError(MomentsError):
    """朋友圈导出失败。"""


class MomentsMediaDownloadError(MomentsError):
    """朋友圈媒体因网络、安全策略或格式问题无法下载。"""


@dataclass(frozen=True)
class DownloadedMedia:
    """经过格式校验的内存图片。"""

    data: bytes
    mime_type: str
    extension: str

    def data_uri(self) -> str:
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"


def moments_media_download_candidates(
    media: dict[str, Any],
) -> list[tuple[str, str, str, str]]:
    """Return full, thumbnail, then low-band URL metadata without duplicates."""

    candidates: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for prefix in ("url", "thumb", "low_band"):
        url_key = "url" if prefix == "url" else f"{prefix}_url"
        value = (
            _as_text(media.get(url_key)),
            _as_text(media.get(f"{prefix}_token")),
            _as_text(media.get(f"{prefix}_key")),
            _as_text(media.get(f"{prefix}_enc_idx")),
        )
        if value[0] and value not in seen:
            seen.add(value)
            candidates.append(value)
    return candidates


@dataclass(frozen=True)
class MomentsExportResult:
    """一次导出的可序列化结果。"""

    path: Path
    posts_count: int
    contacts_count: int
    media_downloaded: int = 0
    media_failed: int = 0
    format: str = "html"
    is_archive: bool = False

    @property
    def post_count(self) -> int:
        """兼容单数命名。"""
        return self.posts_count

    @property
    def contact_count(self) -> int:
        """兼容单数命名。"""
        return self.contacts_count

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["path"] = str(self.path)
        return result


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _canonical_uint64_id(value: Any) -> str:
    """Expose WCDB's signed 64-bit IDs as their canonical uint64 string."""
    text = _as_text(value).strip()
    if not re.fullmatch(r"-?\d+", text):
        return text
    try:
        number = int(text)
    except ValueError:
        return text
    if -(1 << 63) <= number < 0:
        number += 1 << 64
    if 0 <= number < (1 << 64):
        return str(number)
    return text


def _first_nonzero_id(*values: Any) -> str:
    """Return the first usable SNS identifier, treating the text ``"0"`` as empty."""

    for value in values:
        identifier = _canonical_uint64_id(value).strip()
        if identifier and identifier != "0":
            return identifier
    return ""


def _format_timestamp(value: Any) -> str:
    timestamp = _as_int(value)
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _quote_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(row[1]).lower(): str(row[1]) for row in rows}


def _find_table(conn: sqlite3.Connection, expected: str) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=lower(?) LIMIT 1",
            (expected,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return str(row[0]) if row else None


def _decode_zstd_limited(raw: bytes) -> Optional[bytes]:
    if zstd is None:
        return None
    try:
        with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
            decoded = reader.read(SNS_XML_MAX_LENGTH + 1)
        if len(decoded) > SNS_XML_MAX_LENGTH:
            return None
        return decoded
    except Exception:
        return None


def decode_sns_content(value: Any) -> str:
    """将 SnsTimeLine.content 的常见编码转换为 XML 文本。

    支持裸字符串、裸 UTF-8 bytes、zstd bytes、hex 字符串和 base64 字符串。
    返回空字符串表示无法安全解码。
    """

    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if raw.startswith(_ZSTD_MAGIC):
            decoded = _decode_zstd_limited(raw)
            if decoded is None:
                return ""
            raw = decoded
        if len(raw) > SNS_XML_MAX_LENGTH:
            return ""
        return html.unescape(raw.decode("utf-8", errors="ignore").strip())

    text = str(value).strip()
    if not text:
        return ""
    if len(text) > SNS_XML_MAX_LENGTH * 3:
        return ""
    if text.lstrip().startswith("<"):
        decoded_text = html.unescape(text)
        return decoded_text if len(decoded_text) <= SNS_XML_MAX_LENGTH else ""

    compact = "".join(text.split())
    if (
        len(compact) >= 16
        and len(compact) % 2 == 0
        and _HEX_RE.fullmatch(compact)
    ):
        try:
            return decode_sns_content(bytes.fromhex(compact))
        except ValueError:
            pass
    if (
        len(compact) >= 24
        and len(compact) % 4 == 0
        and _BASE64_RE.fullmatch(compact)
    ):
        try:
            return decode_sns_content(base64.b64decode(compact, validate=True))
        except (ValueError, binascii.Error):
            pass

    decoded_text = html.unescape(text)
    return decoded_text if len(decoded_text) <= SNS_XML_MAX_LENGTH else ""


def sanitize_sns_xml(xml_text: str) -> str:
    """修复老朋友圈的伪 XML，同时保持 CDATA 内容不变。"""

    cleaned = _INVALID_XML_CONTROL_RE.sub("", xml_text)
    parts: list[str] = []
    last = 0
    for match in _CDATA_RE.finditer(cleaned):
        parts.append(_BARE_AMP_RE.sub("&amp;", cleaned[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_BARE_AMP_RE.sub("&amp;", cleaned[last:]))
    cleaned = "".join(parts)

    def escape_text_node(match: re.Match[str]) -> str:
        return (
            match.group(1)
            + match.group(3).replace("<", "&lt;").replace(">", "&gt;")
            + match.group(4)
        )

    return _TEXT_ONLY_NODE_RE.sub(escape_text_node, cleaned)


def _find_text(element: Optional[ET.Element], path: str, default: str = "") -> str:
    if element is None:
        return default
    value = element.findtext(path)
    return _as_text(value) if value is not None else default


def _parse_media(timeline: ET.Element) -> list[dict[str, Any]]:
    media_items: list[dict[str, Any]] = []
    for media in timeline.findall(".//media"):
        thumb = media.find("thumb")
        url = media.find("url")
        low_band = media.find("lowBandUrl")
        size = media.find("size")
        media_type = _find_text(media, "type") or _find_text(media, "mediaType")
        video_duration = (
            _find_text(media, "videoDuration")
            or _find_text(media, "videoPlayDuration")
            or "0"
        )
        item: dict[str, Any] = {
            "type": media_type,
            "media_type": media_type,
            "sub_type": _find_text(media, "sub_type"),
            "is_live_photo": media.find("LivePhoto") is not None,
            "video_duration": video_duration,
            "video_play_duration": video_duration,
            "thumb_url": (
                _as_text(thumb.text) if thumb is not None
                else _find_text(media, "thumbUrl")
            ),
            "url": _as_text(url.text) if url is not None else "",
            "width": (
                _as_text(size.get("width")) if size is not None
                else _find_text(media, "width")
            ),
            "height": (
                _as_text(size.get("height")) if size is not None
                else _find_text(media, "height")
            ),
            "total_size": _as_text(size.get("totalSize")) if size is not None else "",
            "media_id": _find_text(media, "id"),
        }
        if thumb is not None:
            item["thumb_key"] = _as_text(thumb.get("key"))
            item["thumb_token"] = _as_text(thumb.get("token"))
            item["thumb_enc_idx"] = _as_text(
                thumb.get("enc_idx") or thumb.get("encIdx")
            )
        if url is not None:
            item["url_md5"] = _as_text(url.get("md5"))
            item["url_key"] = _as_text(url.get("key"))
            item["url_token"] = _as_text(url.get("token"))
            item["url_enc_idx"] = _as_text(
                url.get("enc_idx") or url.get("encIdx")
            )
        if low_band is not None:
            item["low_band_url"] = _as_text(low_band.text)
            item["low_band_key"] = _as_text(low_band.get("key"))
            item["low_band_token"] = _as_text(low_band.get("token"))
            item["low_band_enc_idx"] = _as_text(
                low_band.get("enc_idx") or low_band.get("encIdx")
            )
        media_items.append(item)
    return media_items


def _parse_finder_feed(timeline: ET.Element) -> Optional[dict[str, Any]]:
    finder = timeline.find(".//finderFeed")
    if finder is None:
        return None
    def finder_text(name: str, default: str = "") -> str:
        return _find_text(finder, name) or _find_text(finder, f".//{name}", default)

    result = {
        "media_type": finder_text("mediaType"),
        "thumb_url": finder_text("thumbUrl"),
        "url": finder_text("url"),
        "video_play_duration": finder_text("videoPlayDuration", "0"),
        "object_id": _find_text(finder, "objectId"),
        "nickname": _find_text(finder, "nickname"),
        "username": _find_text(finder, "username"),
        "description": _find_text(finder, "desc") or _find_text(finder, "description"),
    }
    return result


def _interaction_type_name(interaction_type: int) -> str:
    if interaction_type == 1:
        return "点赞"
    if interaction_type == 2:
        return "评论"
    return f"未知({interaction_type})"


def _parse_xml_interactions(root: ET.Element) -> list[dict[str, Any]]:
    """Read the complete interaction snapshot embedded in ``LocalExtraInfo``.

    ``SnsMessage_tmp3`` is a notification stream and does not contain all
    comments (notably comments sent by the current account).  The two lists in
    the timeline XML are therefore the authoritative source for a post's
    visible likes and comments.
    """

    local_extra = root.find(".//LocalExtraInfo")
    if local_extra is None:
        return []

    interactions: list[dict[str, Any]] = []
    for container_name, interaction_type in (
        ("like_user_list", 1),
        ("comment_user_list", 2),
    ):
        container = local_extra.find(container_name)
        if container is None:
            continue
        for item in container.findall("./user_comment"):
            if _as_int(_find_text(item, "b_deleted", "0")) != 0:
                continue
            comment_id = _first_nonzero_id(
                _find_text(item, "comment_64id"),
                _find_text(item, "comment64_id"),
                _find_text(item, "comment_id"),
            )
            create_time = _as_int(_find_text(item, "create_time", "0"))
            interactions.append(
                {
                    "id": comment_id,
                    "create_time": create_time,
                    "create_time_str": _format_timestamp(create_time),
                    "type": interaction_type,
                    "type_name": _interaction_type_name(interaction_type),
                    "from_username": _find_text(item, "username"),
                    "from_nickname": _find_text(item, "nickname"),
                    # XML ref_username is the actual reply target.  This is
                    # intentionally distinct from SnsMessage_tmp3.to_username,
                    # which generally identifies the notification recipient.
                    "to_username": (
                        _find_text(item, "ref_username")
                        if interaction_type == 2
                        else ""
                    ),
                    "to_nickname": (
                        _find_text(item, "ref_nickname")
                        if interaction_type == 2
                        else ""
                    ),
                    "content": (
                        _find_text(item, "content")
                        if interaction_type == 2
                        else ""
                    ),
                }
            )
    return interactions


def parse_sns_timeline_content(value: Any) -> Optional[dict[str, Any]]:
    """安全解析一条 SnsTimeLine.content；失败返回 ``None``。"""

    decoded = decode_sns_content(value)
    if not decoded or len(decoded) > SNS_XML_MAX_LENGTH:
        return None
    if _UNSAFE_XML_RE.search(decoded):
        return None
    try:
        root = ET.fromstring(sanitize_sns_xml(decoded))
    except (ET.ParseError, ValueError):
        return None

    timeline = root if root.tag == "TimelineObject" else root.find(".//TimelineObject")
    if timeline is None:
        return None
    content_object = timeline.find(".//ContentObject")
    content_type = _as_int(_find_text(content_object, "type", "0"))

    location_element = timeline.find(".//location")
    location: Optional[dict[str, str]] = None
    if location_element is not None:
        location = {
            "latitude": _as_text(location_element.get("latitude")),
            "longitude": _as_text(location_element.get("longitude")),
            "poi_name": _as_text(location_element.get("poiName")),
            "city": _as_text(location_element.get("city")),
            "country": _as_text(location_element.get("country")),
        }
        if not any(location.values()):
            location = None

    create_time = _as_int(_find_text(timeline, "createTime", "0"))
    media_items = _parse_media(timeline)
    # WeChat 4.x also uses ContentObject type 54 for Live Photo/image albums.
    # The older static mapping calls every type-54 item a livestream, which is
    # wrong when the object is just a list of ordinary image media.  Classify
    # from the payload structure and keep "直播" only as the non-image fallback.
    content_type_name = _CONTENT_TYPE_NAMES.get(
        content_type, f"未知({content_type})"
    )
    if (
        content_type == 54
        and media_items
        and timeline.find(".//finderFeed") is None
        and all(_as_text(item.get("type")) == "2" for item in media_items)
    ):
        content_type_name = "图文"
    return {
        "xml_id": _find_text(timeline, "id"),
        "xml_username": _find_text(timeline, "username"),
        "create_time": create_time,
        "create_time_str": _format_timestamp(create_time),
        "content": _find_text(timeline, "contentDesc"),
        "content_type": content_type,
        "content_type_name": content_type_name,
        "title": _find_text(content_object, "title"),
        "description": _find_text(content_object, "description"),
        "content_url": _find_text(content_object, "contentUrl"),
        "nickname": _find_text(root, ".//LocalExtraInfo/nickname"),
        "is_private": _find_text(timeline, "private", "0") == "1",
        # isTop is the only explicit per-post pin signal persisted here.
        # guideTop is only a client hint, and SQLite insertion order changes
        # during profile refreshes; neither may promote an ordinary post.
        "is_top": _find_text(timeline, "isTop", "0").strip() == "1",
        "guide_top": _find_text(timeline, "guideTop", "0").strip() == "1",
        "location": location,
        "media": media_items,
        "finder_feed": _parse_finder_feed(timeline),
        # Kept private until hydration so database notification rows can be
        # merged and raw parser consumers do not receive an incomplete field.
        "_xml_interactions": _parse_xml_interactions(root),
    }


class MomentsService:
    """从调用方提供的已解密数据库连接读取朋友圈。"""

    def __init__(
        self,
        sns_conn: sqlite3.Connection,
        contact_conn: Optional[sqlite3.Connection] = None,
        account_id: str = "",
        account_display_name: str = "",
        *,
        display_name: Optional[str] = None,
    ):
        self.sns_conn = sns_conn
        self.contact_conn = contact_conn
        self.account_id = _as_text(account_id)
        self.account_display_name = _as_text(
            account_display_name if display_name is None else display_name
        )
        self._posts_cache: Optional[list[dict[str, Any]]] = None
        self._contacts_cache: Optional[list[dict[str, Any]]] = None
        self._contact_names_cache: Optional[dict[str, str]] = None
        self._current_pin_ids: dict[str, set[str]] = {}
        self._unavailable_pinned_counts: dict[str, int] = {}
        self.parse_failures = 0

    def refresh(self) -> None:
        """丢弃解析缓存；不关闭调用方拥有的数据库连接。"""
        self._posts_cache = None
        self._contacts_cache = None
        self._contact_names_cache = None
        self._unavailable_pinned_counts = {}
        self.parse_failures = 0

    def _self_usernames(self) -> set[str]:
        candidates = {self.account_id} if self.account_id else set()
        match = re.fullmatch(r"(wxid_.+)_([0-9a-fA-F]{4,16})", self.account_id)
        if match:
            candidates.add(match.group(1))
        return {item for item in candidates if item}

    def _apply_current_pin_state(self, post: dict[str, Any]) -> None:
        explicit = bool(post.get("is_top"))
        username = _as_text(post.get("username"))
        feed_id = _as_text(post.get("tid"))
        from_snapshot = feed_id in self._current_pin_ids.get(username, set())
        post["is_top"] = explicit or from_snapshot
        if explicit:
            post["_pin_source"] = "timeline_xml"
        elif from_snapshot:
            post["_pin_source"] = "current_profile_snapshot"
        else:
            post.pop("_pin_source", None)

    def _load_current_profile_top_ids(self) -> dict[str, set[str]]:
        """Load the exact current friend-profile top-list partitions.

        WeChat 4.x stores ordinary profile pagination under ``user_name`` and
        the independent ``mmsnstoplist`` pagination under
        ``{user_name}_user_top`` in ``SnsUserTimeLineBreakFlagV2``.  Unlike
        ``SnsTopItem_1``, these rows form the current profile snapshot and are
        replaced when the real profile is refreshed.  ``break_flag`` only marks
        the end of pagination and is not itself a per-item pin flag.
        """

        table = _find_table(self.sns_conn, "SnsUserTimeLineBreakFlagV2")
        if not table:
            return {}
        columns = _table_columns(self.sns_conn, table)
        tid_column = columns.get("tid")
        username_column = columns.get("user_name") or columns.get("username")
        break_column = columns.get("break_flag")
        if not tid_column or not username_column or not break_column:
            return {}
        try:
            rows = self.sns_conn.execute(
                f"SELECT {_quote_identifier(tid_column)}, "
                f"{_quote_identifier(username_column)}, "
                f"{_quote_identifier(break_column)} "
                f"FROM {_quote_identifier(table)}"
            ).fetchall()
        except sqlite3.DatabaseError:
            return {}

        suffix = "_user_top"
        candidates: dict[str, set[str]] = defaultdict(set)
        complete: set[str] = set()
        for tid, raw_partition, raw_break_flag in rows:
            partition = _as_text(raw_partition).strip()
            if not partition.endswith(suffix) or len(partition) <= len(suffix):
                continue
            username = partition[:-len(suffix)]
            feed_id = _canonical_uint64_id(tid).strip()
            if username and feed_id and feed_id != "0":
                candidates[username].add(feed_id)
                if _as_int(raw_break_flag) != 0:
                    complete.add(username)
        # An absent terminal BreakFlag means WeChat has only cached a partial
        # top-list page.  Keep it out of the official pin section until the
        # profile finishes loading, otherwise older pins could be omitted.
        return {
            username: feed_ids
            for username, feed_ids in candidates.items()
            if username in complete
        }

    def _load_timeline(self) -> list[dict[str, Any]]:
        if self._posts_cache is not None:
            return self._posts_cache

        table = _find_table(self.sns_conn, "SnsTimeLine")
        if not table:
            raise MomentsSchemaError("未找到朋友圈数据表 SnsTimeLine")
        columns = _table_columns(self.sns_conn, table)
        missing = [name for name in ("tid", "user_name", "content") if name not in columns]
        if missing:
            raise MomentsSchemaError(
                "SnsTimeLine 缺少必要字段: " + ", ".join(missing)
            )

        tid_column = _quote_identifier(columns["tid"])
        username_column = _quote_identifier(columns["user_name"])
        content_column = _quote_identifier(columns["content"])
        table_name = _quote_identifier(table)
        try:
            rows = self.sns_conn.execute(
                f"SELECT {tid_column}, {username_column}, {content_column} "
                f"FROM {table_name} WHERE {content_column} IS NOT NULL"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise MomentsSchemaError(f"读取 SnsTimeLine 失败: {exc}") from exc

        # The persisted *_user_top partitions are a complete current snapshot,
        # so assignment (never union) also removes posts that were unpinned.
        persisted_current_pins = self._load_current_profile_top_ids()
        self._current_pin_ids = persisted_current_pins

        posts: list[dict[str, Any]] = []
        failures = 0
        for tid, db_username, content in rows:
            parsed = parse_sns_timeline_content(content)
            if parsed is None:
                failures += 1
                continue
            username = _as_text(db_username) or _as_text(parsed.get("xml_username"))
            if not username:
                failures += 1
                continue
            # SQLite exposes uint64 feed IDs as signed INTEGER values.  The
            # database value remains untouched for SQL joins; only the public
            # JSON field is normalized to a decimal string.
            parsed["tid"] = (
                _canonical_uint64_id(tid)
                or _canonical_uint64_id(parsed.get("xml_id"))
            )
            parsed["username"] = username
            parsed["_tid_db"] = tid
            self._apply_current_pin_state(parsed)
            posts.append(parsed)

        available_ids: dict[str, set[str]] = defaultdict(set)
        for post in posts:
            available_ids[_as_text(post.get("username"))].add(
                _as_text(post.get("tid"))
            )
        self._unavailable_pinned_counts = {
            username: len(feed_ids - available_ids.get(username, set()))
            for username, feed_ids in persisted_current_pins.items()
            if feed_ids - available_ids.get(username, set())
        }
        posts.sort(
            key=lambda post: (
                _as_int(post.get("create_time")),
                _as_int(post.get("tid")),
            ),
            reverse=True,
        )
        self.parse_failures = failures
        self._posts_cache = posts
        return posts

    def _load_contact_names(self) -> dict[str, str]:
        if self._contact_names_cache is not None:
            return dict(self._contact_names_cache)
        if self.contact_conn is None:
            return {}
        table = _find_table(self.contact_conn, "contact")
        if not table:
            return {}
        columns = _table_columns(self.contact_conn, table)
        username = columns.get("username") or columns.get("user_name")
        nickname = columns.get("nick_name") or columns.get("nickname")
        remark = columns.get("remark") or columns.get("remark_name")
        if not username:
            return {}

        selected = [username]
        if remark:
            selected.append(remark)
        if nickname:
            selected.append(nickname)
        sql = "SELECT " + ", ".join(_quote_identifier(item) for item in selected)
        sql += " FROM " + _quote_identifier(table)
        names: dict[str, str] = {}
        try:
            rows = self.contact_conn.execute(sql).fetchall()
        except sqlite3.DatabaseError:
            return {}
        for row in rows:
            user = _as_text(row[0])
            if not user:
                continue
            offset = 1
            remark_value = _as_text(row[offset]) if remark else ""
            offset += int(bool(remark))
            nickname_value = _as_text(row[offset]) if nickname else ""
            names[user] = remark_value or nickname_value or user
        self._contact_names_cache = names
        return dict(names)

    def _interaction_display_name(
        self,
        username: Any,
        xml_nickname: Any,
        contact_names: dict[str, str],
    ) -> str:
        user = _as_text(username)
        if user and user in self._self_usernames():
            return self.account_display_name or "我"
        return contact_names.get(user) or _as_text(xml_nickname) or user

    def get_contacts(self) -> list[dict[str, Any]]:
        """返回实际出现在朋友圈数据库中的发布者（包含本人）。"""
        if self._contacts_cache is not None:
            return [dict(item) for item in self._contacts_cache]

        posts = self._load_timeline()
        contact_names = self._load_contact_names()
        self_names = self._self_usernames()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for post in posts:
            grouped[_as_text(post.get("username"))].append(post)

        contacts: list[dict[str, Any]] = []
        for username, user_posts in grouped.items():
            is_self = username in self_names
            xml_name = next(
                (_as_text(post.get("nickname")) for post in user_posts if post.get("nickname")),
                "",
            )
            name = (
                (self.account_display_name if is_self else "")
                or contact_names.get(username)
                or xml_name
                or username
            )
            timestamps = [
                _as_int(post.get("create_time"))
                for post in user_posts
                if _as_int(post.get("create_time")) > 0
            ]
            contacts.append(
                {
                    "username": username,
                    "display_name": name,
                    "is_self": is_self,
                    "post_count": len(user_posts),
                    "start": min(timestamps) if timestamps else 0,
                    "end": max(timestamps) if timestamps else 0,
                    # Both names are kept because the domain request originally
                    # used start/end while the HTTP/UI contract uses *_time.
                    "start_time": min(timestamps) if timestamps else 0,
                    "end_time": max(timestamps) if timestamps else 0,
                }
            )

        contacts.sort(
            key=lambda item: (
                0 if item["is_self"] else 1,
                -_as_int(item["end"]),
                _as_text(item["display_name"]).casefold(),
            )
        )
        self._contacts_cache = contacts
        return [dict(item) for item in contacts]

    @staticmethod
    def _normalize_selection(usernames: Sequence[str]) -> list[str]:
        if isinstance(usernames, (str, bytes)):
            raise MomentsSelectionError("联系人必须以明确的列表传入")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in usernames or []:
            username = _as_text(value).strip()
            if username and username not in seen:
                seen.add(username)
                normalized.append(username)
        if not normalized:
            raise MomentsSelectionError("请至少选择一个朋友圈联系人")
        if len(normalized) > 5000:
            raise MomentsSelectionError("一次选择的联系人数量过多")
        return normalized

    @staticmethod
    def _normalize_tids(tids: Optional[Sequence[str]]) -> Optional[list[str]]:
        """Normalize public SNS feed IDs without accepting signed DB values."""
        if tids is None:
            return None
        if isinstance(tids, (str, bytes)):
            raise MomentsSelectionError("朋友圈动态标识必须以明确的列表传入")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in tids:
            tid = _as_text(value).strip()
            if not re.fullmatch(r"\d{1,20}", tid):
                raise MomentsSelectionError("朋友圈动态标识格式无效")
            number = int(tid)
            if number < 0 or number >= (1 << 64):
                raise MomentsSelectionError("朋友圈动态标识超出有效范围")
            tid = str(number)
            if tid not in seen:
                seen.add(tid)
                normalized.append(tid)
        if not normalized:
            raise MomentsSelectionError("请至少选择一条朋友圈动态")
        if len(normalized) > 5000:
            raise MomentsSelectionError("一次最多选择 5000 条朋友圈动态")
        return normalized

    @staticmethod
    def _validate_range(start_time: Optional[int], end_time: Optional[int]) -> None:
        if start_time is not None and _as_int(start_time, -1) < 0:
            raise MomentsSelectionError("开始时间不能为负数")
        if end_time is not None and _as_int(end_time, -1) < 0:
            raise MomentsSelectionError("结束时间不能为负数")
        if start_time is not None and end_time is not None:
            if _as_int(start_time) > _as_int(end_time):
                raise MomentsSelectionError("开始时间不能晚于结束时间")

    @staticmethod
    def _interaction_keys(
        feed_id: str, interaction: dict[str, Any]
    ) -> tuple[Optional[tuple[Any, ...]], tuple[Any, ...]]:
        interaction_type = _as_int(interaction.get("type"))
        interaction_id = _first_nonzero_id(interaction.get("id"))
        strong_key = (
            ("id", feed_id, interaction_type, interaction_id)
            if interaction_id
            else None
        )
        actor = _as_text(interaction.get("from_username"))
        if interaction_type == 1:
            # A person can only have one visible like on the same post.
            semantic_key = ("like", feed_id, actor)
        else:
            # Deliberately omit the reply target: SnsMessage_tmp3.to_username
            # is normally the notification recipient, while the XML contains
            # the real ref_username.  This key is used to match the two sources.
            semantic_key = (
                "semantic",
                feed_id,
                interaction_type,
                actor,
                _as_int(interaction.get("create_time")),
                _as_text(interaction.get("content")),
            )
        return strong_key, semantic_key

    def _load_comments(self, posts: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        if not posts:
            return {}

        comments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen_strong: set[tuple[Any, ...]] = set()
        seen_semantic: set[tuple[Any, ...]] = set()

        def append_unique(
            feed_id: str,
            interaction: dict[str, Any],
            *,
            notification: bool = False,
        ) -> None:
            item = dict(interaction)
            item["feed_id"] = feed_id
            strong_key, semantic_key = self._interaction_keys(feed_id, item)
            if strong_key is not None and strong_key in seen_strong:
                return
            # XML is authoritative.  Notification IDs are not always encoded
            # the same way, so their semantic twin must also be suppressed.
            if (strong_key is None or notification) and semantic_key in seen_semantic:
                return
            if strong_key is not None:
                seen_strong.add(strong_key)
            seen_semantic.add(semantic_key)
            comments[feed_id].append(item)

        # The XML snapshot is complete and includes the current account's own
        # comments, so it is deliberately merged before notification rows.
        for post in posts:
            feed_id = _as_text(post.get("tid"))
            for interaction in post.get("_xml_interactions") or []:
                append_unique(feed_id, interaction)

        table = _find_table(self.sns_conn, "SnsMessage_tmp3")
        if not table:
            return dict(comments)
        columns = _table_columns(self.sns_conn, table)
        if not {"feed_id", "type"}.issubset(columns):
            return dict(comments)

        optional_names = ("local_id", "comment_id", "comment64_id")
        query_names = [
            "feed_id",
            "create_time",
            "type",
            "from_username",
            "from_nickname",
            "content",
        ]
        query_names.extend(name for name in optional_names if name in columns)
        raw_feed_ids = list(dict.fromkeys(post.get("_tid_db") for post in posts))
        raw_to_public_feed = {
            _as_text(post.get("_tid_db")): _as_text(post.get("tid"))
            for post in posts
        }

        for offset in range(0, len(raw_feed_ids), 400):
            chunk = raw_feed_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            selected_expressions = []
            for name in query_names:
                if name in columns:
                    selected_expressions.append(_quote_identifier(columns[name]))
                elif name == "create_time":
                    selected_expressions.append(f"0 AS {_quote_identifier(name)}")
                else:
                    selected_expressions.append(f"'' AS {_quote_identifier(name)}")
            selected_columns = ", ".join(selected_expressions)
            where = f"{_quote_identifier(columns['feed_id'])} IN ({placeholders})"
            if "del_status" in columns:
                where += f" AND COALESCE({_quote_identifier(columns['del_status'])}, 0) = 0"
            order_columns = []
            if "create_time" in columns:
                order_columns.append(_quote_identifier(columns["create_time"]))
            if "local_id" in columns:
                order_columns.append(_quote_identifier(columns["local_id"]))
            order_clause = (
                " ORDER BY " + ", ".join(order_columns) if order_columns else ""
            )
            sql = (
                f"SELECT {selected_columns} FROM {_quote_identifier(table)} "
                f"WHERE {where}{order_clause}"
            )
            try:
                rows = self.sns_conn.execute(sql, tuple(chunk)).fetchall()
            except sqlite3.DatabaseError:
                continue
            for row in rows:
                values = dict(zip(query_names, row))
                raw_feed_id = _as_text(values.get("feed_id"))
                feed_id = raw_to_public_feed.get(
                    raw_feed_id, _canonical_uint64_id(raw_feed_id)
                )
                comment_id = values.get("comment64_id") or values.get("comment_id") or ""
                comment_type = _as_int(values.get("type"))
                append_unique(
                    feed_id,
                    {
                        "id": _canonical_uint64_id(comment_id),
                        "create_time": _as_int(values.get("create_time")),
                        "create_time_str": _format_timestamp(values.get("create_time")),
                        "type": comment_type,
                        "type_name": _interaction_type_name(comment_type),
                        "from_username": _as_text(values.get("from_username")),
                        "from_nickname": _as_text(values.get("from_nickname")),
                        # SnsMessage_tmp3.to_username is the notification
                        # receiver, not a reliable reply target.
                        "to_username": "",
                        "to_nickname": "",
                        "content": _as_text(values.get("content")),
                    },
                    notification=True,
                )
        return dict(comments)

    @staticmethod
    def _public_post(post: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in post.items() if not key.startswith("_")}

    def _filter_timeline(
        self,
        usernames: Sequence[str],
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        tids: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        selected = self._normalize_selection(usernames)
        self._validate_range(start_time, end_time)
        selected_tids = self._normalize_tids(tids)
        selected_set = set(selected)
        tid_set = set(selected_tids or [])
        start = _as_int(start_time) if start_time is not None else None
        end = _as_int(end_time) if end_time is not None else None
        posts: list[dict[str, Any]] = []
        found_tids: set[str] = set()
        for post in self._load_timeline():
            if post.get("username") not in selected_set:
                continue
            timestamp = _as_int(post.get("create_time"))
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp > end:
                continue
            tid = _as_text(post.get("tid"))
            if selected_tids is not None and tid not in tid_set:
                continue
            posts.append(post)
            if selected_tids is not None:
                found_tids.add(tid)

        if selected_tids is not None and found_tids != tid_set:
            raise MomentsSelectionError(
                "所选朋友圈动态已变化或不属于当前联系人，请刷新预览后重试"
            )
        return posts

    def _hydrate_public_posts(
        self, posts: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        comments = self._load_comments(posts)
        contact_names = self._load_contact_names()
        public_posts: list[dict[str, Any]] = []
        for post in posts:
            public = self._public_post(post)
            public_comments: list[dict[str, Any]] = []
            for interaction in comments.get(_as_text(post.get("tid")), []):
                item = dict(interaction)
                item["from_display_name"] = self._interaction_display_name(
                    item.get("from_username"),
                    item.get("from_nickname"),
                    contact_names,
                )
                item["to_display_name"] = self._interaction_display_name(
                    item.get("to_username"),
                    item.get("to_nickname"),
                    contact_names,
                )
                public_comments.append(item)
            public["comments"] = public_comments
            public_posts.append(public)
        return public_posts

    def get_posts(
        self,
        usernames: Sequence[str],
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        tids: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        """返回明确选择的联系人、时间和可选动态 ID 交集。"""

        posts = self._filter_timeline(
            usernames,
            start_time=start_time,
            end_time=end_time,
            tids=tids,
        )
        return self._hydrate_public_posts(posts)

    def get_media_item(self, tid: str, media_index: int) -> dict[str, Any]:
        """Return one public post/media pair after validating both selectors.

        Public feed IDs are unsigned decimal strings even when SQLite exposes
        the underlying uint64 value as a signed INTEGER.  Callers must never
        use a raw database ID or index a media list before the owning post has
        been resolved.
        """

        normalized_tid = self._normalize_tids([tid])[0]
        if isinstance(media_index, bool):
            raise MomentsSelectionError("朋友圈媒体序号格式无效")
        if isinstance(media_index, str):
            value = media_index.strip()
            if not re.fullmatch(r"\d+", value):
                raise MomentsSelectionError("朋友圈媒体序号格式无效")
            normalized_index = int(value)
        elif isinstance(media_index, int):
            normalized_index = media_index
        else:
            raise MomentsSelectionError("朋友圈媒体序号格式无效")
        if normalized_index < 0:
            raise MomentsSelectionError("朋友圈媒体序号不能为负数")

        post = next(
            (
                candidate
                for candidate in self._load_timeline()
                if _as_text(candidate.get("tid")) == normalized_tid
            ),
            None,
        )
        if post is None:
            raise MomentsSelectionError("朋友圈动态不存在，请刷新预览后重试")

        media_items = post.get("media") or []
        if normalized_index >= len(media_items):
            raise MomentsSelectionError("朋友圈媒体序号超出有效范围")

        public_post = self._hydrate_public_posts([post])[0]
        public_media_items = public_post.get("media") or []
        return {
            "post": public_post,
            "media": dict(public_media_items[normalized_index]),
            "media_index": normalized_index,
        }

    def get_posts_page(
        self,
        username: str,
        *,
        page: int = 1,
        page_size: int = 20,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        keyword: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a filtered preview page with pinned posts kept separate.

        Keyword matching runs after the contact/date filters but before the
        pinned/timeline split.  Only ``content`` (the user's Moments caption)
        is searched, so a hidden link title/description cannot produce a
        surprising result in the UI's "文案关键词" search.  Matching is a
        case-insensitive substring search; blank input disables it.
        """

        page = max(1, int(page))
        page_size = max(1, min(50, int(page_size)))
        posts = self._filter_timeline(
            [username], start_time=start_time, end_time=end_time
        )
        normalized_keyword = _as_text(keyword).strip().casefold()
        if normalized_keyword:
            posts = [
                post
                for post in posts
                if normalized_keyword in _as_text(post.get("content")).casefold()
            ]
        pinned_posts = [post for post in posts if bool(post.get("is_top"))]
        timeline_posts = [post for post in posts if not bool(post.get("is_top"))]
        total = len(timeline_posts)
        total_pages = (total + page_size - 1) // page_size if total else 0
        offset = (page - 1) * page_size
        page_posts = timeline_posts[offset:offset + page_size]
        return {
            "posts": self._hydrate_public_posts(page_posts),
            "pinned_posts": (
                self._hydrate_public_posts(pinned_posts) if pinned_posts else []
            ),
            "pinned_total": len(pinned_posts),
            "unavailable_pinned_total": self._unavailable_pinned_counts.get(
                _as_text(username).strip(), 0
            ),
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1 and total > 0,
            "has_next": page < total_pages,
        }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """使用已验证 IP 建连，但仍以原主机名进行 TLS SNI/证书校验。"""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # pragma: no cover - exercised by integration/network tests
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class SafeMomentsMediaDownloader:
    """受限的朋友圈图片下载器。

    每次请求先校验 HTTPS/域名，再解析 DNS 并将连接固定到验证过的公网 IP，
    因而不会在校验后重新解析到私网地址。重定向目标会重复完整校验。
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
        max_file_bytes: int = MEDIA_MAX_FILE_BYTES,
        max_total_bytes: int = MEDIA_MAX_TOTAL_BYTES,
        max_redirects: int = 3,
        max_total_seconds: float = MEDIA_MAX_TOTAL_SECONDS,
        max_pixels: int = MEDIA_MAX_PIXELS,
        max_frames: int = MEDIA_MAX_FRAMES,
    ):
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.max_redirects = max(0, int(max_redirects))
        self.max_total_seconds = max(1.0, float(max_total_seconds))
        self.max_pixels = max(1, int(max_pixels))
        self.max_frames = max(1, int(max_frames))
        self.total_downloaded = 0
        self._deadline = 0.0

    def reset_budget(self) -> None:
        self.total_downloaded = 0
        self._deadline = time.monotonic() + self.max_total_seconds

    def _remaining_seconds(self) -> float:
        if self._deadline <= 0:
            self.reset_budget()
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise MomentsMediaDownloadError("媒体下载已达到本次导出的总时限")
        return remaining

    @staticmethod
    def _host_allowed(hostname: str) -> bool:
        host = hostname.lower().rstrip(".")
        return any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_MEDIA_DOMAINS)

    @staticmethod
    def _is_public_ip(value: str) -> bool:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError:
            return False
        return bool(address.is_global) and not any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        )

    def _resolve_public_ips(self, hostname: str, port: int) -> list[str]:
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise MomentsMediaDownloadError("媒体域名解析失败") from exc
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not self._is_public_ip(value) for value in addresses):
            raise MomentsMediaDownloadError("媒体域名解析到了非公网地址")
        return addresses

    def _validate_url(self, url: str) -> tuple[Any, str, int, list[str]]:
        try:
            parsed = urlsplit(_as_text(url).strip())
            port = parsed.port or 443
        except ValueError as exc:
            raise MomentsMediaDownloadError("媒体 URL 无效") from exc
        hostname = (parsed.hostname or "").lower().rstrip(".")
        scheme = parsed.scheme.lower()
        if scheme == "http":
            # Historical sns.db rows predominantly store an http:// CDN URL.
            # Never send it in clear text: connect to the same allow-listed
            # host over TLS instead.  Explicit port 80 was captured above and
            # remains rejected by the port check below.
            parsed = parsed._replace(scheme="https")
        elif scheme != "https":
            raise MomentsMediaDownloadError("媒体仅允许通过 HTTPS 下载")
        if not hostname or parsed.username or parsed.password or port != 443:
            raise MomentsMediaDownloadError("媒体 URL 主机或端口无效")
        if not self._host_allowed(hostname):
            raise MomentsMediaDownloadError("媒体 URL 不属于允许的微信 CDN 域名")
        return parsed, hostname, port, self._resolve_public_ips(hostname, port)

    @staticmethod
    def _prepare_reference_url(url: str, token: str) -> str:
        """Build the authenticated, original-quality WeChat image URL."""

        value = _as_text(url).strip()
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise MomentsMediaDownloadError("朋友圈媒体 URL 无效") from exc
        path = re.sub(r"/150$", "/0", parsed.path)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        clean_token = _as_text(token).strip()
        if clean_token:
            # XML metadata belongs to this exact media reference and is more
            # authoritative than stale/blank query parameters persisted in
            # an old URL.
            query_items = [
                (name, item_value)
                for name, item_value in query_items
                if name.casefold() not in {"token", "idx"}
            ]
            query_items.append(("token", clean_token))
            query_items.append(("idx", "1"))
        return urlunsplit(
            (parsed.scheme, parsed.netloc, path, urlencode(query_items), parsed.fragment)
        )

    @staticmethod
    def _detect_image(data: bytes) -> tuple[str, str]:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", ".gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp", ".webp"
        raise MomentsMediaDownloadError("媒体响应不是受支持的图片格式")

    def _validate_image_safety(self, data: bytes) -> None:
        if Image is None:
            raise MomentsMediaDownloadError("缺少 Pillow，无法校验媒体图片安全性")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    width, height = image.size
                    frames = _as_int(getattr(image, "n_frames", 1), 1)
                    if width <= 0 or height <= 0:
                        raise MomentsMediaDownloadError("媒体图片尺寸无效")
                    if width * height > self.max_pixels:
                        raise MomentsMediaDownloadError("媒体图片像素数量超过安全限制")
                    if frames > self.max_frames:
                        raise MomentsMediaDownloadError("媒体动图帧数超过安全限制")
                    image.verify()
        except MomentsMediaDownloadError:
            raise
        except Exception as exc:
            raise MomentsMediaDownloadError("媒体图片结构校验失败") from exc

    def download_reference(
        self,
        url: str,
        *,
        token: str = "",
        key: str = "",
        enc_idx: str = "",
    ) -> DownloadedMedia:
        """Download and, when required, decrypt one SNS media reference."""

        prepared = self._prepare_reference_url(url, token)
        return self._download(prepared, key=key, enc_idx=enc_idx)

    def download(self, url: str) -> DownloadedMedia:
        """Download a plaintext image URL for backward compatibility."""

        return self._download(url)

    def _download(
        self,
        url: str,
        *,
        key: str = "",
        enc_idx: str = "",
    ) -> DownloadedMedia:
        current_url = _as_text(url).strip()
        for redirect_count in range(self.max_redirects + 1):
            remaining = self._remaining_seconds()
            parsed, hostname, port, addresses = self._validate_url(current_url)
            connection = _PinnedHTTPSConnection(
                hostname, port, addresses[0], min(self.timeout_seconds, remaining)
            )
            try:
                target = parsed.path or "/"
                if parsed.query:
                    target += "?" + parsed.query
                connection.request(
                    "GET",
                    target,
                    headers={
                        "User-Agent": "MicroMessenger Client",
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                if response.status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location or redirect_count >= self.max_redirects:
                        raise MomentsMediaDownloadError("媒体重定向次数过多")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise MomentsMediaDownloadError(
                        f"媒体服务器返回 HTTP {response.status}"
                    )
                encrypted_response = (
                    _as_text(response.getheader("x-Enc")).strip() == "1"
                )
                content_length = response.getheader("Content-Length")
                if content_length:
                    try:
                        announced = int(content_length)
                    except ValueError:
                        announced = 0
                    if announced > self.max_file_bytes:
                        raise MomentsMediaDownloadError("单个媒体文件超过大小限制")
                    if self.total_downloaded + announced > self.max_total_bytes:
                        raise MomentsMediaDownloadError("媒体下载总量超过本次导出限制")

                data = bytearray()
                while True:
                    remaining = self._remaining_seconds()
                    if connection.sock is not None:
                        connection.sock.settimeout(
                            min(self.timeout_seconds, remaining)
                        )
                    chunk = response.read(min(64 * 1024, self.max_file_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > self.max_file_bytes:
                        raise MomentsMediaDownloadError("单个媒体文件超过大小限制")
                    if self.total_downloaded + len(data) > self.max_total_bytes:
                        raise MomentsMediaDownloadError("媒体下载总量超过本次导出限制")
                payload = bytes(data)
                recognized_plain = any(
                    (
                        payload.startswith(b"\xff\xd8\xff"),
                        payload.startswith(b"\x89PNG\r\n\x1a\n"),
                        payload.startswith((b"GIF87a", b"GIF89a")),
                        len(payload) >= 12
                        and payload[:4] == b"RIFF"
                        and payload[8:12] == b"WEBP",
                    )
                )
                should_decrypt = encrypted_response or (
                    _as_text(enc_idx).strip() == "1" and not recognized_plain
                )
                if should_decrypt:
                    try:
                        payload = decrypt_sns_image(payload, key)
                    except SnsMediaCryptoError as exc:
                        raise MomentsMediaDownloadError(
                            "朋友圈图片已加密，但媒体解密 key 无效"
                        ) from exc
                payload = normalize_wechat_image_payload(payload)
                mime_type, extension = self._detect_image(payload)
                if mime_type == "image/jpeg" and not payload.endswith(b"\xff\xd9"):
                    raise MomentsMediaDownloadError("朋友圈 JPEG 图片下载不完整")
                self._validate_image_safety(payload)
                self.total_downloaded += len(payload)
                return DownloadedMedia(payload, mime_type, extension)
            except MomentsMediaDownloadError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise MomentsMediaDownloadError("媒体下载失败") from exc
            finally:
                connection.close()
        raise MomentsMediaDownloadError("媒体重定向次数过多")


def safe_filename(value: str, fallback: str = "朋友圈", max_length: int = 100) -> str:
    text = _as_text(value)
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text.replace("..", "_")
    if not text:
        text = fallback
    device_stem = text.split(".", 1)[0]
    if re.fullmatch(
        r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", device_stem
    ):
        text = "_" + text
    return text[:max_length].rstrip(" .") or fallback


def escape_csv_formula(value: Any) -> str:
    text = _as_text(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


class MomentsExporter:
    """将明确选择的朋友圈联系人导出为 HTML/JSON/CSV/TXT。"""

    SUPPORTED_FORMATS = {"html", "json", "csv", "txt"}

    def __init__(
        self,
        service: MomentsService,
        output_dir: Path | str,
        media_downloader: Optional[Any] = None,
        local_media_resolver: Optional[Any] = None,
    ):
        self.service = service
        self.output_dir = Path(output_dir)
        self.media_downloader = media_downloader
        self.local_media_resolver = local_media_resolver

    def _load_local_assets(
        self,
        posts: Sequence[dict[str, Any]],
    ) -> dict[tuple[str, int], DownloadedMedia]:
        """Load validated preview media without performing network I/O."""

        get_bytes = getattr(self.local_media_resolver, "get_bytes", None)
        if not callable(get_bytes):
            return {}
        assets: dict[tuple[str, int], DownloadedMedia] = {}
        loaded_payload_bytes = 0
        for post in posts:
            tid = _as_text(post.get("tid"))
            for index, _media in enumerate(post.get("media") or []):
                try:
                    status, payload = get_bytes(tid, index)
                except Exception:
                    # A stale or invalid local binding must not prevent the
                    # surrounding text-only export from succeeding.
                    continue
                mime_type = _as_text(getattr(status, "mime_type", ""))
                if not mime_type.startswith("image/") or not isinstance(
                    payload, (bytes, bytearray, memoryview)
                ):
                    continue
                payload = bytes(payload)
                if (
                    len(payload) > MEDIA_MAX_FILE_BYTES
                    or loaded_payload_bytes + len(payload) > MEDIA_MAX_TOTAL_BYTES
                ):
                    continue
                loaded_payload_bytes += len(payload)
                assets[(tid, index)] = DownloadedMedia(
                    payload, mime_type, ""
                )
        return assets

    @staticmethod
    def _unique_path(path: Path) -> Path:
        candidate = path
        counter = 1
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}({counter}){path.suffix}")
            counter += 1
        return candidate

    @staticmethod
    def _contact_lookup(contacts: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {_as_text(item.get("username")): dict(item) for item in contacts}

    def _download_assets(
        self,
        posts: Sequence[dict[str, Any]],
        downloader: Any,
        existing_assets: Optional[dict[tuple[str, int], DownloadedMedia]] = None,
    ) -> tuple[dict[tuple[str, int], DownloadedMedia], int, int]:
        assets = dict(existing_assets or {})
        cache: dict[tuple[str, str, str, str], Optional[DownloadedMedia]] = {}
        downloaded = 0
        failed = 0
        attempts = 0
        for post in posts:
            tid = _as_text(post.get("tid"))
            for index, media in enumerate(post.get("media") or []):
                if (tid, index) in assets:
                    continue
                media_type = _as_text(media.get("type"))
                # Image items prefer the full URL. Video/finder items may only
                # contribute their still thumbnail; the downloader never
                # accepts MP4 or other non-image payloads.
                references = moments_media_download_candidates(media)
                if media_type not in _IMAGE_MEDIA_TYPES:
                    references = [
                        reference
                        for reference in references
                        if reference[0] == _as_text(media.get("thumb_url"))
                    ]
                if not references:
                    continue
                resolved = False
                for reference in references:
                    cached = cache.get(reference)
                    if cached is not None:
                        assets[(tid, index)] = cached
                        resolved = True
                        break
                if resolved:
                    continue
                for reference in references:
                    if reference in cache:
                        continue
                    if attempts >= MEDIA_MAX_ATTEMPTS:
                        break
                    attempts += 1
                    try:
                        url, token, key, enc_idx = reference
                        reference_download = getattr(
                            downloader, "download_reference", None
                        )
                        if callable(reference_download):
                            result = reference_download(
                                url,
                                token=token,
                                key=key,
                                enc_idx=enc_idx,
                            )
                        else:
                            result = downloader.download(url)
                        if not isinstance(result, DownloadedMedia):
                            raise MomentsMediaDownloadError("媒体下载器返回了无效结果")
                        cache[reference] = result
                        assets[(tid, index)] = result
                        downloaded += 1
                        resolved = True
                        break
                    except Exception:
                        cache[reference] = None
                if not resolved:
                    failed += 1
        return assets, downloaded, failed

    @staticmethod
    def _html_document(
        contact: dict[str, Any],
        posts: Sequence[dict[str, Any]],
        assets: dict[tuple[str, int], DownloadedMedia],
    ) -> str:
        display_name = html.escape(_as_text(contact.get("display_name")), quote=True)
        pinned_posts = [post for post in posts if bool(post.get("is_top"))]
        timeline_posts = [post for post in posts if not bool(post.get("is_top"))]
        parts = [
            "<!doctype html>",
            '<html lang="zh-CN"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">',
            f"<title>{display_name} - 朋友圈</title>",
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:850px;margin:auto;padding:20px;background:#f5f5f5;color:#222}.moments-section{margin:24px 0}.section-title{font-size:20px;margin:0 0 12px}.section-empty{color:#888;font-size:13px}.post{background:#fff;border-radius:10px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px #0001}.time{color:#888;font-size:12px}.type{margin-left:8px;color:#087f3c}.text{white-space:pre-wrap;word-break:break-word;line-height:1.6}.media{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.media img{max-width:260px;max-height:300px;object-fit:contain;border-radius:6px}.location,.link{font-size:13px;color:#576b95;margin-top:8px}.likes,.comments{border-top:1px solid #eee;margin-top:10px;padding-top:8px;font-size:13px}.likes{display:flex;align-items:flex-start;gap:10px}.like-list{display:flex;flex-wrap:wrap;gap:4px 12px;min-width:0}.interaction-title{flex:none;color:#888;font-weight:600}.comment{margin:4px 0}.name{color:#576b95}.missing{color:#999;font-size:12px}</style></head><body>",
            (
                f"<h1>{display_name} 的朋友圈</h1><p>共 {len(posts)} 条动态"
                f"（置顶 {len(pinned_posts)} 条，时间线 {len(timeline_posts)} 条）</p>"
            ),
        ]
        embedded_payload_bytes = 0
        data_uri_cache: dict[int, str] = {}
        render_rows: list[tuple[str, Optional[dict[str, Any]]]] = [
            ("pinned", None),
            *(("pinned", post) for post in pinned_posts),
            ("timeline", None),
            *(("timeline", post) for post in timeline_posts),
        ]
        active_section = ""
        for section_name, post in render_rows:
            if post is None:
                if active_section:
                    parts.append("</section>")
                active_section = section_name
                if section_name == "pinned":
                    section_id = "pinned-moments"
                    section_title = f"置顶朋友圈（{len(pinned_posts)}）"
                    is_empty = not pinned_posts
                else:
                    section_id = "moments-timeline"
                    section_title = f"时间线（{len(timeline_posts)}）"
                    is_empty = not timeline_posts
                parts.append(
                    f'<section class="moments-section" id="{section_id}">'
                    f'<h2 class="section-title">{section_title}</h2>'
                )
                if is_empty:
                    parts.append('<p class="section-empty">暂无内容</p>')
                continue
            tid = _as_text(post.get("tid"))
            parts.append('<article class="post">')
            time_text = html.escape(_as_text(post.get("create_time_str")), quote=True)
            type_text = html.escape(_as_text(post.get("content_type_name")), quote=True)
            parts.append(f'<div class="time">{time_text}<span class="type">{type_text}</span></div>')
            body = _as_text(post.get("content"))
            if body:
                parts.append(f'<div class="text">{html.escape(body, quote=True)}</div>')
            title = _as_text(post.get("title"))
            description = _as_text(post.get("description"))
            content_url = _as_text(post.get("content_url"))
            if title or description or content_url:
                link_text = html.escape(title, quote=True)
                if description:
                    link_text += (" — " if link_text else "") + html.escape(
                        description, quote=True
                    )
                if content_url:
                    link_text += (" · " if link_text else "") + html.escape(
                        content_url, quote=True
                    )
                parts.append(
                    '<div class="link">'
                    + link_text
                    + "</div>"
                )
            rendered_media = 0
            media_items = post.get("media") or []
            finder = post.get("finder_feed") or {}
            if media_items:
                parts.append('<div class="media">')
                for index, _media in enumerate(media_items):
                    asset = assets.get((tid, index))
                    if asset is not None:
                        if (
                            embedded_payload_bytes + len(asset.data)
                            > MEDIA_MAX_TOTAL_BYTES
                        ):
                            continue
                        embedded_payload_bytes += len(asset.data)
                        asset_key = id(asset)
                        data_uri = data_uri_cache.get(asset_key)
                        if data_uri is None:
                            data_uri = asset.data_uri()
                            data_uri_cache[asset_key] = data_uri
                        parts.append(
                            f'<img src="{data_uri}" alt="朋友圈图片 {index + 1}">'
                        )
                        rendered_media += 1
                parts.append("</div>")
            if finder and not rendered_media:
                parts.append('<div class="missing">短视频媒体未嵌入</div>')
            elif media_items and not rendered_media:
                parts.append('<div class="missing">媒体未嵌入</div>')
            location = post.get("location") or {}
            if location.get("poi_name"):
                parts.append(
                    '<div class="location">📍 '
                    + html.escape(_as_text(location.get("poi_name")), quote=True)
                    + "</div>"
                )
            comments = post.get("comments") or []
            likes = [
                comment for comment in comments
                if _as_int(comment.get("type")) == 1
            ]
            replies = [
                comment for comment in comments
                if _as_int(comment.get("type")) != 1
            ]
            if likes:
                parts.append(
                    '<div class="likes"><span class="interaction-title">'
                    '❤️ 点赞</span><div class="like-list">'
                )
                for like in likes:
                    name = html.escape(
                        _as_text(like.get("from_display_name"))
                        or _as_text(like.get("from_nickname"))
                        or _as_text(like.get("from_username")),
                        quote=True,
                    )
                    parts.append(f'<span class="like name">{name}</span>')
                parts.append("</div></div>")
            if replies:
                parts.append(
                    '<div class="comments"><div class="interaction-title">'
                    '评论</div>'
                )
                for comment in replies:
                    name = html.escape(
                        _as_text(comment.get("from_display_name"))
                        or _as_text(comment.get("from_nickname"))
                        or _as_text(comment.get("from_username")),
                        quote=True,
                    )
                    target = (
                        _as_text(comment.get("to_display_name"))
                        or _as_text(comment.get("to_nickname"))
                        or _as_text(comment.get("to_username"))
                    )
                    target_html = (
                        ' 回复 <span class="name">'
                        + html.escape(target, quote=True)
                        + "</span>"
                        if target else ""
                    )
                    content = html.escape(
                        _as_text(comment.get("content")), quote=True
                    )
                    parts.append(
                        f'<div class="comment"><span class="name">{name}</span>{target_html}: {content}</div>'
                    )
                parts.append("</div>")
            parts.append("</article>")
        if active_section:
            parts.append("</section>")
        parts.append("</body></html>")
        return "\n".join(parts)

    @staticmethod
    def _json_document(contact: dict[str, Any], posts: Sequence[dict[str, Any]]) -> str:
        payload = {
            "username": _as_text(contact.get("username")),
            "display_name": _as_text(contact.get("display_name")),
            "is_self": bool(contact.get("is_self")),
            "total_posts": len(posts),
            "posts": list(posts),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _csv_document(contact: dict[str, Any], posts: Sequence[dict[str, Any]]) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["发布者", "时间", "类型", "正文", "标题", "链接描述", "链接地址", "位置", "媒体", "评论/点赞"]
        )
        for post in posts:
            location = post.get("location") or {}
            media = json.dumps(post.get("media") or [], ensure_ascii=False, separators=(",", ":"))
            comments = json.dumps(post.get("comments") or [], ensure_ascii=False, separators=(",", ":"))
            values = [
                contact.get("display_name"),
                post.get("create_time_str"),
                post.get("content_type_name"),
                post.get("content"),
                post.get("title"),
                post.get("description"),
                post.get("content_url"),
                location.get("poi_name", ""),
                media,
                comments,
            ]
            writer.writerow([escape_csv_formula(value) for value in values])
        return "\ufeff" + output.getvalue()

    @staticmethod
    def _txt_document(contact: dict[str, Any], posts: Sequence[dict[str, Any]]) -> str:
        lines = [f"{_as_text(contact.get('display_name'))} 的朋友圈", f"共 {len(posts)} 条动态", ""]
        for post in posts:
            lines.append(
                f"[{_as_text(post.get('create_time_str'))}] {_as_text(post.get('content_type_name'))}"
            )
            if post.get("content"):
                lines.append(_as_text(post.get("content")))
            if post.get("title"):
                lines.append("标题: " + _as_text(post.get("title")))
            if post.get("description"):
                lines.append("描述: " + _as_text(post.get("description")))
            if post.get("content_url"):
                lines.append("链接: " + _as_text(post.get("content_url")))
            location = post.get("location") or {}
            if location.get("poi_name"):
                lines.append("位置: " + _as_text(location.get("poi_name")))
            media_count = len(post.get("media") or [])
            if media_count:
                lines.append(f"媒体: {media_count} 项")
            for comment in post.get("comments") or []:
                if _as_int(comment.get("type")) == 1:
                    lines.append(
                        "  赞: "
                        + (
                            _as_text(comment.get("from_display_name"))
                            or _as_text(comment.get("from_nickname"))
                            or _as_text(comment.get("from_username"))
                        )
                    )
                else:
                    name = (
                        _as_text(comment.get("from_display_name"))
                        or _as_text(comment.get("from_nickname"))
                        or _as_text(comment.get("from_username"))
                    )
                    target = (
                        _as_text(comment.get("to_display_name"))
                        or _as_text(comment.get("to_nickname"))
                        or _as_text(comment.get("to_username"))
                    )
                    prefix = name + (" 回复 " + target if target else "")
                    lines.append(f"  {prefix}: {_as_text(comment.get('content'))}")
            lines.append("")
        return "\n".join(lines)

    def _render(
        self,
        fmt: str,
        contact: dict[str, Any],
        posts: Sequence[dict[str, Any]],
        assets: dict[tuple[str, int], DownloadedMedia],
    ) -> str:
        if fmt == "html":
            return self._html_document(contact, posts, assets)
        if fmt == "json":
            return self._json_document(contact, posts)
        if fmt == "csv":
            return self._csv_document(contact, posts)
        if fmt == "txt":
            return self._txt_document(contact, posts)
        raise MomentsExportError(f"不支持的导出格式: {fmt}")

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8", newline="")

    def export(
        self,
        usernames: Sequence[str],
        fmt: str = "html",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        filename: Optional[str] = None,
        download_media: bool = False,
        tids: Optional[Sequence[str]] = None,
    ) -> MomentsExportResult:
        selected = self.service._normalize_selection(usernames)
        fmt = _as_text(fmt).lower().lstrip(".")
        if fmt not in self.SUPPORTED_FORMATS:
            raise MomentsExportError(f"不支持的导出格式: {fmt}")

        contact_lookup = self._contact_lookup(self.service.get_contacts())
        unknown = [username for username in selected if username not in contact_lookup]
        if unknown:
            raise MomentsSelectionError("选择中包含不存在的朋友圈联系人")
        post_filters: dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
        }
        if tids is not None:
            post_filters["tids"] = tids
        posts = self.service.get_posts(selected, **post_filters)
        grouped: dict[str, list[dict[str, Any]]] = {username: [] for username in selected}
        for post in posts:
            grouped[_as_text(post.get("username"))].append(post)

        assets: dict[tuple[str, int], DownloadedMedia] = {}
        downloaded = 0
        failed = 0
        if fmt == "html":
            assets = self._load_local_assets(posts)
        if download_media and fmt == "html":
            downloader = self.media_downloader or SafeMomentsMediaDownloader()
            reset = getattr(downloader, "reset_budget", None)
            if callable(reset):
                reset()
            assets, downloaded, failed = self._download_assets(
                posts, downloader, existing_assets=assets
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        if len(selected) == 1:
            username = selected[0]
            contact = contact_lookup[username]
            stem = safe_filename(
                filename or f"{_as_text(contact.get('display_name'))}_朋友圈"
            )
            path = self._unique_path(self.output_dir / f"{stem}.{fmt}")
            content = self._render(fmt, contact, grouped[username], assets)
            self._write_text(path, content)
            return MomentsExportResult(
                path=path,
                posts_count=len(posts),
                contacts_count=1,
                media_downloaded=downloaded,
                media_failed=failed,
                format=fmt,
                is_archive=False,
            )

        archive_stem = safe_filename(
            filename or f"朋友圈_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        archive_path = self._unique_path(self.output_dir / f"{archive_stem}.zip")
        with tempfile.TemporaryDirectory(prefix="wechat_moments_") as temporary:
            staging = Path(temporary)
            entries: list[tuple[dict[str, Any], str, int]] = []
            used_names: set[str] = set()
            for username in selected:
                contact = contact_lookup[username]
                base = safe_filename(_as_text(contact.get("display_name")))
                entry_name = f"{base}.{fmt}"
                counter = 1
                while entry_name.casefold() in used_names:
                    entry_name = f"{base}({counter}).{fmt}"
                    counter += 1
                used_names.add(entry_name.casefold())
                user_posts = grouped[username]
                self._write_text(
                    staging / entry_name,
                    self._render(fmt, contact, user_posts, assets),
                )
                entries.append((contact, entry_name, len(user_posts)))

            index_parts = [
                "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
                "<title>朋友圈导出索引</title><style>body{font-family:sans-serif;max-width:700px;margin:auto;padding:20px}li{margin:10px 0}</style></head><body>",
                f"<h1>朋友圈导出</h1><p>共 {len(entries)} 位联系人，{len(posts)} 条动态</p><ul>",
            ]
            for contact, entry_name, count in entries:
                label = html.escape(_as_text(contact.get("display_name")), quote=True)
                href = html.escape(quote(entry_name), quote=True)
                index_parts.append(f'<li><a href="{href}">{label}</a>（{count} 条）</li>')
            index_parts.append("</ul></body></html>")
            self._write_text(staging / "index.html", "\n".join(index_parts))

            try:
                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(staging / "index.html", "index.html")
                    for _contact, entry_name, _count in entries:
                        archive.write(staging / entry_name, entry_name)
            except (OSError, zipfile.BadZipFile) as exc:
                try:
                    archive_path.unlink()
                except FileNotFoundError:
                    pass
                raise MomentsExportError("创建朋友圈导出压缩包失败") from exc

        return MomentsExportResult(
            path=archive_path,
            posts_count=len(posts),
            contacts_count=len(selected),
            media_downloaded=downloaded,
            media_failed=failed,
            format=fmt,
            is_archive=True,
        )

    def export_moments(self, *args: Any, **kwargs: Any) -> MomentsExportResult:
        """``export`` 的语义化别名。"""
        return self.export(*args, **kwargs)


__all__ = [
    "DownloadedMedia",
    "MomentsError",
    "MomentsExportError",
    "MomentsExportResult",
    "MomentsExporter",
    "MomentsMediaDownloadError",
    "MomentsSchemaError",
    "MomentsSelectionError",
    "MomentsService",
    "SafeMomentsMediaDownloader",
    "decode_sns_content",
    "escape_csv_formula",
    "parse_sns_timeline_content",
    "safe_filename",
    "sanitize_sns_xml",
]
