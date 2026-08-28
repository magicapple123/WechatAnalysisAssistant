"""Account-isolated access to real WeChat contact avatars.

WeChat 4.x keeps a useful local snapshot in ``head_image.db``.  Contacts that
are not present in that snapshot normally still have a WeChat-hosted avatar
URL in ``contact.db``.  This service deliberately accepts a *username*, never
an arbitrary URL: local bytes are preferred and the network fallback is
restricted to known WeChat image hosts and validated before it is cached.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, UnidentifiedImageError

from .app_paths import app_data_path
from .moments import (
    MomentsMediaDownloadError,
    SafeMomentsMediaDownloader,
)


AVATAR_MAX_FILE_BYTES = 2 * 1024 * 1024
AVATAR_MAX_PIXELS = 16_000_000
AVATAR_MAX_FRAMES = 50

_SELF_ALIAS = "__self__"
_ALLOWED_AVATAR_DOMAINS = (
    "wx.qlogo.cn",
    "thirdwx.qlogo.cn",
    "wework.qpic.cn",
    "mmhead.c2c.wechat.com",
    "mmhead.hk.wechat.com",
    "p.qpic.cn",
)
_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}


class AvatarError(RuntimeError):
    """Base class for avatar lookup failures."""


class AvatarNotFound(AvatarError):
    """The current account has no usable avatar for this username."""


class AvatarValidationError(AvatarError):
    """Avatar bytes did not pass format and resource-limit checks."""


@dataclass(frozen=True)
class AvatarImage:
    data: bytes
    mime_type: str
    etag: str
    source: str


def _default_cache_root() -> Path:
    return app_data_path("avatars")


def _account_digest(account_id: str) -> str:
    return hashlib.sha256(str(account_id or "").encode("utf-8")).hexdigest()[:24]


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_name(conn: sqlite3.Connection, wanted: str) -> str:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND lower(name)=lower(?) LIMIT 1",
        (wanted,),
    ).fetchone()
    return str(row[0]) if row else ""


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table)})"):
        actual = str(row[1])
        result[actual.lower()] = actual
    return result


class _SafeAvatarDownloader(SafeMomentsMediaDownloader):
    """Reuse the DNS-pinned downloader with an avatar-specific host list."""

    @staticmethod
    def _host_allowed(hostname: str) -> bool:
        host = str(hostname or "").lower().rstrip(".")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_AVATAR_DOMAINS
        )


class AvatarService:
    """Resolve one current-account username to a validated image payload."""

    def __init__(
        self,
        *,
        account_id: str,
        contact_db_path: Optional[Path] = None,
        head_image_db_path: Optional[Path] = None,
        cache_root: Optional[Path] = None,
        downloader_factory: Optional[Callable[[], object]] = None,
    ):
        self.account_id = str(account_id or "").strip()
        self.contact_db_path = (
            Path(contact_db_path) if contact_db_path else None
        )
        self.head_image_db_path = (
            Path(head_image_db_path) if head_image_db_path else None
        )
        root = Path(cache_root) if cache_root else _default_cache_root()
        self.cache_dir = root / _account_digest(self.account_id)
        self._downloader_factory = downloader_factory or self._make_downloader
        self._locks_guard = threading.Lock()
        self._username_locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _make_downloader() -> _SafeAvatarDownloader:
        return _SafeAvatarDownloader(
            timeout_seconds=5,
            max_file_bytes=AVATAR_MAX_FILE_BYTES,
            max_total_bytes=AVATAR_MAX_FILE_BYTES,
            max_redirects=3,
            max_total_seconds=15,
            max_pixels=AVATAR_MAX_PIXELS,
            max_frames=AVATAR_MAX_FRAMES,
        )

    @staticmethod
    def _open_readonly(path: Optional[Path]) -> Optional[sqlite3.Connection]:
        if path is None or not path.is_file():
            return None
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _normalize_username(username: str) -> str:
        value = str(username or "").strip()
        if not value or len(value) > 256:
            raise AvatarNotFound("头像账号无效")
        if any(ord(char) < 32 for char in value) or any(
            char in value for char in ("/", "\\")
        ):
            raise AvatarNotFound("头像账号无效")
        return value

    def _candidate_usernames(self, username: str) -> list[str]:
        normalized = self._normalize_username(username)
        candidates: list[str] = []
        if normalized != _SELF_ALIAS:
            candidates.append(normalized)
        if normalized == _SELF_ALIAS or normalized == self.account_id:
            match = re.fullmatch(
                r"(wxid_.+)_([0-9a-fA-F]{4,16})", self.account_id
            )
            if match:
                candidates.append(match.group(1))
            if self.account_id:
                candidates.append(self.account_id)
        return list(dict.fromkeys(item for item in candidates if item))

    def _lock_for(self, username: str) -> threading.Lock:
        with self._locks_guard:
            return self._username_locks.setdefault(username, threading.Lock())

    @staticmethod
    def _validate(data: bytes, *, source: str) -> AvatarImage:
        payload = bytes(data or b"")
        if not payload or len(payload) > AVATAR_MAX_FILE_BYTES:
            raise AvatarValidationError("头像文件为空或超过大小限制")
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image_format = str(image.format or "").upper()
                mime_type = _FORMAT_TO_MIME.get(image_format)
                if not mime_type:
                    raise AvatarValidationError("头像图片格式不受支持")
                width, height = image.size
                frames = int(getattr(image, "n_frames", 1) or 1)
                if width <= 0 or height <= 0:
                    raise AvatarValidationError("头像图片尺寸无效")
                if width * height > AVATAR_MAX_PIXELS:
                    raise AvatarValidationError("头像图片像素超过安全限制")
                if frames > AVATAR_MAX_FRAMES:
                    raise AvatarValidationError("头像动图帧数超过安全限制")
                image.verify()
        except AvatarValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AvatarValidationError("头像图片校验失败") from exc
        digest = hashlib.sha256(payload).hexdigest()
        return AvatarImage(
            data=payload,
            mime_type=mime_type,
            etag=digest,
            source=source,
        )

    def _read_local_avatar(self, candidates: list[str]) -> Optional[AvatarImage]:
        conn = self._open_readonly(self.head_image_db_path)
        if conn is None:
            return None
        try:
            table = _table_name(conn, "head_image")
            if not table:
                return None
            columns = _columns(conn, table)
            username_col = columns.get("username") or columns.get("user_name")
            buffer_col = columns.get("image_buffer") or columns.get("image")
            update_col = columns.get("update_time")
            if not username_col or not buffer_col:
                return None
            order = (
                f" ORDER BY {_quote_identifier(update_col)} DESC"
                if update_col
                else ""
            )
            sql = (
                f"SELECT {_quote_identifier(buffer_col)} FROM "
                f"{_quote_identifier(table)} WHERE "
                f"{_quote_identifier(username_col)}=?{order} LIMIT 1"
            )
            for candidate in candidates:
                row = conn.execute(sql, (candidate,)).fetchone()
                if not row or row[0] is None:
                    continue
                try:
                    return self._validate(bytes(row[0]), source="local")
                except AvatarValidationError:
                    continue
            return None
        finally:
            conn.close()

    def _contact_metadata(
        self, candidates: list[str]
    ) -> tuple[str, list[str], str]:
        conn = self._open_readonly(self.contact_db_path)
        if conn is None:
            return "", [], ""
        try:
            table = _table_name(conn, "contact")
            if not table:
                return "", [], ""
            columns = _columns(conn, table)
            username_col = columns.get("username") or columns.get("user_name")
            small_col = columns.get("small_head_url")
            big_col = columns.get("big_head_url")
            md5_col = columns.get("head_img_md5")
            if not username_col or not (small_col or big_col):
                return "", [], ""
            selected = [username_col]
            if small_col:
                selected.append(small_col)
            if big_col:
                selected.append(big_col)
            if md5_col:
                selected.append(md5_col)
            sql = (
                "SELECT "
                + ", ".join(_quote_identifier(item) for item in selected)
                + f" FROM {_quote_identifier(table)} WHERE "
                + f"{_quote_identifier(username_col)}=? LIMIT 1"
            )
            for candidate in candidates:
                row = conn.execute(sql, (candidate,)).fetchone()
                if not row:
                    continue
                offset = 1
                small = str(row[offset] or "").strip() if small_col else ""
                offset += int(bool(small_col))
                big = str(row[offset] or "").strip() if big_col else ""
                offset += int(bool(big_col))
                image_md5 = str(row[offset] or "").strip() if md5_col else ""
                urls = list(dict.fromkeys(item for item in (small, big) if item))
                return candidate, urls, image_md5
            return "", [], ""
        finally:
            conn.close()

    def _cache_path(
        self, resolved_username: str, urls: list[str], image_md5: str
    ) -> Path:
        identity = "\0".join(
            [self.account_id, resolved_username, image_md5, *urls]
        )
        filename = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".img"
        return self.cache_dir / filename

    def _read_cached(self, path: Path) -> Optional[AvatarImage]:
        try:
            return self._validate(path.read_bytes(), source="network-cache")
        except (OSError, AvatarValidationError):
            try:
                path.unlink()
            except OSError:
                pass
            return None

    @staticmethod
    def _write_cached(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix="avatar_", delete=False
            ) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def get_avatar(self, username: str) -> AvatarImage:
        normalized = self._normalize_username(username)
        candidates = self._candidate_usernames(normalized)
        if not candidates:
            raise AvatarNotFound("未找到头像账号")

        with self._lock_for(normalized):
            local = self._read_local_avatar(candidates)
            if local is not None:
                return local

            resolved, urls, image_md5 = self._contact_metadata(candidates)
            if not resolved or not urls:
                raise AvatarNotFound("本地没有该联系人的头像")

            cache_path = self._cache_path(resolved, urls, image_md5)
            if cache_path.is_file():
                cached = self._read_cached(cache_path)
                if cached is not None:
                    return cached

            last_error: Optional[Exception] = None
            for url in urls:
                try:
                    downloader = self._downloader_factory()
                    downloaded = downloader.download(url)
                    avatar = self._validate(
                        bytes(downloaded.data), source="network"
                    )
                    self._write_cached(cache_path, avatar.data)
                    return avatar
                except (MomentsMediaDownloadError, AvatarValidationError, OSError) as exc:
                    last_error = exc
                    continue
            raise AvatarNotFound("微信头像暂时无法加载") from last_error


__all__ = [
    "AvatarError",
    "AvatarImage",
    "AvatarNotFound",
    "AvatarService",
    "AvatarValidationError",
]
