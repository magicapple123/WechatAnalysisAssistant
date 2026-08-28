"""Exact resource-state verification for UI-triggered WeChat image downloads."""

from __future__ import annotations

import glob
import hashlib
import io
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .decrypt import DatabaseDecryptor
from .image_service import (
    ImageResolutionError,
    WeChatImageService,
    convert_wxgf_to_jpeg,
    decrypt_dat_bytes,
    extract_md5_from_packed_info,
)


IMAGE_TYPES = (2, 3)
RESOURCE_HIGH = 0x10001
RESOURCE_MID = 0x20001
RESOURCE_THUMBNAIL = 0x40001
RESOURCE_STATUS_EXPIRED = 3


class ImageHDResourceError(RuntimeError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ImageHDTarget:
    message_id: int
    create_time: int
    server_id: str
    message_key: str
    time_str: str
    file_md5: str
    resource_info_ids: tuple[int, ...]
    initially_verified: bool = False

    def public_reference(self) -> dict:
        return {
            "message_id": self.message_id,
            "create_time": self.create_time,
            "time_str": self.time_str,
        }


def _database_fingerprint(path: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    def fingerprint(item: Path) -> tuple[int, int]:
        try:
            stat = item.stat()
            return stat.st_size, stat.st_mtime_ns
        except OSError:
            return 0, 0

    return fingerprint(path), fingerprint(Path(str(path) + "-wal"))


class WeChatHDResourceMonitor:
    """Monitor one account without reusing stale decrypted DB snapshots."""

    def __init__(
        self,
        *,
        account_id: str,
        account_root: Path,
        encrypted_resource_db: Path,
        database_key: str,
        image_aes_key: object = None,
        image_xor_key: object = "auto",
    ):
        self.account_id = account_id
        self.account_root = Path(account_root).resolve()
        self.attach_dir = self.account_root / "msg" / "attach"
        self.encrypted_resource_db = Path(encrypted_resource_db)
        self.database_key = database_key
        self.image_aes_key = image_aes_key
        self.image_xor_key = image_xor_key
        self._details: dict[int, list[tuple[int, int, int, int]]] = {}
        self._resource_ids: set[int] = set()
        self._last_db_fingerprint = ((0, 0), (0, 0))
        self._last_refresh_at = 0.0
        self._stable_stats: dict[Path, tuple[int, int, float, int]] = {}
        self._verified_files: dict[Path, tuple[int, int]] = {}
        self._verified_dimensions: dict[
            Path, tuple[tuple[int, int], tuple[int, int]]
        ] = {}
        self._baseline_details: dict[str, tuple[tuple[int, int, int, int, int], ...]] = {}
        self._baseline_activity_files: dict[
            str, dict[Path, tuple[int, int]]
        ] = {}

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")')
        }

    @staticmethod
    def _load_details(
        conn: sqlite3.Connection, resource_ids: set[int]
    ) -> dict[int, list[tuple[int, int, int, int]]]:
        result: dict[int, list[tuple[int, int, int, int]]] = {
            resource_id: [] for resource_id in resource_ids
        }
        identifiers = sorted(resource_ids)
        for start in range(0, len(identifiers), 500):
            chunk = identifiers[start:start + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT message_id, type, status, size, access_time "
                f"FROM MessageResourceDetail WHERE message_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for message_id, resource_type, status, size, access_time in rows:
                result.setdefault(int(message_id), []).append(
                    (
                        int(resource_type or 0),
                        int(status or 0),
                        int(size or 0),
                        int(access_time or 0),
                    )
                )
        return result

    def prepare_targets(
        self, talker: str, image_messages: list[dict]
    ) -> list[ImageHDTarget]:
        if not self.encrypted_resource_db.exists():
            raise ImageHDResourceError("未找到图片资源数据库", 404)
        conn, decryptor, fingerprint = self._open_consistent_connection()
        try:
            chat_id = WeChatImageService._find_chat_id(conn, talker)
            if chat_id is None:
                raise ImageHDResourceError("图片资源数据库中没有该聊天", 404)
            columns = self._table_columns(conn, "MessageResourceInfo")
            required = {
                "message_id",
                "chat_id",
                "message_local_id",
                "message_create_time",
                "packed_info",
            }
            if not required.issubset(columns):
                raise ImageHDResourceError("图片资源数据库结构不受支持")

            targets: list[ImageHDTarget] = []
            for message in image_messages:
                message_id = int(message.get("id") or 0)
                create_time = int(message.get("create_time") or 0)
                server_id = str(message.get("server_id") or "")
                type_clause = ""
                if "message_local_type" in columns:
                    type_clause = (
                        " AND ((message_local_type & 4294967295) IN (2, 3))"
                    )
                if server_id and server_id != "0" and "message_svr_id" in columns:
                    rows = conn.execute(
                        "SELECT message_id, packed_info FROM MessageResourceInfo "
                        "WHERE chat_id=? AND message_svr_id=?" + type_clause,
                        (chat_id, server_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT message_id, packed_info FROM MessageResourceInfo "
                        "WHERE chat_id=? AND message_local_id=? "
                        "AND message_create_time=?" + type_clause,
                        (chat_id, message_id, create_time),
                    ).fetchall()
                if not rows:
                    continue
                md5_values = {
                    value
                    for _resource_id, packed_info in rows
                    if (value := extract_md5_from_packed_info(packed_info))
                }
                if len(md5_values) != 1:
                    continue
                resource_ids = tuple(sorted({int(row[0]) for row in rows}))
                self._resource_ids.update(resource_ids)
                targets.append(
                    ImageHDTarget(
                        message_id=message_id,
                        create_time=create_time,
                        server_id=server_id,
                        message_key=str(message.get("message_key") or ""),
                        time_str=str(message.get("time_str") or ""),
                        file_md5=next(iter(md5_values)),
                        resource_info_ids=resource_ids,
                    )
                )

            self._details = self._load_details(conn, self._resource_ids)
            self._last_db_fingerprint = fingerprint
            self._last_refresh_at = time.monotonic()
        finally:
            conn.close()
            if decryptor is not None:
                decryptor.close()

        # Take two filesystem observations together instead of sleeping once
        # per target. This keeps preparation bounded for large ranges.
        first = {
            target.message_key: self._candidate_stats(talker, target)
            for target in targets
        }
        for target in targets:
            self._baseline_details[target.message_key] = self._detail_state(target)
        time.sleep(0.75)
        prepared: list[ImageHDTarget] = []
        for target in targets:
            second = self._candidate_stats(talker, target)
            verified = self._validate_from_observations(
                talker,
                target,
                first.get(target.message_key, {}),
                second,
            )
            prepared.append(
                ImageHDTarget(
                    **{
                        **target.__dict__,
                        "initially_verified": verified,
                    }
                )
            )
            self._baseline_activity_files[target.message_key] = (
                self._activity_candidate_stats(talker, target)
            )
        return prepared

    def _candidate_paths(self, talker: str, target: ImageHDTarget) -> list[Path]:
        talker_hash = hashlib.md5(talker.encode("utf-8")).hexdigest()
        base = self.attach_dir / talker_hash
        if not base.is_dir():
            return []
        pattern = str(base / "*" / "Img" / f"{target.file_md5}*.dat")
        result: list[Path] = []
        for item in glob.glob(pattern):
            path = Path(item).resolve()
            try:
                path.relative_to(self.account_root)
            except ValueError:
                continue
            suffix = path.name.lower()[len(target.file_md5):]
            if path.is_file() and not suffix.startswith("_t"):
                result.append(path)
        return sorted(result)

    def _thumbnail_paths(self, talker: str, target: ImageHDTarget) -> list[Path]:
        talker_hash = hashlib.md5(talker.encode("utf-8")).hexdigest()
        base = self.attach_dir / talker_hash
        if not base.is_dir():
            return []
        result: list[Path] = []
        for item in glob.glob(
            str(base / "*" / "Img" / f"{target.file_md5}_t*.dat")
        ):
            path = Path(item).resolve()
            try:
                path.relative_to(self.account_root)
            except ValueError:
                continue
            if path.is_file():
                result.append(path)
        return sorted(result)

    def _candidate_stats(
        self, talker: str, target: ImageHDTarget
    ) -> dict[Path, tuple[int, int]]:
        result = {}
        for path in self._candidate_paths(talker, target):
            try:
                stat = path.stat()
                result[path] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
        return result

    def _activity_candidate_stats(
        self, talker: str, target: ImageHDTarget
    ) -> dict[Path, tuple[int, int]]:
        """Observe only files whose exact MD5 belongs to the prompted image."""
        talker_hash = hashlib.md5(talker.encode("utf-8")).hexdigest()
        patterns = (
            self.attach_dir / talker_hash / "*" / "Img" / f"{target.file_md5}*.dat",
            self.account_root
            / "cache"
            / "*"
            / "Message"
            / talker_hash
            / "Bubble"
            / f"{target.file_md5}*.dat",
        )
        result: dict[Path, tuple[int, int]] = {}
        for pattern in patterns:
            for item in glob.glob(str(pattern)):
                path = Path(item).resolve()
                try:
                    path.relative_to(self.account_root)
                    if not path.name.lower().startswith(target.file_md5.lower()):
                        continue
                    stat = path.stat()
                    if path.is_file():
                        result[path] = (stat.st_size, stat.st_mtime_ns)
                except (OSError, ValueError):
                    continue
        return result

    def _detail_state(
        self, target: ImageHDTarget
    ) -> tuple[tuple[int, int, int, int, int], ...]:
        return tuple(
            sorted(
                (
                    resource_id,
                    resource_type,
                    status,
                    size,
                    access_time,
                )
                for resource_id in target.resource_info_ids
                for resource_type, status, size, access_time in self._details.get(
                    resource_id, []
                )
            )
        )

    def _expected_quality_sizes(self, target: ImageHDTarget) -> dict[int, set[int]]:
        sizes: dict[int, set[int]] = {}
        for resource_id in target.resource_info_ids:
            for resource_type, status, size, _access_time in self._details.get(
                resource_id, []
            ):
                if (
                    resource_type in (RESOURCE_HIGH, RESOURCE_MID)
                    and status == 1
                    and size > 0
                ):
                    sizes.setdefault(size, set()).add(resource_type)
        return sizes

    def _is_verified_quality(
        self,
        talker: str,
        target: ImageHDTarget,
        path: Path,
        fingerprint: tuple[int, int],
        resource_types: set[int],
    ) -> bool:
        dimensions = self._decrypted_dimensions(path, fingerprint)
        if dimensions is None:
            return False
        if RESOURCE_HIGH in resource_types:
            return True

        # In WeChat 4.x a viewer-loaded clear image is frequently stored as
        # the base .dat with type 0x20001 rather than as _h.dat/0x10001.
        # Accept it only when its decoded pixels materially exceed the exact
        # target's thumbnail, so a renamed thumbnail cannot pass validation.
        thumbnail_dimensions: list[tuple[int, int]] = []
        for thumbnail in self._thumbnail_paths(talker, target):
            try:
                stat = thumbnail.stat()
            except OSError:
                continue
            decoded = self._decrypted_dimensions(
                thumbnail, (stat.st_size, stat.st_mtime_ns)
            )
            if decoded is not None:
                thumbnail_dimensions.append(decoded)
        width, height = dimensions
        if thumbnail_dimensions:
            thumb_width, thumb_height = max(
                thumbnail_dimensions, key=lambda item: item[0] * item[1]
            )
            return bool(
                width * height >= thumb_width * thumb_height * 1.5
                and max(width, height) > max(thumb_width, thumb_height)
            )
        return max(width, height) >= 640 and width * height >= 120_000

    def _validate_from_observations(
        self,
        talker: str,
        target: ImageHDTarget,
        first: dict[Path, tuple[int, int]],
        second: dict[Path, tuple[int, int]],
    ) -> bool:
        expected_sizes = self._expected_quality_sizes(target)
        if not expected_sizes:
            return False
        for path, fingerprint in second.items():
            resource_types = expected_sizes.get(fingerprint[0])
            if first.get(path) != fingerprint or not resource_types:
                continue
            if self._is_verified_quality(
                talker, target, path, fingerprint, resource_types
            ):
                return True
        return False

    def _open_consistent_connection(
        self,
    ) -> tuple[
        sqlite3.Connection,
        Optional[DatabaseDecryptor],
        tuple[tuple[int, int], tuple[int, int]],
    ]:
        last_error: Optional[Exception] = None
        for _attempt in range(3):
            before = _database_fingerprint(self.encrypted_resource_db)
            decryptor: Optional[DatabaseDecryptor] = None
            try:
                with self.encrypted_resource_db.open("rb") as handle:
                    is_plaintext = handle.read(16) == b"SQLite format 3\x00"
                if is_plaintext:
                    database_path = self.encrypted_resource_db
                else:
                    decryptor = DatabaseDecryptor(
                        self.encrypted_resource_db, self.database_key
                    )
                    database_path = decryptor.decrypt_to_temp()
                after = _database_fingerprint(self.encrypted_resource_db)
                if before != after:
                    raise OSError("图片资源数据库正在更新")
                conn = sqlite3.connect(
                    f"file:{database_path}?mode=ro",
                    uri=True,
                    timeout=10,
                )
                # Force SQLite to read the schema while the before/after
                # fingerprint guarantee is still current.
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                return conn, decryptor, after
            except Exception as exc:
                last_error = exc
                if decryptor is not None:
                    decryptor.close()
                time.sleep(0.15)
        raise ImageHDResourceError(
            "无法取得一致的图片资源数据库快照，请稍后重试"
        ) from last_error

    def _verify_decrypted_file(
        self, path: Path, fingerprint: tuple[int, int]
    ) -> bool:
        return self._decrypted_dimensions(path, fingerprint) is not None

    def _decrypted_dimensions(
        self, path: Path, fingerprint: tuple[int, int]
    ) -> Optional[tuple[int, int]]:
        cached = self._verified_dimensions.get(path)
        if cached and cached[0] == fingerprint:
            return cached[1]
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or (after.st_size, after.st_mtime_ns) != fingerprint
            ):
                return False
            decoded, image_format, _mime_type = decrypt_dat_bytes(
                data,
                aes_key=self.image_aes_key,
                xor_key=self.image_xor_key,
            )
            if image_format == "hevc":
                decoded = convert_wxgf_to_jpeg(decoded)
            from PIL import Image

            with Image.open(io.BytesIO(decoded)) as image:
                width, height = image.size
                if width <= 0 or height <= 0:
                    return None
                image.verify()
            self._verified_files[path] = fingerprint
            dimensions = (int(width), int(height))
            self._verified_dimensions[path] = (fingerprint, dimensions)
            return dimensions
        except (OSError, ImageResolutionError, ValueError):
            return None
        except Exception:
            return None

    def _refresh_details_if_changed(self, force: bool = False) -> None:
        now = time.monotonic()
        fingerprint = _database_fingerprint(self.encrypted_resource_db)
        if not force and (
            fingerprint == self._last_db_fingerprint
            or now - self._last_refresh_at < 0.75
        ):
            return
        last_error: Optional[Exception] = None
        for _attempt in range(3):
            try:
                conn, decryptor, after = self._open_consistent_connection()
                details = self._load_details(conn, self._resource_ids)
                self._details = details
                self._last_db_fingerprint = after
                self._last_refresh_at = time.monotonic()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.15)
            finally:
                if "conn" in locals() and conn is not None:
                    conn.close()
                    conn = None
                if "decryptor" in locals() and decryptor is not None:
                    decryptor.close()
                    decryptor = None
        if force and last_error is not None:
            raise ImageHDResourceError("刷新图片资源状态失败，请稍后重试") from last_error

    def is_verified_high(self, talker: str, target: ImageHDTarget) -> bool:
        self._refresh_details_if_changed()
        expected_sizes = self._expected_quality_sizes(target)
        now = time.monotonic()
        for path in self._candidate_paths(talker, target):
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint = (stat.st_size, stat.st_mtime_ns)
            previous = self._stable_stats.get(path)
            if previous and previous[:2] == fingerprint:
                first_stable = previous[2]
                count = previous[3] + 1
            else:
                first_stable = now
                count = 1
            self._stable_stats[path] = (
                fingerprint[0],
                fingerprint[1],
                first_stable,
                count,
            )
            resource_types = expected_sizes.get(fingerprint[0])
            if (
                resource_types
                and count >= 3
                and now - first_stable >= 1.0
                and self._is_verified_quality(
                    talker, target, path, fingerprint, resource_types
                )
            ):
                return True
        return False

    def is_expired(self, target: ImageHDTarget) -> bool:
        """Return true only for an explicit WeChat unavailable/expired state."""
        self._refresh_details_if_changed()
        relevant = [
            (status, size)
            for resource_id in target.resource_info_ids
            for resource_type, status, size, _access_time in self._details.get(
                resource_id, []
            )
            if resource_type in (RESOURCE_HIGH, RESOURCE_MID)
        ]
        if any(status == 1 and size > 0 for status, size in relevant):
            return False
        return any(status == RESOURCE_STATUS_EXPIRED for status, _size in relevant)

    def has_activity(self, talker: str, target: ImageHDTarget) -> bool:
        """Confirm that the prepared first target was actually opened in WeChat."""
        self._refresh_details_if_changed()
        if self._detail_state(target) != self._baseline_details.get(
            target.message_key, ()
        ):
            return True
        current_files = self._activity_candidate_stats(talker, target)
        return current_files != self._baseline_activity_files.get(
            target.message_key, {}
        )

    def force_refresh(self) -> None:
        self._refresh_details_if_changed(force=True)
