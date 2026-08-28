"""
微信解析助手 - 微信 4.x 聊天记录解析模块

4.x 架构:
- message_0/1/2.db: 消息数据库
  - Name2Id: user_name 映射
  - Msg_{MD5(user_name)}: 每个聊天的消息表
- contact/contact.db: 联系人/群聊信息
- session/session.db: 会话信息
"""
import base64
import hashlib
import html
import sqlite3
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from datetime import datetime


_XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_BARE_AMP_RE = re.compile(
    r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)"
)
_XML_PARSE_MAX_CHARS = 512_000
_ADDRESS_BOOK_MAX_CONTACTS = 50_000
_CONTACT_FIELD_LIMITS = {
    "username": 256,
    "nick_name": 256,
    "remark": 256,
    "alias": 128,
    "description": 2_000,
    "phone": 128,
}

try:
    import zstandard as zstd
    _ZSTD_CTX = zstd.ZstdDecompressor()
except ImportError:
    _ZSTD_CTX = None

MSG_TYPE_NAMES = {
    1: "文本", 2: "图片", 3: "图片", 4: "语音",
    5: "视频", 6: "文件", 7: "链接", 8: "表情",
    9: "位置", 10: "系统", 11: "红包", 12: "转账",
    13: "小程序", 14: "聊天记录", 15: "视频通话",
    34: "语音", 43: "视频", 47: "表情",
    48: "位置", 49: "链接", 50: "实时对讲",
    10000: "系统消息", 10002: "系统通知",
}

# 非文本消息类型的占位符
TYPE_PLACEHOLDERS = {
    2: "[图片]", 3: "[图片]",
    4: "[语音]", 34: "[语音]",
    5: "[视频]", 43: "[视频]",
    6: "[文件]",
    7: "[链接]", 49: "[链接]",
    8: "[表情]", 47: "[表情]",
    9: "[位置]", 48: "[位置]",
    10: "[系统消息]", 10000: "[系统消息]", 10002: "[系统通知]",
    11: "[红包]", 12: "[转账]",
    13: "[小程序]", 14: "[聊天记录]",
    15: "[视频通话]", 50: "[实时对讲]",
    37: "[好友验证]", 42: "[名片]", 51: "[视频通话]",
}


def _split_msg_type(t: int) -> tuple[int, int]:
    """拆分 WeChat 复合消息类型: (base_type, sub_type)"""
    if t > 0xFFFFFFFF:
        return t & 0xFFFFFFFF, t >> 32
    return t, 0


def _decompress_zstd(raw: bytes) -> Optional[str]:
    """zstd 解压消息内容，失败返回 None"""
    if _ZSTD_CTX is None or not raw:
        return None
    try:
        return _ZSTD_CTX.decompress(raw).decode("utf-8", errors="replace")
    except Exception:
        return None


def _bounded_text(value, limit: int) -> str:
    """Return a control-character-free, size-bounded text field."""
    if value is None:
        return ""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = _sanitize(str(value)).strip()
    return text[:max(0, int(limit))]


def _safe_int(value, default: int = 0, *, minimum: int = 0,
              maximum: int = 2**63 - 1) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed < minimum or parsed > maximum:
        return default
    return parsed


def _strip_sender_prefix(text: str) -> str:
    """Remove the ``wxid:\n`` prefix used by group rich messages."""
    value = str(text or "")
    if value.lstrip().startswith("<"):
        return value.lstrip()
    if ":\n" in value:
        prefix, rest = value.split(":\n", 1)
        if len(prefix) <= 256 and rest.lstrip().startswith("<"):
            return rest.lstrip()
    if ":" in value[:260]:
        prefix, rest = value.split(":", 1)
        if len(prefix) <= 256 and rest.lstrip().startswith("<"):
            return rest.lstrip()
    return value.lstrip()


def _decode_rich_payload(raw, ct_flag: int = 0) -> str:
    if raw is None:
        return ""
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, bytes):
        if ct_flag == 4:
            decompressed = _decompress_zstd(raw)
            if decompressed is not None:
                return _strip_sender_prefix(decompressed)
        try:
            return _strip_sender_prefix(raw.decode("utf-8", errors="replace"))
        except Exception:
            return ""
    return _strip_sender_prefix(str(raw))


def _safe_xml_root(xml_text: str) -> Optional[ET.Element]:
    # WCDB zstd payloads may retain NUL/C1 padding after the closing XML tag.
    # These bytes are not message data and make ElementTree reject otherwise
    # valid appmsg XML, including transfer amounts in wcpayinfo.
    text = _sanitize(_strip_sender_prefix(str(xml_text or "")))
    if not text or len(text) > _XML_PARSE_MAX_CHARS or _XML_UNSAFE_RE.search(text):
        return None
    # Some WeChat 4.x builds persist query strings with bare ampersands. Repair
    # only XML-invalid ampersands after rejecting declarations/entities.
    text = _BARE_AMP_RE.sub("&amp;", text)
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError, RecursionError):
        return None


def _xml_element(root: Optional[ET.Element], tag: str) -> Optional[ET.Element]:
    if root is None:
        return None
    if root.tag == tag:
        return root
    return root.find(f".//{tag}")


def _valid_md5(value) -> str:
    candidate = _bounded_text(value, 64).lower()
    return candidate if re.fullmatch(r"[0-9a-f]{32}", candidate) else ""


def _server_id_text_candidates(value) -> list[str]:
    """Return equivalent signed/unsigned SQLite text forms for one uint64 ID."""
    normalized = _bounded_text(value, 128)
    if not normalized or normalized == "0":
        return []
    candidates = [normalized]
    try:
        numeric = int(normalized, 10)
    except (TypeError, ValueError, OverflowError):
        return candidates
    if 0 <= numeric <= (2**64 - 1):
        signed = numeric if numeric <= (2**63 - 1) else numeric - 2**64
        signed_text = str(signed)
        if signed_text not in candidates:
            candidates.append(signed_text)
    elif -(2**63) <= numeric < 0:
        unsigned_text = str(numeric + 2**64)
        if unsigned_text not in candidates:
            candidates.append(unsigned_text)
    return candidates


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _decode_sticker_description(value) -> str:
    """Decode WeChat's bounded base64/protobuf sticker description."""
    encoded = _bounded_text(value, 8_192)
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        # A few builds store a short human-readable description directly.
        return encoded[:256] if len(encoded) <= 256 else ""
    if len(raw) > 8_192:
        return ""

    marker = b"default"
    index = raw.find(marker)
    if index < 0:
        return ""
    position = index + len(marker)
    if position >= len(raw) or raw[position] != 0x12:
        return ""
    position += 1

    length = 0
    shift = 0
    for _ in range(5):
        if position >= len(raw):
            return ""
        byte = raw[position]
        position += 1
        length |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    else:
        return ""
    if length <= 0 or length > 1_024 or position + length > len(raw):
        return ""
    try:
        return _bounded_text(raw[position:position + length], 256)
    except Exception:
        return ""


def _parse_sticker_xml(xml_text: str) -> tuple[dict, dict]:
    """Return a public sticker contract plus private resolution material."""
    root = _safe_xml_root(xml_text)
    emoji = _xml_element(root, "emoji")
    public = {"md5": "", "description": "", "available": False}
    if emoji is None:
        return public, {}

    attrs = {str(key).lower(): value for key, value in emoji.attrib.items()}
    md5 = _valid_md5(attrs.get("md5") or attrs.get("androidmd5"))
    description = _decode_sticker_description(attrs.get("desc"))
    if not description:
        description = _bounded_text(
            attrs.get("caption") or attrs.get("meaning") or "", 256
        )

    source_names = {
        "thumb_url": ("thumburl", "thumb_url"),
        "extern_url": ("externurl", "extern_url"),
        "cdn_url": ("cdnurl", "cdn_url"),
        "encrypt_url": ("encrypturl", "encrypt_url"),
        "aes_key": ("aeskey", "aes_key"),
        "product_id": ("productid", "product_id"),
    }
    private = {"md5": md5} if md5 else {}
    extern_md5 = _valid_md5(
        attrs.get("externmd5") or attrs.get("extern_md5")
    )
    if extern_md5:
        # This digest belongs to the decrypted extern_url payload (commonly
        # WXGF), not to the public/main sticker.  Keep it backend-private so
        # candidate-specific integrity checks do not weaken the API contract.
        private["extern_md5"] = extern_md5
    for output_name, candidates in source_names.items():
        raw_value = next((attrs.get(name) for name in candidates if attrs.get(name)), "")
        limit = 4_096 if output_name.endswith("url") else 512
        bounded = _bounded_text(raw_value, limit)
        if bounded:
            private[output_name] = bounded

    available = bool(
        md5
        or private.get("thumb_url")
        or private.get("extern_url")
        or private.get("cdn_url")
        or (private.get("encrypt_url") and private.get("aes_key"))
    )
    public.update({
        "md5": md5,
        "description": description,
        "available": available,
    })
    return public, private


def _parse_voice_duration(xml_text: str) -> float:
    root = _safe_xml_root(xml_text)
    voice = _xml_element(root, "voicemsg")
    if voice is None:
        return 0.0
    length_ms = _safe_int(
        voice.get("voicelength"), 0, minimum=1, maximum=86_400_000
    )
    return round(length_ms / 1000.0, 3) if length_ms else 0.0


def _parse_image_safe_metadata(xml_text: str, *, server_id: str = "") -> dict:
    root = _safe_xml_root(xml_text)
    image = _xml_element(root, "img")
    md5 = ""
    width = 0
    height = 0
    if image is not None:
        attrs = {str(key).lower(): value for key, value in image.attrib.items()}
        md5 = _valid_md5(attrs.get("md5") or attrs.get("md5sum"))
        width = _safe_int(
            attrs.get("cdnthumbwidth") or attrs.get("width"),
            0, minimum=1, maximum=200_000,
        )
        height = _safe_int(
            attrs.get("cdnthumbheight") or attrs.get("height"),
            0, minimum=1, maximum=200_000,
        )
    metadata = {
        "kind": "image",
        "md5": md5,
        "available": bool(server_id or md5),
    }
    if width:
        metadata["width"] = width
    if height:
        metadata["height"] = height
    return metadata


# 引用消息 inner type 标签
_REFER_INNER_TYPE = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片",
    43: "视频", 47: "表情", 48: "位置", 49: "链接",
    50: "通话",
}


# appmsg inner type 标签
_APPMSG_TYPE_LABEL = {
    4: "音乐", 5: "链接", 6: "文件", 8: "表情",
    19: "聊天记录", 33: "小程序", 36: "小程序",
    51: "视频号", 57: "回复",
    62: "拍一拍", 2000: "转账", 2001: "红包",
}

_TRANSFER_STATUS_LABEL = {
    "1": "发起转账",
    "3": "已收款",
    "4": "已退还",
    "5": "过期已退还",
    "7": "待领取",
    "8": "已领取",
}
_PAYMENT_AMOUNT_RE = re.compile(
    r"^(?:人民币\s*)?[¥￥]?\s*(\d{1,12}(?:,\d{3})*(?:\.\d{1,2})?)\s*(?:元)?$"
)
_CUSTOM_LINK_RE = re.compile(
    r"<\s*[\*_]?wc_custom_link[\*_]?(?:\s+[^>]*)?>(.*?)"
    r"</\s*[\*_]?wc_custom_link[\*_]?\s*>",
    re.IGNORECASE | re.DOTALL,
)
_RICH_IMAGE_RE = re.compile(r"<\s*img\b[^>]*?/?>", re.IGNORECASE | re.DOTALL)
_RICH_TAG_RE = re.compile(r"</?\s*[A-Za-z][^>]{0,4096}>", re.DOTALL)
_PAYMENT_PROTOCOL_RE = re.compile(
    r"(?:weixin|wxpay)://[^\s<>\"']+", re.IGNORECASE
)
_ENCODED_OPEN_TAG_RE = re.compile(
    r"&(?:(?:amp;)*)(?:lt|#0*60|#x0*3c);", re.IGNORECASE
)
_PAYMENT_NULL_VALUES = {"null", "(null)", "none", "-1"}
_PAYMENT_EMBEDDED_AMOUNT_RE = re.compile(
    r"(?:人民币\s*)?[¥￥]\s*\d{1,12}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:元)?"
)


def _normalise_payment_amount(value) -> str:
    """Return a bounded display amount without parsing IDs as money."""
    text = _bounded_text(value, 64).strip()
    if not text or text.lower() in {"null", "(null)", "none", "-1"}:
        return ""
    match = _PAYMENT_AMOUNT_RE.fullmatch(text)
    if not match:
        return ""
    return "¥" + match.group(1).replace(",", "")


def _payment_fields(element: Optional[ET.Element]) -> dict[str, str]:
    if element is None:
        return {}
    fields: dict[str, str] = {}
    for child in list(element):
        tag = str(child.tag).rsplit("}", 1)[-1].lower()
        value = _bounded_text("".join(child.itertext()), 512).strip()
        if value.lower() in _PAYMENT_NULL_VALUES:
            value = ""
        if tag and value and tag not in fields:
            fields[tag] = value
    return fields


def _pick_payment_field(fields: dict[str, str], *names: str) -> str:
    return next((fields.get(name.lower(), "") for name in names if fields.get(name.lower())), "")


def _clean_payment_notice_text(value) -> str:
    """Remove WeChat's pseudo-HTML while retaining only visible notice text."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = _sanitize(str(value or ""))[:_XML_PARSE_MAX_CHARS]
    # Some builds store the pseudo tags entity-escaped inside appmsg/title.
    text = html.unescape(text)
    text = _RICH_IMAGE_RE.sub("", text)
    text = _CUSTOM_LINK_RE.sub(lambda match: match.group(1), text)
    text = _PAYMENT_PROTOCOL_RE.sub("", text)
    text = _RICH_TAG_RE.sub("", text)
    # Remove a malformed custom-link tag without ever retaining href params.
    text = re.sub(
        r"</?\s*[\*_]?wc_custom_link[\*_]?[^>]*>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return _bounded_text(text, 1_000)


def _parse_protocol_payment_notice(value) -> Optional[dict]:
    """Recognise only strong WeChat protocol/icon markers, never keywords."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    raw = html.unescape(_sanitize(str(value or ""))[:_XML_PARSE_MAX_CHARS])
    lowered = raw.lower()
    if (
        "systemmessages_hongbaoicon.png" in lowered
        or "weixin://weixinhongbao/opendetail" in lowered
        or "wxpay://c2cbizmessagehandler/hongbao/" in lowered
    ):
        kind, label = "red_packet", "红包"
    elif (
        "systemmessages_transfericon.png" in lowered
        or "weixin://wcpay/transfer" in lowered
        or "weixin://weixintransfer" in lowered
        or "wxpay://c2cbizmessagehandler/transfer" in lowered
    ):
        kind, label = "transfer", "转账"
    else:
        return None

    # Only flatten the two display-only tags used by the system notice.  If
    # any normal XML element remains, returning its node values could expose
    # private payment IDs from a damaged appmsg body.
    notice_probe = _RICH_IMAGE_RE.sub("", raw)
    notice_probe = _CUSTOM_LINK_RE.sub(lambda match: match.group(1), notice_probe)
    notice_probe = re.sub(
        r"</?\s*[\*_]?wc_custom_link[\*_]?[^>]*>",
        "",
        notice_probe,
        flags=re.IGNORECASE | re.DOTALL,
    )
    has_structured_xml = bool(
        _RICH_TAG_RE.search(notice_probe)
        or _ENCODED_OPEN_TAG_RE.search(notice_probe)
    )
    has_display_icon = (
        "systemmessages_hongbaoicon.png" in lowered
        or "systemmessages_transfericon.png" in lowered
    )
    # Only icon-based UI notices are safe to flatten into human-readable text.
    # A protocol URI by itself may sit beside private nodes in a malformed or
    # repeatedly entity-encoded system payload, so it degrades to a bare label.
    title = (
        _clean_payment_notice_text(raw)
        if has_display_icon and not has_structured_xml
        else label
    )
    return {
        "kind": kind,
        "label": label,
        "amount": "",
        "status": "",
        "memo": "",
        "title": title or label,
    }


def _parse_appmsg_payment(
    appmsg: ET.Element,
    inner_type: int,
    title: str,
) -> Optional[dict]:
    if inner_type not in (2000, 2001):
        return None
    info = appmsg.find("wcpayinfo")
    if info is None:
        info = appmsg.find(".//wcpayinfo")
    fields = _payment_fields(info)
    if inner_type == 2000:
        status_code = _pick_payment_field(fields, "paysubtype")
        amount = _normalise_payment_amount(
            _pick_payment_field(fields, "feedesc", "feeDesc")
        )
        if not amount:
            embedded_amount = _PAYMENT_EMBEDDED_AMOUNT_RE.search(
                _pick_payment_field(fields, "feedescxml")
            )
            if embedded_amount:
                amount = _normalise_payment_amount(embedded_amount.group(0))
        memo = _bounded_text(
            _pick_payment_field(fields, "pay_memo", "paymemo"), 200
        )
        return {
            "kind": "transfer",
            "label": "转账",
            "amount": amount,
            "status": _TRANSFER_STATUS_LABEL.get(status_code, ""),
            "memo": memo,
            "title": _clean_payment_notice_text(title) or "微信转账",
        }

    scene = _bounded_text(_pick_payment_field(fields, "scenetext"), 128)
    greeting = _bounded_text(
        _pick_payment_field(fields, "sendertitle"), 256
    )
    return {
        "kind": "red_packet",
        "label": "红包",
        # Standard red-packet XML does not contain a trustworthy amount.
        "amount": "",
        "status": scene,
        "memo": "",
        "title": greeting or _clean_payment_notice_text(title) or "微信红包",
    }


def _payment_content(payment: dict) -> str:
    label = _bounded_text(payment.get("label"), 32) or "支付消息"
    status = _bounded_text(payment.get("status"), 64)
    amount = _normalise_payment_amount(payment.get("amount"))
    memo = _bounded_text(payment.get("memo"), 200)
    title = _bounded_text(payment.get("title"), 500)
    prefix = f"[{label}·{status}]" if status and status != label else f"[{label}]"
    parts = [prefix]
    if amount:
        parts.append(amount)
    if memo:
        parts.append(f"备注：{memo}")
    generic_titles = {label, f"微信{label}"}
    if title and title not in generic_titles and title not in " ".join(parts):
        parts.append(title)
    return " ".join(parts)


def _summarize_refer(refer_type: int, content: str) -> str:
    """生成被引用消息的摘要"""
    label = _REFER_INNER_TYPE.get(refer_type)
    if refer_type == 1:
        text = re.sub(r"\s+", " ", _bounded_text(content, 2_000)).strip()
        return text[:160] if text else "[文本]"
    if label:
        return f"[{label}]"
    return "[消息]"


def _find_matching_close(text: str, start: int, tag: str) -> int:
    """找到与 start 处开标签匹配的闭标签位置（处理嵌套）"""
    open_tag = f"<{tag}"
    close_tag = f"</{tag}>"
    depth = 0
    i = start
    while i < len(text):
        if text[i:i+len(open_tag)] == open_tag:
            next_char = text[i+len(open_tag)] if i+len(open_tag) < len(text) else ''
            if next_char in '> \t\n\r':
                depth += 1
                i += len(open_tag)
                continue
        if text[i:i+len(close_tag)] == close_tag:
            depth -= 1
            if depth == 0:
                return i + len(close_tag)
            i += len(close_tag)
            continue
        i += 1
    return -1


def _extract_dataitems(html_text: str) -> list[str]:
    """提取顶层 dataitem（跳过嵌套的 dataitem）"""
    result = []
    pos = 0
    while pos < len(html_text):
        start = html_text.find("<dataitem", pos)
        if start < 0:
            break
        depth = 0
        i = start
        while i < len(html_text):
            if html_text[i:i+9] == "<dataitem":
                if i + 9 >= len(html_text) or html_text[i+9] in '> \t\n\r/':
                    depth += 1
                    i += 9
                    continue
            if html_text[i:i+11] == "</dataitem>":
                depth -= 1
                if depth == 0:
                    result.append(html_text[start:i+11])
                    pos = i + 11
                    break
                i += 11
                continue
            i += 1
        else:
            pos = start + 9
    return result


def _resolve_names_in_text(text: str, name_resolver=None, nick_map: dict = None) -> str:
    """将文本中的昵称替换为备注名"""
    if not name_resolver or not text:
        return text
    known_names = []
    if nick_map:
        known_names = sorted(nick_map.keys(), key=len, reverse=True)
    for name in known_names:
        if name in text:
            resolved = name_resolver(name)
            if resolved and resolved != name:
                text = text.replace(name, resolved)
    return text


def _parse_record_msg(root, name_resolver=None, depth=0, nick_map=None) -> Optional[str]:
    """解析合并转发的聊天记录 (type=19)，支持嵌套"""
    import html as _html
    import re as _re

    try:
        appmsg = root.find("appmsg")
        if appmsg is None:
            return None
        title_el = appmsg.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else "聊天记录"
        title = _resolve_names_in_text(title, name_resolver, nick_map)

        record_el = appmsg.find("recorditem")
        if record_el is None or not (record_el.text and record_el.text.strip()):
            des_el = appmsg.find("des")
            if des_el is not None and des_el.text:
                return "\n".join(des_el.text.strip().split("\n")[:20])
            return f"[聊天记录] {title}" if title else "[聊天记录]"

        rec_xml = _html.unescape(record_el.text)
        dl_start = rec_xml.find("<datalist")
        if dl_start < 0:
            return f"[聊天记录] {title}" if title else "[聊天记录]"
        dl_end = _find_matching_close(rec_xml, dl_start, "datalist")
        if dl_end < 0:
            return f"[聊天记录] {title}" if title else "[聊天记录]"
        inner_start = rec_xml.find(">", dl_start) + 1
        raw_items = rec_xml[inner_start:dl_end - len("</datalist>")]
        items = _extract_dataitems(raw_items)

        if not items:
            return f"[聊天记录] {title}" if title else "[聊天记录]"

        prefix = "  " * depth
        indent = "  " * (depth + 1)

        lines = []
        lines.append(f"{prefix}[聊天记录: {title}]")

        for item in items:
            dt = _re.search(r'datatype="(\d+)"', item)
            dtype = dt.group(1) if dt else "1"
            sn = _re.search(r"<sourcename>(.*?)</sourcename>", item)
            sname = sn.group(1).strip() if sn else ""
            dd = _re.search(r"<datadesc>(.*?)</datadesc>", item, _re.DOTALL)
            desc = dd.group(1).strip() if dd else ""

            display = sname
            if name_resolver and sname:
                resolved = name_resolver(sname)
                if resolved and resolved != sname:
                    display = resolved

            if dtype == "17":
                # 嵌套聊天记录
                rx_start = item.find("<recordxml>")
                if rx_start >= 0:
                    rx_end = item.find("</recordxml>", rx_start)
                    rx_text = item[rx_start+11:rx_end] if rx_end >= 0 else ""
                else:
                    rx_text = ""

                # 从嵌套消息提取参与者
                nested_items = []
                ndl_start2 = rx_text.find("<datalist") if rx_text else -1
                if ndl_start2 >= 0:
                    ndl_end2 = _find_matching_close(rx_text, ndl_start2, "datalist")
                    if ndl_end2 >= 0:
                        ninner2 = rx_text.find(">", ndl_start2) + 1
                        nested_items = _extract_dataitems(rx_text[ninner2:ndl_end2 - len("</datalist>")])

                # 生成嵌套标题
                ri = _re.search(r"<recordinfo>(.*?)</recordinfo>", rx_text, _re.DOTALL) if rx_text else None
                search_in = ri.group(1) if ri else desc
                nt_m = _re.search(r"<title>(.*?)</title>", search_in)
                if nt_m:
                    nt = nt_m.group(1)
                else:
                    participants = []
                    for ni in nested_items[:20]:
                        nsn = _re.search(r"<sourcename>(.*?)</sourcename>", ni)
                        if nsn:
                            n = nsn.group(1).strip()
                            rd = name_resolver(n) if name_resolver and n else n
                            if rd and rd not in participants:
                                participants.append(rd or n)
                            if len(participants) >= 2:
                                break
                    if len(participants) >= 2:
                        nt = f"{participants[0]}和{participants[1]}的聊天记录"
                    elif participants:
                        nt = f"{participants[0]}的聊天记录"
                    else:
                        nt = "聊天记录"
                nt = _resolve_names_in_text(nt, name_resolver, nick_map)
                lines.append(f"{indent}[聊天记录: {nt}]")

                ni_indent = "  " * (depth + 2)
                for ni in nested_items[:15 if depth == 0 else 5]:
                    ndt = _re.search(r'datatype="(\d+)"', ni)
                    ndtype = ndt.group(1) if ndt else "1"
                    nsn = _re.search(r"<sourcename>(.*?)</sourcename>", ni)
                    nsname = nsn.group(1).strip() if nsn else ""
                    ndd = _re.search(r"<datadesc>(.*?)</datadesc>", ni, _re.DOTALL)
                    ndesc = ndd.group(1).strip() if ndd else ""
                    ndisplay = nsname
                    if name_resolver and nsname:
                        nresolved = name_resolver(nsname)
                        if nresolved and nresolved != nsname:
                            ndisplay = nresolved
                    tl2 = {"1":"","2":"[图片]","3":"[图片]","4":"[语音]","34":"[语音]","5":"[视频]","43":"[视频]","8":"[表情]","47":"[表情]","37":"","6":"[文件]","49":"[链接]"}
                    nlbl = tl2.get(ndtype, f"[类型{ndtype}]")
                    if ndtype in ("1","37"):
                        lines.append(f"{ni_indent}{ndisplay}: {ndesc or nlbl}")
                    else:
                        lines.append(f"{ni_indent}{ndisplay}: {nlbl}{' ' + ndesc if ndesc and ndtype not in ('2','3') else ''}")
                lines.append(f"{indent}[/聊天记录]")
                continue

            type_labels = {"1":"","2":"[图片]","3":"[图片]","4":"[语音]","34":"[语音]","5":"[视频]","43":"[视频]","8":"[表情]","47":"[表情]","37":"","6":"[文件]","49":"[链接]"}
            label = type_labels.get(dtype, f"[类型{dtype}]")
            if dtype in ("1","37"):
                lines.append(f"{indent}{display}: {desc or label}")
            else:
                text = f"{display}: {label}"
                if desc and dtype not in ("2","3"):
                    text += f" {desc}"
                lines.append(f"{indent}{text}")

        # 结束标记
        lines.append(f"{prefix}[/聊天记录]")

        if len(lines) > 52:
            lines = lines[:52]
            lines.append(f"{indent}... 还有更多消息")

        return "\n".join(lines)
    except Exception:
        return None


def _parse_refer_details(
    appmsg: ET.Element,
    *,
    name_resolver=None,
) -> Optional[dict]:
    refermsg = appmsg.find("refermsg")
    if refermsg is None:
        return None

    title = _bounded_text(appmsg.findtext("title"), 2_000)
    refer_type = _safe_int(
        refermsg.findtext("type"), 1, minimum=0, maximum=100_000
    )
    refer_content = _bounded_text(refermsg.findtext("content"), _XML_PARSE_MAX_CHARS)
    from_username = _bounded_text(refermsg.findtext("fromusr"), 256)
    chat_username = _bounded_text(refermsg.findtext("chatusr"), 256)
    sender_username = chat_username or from_username
    sender_name = _bounded_text(refermsg.findtext("displayname"), 256)
    if name_resolver:
        for username in (chat_username, from_username):
            if not username:
                continue
            try:
                resolved = _bounded_text(name_resolver(username), 256)
            except Exception:
                resolved = ""
            if resolved and resolved != username:
                sender_name = resolved
                break
    if not sender_name:
        sender_name = sender_username

    server_id = _bounded_text(refermsg.findtext("svrid"), 128)
    if server_id == "0":
        server_id = ""
    create_time = _safe_int(
        refermsg.findtext("createtime"), 0, minimum=0, maximum=2**63 - 1
    )
    summary = _summarize_refer(refer_type, refer_content)
    refer_type_name = _REFER_INNER_TYPE.get(refer_type, "消息")

    reply = {
        "reply_text": title,
        "refer_type": refer_type,
        "refer_type_name": refer_type_name,
        "sender_username": sender_username,
        "sender_name": sender_name,
        "server_id": server_id,
        "create_time": create_time,
        "summary": summary,
        "media": None,
    }
    if refer_type == 3:
        reply["media"] = _parse_image_safe_metadata(
            refer_content, server_id=server_id
        )
    elif refer_type == 34:
        duration = _parse_voice_duration(refer_content)
        reply["media"] = {
            "kind": "voice",
            "duration_seconds": duration,
            "available": bool(server_id),
        }
    elif refer_type == 43:
        root = _safe_xml_root(refer_content)
        video = _xml_element(root, "videomsg")
        duration = 0.0
        if video is not None:
            play_length = _safe_int(
                video.get("playlength"), 0, minimum=1, maximum=86_400_000
            )
            duration = float(play_length) if play_length else 0.0
        reply["media"] = {
            "kind": "video",
            "duration_seconds": duration,
            "available": bool(server_id),
        }
    elif refer_type == 47:
        sticker, sticker_source = _parse_sticker_xml(refer_content)
        reply["media"] = {"kind": "sticker", **sticker}
        if sticker_source:
            # API consumers must strip underscore-prefixed fields before
            # serializing public message data. Raw URLs/AES material only lives
            # in this explicitly private resolver field.
            reply["_sticker_source"] = sticker_source

    return reply


def _parse_appmsg_details(
    xml_text: str,
    name_resolver=None,
    nick_map: dict = None,
) -> Optional[dict]:
    """Parse one appmsg once and retain its structured subtype contract."""
    raw_notice = _parse_protocol_payment_notice(xml_text)
    root = _safe_xml_root(xml_text)
    if root is None:
        if raw_notice:
            # Never flatten malformed appmsg XML. Repeated entity decoding can
            # otherwise turn private payment nodes into visible text.
            raw_notice = dict(raw_notice)
            raw_notice["title"] = str(raw_notice.get("label") or "支付消息")
            return {
                "inner_type": 0,
                "content": _payment_content(raw_notice),
                "payment": raw_notice,
            }
        return None
    appmsg = root if root.tag == "appmsg" else root.find(".//appmsg")
    if appmsg is None:
        return None
    inner_type = _safe_int(
        appmsg.findtext("type"), 0, minimum=0, maximum=100_000
    )
    title = _bounded_text(appmsg.findtext("title"), 2_000)
    details = {"inner_type": inner_type, "content": ""}

    payment = _parse_appmsg_payment(appmsg, inner_type, title)
    if payment:
        details["payment"] = payment
        details["content"] = _payment_content(payment)
        return details

    # A received-red-packet notice is sometimes wrapped in a generic type=5
    # appmsg title/description. Strong protocol markers distinguish it from a
    # normal article that merely contains the word “红包”.
    description = _bounded_text(appmsg.findtext("des"), 4_000)
    embedded_notice = _parse_protocol_payment_notice(
        "\n".join(value for value in (title, description) if value)
    )
    if embedded_notice:
        details["payment"] = embedded_notice
        details["content"] = _payment_content(embedded_notice)
        return details

    if inner_type == 57:
        reply = _parse_refer_details(appmsg, name_resolver=name_resolver)
        details["reply"] = reply
        if reply is None:
            details["content"] = f"[引用] {title}" if title else "[引用消息]"
            return details
        summary = str(reply.get("summary") or "[消息]")
        sender_name = str(reply.get("sender_name") or "")
        parts = [title] if title else []
        if sender_name:
            parts.append(f"  ↳ 回复 {sender_name}: {summary}")
        else:
            parts.append(f"  ↳ {summary}")
        details["content"] = "\n".join(parts)
        return details

    if inner_type == 19:
        result = _parse_record_msg(root, name_resolver, nick_map=nick_map)
        details["content"] = (
            result
            or (f"[聊天记录] {title}" if title else "[聊天记录]")
        )
        return details

    label = _APPMSG_TYPE_LABEL.get(inner_type, "消息")
    details["content"] = f"[{label}] {title}" if title else f"[{label}]"
    return details


def _parse_appmsg_xml(xml_text: str, name_resolver=None,
                      nick_map: dict = None) -> Optional[str]:
    """兼容旧调用：返回 appmsg 的格式化文本。"""
    details = _parse_appmsg_details(
        xml_text, name_resolver=name_resolver, nick_map=nick_map
    )
    return str(details.get("content") or "") if details else None


class MessageParserV4:
    """微信 4.x 消息解析器"""

    def __init__(self, connections: list[sqlite3.Connection], wx_root: Path,
                 wxid: str, contact_conn: Optional[sqlite3.Connection] = None):
        self.conns = connections  # 所有 message_N.db 的连接
        self.contact_conn = contact_conn
        self.wx_root = wx_root
        self.wxid = wxid
        self._name_cache: dict[str, str] = {}
        self._nick_to_wxid: dict[str, str] = {}
        self._self_usernames = self._build_self_usernames(wxid)
        # 每个数据库连接 → 用户自己的 real_sender_id
        self._self_sender_ids: dict[sqlite3.Connection, int] = {}
        # 每个 message_N.db 的 Name2Id.rowid → 发送者 wxid。
        # real_sender_id 正是这个 rowid；直接映射比从消息正文猜测更可靠。
        self._sender_id_cache: dict[tuple, str] = {}
        self._load_chat_names()
        self._load_sender_id_maps()
        self._discover_self_sender_ids()

    @staticmethod
    def _build_self_usernames(wxid: str) -> set[str]:
        value = str(wxid or "").strip()
        candidates = {value} if value else set()
        match = re.fullmatch(r"(wxid_.+)_([0-9a-fA-F]{4,16})", value)
        if match:
            candidates.add(match.group(1))
        return {item for item in candidates if item}

    def _primary_self_username(self) -> str:
        for username in self._self_usernames:
            if username != self.wxid:
                return username
        return str(self.wxid or "")

    def _load_sender_id_maps(self) -> None:
        for conn in self.conns:
            try:
                for row in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                    sender_id = row[0]
                    username = str(row[1] or "").strip()
                    if sender_id is not None and username:
                        self._sender_id_cache[(id(conn), int(sender_id))] = username
            except Exception:
                continue

    def _load_chat_names(self):
        """从 contact.db 加载联系人名称，优先使用备注名"""
        if not self.contact_conn:
            return
        try:
            cur = self.contact_conn.cursor()
            for table in ["Contact", "contact", "Friend", "ChatRoom"]:
                try:
                    cur.execute(f"SELECT * FROM [{table}] LIMIT 1")
                    cols = [d[0].lower() for d in cur.description]
                    uname_col = None
                    remark_col = None
                    nick_col = None
                    for c in cols:
                        if c in ("username", "user_name", "userid"):
                            uname_col = c
                        if c == "remark":
                            remark_col = c
                        if c in ("nick_name", "nickname", "display_name"):
                            nick_col = c
                    if not uname_col:
                        continue
                    # 构建查询：优先 remark，其次 nick_name
                    if remark_col and nick_col:
                        cur.execute(
                            f"SELECT [{uname_col}], [{remark_col}], [{nick_col}] FROM [{table}]"
                        )
                        for row in cur.fetchall():
                            self._name_cache[row[0]] = row[1] or row[2] or row[0]
                            if row[2] and (row[2] not in self._nick_to_wxid or row[1]):
                                self._nick_to_wxid[row[2]] = row[0]
                    elif remark_col:
                        cur.execute(
                            f"SELECT [{uname_col}], [{remark_col}] FROM [{table}]"
                        )
                        for row in cur.fetchall():
                            self._name_cache[row[0]] = row[1] or row[0]
                    elif nick_col:
                        cur.execute(
                            f"SELECT [{uname_col}], [{nick_col}] FROM [{table}]"
                        )
                        for row in cur.fetchall():
                            self._name_cache[row[0]] = row[1] or row[0]
                            if row[1]:
                                self._nick_to_wxid[row[1]] = row[0]
                    break
                except Exception:
                    continue
        except Exception:
            pass

    def _discover_self_sender_ids(self):
        """找出每个数据库中映射到用户自己的 real_sender_id

        策略: 直接扫描数据库中实际存在的 Msg_* 表，
        在所有 1对1 聊天表中都出现的 real_sender_id 就是用户自己。
        """
        from collections import Counter
        for conn in self.conns:
            try:
                cur = conn.cursor()
                # 获取该数据库中实际存在的 Msg_ 表名
                cur.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'Msg_%'"
                )
                tables = [r[0] for r in cur.fetchall()]
                if len(tables) < 2:
                    continue

                # 先区分群聊/单聊: 检查 talker 格式
                # Msg 表名 = Msg_{MD5(talker)}
                # 我们需要知道哪些表是单聊。用 Name2Id 反查。
                cur.execute("SELECT user_name FROM Name2Id")
                name2id_users = {r[0] for r in cur.fetchall()}

                # 统计每个 real_sender_id 出现在多少个单聊表中
                id_chat_count: Counter = Counter()
                single_chat_count = 0
                for table in tables[:50]:  # 最多采样 50 个
                    try:
                        # 只统计单聊表 (跳过群聊 @chatroom 和 @openim)
                        # 通过 Name2Id 反查 user_name
                        is_single = False
                        for u in name2id_users:
                            if not u:
                                continue
                            expected = f"Msg_{hashlib.md5(u.encode()).hexdigest()}"
                            if expected == table:
                                if "@chatroom" not in u and "openim" not in u:
                                    is_single = True
                                break
                        if not is_single:
                            continue

                        single_chat_count += 1
                        cur.execute(
                            f"SELECT DISTINCT real_sender_id FROM [{table}] LIMIT 5"
                        )
                        for row in cur.fetchall():
                            id_chat_count[row[0]] += 1
                    except Exception:
                        continue

                if single_chat_count < 2:
                    continue

                # 真实用户会出现在大多数单聊中
                if id_chat_count:
                    most_common_id, count = id_chat_count.most_common(1)[0]
                    if count >= single_chat_count * 0.5:
                        self._self_sender_ids[conn] = most_common_id
            except Exception:
                continue

    def _get_display_name(self, username: str) -> str:
        """获取用户显示名称 (按 wxid 查找)"""
        if username in self._name_cache:
            return self._name_cache[username]
        if username.endswith("@chatroom"):
            return username.split("@")[0]
        if username.startswith("gh_"):
            return f"公众号({username})"
        if "@openim" in username:
            return username.split("@")[0]
        return username

    def _resolve_name(self, name: str) -> str:
        """将任意名称（wxid/昵称/备注）解析为最终显示名（备注优先）"""
        if not name:
            return name
        # 1. wxid 直接匹配
        if name in self._name_cache:
            return self._name_cache[name]
        # 2. 昵称 → wxid → 备注
        if name in self._nick_to_wxid:
            wxid = self._nick_to_wxid[name]
            cached = self._name_cache.get(wxid)
            if cached and cached != name:
                return cached
        return name

    def _get_msg_tables(self, username: str) -> list[tuple[sqlite3.Connection, str]]:
        """获取用户在所有消息分片中对应的消息表。

        微信会让同一个 ``Msg_<md5>`` 表同时出现在多个 ``message_N.db``
        中，因此调用方不能只取第一个命中的连接。
        """
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        tables = []
        for conn in self.conns:
            try:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name=? LIMIT 1",
                    (table_name,),
                ).fetchone()
                if row:
                    tables.append((conn, table_name))
            except Exception:
                continue
        return tables

    def _get_msg_table(self, username: str) -> Optional[tuple[sqlite3.Connection, str]]:
        """兼容旧调用：返回第一个包含该聊天消息表的分片。"""
        tables = self._get_msg_tables(username)
        return tables[0] if tables else None

    def get_contacts(self) -> list[dict]:
        """获取聊天联系人列表"""
        contacts = {}
        for conn in self.conns:
            try:
                cur = conn.cursor()
                cur.execute("SELECT user_name FROM Name2Id")
                for row in cur.fetchall():
                    username = row[0]
                    if not username:
                        continue
                    if username in contacts:
                        continue
                    contacts[username] = {
                        "talker": username,
                        "display_name": self._get_display_name(username),
                        "is_group": "@chatroom" in username,
                        "last_message": "",
                        "last_time": 0,
                        "last_time_str": "",
                        "message_count": 0,
                    }
            except Exception:
                pass

        # 获取每个聊天的摘要（同一聊天可能横跨多个 message_N.db）
        for username in contacts:
            msg_tables = self._get_msg_tables(username)
            if not msg_tables:
                continue
            latest = None
            total_count = 0
            for shard_index, (msg_conn, table) in enumerate(msg_tables):
                try:
                    cur = msg_conn.cursor()
                    cur.execute(
                        f"SELECT local_id, message_content, create_time, local_type,"
                        f" WCDB_CT_message_content FROM [{table}] "
                        f"ORDER BY create_time DESC, local_id DESC LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row:
                        key = (row[2] or 0, row[0] or 0, shard_index)
                        if latest is None or key > latest[0]:
                            latest = (key, row)

                    cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                    total_count += cur.fetchone()[0]
                except Exception:
                    continue

            if latest:
                row = latest[1]
                raw_type = row[3] or 1
                raw = row[1]
                create_time = row[2] or 0
                ct_flag = row[4] if len(row) > 4 else 0
                base_type, sub_type = _split_msg_type(raw_type)

                if base_type == 1:
                    content = self._parse_text_content(raw)
                    raw_bytes = raw if isinstance(raw, bytes) else (raw.encode('utf-8', errors='replace') if raw else b'')
                    if content and (_looks_like_binary(raw_bytes) or _looks_random(content)):
                        content = "[文本(编码异常)]"
                elif base_type == 49:
                    # app 消息：统一解析压缩与未压缩正文，支付消息不再退回 [链接]。
                    xml_text = _decode_rich_payload(raw, ct_flag)
                    if xml_text:
                        parsed = _parse_appmsg_xml(xml_text, self._resolve_name, nick_map=self._nick_to_wxid)
                        if parsed and "\n" in parsed:
                            # 多行引用消息只取第一行
                            content = parsed.split("\n")[0][:50]
                        elif parsed:
                            content = parsed[:50]
                        else:
                            label = _APPMSG_TYPE_LABEL.get(sub_type, "消息")
                            content = f"[{label}]"
                    else:
                        label = _APPMSG_TYPE_LABEL.get(sub_type, "消息")
                        content = f"[{label}]"
                elif base_type in (10000, 10002):
                    sys_content, _ = self._parse_system_msg(raw)
                    content = sys_content[:50] if sys_content else "[系统消息]"
                else:
                    content = TYPE_PLACEHOLDERS.get(
                        base_type,
                        MSG_TYPE_NAMES.get(base_type, f"[消息类型:{base_type}]")
                    )
                contacts[username]["last_message"] = (content or "")[:200]
                contacts[username]["last_time"] = create_time
                contacts[username]["last_time_str"] = _fmt_time(create_time)

            contacts[username]["message_count"] = total_count

        result = sorted(
            contacts.values(),
            key=lambda x: x.get("last_time") or 0,
            reverse=True,
        )
        return result

    def get_address_book(self, limit: int = _ADDRESS_BOOK_MAX_CONTACTS) -> list[dict]:
        """Read the bounded WeChat 4.x address book from ``contact.db``.

        ``contact.local_type=3`` rows are per-room member snapshots rather than
        address-book entries and are intentionally excluded.  Schema discovery
        keeps this compatible with minor WeChat 4.x column drift without
        falling back to any 3.x database layout.
        """
        if not self.contact_conn:
            return []
        try:
            normalized_limit = max(
                1, min(int(limit), _ADDRESS_BOOK_MAX_CONTACTS)
            )
        except (TypeError, ValueError, OverflowError):
            normalized_limit = _ADDRESS_BOOK_MAX_CONTACTS

        try:
            table_row = self.contact_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND lower(name)='contact' LIMIT 1"
            ).fetchone()
            if not table_row:
                return []
            table = str(table_row[0])
            columns = {
                str(row[1]).lower(): str(row[1])
                for row in self.contact_conn.execute(
                    f"PRAGMA table_info({_quote_identifier(table)})"
                ).fetchall()
            }
        except sqlite3.Error:
            return []

        def choose(*names: str) -> Optional[str]:
            return next((columns[name] for name in names if name in columns), None)

        semantic_columns = {
            "username": choose("username", "user_name", "userid"),
            "nick_name": choose("nick_name", "nickname", "display_name"),
            "remark": choose("remark"),
            "alias": choose("alias"),
            "description": choose("description", "signature"),
            "phone": choose(
                "phone", "phone_number", "mobile", "mobile_phone", "telephone"
            ),
            "local_type": choose("local_type"),
        }
        if not semantic_columns["username"]:
            return []

        selected = [
            (semantic, column)
            for semantic, column in semantic_columns.items()
            if column is not None
        ]
        select_sql = ", ".join(
            _quote_identifier(column) for _, column in selected
        )
        sql = f"SELECT {select_sql} FROM {_quote_identifier(table)}"
        local_type_column = semantic_columns.get("local_type")
        if local_type_column:
            sql += f" WHERE COALESCE({_quote_identifier(local_type_column)}, 0) != 3"
        sql += " LIMIT ?"

        try:
            rows = self.contact_conn.execute(sql, (normalized_limit,)).fetchall()
        except sqlite3.Error:
            return []

        by_username: dict[str, dict] = {}
        for row in rows:
            raw = {
                semantic: row[index]
                for index, (semantic, _) in enumerate(selected)
            }
            username = _bounded_text(
                raw.get("username"), _CONTACT_FIELD_LIMITS["username"]
            )
            if not username:
                continue
            candidate = {
                "username": username,
                "talker": username,
                "nick_name": _bounded_text(
                    raw.get("nick_name"), _CONTACT_FIELD_LIMITS["nick_name"]
                ),
                "remark": _bounded_text(
                    raw.get("remark"), _CONTACT_FIELD_LIMITS["remark"]
                ),
                "alias": _bounded_text(
                    raw.get("alias"), _CONTACT_FIELD_LIMITS["alias"]
                ),
                "description": _bounded_text(
                    raw.get("description"), _CONTACT_FIELD_LIMITS["description"]
                ),
                "phone": _bounded_text(
                    raw.get("phone"), _CONTACT_FIELD_LIMITS["phone"]
                ),
                "local_type": _safe_int(
                    raw.get("local_type"), 0, minimum=0, maximum=1_000_000
                ),
            }
            existing = by_username.get(username)
            if existing:
                for field in (
                    "nick_name", "remark", "alias", "description", "phone"
                ):
                    if not existing.get(field) and candidate.get(field):
                        existing[field] = candidate[field]
                if not existing.get("local_type") and candidate.get("local_type"):
                    existing["local_type"] = candidate["local_type"]
                continue
            by_username[username] = candidate

        contacts = []
        for contact in by_username.values():
            username = contact["username"]
            contact["display_name"] = (
                contact.get("remark")
                or contact.get("nick_name")
                or username
            )
            contact["is_group"] = username.endswith("@chatroom")
            contact["is_official"] = username.startswith("gh_")
            contact["is_self"] = username in self._self_usernames
            contacts.append(contact)
        contacts.sort(
            key=lambda item: (
                str(item.get("display_name") or "").casefold(),
                str(item.get("username") or "").casefold(),
            )
        )
        return contacts

    def get_messages(
        self,
        talker: str,
        page: int = 1,
        page_size: int = 50,
        msg_type: Optional[int] = None,
        keyword: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        sender_name: Optional[str] = None,
        message_ids: Optional[list[int]] = None,
    ) -> dict:
        """获取与指定联系人的聊天消息

        支持按消息类型、关键词、时间范围、消息ID筛选。
        """
        msg_tables = self._get_msg_tables(talker)
        if not msg_tables:
            return {"messages": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 1}

        clauses = []
        params = []
        if msg_type is not None:
            normalized_type = int(msg_type) & 0xFFFFFFFF
            if normalized_type in (2, 3):
                clauses.append("(local_type & 4294967295) IN (2, 3)")
            else:
                clauses.append("(local_type & 4294967295) = ?")
                params.append(normalized_type)
        if keyword:
            clauses.append("CAST(message_content AS TEXT) LIKE ?")
            params.append(f"%{keyword}%")
        if sender_name:
            # 群聊消息前缀可能是 wxid 或昵称，都需要匹配
            patterns = [f"%{sender_name}:%", f"%{sender_name}\n%"]
            # 查找该名称对应的所有可能 wxid
            possible_wxids = []
            for wxid, display in self._name_cache.items():
                if sender_name.lower() in display.lower():
                    possible_wxids.append(wxid)
            # 限制数量避免 SQL 过长
            conditions = []
            for p in patterns:
                conditions.append("CAST(message_content AS TEXT) LIKE ?")
                params.append(p)
            for wxid in possible_wxids[:5]:
                conditions.append("CAST(message_content AS TEXT) LIKE ?")
                params.append(f"%{wxid}:%")
            clauses.append(f"({' OR '.join(conditions)})")
        if start_time is not None:
            clauses.append("create_time >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("create_time <= ?")
            params.append(end_time)
        if message_ids is not None and len(message_ids) > 0:
            placeholders = ",".join("?" * len(message_ids))
            clauses.append(f"local_id IN ({placeholders})")
            params.extend(message_ids)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        offset = (page - 1) * page_size
        candidate_limit = offset + page_size
        total = 0
        candidates = []

        # 各分片先取全局当前页可能用到的前 N 条，再统一排序和切页。
        # 一个分片中排在其前 N 条之后的记录，不可能进入全局前 N 条。
        for shard_index, (msg_conn, table) in enumerate(msg_tables):
            try:
                cur = msg_conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM [{table}]{where}", params)
                shard_total = cur.fetchone()[0]

                cur.execute(
                    f"""
                    SELECT local_id, server_id, local_type, real_sender_id,
                           create_time, message_content, source, packed_info_data,
                           WCDB_CT_message_content
                    FROM [{table}]{where}
                    ORDER BY create_time ASC, local_id ASC
                    LIMIT ?
                    """,
                    params + [candidate_limit],
                )
                rows = cur.fetchall()
                total += shard_total
                for row in rows:
                    candidates.append((
                        row["create_time"] or 0,
                        row["local_id"] or 0,
                        shard_index,
                        msg_conn,
                        row,
                    ))
            except Exception:
                continue

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        page_rows = candidates[offset:offset + page_size]
        messages = [
            self._format_message(row, talker, conn=msg_conn)
            for _, _, _, msg_conn, row in page_rows
        ]

        # Enrich only replies already present on this page.  The small cache
        # prevents repeated shard scans when several replies reference the same
        # original message, while avoiding a full-chat eager join.
        reply_target_cache: dict[tuple[str, int], Optional[dict]] = {}
        for message in messages:
            reply = message.get("reply")
            if not isinstance(reply, dict):
                continue
            refer_server_id = _bounded_text(reply.get("server_id"), 128)
            refer_create_time = _safe_int(
                reply.get("create_time"), 0, minimum=0, maximum=2**63 - 1
            )
            if not refer_server_id or refer_server_id == "0":
                reply["target_message"] = None
                continue
            cache_key = (refer_server_id, refer_create_time)
            if cache_key not in reply_target_cache:
                original = self.get_message_by_server_id(
                    talker,
                    refer_server_id,
                    create_time=refer_create_time or None,
                )
                if original is None:
                    reply_target_cache[cache_key] = None
                else:
                    target = {
                        field: original.get(field)
                        for field in (
                            "id", "create_time", "server_id", "message_key",
                            "type", "type_name", "content",
                        )
                    }
                    if isinstance(original.get("sticker"), dict):
                        target["sticker"] = dict(original["sticker"])
                    if "voice_duration_seconds" in original:
                        target["voice_duration_seconds"] = original.get(
                            "voice_duration_seconds"
                        )
                    if isinstance(original.get("_sticker_source"), dict):
                        target["_sticker_source"] = dict(
                            original["_sticker_source"]
                        )
                    reply_target_cache[cache_key] = target
            cached_target = reply_target_cache[cache_key]
            reply["target_message"] = (
                dict(cached_target) if cached_target is not None else None
            )

        return {
            "messages": messages,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    def _parse_text_content(self, raw) -> str:
        """解析文本消息内容: sender_wxid:\ncontent"""
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return ""

        text = str(raw)
        # 格式: sender:\nmessage
        if "\n" in text:
            parts = text.split("\n", 1)
            if len(parts) == 2:
                text = parts[1]

        # 洗掉控制字符和替换字符
        return _sanitize(text)

    def _resolve_sender_username(self, raw, conn=None, real_sender_id=None) -> str:
        """Resolve a stable sender username, preferring Name2Id.rowid."""
        if conn is not None and real_sender_id is not None:
            cached = self._sender_id_cache.get((id(conn), int(real_sender_id)), "")
            if cached:
                return cached

        sender_wxid = ""
        if isinstance(raw, str) and raw:
            text = raw
        elif isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                return ""
        else:
            return ""

        # 从文本内容提取 wxid:\n 前缀
        if ":\n" in text and not text.startswith("<"):
            prefix = text.split(":\n", 1)[0]
            if len(prefix) < 50 and not any(c in prefix for c in '<>&'):
                sender_wxid = prefix.strip()
        elif ":" in text[:60] and not text.startswith("<"):
            prefix, rest = text.split(":", 1)
            if len(prefix) < 50 and not any(c in prefix for c in '<>&') and rest.strip().startswith("<"):
                sender_wxid = prefix.strip()

        # 缓存 sender_id → wxid 映射
        if conn is not None and real_sender_id is not None and sender_wxid:
            self._sender_id_cache[(id(conn), int(real_sender_id))] = sender_wxid

        return sender_wxid

    def _resolve_group_sender(self, raw, conn=None, real_sender_id=None) -> str:
        """从群聊消息中提取发送者名称（优先备注名）"""
        sender_wxid = self._resolve_sender_username(
            raw, conn=conn, real_sender_id=real_sender_id
        )
        return self._get_display_name(sender_wxid) if sender_wxid else ""

    def _resolve_sender_by_id(self, conn, real_sender_id) -> str:
        """通过 real_sender_id 查找发送者名称"""
        if not conn or real_sender_id is None:
            return ""
        key = (id(conn), real_sender_id)
        wxid = self._sender_id_cache.get(key, "")
        if wxid:
            return self._get_display_name(wxid)
        return ""

    def _is_self(
        self,
        raw,
        real_sender_id=None,
        conn=None,
        is_group=False,
        sender_username: str = "",
    ) -> bool:
        """判断消息是否自己发送

        微信 4.x 中，自己的消息（无论单聊还是群聊）都没有 sender_wxid:\\n 前缀，
        因此统一使用 real_sender_id 匹配每个数据库中的 self id。
        """
        if sender_username and sender_username in self._self_usernames:
            return True
        if real_sender_id is not None and conn in self._self_sender_ids:
            return real_sender_id == self._self_sender_ids[conn]
        return False

    def _parse_system_msg(self, raw) -> tuple[str, bool]:
        """解析系统消息 (type=10000)，返回 (内容, 是否居中显示)"""
        if not raw:
            return "[系统消息]", True
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", errors="replace")
            except Exception:
                return "[系统消息]", True
        text = _sanitize(raw)
        payment_notice = _parse_protocol_payment_notice(text)
        if payment_notice:
            return str(payment_notice.get("title") or payment_notice["label"]), True
        # 解析撤回消息: "xxx" 撤回了一条消息 或 你撤回了一条消息
        if "revokemsg" in text:
            import re as _re
            # 匹配带引号的名字
            m = _re.search(r'"([^"]*)"\s*撤回了一条消息', text)
            if m:
                return f'"{m.group(1)}" 撤回了一条消息', True
            # 匹配 "你撤回了一条消息"
            m = _re.search(r'(你撤回了一条消息)', text)
            if m:
                return m.group(1), True
            return "[撤回了一条消息]", True
        # 提取 XML 中的纯文本
        import re as _re
        m = _re.search(r"<content>(.*?)</content>", text)
        if m:
            return _sanitize(m.group(1)), True
        # 纯文本系统消息
        if text and len(text) < 500:
            return text, True
        return "[系统消息]", True

    def _format_message(self, row: sqlite3.Row, talker: str,
                        conn: Optional[sqlite3.Connection] = None) -> dict:
        """格式化单条消息"""
        raw_type = row["local_type"] or 1
        raw = row["message_content"]
        real_sender_id = row["real_sender_id"]
        is_group = talker.endswith("@chatroom") if talker else False
        # 压缩类型标记
        try:
            ct_flag = row["WCDB_CT_message_content"]
        except (IndexError, KeyError):
            ct_flag = 0

        # 拆分复合类型
        base_type, sub_type = _split_msg_type(raw_type)
        msg_type = base_type

        # 系统消息 (type=10000, 10002) 特殊处理
        is_system = msg_type in (10000, 10002)
        if msg_type == 1:
            content = self._parse_text_content(raw)
            raw_bytes = raw if isinstance(raw, bytes) else (raw.encode('utf-8', errors='replace') if raw else b'')
            if content and (_looks_like_binary(raw_bytes) or _looks_random(content)):
                content = "[文本(编码异常)]"
        elif is_system:
            content, _ = self._parse_system_msg(raw)
        # 类型名称和内容
        type_name = MSG_TYPE_NAMES.get(msg_type)
        appmsg_details = None
        voice_duration_seconds = 0.0
        sticker = None
        sticker_source = None
        reply = None
        payment = None

        if msg_type == 1:
            # 某些消息虽然 db type=1 但实际是 WCDB 压缩二进制 (header: 28b52ffd)
            if isinstance(raw, bytes) and raw[:4] == b'\x28\xb5\x2f\xfd' and ct_flag == 4:
                decompressed = _decompress_zstd(raw)
                if decompressed:
                    # 可能是 appmsg XML，也可能是压缩的纯文本
                    if "<appmsg" in decompressed or "<?xml" in decompressed:
                        appmsg_details = _parse_appmsg_details(
                            decompressed,
                            self._resolve_name,
                            nick_map=self._nick_to_wxid,
                        )
                        content = (
                            str(appmsg_details.get("content") or "[消息]")
                            if appmsg_details else "[消息]"
                        )
                    else:
                        # 压缩纯文本：按普通文本处理
                        content = self._parse_text_content(decompressed)
                        if _looks_random(content):
                            content = "[文本(编码异常)]"
                else:
                    content = "[文本(编码异常)]"
            else:
                content = self._parse_text_content(raw)
                raw_bytes = raw if isinstance(raw, bytes) else (raw.encode('utf-8', errors='replace') if raw else b'')
                if content and (_looks_like_binary(raw_bytes) or _looks_random(content)):
                    content = "[文本(编码异常)]"
        elif is_system:
            # 系统消息也可能是 WCDB 压缩的
            sys_raw = raw
            if isinstance(raw, bytes) and raw[:4] == b'\x28\xb5\x2f\xfd' and ct_flag == 4:
                decompressed = _decompress_zstd(raw)
                if decompressed:
                    sys_raw = decompressed
            content, _ = self._parse_system_msg(sys_raw)
            payment = _parse_protocol_payment_notice(sys_raw)
        elif base_type == 34:
            content = TYPE_PLACEHOLDERS[34]
            voice_duration_seconds = _parse_voice_duration(
                _decode_rich_payload(raw, ct_flag)
            )
        elif base_type == 47:
            sticker, sticker_source = _parse_sticker_xml(
                _decode_rich_payload(raw, ct_flag)
            )
            description = str(sticker.get("description") or "")
            content = f"[表情] {description}" if description else "[表情]"
        elif base_type == 49:
            # app 消息 (引用/文件/链接/转账等)：尝试 zstd 解压 + XML 解析
            content = None
            xml_text = _decode_rich_payload(raw, ct_flag)
            if xml_text:
                appmsg_details = _parse_appmsg_details(
                    xml_text,
                    self._resolve_name,
                    nick_map=self._nick_to_wxid,
                )
                if appmsg_details:
                    content = str(appmsg_details.get("content") or "")
            if not content:
                content = type_name or "[消息]"
            # 用 appmsg 子类型命名
            if not type_name:
                type_name = _APPMSG_TYPE_LABEL.get(sub_type, "消息")
        else:
            placeholder = TYPE_PLACEHOLDERS.get(msg_type)
            if placeholder:
                content = placeholder
            else:
                content = type_name or f"[消息类型:{msg_type}]"

        appmsg_type = 0
        if appmsg_details:
            appmsg_type = _safe_int(
                appmsg_details.get("inner_type"), 0,
                minimum=0, maximum=100_000,
            )
            parsed_payment = appmsg_details.get("payment")
            if isinstance(parsed_payment, dict):
                payment = dict(parsed_payment)
            if appmsg_type == 57:
                type_name = "回复"
                reply = appmsg_details.get("reply")

        if (
            base_type == 49
            and appmsg_type == 0
            and sub_type in (57, 2000, 2001)
        ):
            # The composite subtype is reliable even when a damaged XML body
            # cannot be decoded. Keep the public semantic label correct.
            if sub_type == 57 and reply is None:
                type_name = "回复"
            elif sub_type in (2000, 2001) and payment is None:
                type_name = _APPMSG_TYPE_LABEL[sub_type]
                payment = {
                    "kind": "transfer" if sub_type == 2000 else "red_packet",
                    "label": type_name,
                    "amount": "",
                    "status": "",
                    "memo": "",
                    "title": f"微信{type_name}",
                }
                content = f"[{type_name}]"

        if isinstance(payment, dict):
            payment_label = _bounded_text(payment.get("label"), 32)
            if payment_label in ("红包", "转账"):
                type_name = payment_label

        if not type_name:
            type_name = f"类型({msg_type})"

        # Name2Id.rowid 是 real_sender_id 的稳定映射；正文前缀仅作兼容兜底。
        sender_raw = raw
        if base_type == 49 and ct_flag == 4 and isinstance(raw, bytes):
            decompressed = _decompress_zstd(raw)
            if decompressed:
                sender_raw = decompressed

        # 系统消息 + 拍一拍居中显示，不算作任何一方的消息
        is_nudge = (base_type == 49 and sub_type == 62)
        sender_username = "" if (is_system or is_nudge) else self._resolve_sender_username(
            sender_raw, conn=conn, real_sender_id=real_sender_id
        )
        is_self = None if (is_system or is_nudge) else self._is_self(
            raw,
            real_sender_id=real_sender_id,
            conn=conn,
            is_group=is_group,
            sender_username=sender_username,
        )
        if is_self and not sender_username:
            sender_username = self._primary_self_username()
        elif is_self is False and not is_group and not sender_username:
            sender_username = str(talker or "")

        # 简化拍一拍显示
        if is_nudge and content and content.startswith("[拍一拍]"):
            content = content[5:].strip()
        create_time = row["create_time"] or 0

        # 群聊提取发送者名称——优先用解压后文本解析
        sender_name = ""
        if is_group and not is_self and not is_system and not is_nudge:
            sender_name = (
                self._get_display_name(sender_username)
                if sender_username
                else self._resolve_group_sender(
                    sender_raw, conn=conn, real_sender_id=real_sender_id
                )
            )

        result = {
            "id": row["local_id"],
            "server_id": row["server_id"],
            "type": msg_type,
            "raw_type": raw_type,
            "sub_type": sub_type,
            "type_name": type_name,
            "is_sender": is_self,
            "is_self": is_self,
            "sender_username": sender_username,
            "sender_name": sender_name,
            "content": content,
            "content_preview": (content or "")[:100],
            "create_time": create_time,
            "time_str": _fmt_time(create_time),
            "date_str": _fmt_date(create_time),
            "talker": talker,
        }
        if base_type == 34:
            result["voice_duration_seconds"] = voice_duration_seconds
        if base_type == 47:
            result["sticker"] = sticker or {
                "md5": "", "description": "", "available": False,
            }
            if sticker_source:
                result["_sticker_source"] = sticker_source
        if reply is not None:
            result["reply"] = reply
        if isinstance(payment, dict):
            result["payment"] = payment
        return result

    def get_message_by_server_id(
        self,
        talker: str,
        server_id,
        *,
        create_time: Optional[int] = None,
    ) -> Optional[dict]:
        """Resolve one original message by server ID across every 4.x shard.

        A referenced message's ``refermsg/svrid`` is stable while ``local_id``
        can be reused by different shards.  ``create_time`` is an optional
        deterministic tie-breaker for duplicated/migrated rows.
        """
        server_id_candidates = _server_id_text_candidates(server_id)
        if not server_id_candidates:
            return None
        target_time = None
        if create_time is not None:
            target_time = _safe_int(
                create_time, 0, minimum=0, maximum=2**63 - 1
            )

        candidates = []
        for shard_index, (msg_conn, table) in enumerate(
            self._get_msg_tables(talker)
        ):
            try:
                placeholders = ", ".join("?" for _ in server_id_candidates)
                rows = msg_conn.execute(
                    f"""
                    SELECT local_id, server_id, local_type, real_sender_id,
                           create_time, message_content, source, packed_info_data,
                           WCDB_CT_message_content
                    FROM [{table}]
                    WHERE CAST(server_id AS TEXT) IN ({placeholders})
                    ORDER BY create_time ASC, local_id ASC
                    LIMIT 20
                    """,
                    server_id_candidates,
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                timestamp = _safe_int(
                    row["create_time"], 0, minimum=0, maximum=2**63 - 1
                )
                distance = (
                    abs(timestamp - target_time)
                    if target_time is not None else 0
                )
                candidates.append((
                    distance,
                    timestamp,
                    _safe_int(row["local_id"], 0),
                    shard_index,
                    msg_conn,
                    row,
                ))
        if not candidates:
            return None

        _, _, _, _, msg_conn, row = min(
            candidates, key=lambda item: item[:4]
        )
        message = self._format_message(row, talker, conn=msg_conn)
        local_id = _safe_int(message.get("id"), 0)
        timestamp = _safe_int(message.get("create_time"), 0)
        resolved_server_id = _bounded_text(message.get("server_id"), 128)
        identity = resolved_server_id if resolved_server_id != "0" else str(local_id)
        message["message_key"] = (
            f"{talker}:{identity}:{timestamp}:{local_id}"
        )
        return message

    def get_message_position(
        self,
        talker: str,
        msg_id: int,
        page_size: int = 50,
        create_time: Optional[int] = None,
        message_key: Optional[str] = None,
    ) -> Optional[dict]:
        """按全分片的 ``create_time, local_id`` 顺序查询消息页码。"""
        msg_tables = self._get_msg_tables(talker)
        if not msg_tables:
            return None

        # local_id 可能跨分片复用；旧接口没有 create_time 参数，因此选择
        # 全局排序后第一个匹配项，并保持结果确定性。
        targets = []
        for shard_index, (msg_conn, table) in enumerate(msg_tables):
            try:
                conditions = "local_id=?"
                values = [msg_id]
                if create_time is not None:
                    conditions += " AND create_time=?"
                    values.append(int(create_time))
                rows = msg_conn.execute(
                    f"SELECT create_time, local_id, server_id FROM [{table}] "
                    f"WHERE {conditions} "
                    f"ORDER BY create_time ASC, local_id ASC",
                    values,
                ).fetchall()
                for row in rows:
                    if message_key:
                        server_id = str(row[2] or "")
                        local_id = int(row[1] or 0)
                        timestamp = int(row[0] or 0)
                        identity = server_id if server_id and server_id != "0" else str(local_id)
                        if f"{talker}:{identity}:{timestamp}:{local_id}" != message_key:
                            continue
                    targets.append((row[0] or 0, row[1] or 0, shard_index))
            except Exception:
                continue
        if not targets:
            return None

        target_time, target_id, target_shard = min(targets)
        count_before = 0
        total = 0
        for shard_index, (msg_conn, table) in enumerate(msg_tables):
            try:
                cur = msg_conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM [{table}]")
                total += cur.fetchone()[0]
                cur.execute(
                    f"SELECT COUNT(*) FROM [{table}] "
                    f"WHERE COALESCE(create_time, 0) < ? "
                    f"OR (COALESCE(create_time, 0) = ? "
                    f"AND COALESCE(local_id, 0) < ?)",
                    (target_time, target_time, target_id),
                )
                count_before += cur.fetchone()[0]
                if shard_index < target_shard:
                    cur.execute(
                        f"SELECT COUNT(*) FROM [{table}] "
                        f"WHERE COALESCE(create_time, 0) = ? "
                        f"AND COALESCE(local_id, 0) = ?",
                        (target_time, target_id),
                    )
                    count_before += cur.fetchone()[0]
            except Exception:
                continue

        page_size = max(1, page_size)
        page = (count_before // page_size) + 1
        total_pages = max(1, (total + page_size - 1) // page_size)
        return {"page": page, "total_pages": total_pages, "total": total}

    def search_messages(self, keyword: str, limit: int = 100) -> list[dict]:
        """全局搜索消息，返回包含 talker 和 display_name 的结果"""
        results = []
        seen = set()
        for conn in self.conns:
            try:
                cur = conn.cursor()
                cur.execute("SELECT user_name FROM Name2Id")
                name2id_users = {r[0] for r in cur.fetchall()}
                for user in name2id_users:
                    if not user:
                        continue
                    table = f"Msg_{hashlib.md5(user.encode()).hexdigest()}"
                    try:
                        cur.execute(
                            f"SELECT local_id, server_id, local_type, real_sender_id,"
                            f" create_time, message_content, source, packed_info_data,"
                            f" WCDB_CT_message_content FROM [{table}] "
                            f"WHERE CAST(message_content AS TEXT) LIKE ? "
                            f"ORDER BY create_time DESC LIMIT ?",
                            (f"%{keyword}%", limit),
                        )
                        for row in cur.fetchall():
                            msg_id = row[0]
                            key = (user, msg_id)
                            if key in seen:
                                continue
                            seen.add(key)
                            msg = self._format_message(row, user, conn=conn)
                            msg["talker"] = user
                            msg["display_name"] = self._get_display_name(user)
                            msg["is_group"] = "@chatroom" in user
                            results.append(msg)
                    except Exception:
                        continue
            except Exception:
                continue
            if len(results) >= limit:
                break
        results.sort(key=lambda x: x.get("create_time", 0), reverse=True)
        return results[:limit]

    def get_statistics(self) -> dict:
        """统计信息"""
        contacts = self.get_contacts()
        total_msgs = sum(c.get("message_count", 0) for c in contacts)

        return {
            "total_messages": total_msgs,
            "total_talkers": len(contacts),
            "msg_by_type": {},
            "date_range": {"start": "", "end": ""},
        }


def _looks_random(text: str) -> bool:
    """检测解码后的文本是否像随机乱码"""
    if not text or len(text) < 4:
        return False
    # URL 链接 → 不是乱码
    if "://" in text or text.startswith("http"):
        return False
    # CJK 字符占比 > 10% → 真实中文文本
    cjk = sum(1 for c in text if '一' <= c <= '鿿' or '぀' <= c <= 'ヿ')
    if cjk >= len(text) * 0.1:
        return False
    # 统计 Unicode 区块数。真实文本通常只在 2-3 个区块内 (ASCII + CJK + 标点)
    # 乱码解码后会散落在十几个不同区块
    blocks: set[int] = set()
    for c in text:
        blocks.add(ord(c) >> 8)  # 高 8 位 = Unicode 块
    if len(blocks) >= 5:
        return True
    # 纯字母+空格 → 英文文本
    alpha = sum(1 for c in text if c.isalpha() or c == ' ')
    if alpha >= len(text) * 0.7:
        return False
    # URL 常用字符: 允许 : / . -
    url_punct = set('+-*/=<>@#$%&!?,.;:()[]{}|^~')
    url_chars = sum(1 for c in text if c.isdigit() or c in url_punct or c in '/:._-')
    if url_chars >= len(text) * 0.9:
        return False
    # 长度 > 8 且不符合上述 → 乱码
    return len(text) > 8


def _sanitize(text: str) -> str:
    """移除控制字符和替换字符，保留换行和制表"""
    if not text:
        return ""
    result = []
    for ch in text:
        cp = ord(ch)
        if cp == 0xFFFD:  # 替换字符
            continue
        if cp <= 0x1F and ch not in '\t\n\r':  # C0 控制字符
            continue
        if 0x7F <= cp <= 0x9F:  # DEL + C1 控制字符
            continue
        result.append(ch)
    return "".join(result)


def _looks_like_binary(raw) -> bool:
    """检测内容是否像二进制乱码"""
    if raw is None:
        return False
    if isinstance(raw, bytes):
        if len(raw) == 0:
            return False
        # 超过 25% 的字节是非文本 → 二进制
        non_text = sum(1 for b in raw if b < 0x20 and b not in (0x09, 0x0A, 0x0D))
        if non_text / len(raw) > 0.25:
            return True
        # 大量高位字节 (>= 0x80) 且不是有效 UTF-8
        high = sum(1 for b in raw if b >= 0x80)
        if high / len(raw) > 0.5:
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                return True
        return False
    if isinstance(raw, str):
        if len(raw) == 0:
            return False
        # U+FFFD 替换字符过多 → 解码失败的二进制
        fffd = raw.count('�')
        if fffd >= max(len(raw) * 0.3, 3):
            return True
        # C0 控制字符 (U+0000-U+001F, 除了 \t\n\r)
        control = sum(1 for c in raw if ord(c) <= 0x1F and c not in '\t\n\r')
        # 私用区 / 代理项
        unusual = sum(1 for c in raw if 0xE000 <= ord(c) <= 0xF8FF
                      or 0xD800 <= ord(c) <= 0xDFFF)
        bad = control + unusual + fffd
        if len(raw) <= 10:
            return bad >= len(raw) * 0.4
        return bad >= len(raw) * 0.25
    return False


def _fmt_time(ts) -> str:
    if not ts or ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _fmt_date(ts) -> str:
    if not ts or ts <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(ts)
        now = datetime.now()
        if dt.date() == now.date():
            return "今天"
        elif (now - dt).days == 1:
            return "昨天"
        elif dt.year == now.year:
            return dt.strftime("%m月%d日")
        return dt.strftime("%Y年%m月%d日")
    except Exception:
        return str(ts)
