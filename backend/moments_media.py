"""Strict, account-isolated storage for locally acquired Moments images.

WeChat 4.x stores Moments image payloads below ``cache/<month>/Sns/Img`` but
does not expose a dependable ``tid + media index -> cache file`` mapping in
``sns.db``.  This module therefore never associates files by timestamp,
dimensions, filename, or proximity.  An encrypted cache file is bound only
when its fully validated plaintext MD5 exactly matches the ``url@md5`` value
from the corresponding TimelineObject.

The module is deliberately independent from the HTTP API and UI automation.
Callers may also save bytes obtained from an already verified viewer action;
those bytes still pass the same format, completeness, size, and pixel checks.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import tempfile
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from PIL import Image, UnidentifiedImageError

from .app_paths import app_data_path, prune_cache_directory
from .image_service import ImageResolutionError, decrypt_dat_bytes, detect_image_format


STATUS_READY = "ready"
STATUS_PARTIAL = "partial"
STATUS_UNBOUND = "unbound"
STATUS_UNSUPPORTED = "unsupported"

QUALITY_THUMBNAIL = "thumbnail"
QUALITY_HIGH = "high"
QUALITY_UNKNOWN = "unknown"

DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024
# 单账号朋友圈 blob 缓存容量上限：存储初始化（低频）时按 mtime 淘汰最旧文件
MOMENTS_BLOB_CACHE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_FRAMES = 200
DEFAULT_MAX_SCAN_FILES = 50_000

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_EXTENSIONS = {
    "jpg": "jpg",
    "png": "png",
    "gif": "gif",
    "bmp": "bmp",
    "webp": "webp",
    "tif": "tif",
}
_IMAGE_MEDIA_TYPES = {"", "2"}


class MomentsMediaError(RuntimeError):
    """Base error for the local Moments media store."""


class MomentsMediaValidationError(MomentsMediaError):
    """The supplied bytes or metadata failed strict validation."""


class MomentsMediaPathError(MomentsMediaError):
    """A source or stored path escaped its configured account root."""


class MomentsMediaNotFound(MomentsMediaError):
    """No complete, validated media is bound to the requested reference."""


@dataclass(frozen=True)
class ValidatedMomentsImage:
    data: bytes
    image_format: str
    mime_type: str
    extension: str
    file_md5: str
    sha256: str
    width: int
    height: int
    frames: int


@dataclass(frozen=True)
class MomentsMediaStatus:
    status: str
    quality: str = QUALITY_UNKNOWN
    mime_type: str = ""
    width: int = 0
    height: int = 0
    size: int = 0
    version: str = ""
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, Any]:
        """Return the frontend-safe portion of the status (never a path/hash)."""

        return {
            "status": self.status,
            "quality": self.quality,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "size": self.size,
            "version": self.version,
            "reason": self.reason,
            "ready": self.ready,
        }


@dataclass(frozen=True)
class MomentsMediaScanResult:
    files_seen: int = 0
    files_decoded: int = 0
    partial_files: int = 0
    unmatched_files: int = 0
    exact_content_matches: int = 0
    media_bound: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "files_seen": self.files_seen,
            "files_decoded": self.files_decoded,
            "partial_files": self.partial_files,
            "unmatched_files": self.unmatched_files,
            "exact_content_matches": self.exact_content_matches,
            "media_bound": self.media_bound,
        }


def _default_store_root() -> Path:
    return app_data_path("moments_media")


def _account_digest(account_id: str) -> str:
    value = str(account_id or "").strip()
    if not value or len(value) > 512:
        raise MomentsMediaValidationError("朋友圈媒体缓存缺少有效账号标识")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_tid(value: Any) -> str:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise MomentsMediaValidationError("朋友圈动态 ID 无效") from exc
    if number < 0:
        number += 1 << 64
    if not 0 <= number <= (1 << 64) - 1:
        raise MomentsMediaValidationError("朋友圈动态 ID 超出 uint64 范围")
    return str(number)


def _media_index(value: Any) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MomentsMediaValidationError("朋友圈媒体序号无效") from exc
    if not 0 <= index <= 10_000:
        raise MomentsMediaValidationError("朋友圈媒体序号超出允许范围")
    return index


def _optional_positive_int(value: Any) -> int:
    try:
        number = int(float(str(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _normalize_md5(value: Any, *, strict: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if _MD5_RE.fullmatch(text):
        return text
    if strict:
        raise MomentsMediaValidationError("朋友圈媒体 MD5 必须是 32 位十六进制")
    return ""


def _quality_for_dimensions(
    width: int,
    height: int,
    expected_width: Any = 0,
    expected_height: Any = 0,
) -> str:
    wanted_width = _optional_positive_int(expected_width)
    wanted_height = _optional_positive_int(expected_height)
    if width <= 0 or height <= 0 or wanted_width <= 0 or wanted_height <= 0:
        return QUALITY_UNKNOWN

    actual_short, actual_long = sorted((width, height))
    wanted_short, wanted_long = sorted((wanted_width, wanted_height))
    if (
        actual_short >= wanted_short * 0.9
        and actual_long >= wanted_long * 0.9
    ):
        return QUALITY_HIGH
    return QUALITY_THUMBNAIL


def _validate_trailer(data: bytes, image_format: str) -> None:
    if image_format == "jpg" and not data.endswith(b"\xff\xd9"):
        raise MomentsMediaValidationError("JPEG 图片未完整加载")
    if image_format == "png" and b"IEND\xaeB`\x82" not in data[-20:]:
        raise MomentsMediaValidationError("PNG 图片未完整加载")
    if image_format == "gif" and not data.endswith(b";"):
        raise MomentsMediaValidationError("GIF 图片未完整加载")
    if image_format == "webp" and len(data) >= 12:
        declared = int.from_bytes(data[4:8], "little") + 8
        if declared != len(data):
            raise MomentsMediaValidationError("WebP 图片长度校验失败")


def validate_moments_image(
    data: bytes,
    *,
    expected_md5: Any = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> ValidatedMomentsImage:
    """Fully validate decoded image bytes before they enter persistent storage."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MomentsMediaValidationError("朋友圈媒体数据类型无效")
    raw = bytes(data)
    if not raw:
        raise MomentsMediaValidationError("朋友圈媒体为空")
    if max_file_bytes <= 0 or len(raw) > max_file_bytes:
        raise MomentsMediaValidationError("朋友圈媒体超过允许的文件大小")

    image_format, mime_type = detect_image_format(raw)
    extension = _EXTENSIONS.get(image_format)
    if not extension:
        raise MomentsMediaValidationError("朋友圈媒体不是支持的图片格式")
    _validate_trailer(raw, image_format)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                frames = int(getattr(image, "n_frames", 1) or 1)
                if width <= 0 or height <= 0:
                    raise MomentsMediaValidationError("朋友圈图片尺寸无效")
                if width * height > max_pixels:
                    raise MomentsMediaValidationError("朋友圈图片像素数量超过上限")
                if frames <= 0 or frames > max_frames:
                    raise MomentsMediaValidationError("朋友圈图片帧数超过上限")
                image.verify()
    except MomentsMediaValidationError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MomentsMediaValidationError("朋友圈图片像素数量超过上限") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise MomentsMediaValidationError("朋友圈图片不完整或已损坏") from exc

    file_md5 = hashlib.md5(raw).hexdigest()
    wanted_md5 = _normalize_md5(expected_md5, strict=bool(expected_md5))
    if wanted_md5 and file_md5 != wanted_md5:
        raise MomentsMediaValidationError("朋友圈图片内容 MD5 与动态元数据不一致")
    return ValidatedMomentsImage(
        data=raw,
        image_format=image_format,
        mime_type=mime_type,
        extension=extension,
        file_md5=file_md5,
        sha256=hashlib.sha256(raw).hexdigest(),
        width=int(width),
        height=int(height),
        frames=frames,
    )


class MomentsMediaStore:
    """Per-account SQLite manifest plus content-addressed validated blobs."""

    def __init__(
        self,
        account_id: str,
        root: Optional[Path] = None,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_frames: int = DEFAULT_MAX_FRAMES,
    ):
        self.account_hash = _account_digest(account_id)
        self.root = (Path(root) if root else _default_store_root()).resolve()
        self.account_root = self.root / "accounts" / self.account_hash
        self.blob_root = self.account_root / "blobs"
        self.manifest_path = self.account_root / "manifest.sqlite3"
        self.max_file_bytes = int(max_file_bytes)
        self.max_pixels = int(max_pixels)
        self.max_frames = int(max_frames)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        # 内容寻址 blob 只增不减：存储初始化（低频）时按容量淘汰最旧文件，
        # 被淘汰的 blob 在下次访问对应动态时会重新解析/下载并写回。
        prune_cache_directory(self.blob_root, MOMENTS_BLOB_CACHE_MAX_BYTES)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.manifest_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moments_media (
                    tid TEXT NOT NULL,
                    media_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    relative_path TEXT NOT NULL DEFAULT '',
                    file_md5 TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    extension TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL DEFAULT 0,
                    height INTEGER NOT NULL DEFAULT 0,
                    frames INTEGER NOT NULL DEFAULT 0,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    quality TEXT NOT NULL DEFAULT 'unknown',
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (tid, media_index)
                )
                """
            )

    @staticmethod
    def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_record(self, tid: Any, media_index: Any) -> Optional[dict[str, Any]]:
        ref = (_canonical_tid(tid), _media_index(media_index))
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM moments_media WHERE tid=? AND media_index=?",
                ref,
            ).fetchone()
        return self._row_dict(row)

    def _blob_path(self, relative_path: Any, *, must_exist: bool = True) -> Path:
        text = str(relative_path or "")
        candidate = Path(text)
        if not text or candidate.is_absolute():
            raise MomentsMediaPathError("朋友圈媒体缓存路径无效")
        account_root = self.account_root.resolve()
        try:
            resolved = (self.account_root / candidate).resolve(strict=must_exist)
            resolved.relative_to(account_root)
        except (OSError, ValueError) as exc:
            raise MomentsMediaPathError("朋友圈媒体缓存路径越界") from exc
        if must_exist and not resolved.is_file():
            raise MomentsMediaNotFound("朋友圈媒体缓存文件不存在")
        return resolved

    def _upsert(
        self,
        *,
        tid: Any,
        media_index: Any,
        status: str,
        relative_path: str = "",
        image: Optional[ValidatedMomentsImage] = None,
        quality: str = QUALITY_UNKNOWN,
        reason: str = "",
    ) -> None:
        ref = (_canonical_tid(tid), _media_index(media_index))
        image = image or ValidatedMomentsImage(
            data=b"",
            image_format="",
            mime_type="",
            extension="",
            file_md5="",
            sha256="",
            width=0,
            height=0,
            frames=0,
        )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO moments_media (
                    tid, media_index, status, relative_path, file_md5, sha256,
                    mime_type, extension, width, height, frames, file_size,
                    quality, reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tid, media_index) DO UPDATE SET
                    status=excluded.status,
                    relative_path=excluded.relative_path,
                    file_md5=excluded.file_md5,
                    sha256=excluded.sha256,
                    mime_type=excluded.mime_type,
                    extension=excluded.extension,
                    width=excluded.width,
                    height=excluded.height,
                    frames=excluded.frames,
                    file_size=excluded.file_size,
                    quality=excluded.quality,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (
                    ref[0], ref[1], status, relative_path,
                    image.file_md5, image.sha256, image.mime_type,
                    image.extension, image.width, image.height, image.frames,
                    len(image.data), quality, str(reason or "")[:300],
                    int(time.time()),
                ),
            )

    def save_partial(self, tid: Any, media_index: Any, reason: str) -> None:
        existing = self.get_record(tid, media_index)
        if existing and existing.get("status") == STATUS_READY:
            return
        self._upsert(
            tid=tid,
            media_index=media_index,
            status=STATUS_PARTIAL,
            reason=reason,
        )

    def save_bytes(
        self,
        tid: Any,
        media_index: Any,
        data: bytes,
        *,
        expected_md5: Any = None,
        expected_width: Any = 0,
        expected_height: Any = 0,
    ) -> MomentsMediaStatus:
        image = validate_moments_image(
            data,
            expected_md5=expected_md5,
            max_file_bytes=self.max_file_bytes,
            max_pixels=self.max_pixels,
            max_frames=self.max_frames,
        )
        quality = _quality_for_dimensions(
            image.width,
            image.height,
            expected_width,
            expected_height,
        )
        relative = Path("blobs") / image.sha256[:2] / (
            f"{image.sha256}.{image.extension}"
        )
        target = self._blob_path(relative, must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, temporary_name = tempfile.mkstemp(
                prefix="moments_", suffix=".tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(image.data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, target)
            finally:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
        self._upsert(
            tid=tid,
            media_index=media_index,
            status=STATUS_READY,
            relative_path=relative.as_posix(),
            image=image,
            quality=quality,
        )
        return MomentsMediaStatus(
            status=STATUS_READY,
            quality=quality,
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            size=len(image.data),
            version=image.sha256[:16],
        )

    def _validated_record_bytes(
        self, record: dict[str, Any]
    ) -> tuple[ValidatedMomentsImage, bytes]:
        if record.get("status") != STATUS_READY:
            raise MomentsMediaNotFound("朋友圈媒体尚未完整加载")
        path = self._blob_path(record.get("relative_path"), must_exist=True)
        try:
            if path.stat().st_size > self.max_file_bytes:
                raise MomentsMediaValidationError("朋友圈媒体缓存超过大小上限")
            data = path.read_bytes()
        except OSError as exc:
            raise MomentsMediaNotFound("朋友圈媒体缓存读取失败") from exc
        image = validate_moments_image(
            data,
            expected_md5=record.get("file_md5"),
            max_file_bytes=self.max_file_bytes,
            max_pixels=self.max_pixels,
            max_frames=self.max_frames,
        )
        if image.sha256 != str(record.get("sha256") or ""):
            raise MomentsMediaValidationError("朋友圈媒体缓存完整性校验失败")
        return image, data

    def status(
        self,
        tid: Any,
        media_index: Any,
        *,
        expected_width: Any = 0,
        expected_height: Any = 0,
    ) -> MomentsMediaStatus:
        record = self.get_record(tid, media_index)
        if record is None:
            return MomentsMediaStatus(STATUS_UNBOUND)
        if record.get("status") == STATUS_PARTIAL:
            return MomentsMediaStatus(
                STATUS_PARTIAL,
                reason=str(record.get("reason") or ""),
            )
        try:
            image, data = self._validated_record_bytes(record)
        except (MomentsMediaError, OSError):
            return MomentsMediaStatus(
                STATUS_PARTIAL,
                reason="stored_media_invalid",
            )
        quality = _quality_for_dimensions(
            image.width,
            image.height,
            expected_width,
            expected_height,
        )
        if quality == QUALITY_UNKNOWN:
            quality = str(record.get("quality") or QUALITY_UNKNOWN)
        return MomentsMediaStatus(
            STATUS_READY,
            quality=quality,
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            size=len(data),
            version=image.sha256[:16],
        )

    def get_bytes(
        self, tid: Any, media_index: Any
    ) -> tuple[MomentsMediaStatus, bytes]:
        record = self.get_record(tid, media_index)
        if record is None:
            raise MomentsMediaNotFound("朋友圈媒体尚未绑定")
        image, data = self._validated_record_bytes(record)
        return (
            MomentsMediaStatus(
                STATUS_READY,
                quality=str(record.get("quality") or QUALITY_UNKNOWN),
                mime_type=image.mime_type,
                width=image.width,
                height=image.height,
                size=len(data),
                version=image.sha256[:16],
            ),
            data,
        )


class MomentsMediaResolver:
    """Decrypt explicitly selected SNS cache files and hydrate post statuses."""

    def __init__(
        self,
        *,
        account_id: str,
        aes_key: Any = None,
        xor_key: Any = "auto",
        store_root: Optional[Path] = None,
        sns_cache_root: Optional[Path] = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
    ):
        self.store = MomentsMediaStore(
            account_id,
            store_root,
            max_file_bytes=max_file_bytes,
            max_pixels=max_pixels,
            max_frames=max_frames,
        )
        self.aes_key = aes_key
        self.xor_key = xor_key
        self.max_scan_files = max(1, int(max_scan_files))
        self.sns_cache_root = (
            Path(sns_cache_root).resolve()
            if sns_cache_root is not None
            else None
        )

    @staticmethod
    def _is_image_media(media: Optional[dict[str, Any]]) -> bool:
        if not isinstance(media, dict):
            return True
        media_type = str(media.get("type") or media.get("media_type") or "")
        return media_type in _IMAGE_MEDIA_TYPES

    def _safe_source_path(self, source: Path) -> Path:
        if self.sns_cache_root is None:
            raise MomentsMediaPathError("尚未配置当前账号的朋友圈缓存目录")
        root = self.sns_cache_root.resolve(strict=True)
        candidate = Path(source)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise MomentsMediaPathError("朋友圈源缓存文件不在当前账号目录内") from exc
        if not resolved.is_file():
            raise MomentsMediaPathError("朋友圈源缓存路径不是文件")
        return resolved

    def discover_cache_files(self) -> list[Path]:
        """Return a bounded, account-root-contained list of SNS image files."""

        if self.sns_cache_root is None or not self.sns_cache_root.is_dir():
            return []
        candidates: list[Path] = []
        root = self.sns_cache_root.resolve()
        patterns = ("*/Sns/Img/*/*", "Sns/Img/*/*", "*/*")
        seen: set[Path] = set()
        for pattern in patterns:
            for path in root.glob(pattern):
                if len(candidates) >= self.max_scan_files:
                    return candidates
                try:
                    safe = self._safe_source_path(path)
                except MomentsMediaPathError:
                    continue
                if safe in seen:
                    continue
                seen.add(safe)
                candidates.append(safe)
        return candidates

    def _read_encrypted_source(self, source: Path) -> bytes:
        safe = self._safe_source_path(source)
        try:
            size = safe.stat().st_size
            # V1/V2 framing adds only a small amount, but leave bounded room
            # for encrypted padding and future headers.
            if size <= 0 or size > self.store.max_file_bytes + 1024 * 1024:
                raise MomentsMediaValidationError(
                    "朋友圈加密缓存为空或超过允许大小"
                )
            return safe.read_bytes()
        except OSError as exc:
            raise MomentsMediaValidationError("朋友圈加密缓存读取失败") from exc

    def _decode_source_variants(
        self, source: Path
    ) -> tuple[list[ValidatedMomentsImage], Optional[Exception]]:
        try:
            encrypted = self._read_encrypted_source(source)
        except MomentsMediaPathError:
            # A containment failure is a caller/security error, not an
            # incomplete cache payload, and must never be downgraded to a
            # harmless-looking ``partial`` status.
            raise
        except MomentsMediaError as exc:
            return [], exc

        xor_values: list[Any] = [self.xor_key]
        if str(self.xor_key or "").strip().lower() != "auto":
            xor_values.append("auto")
        decoded: list[ValidatedMomentsImage] = []
        seen_hashes: set[str] = set()
        last_error: Optional[Exception] = None
        for xor_value in xor_values:
            try:
                plain, _image_format, _mime_type = decrypt_dat_bytes(
                    encrypted,
                    aes_key=self.aes_key,
                    xor_key=xor_value,
                )
                image = validate_moments_image(
                    plain,
                    max_file_bytes=self.store.max_file_bytes,
                    max_pixels=self.store.max_pixels,
                    max_frames=self.store.max_frames,
                )
                if image.sha256 not in seen_hashes:
                    seen_hashes.add(image.sha256)
                    decoded.append(image)
            except (ImageResolutionError, MomentsMediaError, ValueError) as exc:
                last_error = exc
        return decoded, last_error

    def bind_cache_file(
        self,
        *,
        tid: Any,
        media_index: Any,
        source: Path,
        expected_md5: Any,
        expected_width: Any = 0,
        expected_height: Any = 0,
    ) -> MomentsMediaStatus:
        """Bind one explicit cache file only after an exact plaintext MD5 hit."""

        wanted_md5 = _normalize_md5(expected_md5)
        if not wanted_md5:
            return MomentsMediaStatus(
                STATUS_UNBOUND,
                reason="expected_md5_missing",
            )
        images, _error = self._decode_source_variants(Path(source))
        if not images:
            self.store.save_partial(tid, media_index, "cache_incomplete")
            return MomentsMediaStatus(
                STATUS_PARTIAL,
                reason="cache_incomplete",
            )
        for image in images:
            if image.file_md5 != wanted_md5:
                continue
            return self.store.save_bytes(
                tid,
                media_index,
                image.data,
                expected_md5=wanted_md5,
                expected_width=expected_width,
                expected_height=expected_height,
            )
        return MomentsMediaStatus(STATUS_UNBOUND, reason="md5_mismatch")

    def scan_exact_cache_files(
        self,
        posts: Iterable[dict[str, Any]],
        cache_files: Sequence[Path],
    ) -> MomentsMediaScanResult:
        """Bind cache files to posts solely by exact decoded-content MD5."""

        expected: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
        for post in posts:
            tid_value = post.get("tid")
            try:
                tid = _canonical_tid(tid_value)
            except MomentsMediaValidationError:
                continue
            for index, media in enumerate(list(post.get("media") or [])):
                if not self._is_image_media(media):
                    continue
                md5_value = _normalize_md5(media.get("url_md5"))
                if md5_value:
                    expected.setdefault(md5_value, []).append((tid, index, media))

        files_seen = files_decoded = partial_files = unmatched_files = 0
        exact_content_matches = media_bound = 0
        seen_sources: set[Path] = set()
        for raw_source in list(cache_files)[: self.max_scan_files]:
            source = self._safe_source_path(Path(raw_source))
            if source in seen_sources:
                continue
            seen_sources.add(source)
            files_seen += 1
            images, _error = self._decode_source_variants(source)
            if not images:
                partial_files += 1
                continue
            files_decoded += 1
            matched_file = False
            matched_hashes: set[str] = set()
            for image in images:
                refs = expected.get(image.file_md5, [])
                if not refs or image.file_md5 in matched_hashes:
                    continue
                matched_hashes.add(image.file_md5)
                matched_file = True
                exact_content_matches += 1
                for tid, index, media in refs:
                    self.store.save_bytes(
                        tid,
                        index,
                        image.data,
                        expected_md5=image.file_md5,
                        expected_width=media.get("width"),
                        expected_height=media.get("height"),
                    )
                    media_bound += 1
            if not matched_file:
                unmatched_files += 1
        return MomentsMediaScanResult(
            files_seen=files_seen,
            files_decoded=files_decoded,
            partial_files=partial_files,
            unmatched_files=unmatched_files,
            exact_content_matches=exact_content_matches,
            media_bound=media_bound,
        )

    def get_status(
        self,
        tid: Any,
        media_index: Any,
        media: Optional[dict[str, Any]] = None,
    ) -> MomentsMediaStatus:
        if not self._is_image_media(media):
            return MomentsMediaStatus(STATUS_UNSUPPORTED)
        return self.store.status(
            tid,
            media_index,
            expected_width=(media or {}).get("width", 0),
            expected_height=(media or {}).get("height", 0),
        )

    def hydrate_posts(
        self, posts: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return shallow post/media copies enriched with local preview state."""

        hydrated: list[dict[str, Any]] = []
        for original in posts:
            post = dict(original)
            tid = post.get("tid")
            media_items: list[dict[str, Any]] = []
            ready = partial = unbound = unsupported = 0
            for index, original_media in enumerate(list(post.get("media") or [])):
                media = dict(original_media)
                try:
                    status = self.get_status(tid, index, media)
                except MomentsMediaValidationError:
                    status = MomentsMediaStatus(
                        STATUS_UNBOUND,
                        reason="invalid_post_reference",
                    )
                media["local_media"] = status.to_dict()
                if status.status == STATUS_READY:
                    ready += 1
                elif status.status == STATUS_PARTIAL:
                    partial += 1
                elif status.status == STATUS_UNSUPPORTED:
                    unsupported += 1
                else:
                    unbound += 1
                media_items.append(media)
            post["media"] = media_items
            post["local_media_summary"] = {
                "total": len(media_items),
                "ready": ready,
                "partial": partial,
                "unbound": unbound,
                "unsupported": unsupported,
            }
            hydrated.append(post)
        return hydrated

    def get_bytes(
        self, tid: Any, media_index: Any
    ) -> tuple[MomentsMediaStatus, bytes]:
        return self.store.get_bytes(tid, media_index)


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_MAX_PIXELS",
    "MomentsMediaError",
    "MomentsMediaNotFound",
    "MomentsMediaPathError",
    "MomentsMediaResolver",
    "MomentsMediaScanResult",
    "MomentsMediaStatus",
    "MomentsMediaStore",
    "MomentsMediaValidationError",
    "QUALITY_HIGH",
    "QUALITY_THUMBNAIL",
    "QUALITY_UNKNOWN",
    "STATUS_PARTIAL",
    "STATUS_READY",
    "STATUS_UNBOUND",
    "STATUS_UNSUPPORTED",
    "ValidatedMomentsImage",
    "validate_moments_image",
]
