"""Resolve and decrypt locally cached WeChat chat images.

The V1/V2/XOR primitives are adapted from ``wechat-decrypt-main/decode_image.py``.
This module intentionally keeps image display local; it never performs network
requests.  Network-backed descriptions live in :mod:`backend.image_recognition`.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
import re
import sqlite3
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Util import Padding


V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
V1_AES_KEY = b"cfcd208495d565ef"


class ImageResolutionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DecodedImage:
    path: Path
    format: str
    mime_type: str
    file_md5: str
    sha256: str
    source_path: Path


def aligned_aes_block_size(aes_size: int) -> int:
    if aes_size < 0:
        raise ImageResolutionError("图片 AES 区段长度无效")
    remainder = aes_size % AES.block_size
    return aes_size + (AES.block_size - remainder if remainder else AES.block_size)


def detect_image_format(data: bytes) -> tuple[str, str]:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"GIF":
        return "gif", "image/gif"
    if data[:2] == b"BM":
        return "bmp", "image/bmp"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif", "image/tiff"
    if data[:4] == b"wxgf":
        return "hevc", "video/h265"
    return "", "application/octet-stream"


def normalize_wechat_image_payload(data: bytes) -> bytes:
    """Strip only a cryptographically verified WeChat JPEG cache footer.

    WeChat 4.x may append 24 bytes to an otherwise complete JPEG.  The final
    16 bytes are the raw MD5 digest of the JPEG through its EOI marker.  This
    strict check avoids accepting arbitrary trailing data while allowing both
    local SNS cache files and encrypted CDN responses to pass normal image
    validation.
    """

    raw = bytes(data)
    if not raw.startswith(b"\xff\xd8\xff") or raw.endswith(b"\xff\xd9"):
        return raw
    eoi_offset = raw.rfind(b"\xff\xd9")
    if eoi_offset < 2:
        return raw
    image_end = eoi_offset + 2
    footer = raw[image_end:]
    if (
        len(footer) == 24
        and footer[8:] == hashlib.md5(raw[:image_end]).digest()
    ):
        return raw[:image_end]
    return raw


def convert_wxgf_to_jpeg(data: bytes, quality: int = 92) -> bytes:
    """Decode the first HEVC frame embedded in a WeChat wxgf container."""
    if not data.startswith(b"wxgf"):
        raise ImageResolutionError("待转换的数据不是 wxgf/HEVC 图片")
    try:
        import av
    except ImportError as exc:
        raise ImageResolutionError(
            "缺少 PyAV，无法显示微信 wxgf/HEVC 高清图片"
        ) from exc

    signatures = (
        b"\x00\x00\x00\x01\x40\x01",  # HEVC VPS
        b"\x00\x00\x00\x01\x42\x01",  # HEVC SPS fallback
    )
    offsets = [data.find(signature) for signature in signatures]
    offsets = [offset for offset in offsets if offset >= 0]
    if not offsets:
        raise ImageResolutionError("wxgf 文件中没有可识别的 HEVC 视频流")

    container = None
    try:
        container = av.open(io.BytesIO(data[min(offsets):]), format="hevc")
        frame = next(container.decode(video=0), None)
        if frame is None:
            raise ImageResolutionError("wxgf/HEVC 文件没有可解码的图片帧")
        output = io.BytesIO()
        frame.to_image().convert("RGB").save(
            output,
            format="JPEG",
            quality=max(70, min(100, int(quality))),
            optimize=True,
        )
        return output.getvalue()
    except ImageResolutionError:
        raise
    except Exception as exc:
        raise ImageResolutionError("wxgf/HEVC 高清图片转换失败") from exc
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


def extract_md5_from_packed_info(blob: object) -> Optional[str]:
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    if not isinstance(blob, (bytes, bytearray)):
        return None
    raw = bytes(blob)
    marker = b"\x12\x22\x0a\x20"
    index = raw.find(marker)
    if index >= 0:
        candidate = raw[index + len(marker):index + len(marker) + 32]
        if re.fullmatch(rb"[0-9a-fA-F]{32}", candidate):
            return candidate.decode("ascii").lower()
    match = re.search(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", raw)
    return match.group(1).decode("ascii").lower() if match else None


def _derive_xor_key_from_tail(encrypted_tail: bytes) -> Optional[int]:
    if len(encrypted_tail) >= 2:
        jpg_key = encrypted_tail[-2] ^ 0xFF
        if (encrypted_tail[-1] ^ jpg_key) == 0xD9:
            return jpg_key
    # WeChat 4.x SNS JPEG caches may append a verified 24-byte footer after
    # the normal EOI marker.  In that layout EOI is exactly 26 bytes from the
    # end, so the one-byte XOR key remains derivable without guessing.
    if len(encrypted_tail) >= 26:
        jpg_footer_key = encrypted_tail[-26] ^ 0xFF
        if (encrypted_tail[-25] ^ jpg_footer_key) == 0xD9:
            return jpg_footer_key
    png_trailer = b"IEND\xaeB`\x82"
    if len(encrypted_tail) >= len(png_trailer):
        sample = encrypted_tail[-len(png_trailer):]
        png_key = sample[0] ^ png_trailer[0]
        if all((value ^ png_key) == expected for value, expected in zip(sample, png_trailer)):
            return png_key
    return None


def _parse_xor_key(value: object, encrypted_tail: bytes) -> int:
    if value is None or str(value).strip().lower() in ("", "auto"):
        detected = _derive_xor_key_from_tail(encrypted_tail)
        if detected is None:
            raise ImageResolutionError(
                "无法自动推导该图片的 XOR key，请在设置中填写图片 XOR key"
            )
        return detected
    try:
        parsed = int(str(value), 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ImageResolutionError("图片 XOR key 格式无效，应为 auto、十进制或 0x 十六进制") from exc
    if not 0 <= parsed <= 0xFF:
        raise ImageResolutionError("图片 XOR key 必须在 0x00 到 0xFF 之间")
    return parsed


def _normalize_aes_key(value: object) -> Optional[bytes]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value if len(value) == 16 else None
    text = str(value).strip()
    if len(text) != 16:
        return None
    try:
        return text.encode("ascii")
    except UnicodeEncodeError:
        return None


def decrypt_dat_bytes(data: bytes, aes_key: object = None, xor_key: object = "auto") -> tuple[bytes, str, str]:
    """Decrypt a V2, V1, or legacy-XOR WeChat ``.dat`` payload."""
    if len(data) < 4:
        raise ImageResolutionError("图片缓存文件过短或已损坏")

    signature = data[:6]
    if signature in (V1_MAGIC, V2_MAGIC):
        if len(data) < 15:
            raise ImageResolutionError("图片缓存头不完整")
        aes_size, xor_size = struct.unpack_from("<II", data, 6)
        encrypted_aes_size = aligned_aes_block_size(aes_size)
        offset = 15
        if offset + encrypted_aes_size > len(data):
            raise ImageResolutionError("图片 AES 区段越界，缓存可能已损坏")
        if xor_size > len(data) - (offset + encrypted_aes_size):
            raise ImageResolutionError("图片 XOR 区段越界，缓存可能已损坏")

        key = V1_AES_KEY if signature == V1_MAGIC else _normalize_aes_key(aes_key)
        if key is None:
            raise ImageResolutionError(
                "该图片使用 V2 加密，请先在设置中填写当前微信账号的图片 AES key",
                status_code=409,
            )
        try:
            aes_plain = Padding.unpad(
                AES.new(key, AES.MODE_ECB).decrypt(
                    data[offset:offset + encrypted_aes_size]
                ),
                AES.block_size,
            )
        except (ValueError, KeyError) as exc:
            raise ImageResolutionError("图片 AES key 不正确或缓存已损坏", status_code=409) from exc

        offset += encrypted_aes_size
        raw_end = len(data) - xor_size
        raw_plain = data[offset:raw_end]
        encrypted_tail = data[raw_end:]
        if encrypted_tail:
            tail_key = _parse_xor_key(xor_key, encrypted_tail)
            tail_plain = bytes(value ^ tail_key for value in encrypted_tail)
        else:
            tail_plain = b""
        decrypted = aes_plain + raw_plain + tail_plain
    else:
        magics = (
            b"\x89PNG",
            b"GIF8",
            b"RIFF",
            b"\xff\xd8\xff",
            b"II*\x00",
            b"MM\x00*",
        )
        detected_key: Optional[int] = None
        for magic in magics:
            candidate_key = data[0] ^ magic[0]
            if len(data) >= len(magic) and all(
                (data[index] ^ candidate_key) == expected
                for index, expected in enumerate(magic)
            ):
                detected_key = candidate_key
                break
        if detected_key is None and len(data) >= 14:
            candidate_key = data[0] ^ ord("B")
            header = bytes(value ^ candidate_key for value in data[:14])
            if header[:2] == b"BM":
                file_size = struct.unpack_from("<I", header, 2)[0]
                pixel_offset = struct.unpack_from("<I", header, 10)[0]
                if abs(file_size - len(data)) < 1024 and 14 <= pixel_offset <= 1078:
                    detected_key = candidate_key
        if detected_key is None:
            raise ImageResolutionError("无法识别旧版图片 XOR key 或图片格式")
        decrypted = bytes(value ^ detected_key for value in data)

    decrypted = normalize_wechat_image_payload(decrypted)
    image_format, mime_type = detect_image_format(decrypted)
    if not image_format:
        raise ImageResolutionError("解密后的图片格式无法识别，可能是图片密钥不正确")
    if image_format == "jpg" and len(decrypted) >= 2 and decrypted[-2:] != b"\xff\xd9":
        raise ImageResolutionError("JPEG 尾部校验失败，图片 XOR key 可能不正确")
    if image_format == "png" and b"IEND" not in decrypted[-16:]:
        raise ImageResolutionError("PNG 尾部校验失败，图片 XOR key 可能不正确")
    return decrypted, image_format, mime_type


class WeChatImageService:
    """Maps a message reference to a local cache file and decrypts it lazily."""

    def __init__(
        self,
        *,
        account_id: str,
        account_root: Path,
        resource_db_path: Path,
        aes_key: object = None,
        xor_key: object = "auto",
    ):
        self.account_id = account_id
        self.account_root = Path(account_root)
        self.attach_dir = self.account_root / "msg" / "attach"
        self.resource_db_path = Path(resource_db_path)
        self.aes_key = aes_key
        self.xor_key = xor_key
        self._tmp = tempfile.TemporaryDirectory(
            prefix=f"wechat_analysis_images_{os.getpid()}_"
        )
        self.cache_dir = Path(self._tmp.name)
        self._lock = threading.RLock()
        self._resolution_cache: dict[tuple, tuple[str, list[Path]]] = {}

    def close(self) -> None:
        with self._lock:
            self._resolution_cache.clear()
            self._tmp.cleanup()

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        escaped = table.replace('"', '""')
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")')}

    @staticmethod
    def _find_chat_id(conn: sqlite3.Connection, talker: str) -> Optional[int]:
        for table in ("ChatName2Id", "Name2Id"):
            try:
                columns = WeChatImageService._table_columns(conn, table)
                username_column = next(
                    (name for name in ("user_name", "username", "userName") if name in columns),
                    None,
                )
                if not username_column:
                    continue
                row = conn.execute(
                    f'SELECT rowid FROM "{table}" WHERE "{username_column}"=? LIMIT 1',
                    (talker,),
                ).fetchone()
                if row:
                    return int(row[0])
            except sqlite3.Error:
                continue
        return None

    def _resolve_file_md5(
        self,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = None,
    ) -> str:
        if not self.resource_db_path.exists():
            raise ImageResolutionError("未找到已解密的 message_resource.db", status_code=404)
        conn = sqlite3.connect(
            f"file:{self.resource_db_path}?mode=ro", uri=True, timeout=15
        )
        try:
            chat_id = self._find_chat_id(conn, talker)
            if chat_id is None:
                raise ImageResolutionError("图片资源库中没有该聊天", status_code=404)
            columns = self._table_columns(conn, "MessageResourceInfo")
            if "packed_info" not in columns:
                raise ImageResolutionError("图片资源库结构不受支持", status_code=422)

            type_clause = ""
            if "message_local_type" in columns:
                type_clause = (
                    " AND ((message_local_type & 4294967295) IN (2, 3))"
                )
            row = None
            if server_id and "message_svr_id" in columns:
                row = conn.execute(
                    "SELECT packed_info FROM MessageResourceInfo "
                    "WHERE chat_id=? AND message_svr_id=?" + type_clause + " "
                    "ORDER BY rowid DESC LIMIT 1",
                    (chat_id, server_id),
                ).fetchone()

            if row is None:
                if "message_create_time" in columns and create_time:
                    row = conn.execute(
                        "SELECT packed_info FROM MessageResourceInfo "
                        "WHERE chat_id=? AND message_local_id=?" + type_clause + " "
                        "ORDER BY ABS(message_create_time-?) ASC, "
                        "message_create_time DESC LIMIT 1",
                        (chat_id, int(message_id), int(create_time)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT packed_info FROM MessageResourceInfo "
                        "WHERE chat_id=? AND message_local_id=?" + type_clause + " "
                        "ORDER BY rowid DESC LIMIT 1",
                        (chat_id, int(message_id)),
                    ).fetchone()
            file_md5 = extract_md5_from_packed_info(row[0] if row else None)
            if not file_md5:
                raise ImageResolutionError("无法从图片资源库定位本地图片", status_code=404)
            return file_md5
        finally:
            conn.close()

    def _candidate_files(
        self,
        talker: str,
        file_md5: str,
        purpose: str,
        quality: str = "thumbnail",
    ) -> list[Path]:
        talker_hash = hashlib.md5(talker.encode("utf-8")).hexdigest()
        base = self.attach_dir / talker_hash
        if not base.is_dir():
            return []
        pattern = str(base / "*" / "Img" / f"{file_md5}*.dat")
        files = [Path(path) for path in glob.glob(pattern)]

        prefer_best = purpose == "analysis" or quality == "best"

        def rank(path: Path) -> tuple[int, int, str]:
            stem = path.stem.lower()
            suffix = stem[len(file_md5):]
            is_thumbnail = suffix in ("_t", "_t_w")
            try:
                size_rank = -path.stat().st_size
            except OSError:
                size_rank = 0
            if prefer_best:
                # File suffix conventions changed across WeChat releases.
                # Prefer any non-thumbnail, then the largest available file.
                quality_rank = 1 if is_thumbnail else 0
            else:
                quality_rank = 0 if is_thumbnail else 1
            return quality_rank, size_rank, str(path)

        return sorted(files, key=rank)

    def get_image(
        self,
        *,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = None,
        purpose: str = "display",
        quality: str = "thumbnail",
    ) -> DecodedImage:
        if purpose not in ("display", "analysis"):
            raise ImageResolutionError("图片用途参数无效")
        if quality not in ("thumbnail", "best"):
            raise ImageResolutionError("图片质量参数无效")
        cache_key = (
            talker,
            int(message_id),
            int(create_time or 0),
            str(server_id or ""),
            purpose,
            quality,
        )
        with self._lock:
            cached_resolution = self._resolution_cache.get(cache_key)
        if cached_resolution:
            file_md5, candidates = cached_resolution
            if quality == "best" or purpose == "analysis":
                # WeChat may download the high-resolution companion after the
                # thumbnail was first viewed. Refresh candidates on every
                # high-quality request while retaining the MD5 lookup cache.
                refreshed = self._candidate_files(
                    talker, file_md5, purpose, quality=quality
                )
                if refreshed:
                    candidates = refreshed
                    with self._lock:
                        self._resolution_cache[cache_key] = (
                            file_md5,
                            candidates,
                        )
        else:
            file_md5 = self._resolve_file_md5(
                talker, message_id, create_time, server_id=server_id
            )
            candidates = self._candidate_files(
                talker, file_md5, purpose, quality=quality
            )
            if candidates:
                with self._lock:
                    self._resolution_cache[cache_key] = (file_md5, candidates)

        if not candidates:
            raise ImageResolutionError("微信本地缓存中没有这张图片", status_code=404)

        errors: list[str] = []
        for source_path in candidates:
            try:
                stat = source_path.stat()
                fingerprint = hashlib.sha256(
                    (
                        f"{source_path}|{stat.st_mtime_ns}|{stat.st_size}|"
                        f"{self.aes_key}|{self.xor_key}"
                    ).encode("utf-8", errors="replace")
                ).hexdigest()

                with self._lock:
                    existing = [
                        path for path in self.cache_dir.glob(f"{fingerprint}.*")
                        if not path.name.endswith(".tmp")
                    ]
                    if existing:
                        output_path = existing[0]
                        decoded = output_path.read_bytes()
                        image_format, mime_type = detect_image_format(decoded)
                    else:
                        encrypted = source_path.read_bytes()
                        decoded, image_format, mime_type = decrypt_dat_bytes(
                            encrypted, aes_key=self.aes_key, xor_key=self.xor_key
                        )
                        if image_format == "hevc":
                            decoded = convert_wxgf_to_jpeg(decoded)
                            image_format, mime_type = "jpg", "image/jpeg"
                        output_path = self.cache_dir / f"{fingerprint}.{image_format}"
                        fd, temporary_name = tempfile.mkstemp(
                            prefix=f"{fingerprint}_",
                            suffix=".tmp",
                            dir=str(self.cache_dir),
                        )
                        try:
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(decoded)
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(temporary_name, output_path)
                        finally:
                            try:
                                Path(temporary_name).unlink()
                            except FileNotFoundError:
                                pass

                if image_format == "hevc":
                    hevc_cache_path = output_path
                    decoded = convert_wxgf_to_jpeg(decoded)
                    image_format, mime_type = "jpg", "image/jpeg"
                    converted_path = self.cache_dir / f"{fingerprint}.jpg"
                    fd, temporary_name = tempfile.mkstemp(
                        prefix=f"{fingerprint}_",
                        suffix=".tmp",
                        dir=str(self.cache_dir),
                    )
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(decoded)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary_name, converted_path)
                    finally:
                        try:
                            Path(temporary_name).unlink()
                        except FileNotFoundError:
                            pass
                    output_path = converted_path
                    if hevc_cache_path != converted_path:
                        try:
                            hevc_cache_path.unlink()
                        except FileNotFoundError:
                            pass
                return DecodedImage(
                    path=output_path,
                    format=image_format,
                    mime_type=mime_type,
                    file_md5=file_md5,
                    sha256=hashlib.sha256(decoded).hexdigest(),
                    source_path=source_path,
                )
            except ImageResolutionError as exc:
                errors.append(str(exc))
            except OSError as exc:
                errors.append(f"读取图片缓存失败: {exc}")

        message = errors[-1] if errors else "图片解密失败"
        raise ImageResolutionError(message)

    def get_image_bytes(self, **kwargs) -> tuple[DecodedImage, bytes]:
        """Resolve and copy bytes while holding the lifecycle lock."""
        with self._lock:
            image = self.get_image(**kwargs)
            return image, image.path.read_bytes()
