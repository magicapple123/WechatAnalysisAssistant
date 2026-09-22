"""Application data and bundled-resource paths.

The source checkout is immutable from the application's point of view.  All
user-controlled state is kept below the per-user application-data directory,
which also makes upgrades and installations under ``Program Files`` safe.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional


APP_NAME = "WechatAnalysisAssistant"
DATA_DIR_ENV = "WECHAT_ASSISTANT_DATA_DIR"


def _absolute_environment_path(value: object) -> Optional[Path]:
    """Expand an environment path only when it is already absolute."""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        candidate = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not candidate.is_absolute():
            return None
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def source_root() -> Path:
    """Return the project root when running from a source checkout."""

    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Return the read-only resource root in source and PyInstaller builds."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return source_root()


def resource_path(*parts: str) -> Path:
    """Resolve a file bundled by PyInstaller without writing beside it."""

    return bundle_root().joinpath(*parts)


def get_app_data_dir(
    environ: Optional[Mapping[str, str]] = None,
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return the per-user writable data directory.

    ``WECHAT_ASSISTANT_DATA_DIR`` is intentionally supported for isolated
    tests, portable diagnostics and managed deployments.  It is never set by
    the application itself.
    """

    env = os.environ if environ is None else environ
    explicit = _absolute_environment_path(env.get(DATA_DIR_ENV, ""))
    if explicit is not None:
        return explicit

    current_platform = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else Path(home)
    if current_platform == "win32":
        root = _absolute_environment_path(env.get("LOCALAPPDATA", ""))
        if root is None:
            root = user_home.resolve() / "AppData" / "Local"
    elif current_platform == "darwin":
        root = user_home / "Library" / "Application Support"
    else:
        root = _absolute_environment_path(env.get("XDG_DATA_HOME", ""))
        if root is None:
            root = user_home / ".local" / "share"
    return (root / APP_NAME).resolve()


APP_DATA_DIR = get_app_data_dir()


def app_data_path(*parts: str) -> Path:
    return APP_DATA_DIR.joinpath(*parts)


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    file_mode: int = 0o600,
) -> None:
    """Durably replace one text file without exposing a partial destination."""

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(str(content))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(file_mode)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_validator(payload: bytes) -> bool:
    try:
        decoded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded, dict)


def migrate_legacy_file(
    destination: Path,
    legacy_paths: Iterable[Path],
    *,
    validator: Optional[Callable[[bytes], bool]] = None,
) -> Optional[Path]:
    """Copy the first valid legacy file to ``destination`` exactly once.

    The source is deliberately retained: a migration must never destroy the
    user's only copy if a later application start fails.  The destination is
    written through a same-directory temporary file and atomically replaced.
    Existing destinations always win, so subsequent starts cannot overwrite a
    newer setting or key file with stale checkout data.
    """

    destination = Path(destination)
    if destination.exists():
        return None

    try:
        destination_resolved = destination.resolve(strict=False)
    except OSError:
        destination_resolved = destination.absolute()

    for candidate in legacy_paths:
        source = Path(candidate)
        try:
            if source.resolve(strict=False) == destination_resolved or not source.is_file():
                continue
            payload = source.read_bytes()
        except OSError:
            continue
        if not payload or (validator is not None and not validator(payload)):
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".migrating",
            dir=str(destination.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if destination.exists():
                return None
            os.replace(temporary, destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            return source
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return None


def migrate_legacy_json_file(
    destination: Path,
    legacy_paths: Iterable[Path],
) -> Optional[Path]:
    """Migrate a legacy JSON object after validating it is readable."""

    return migrate_legacy_file(destination, legacy_paths, validator=_json_validator)


def directory_size(directory: Path) -> int:
    """Return the total byte size of regular files directly inside ``directory``."""

    total = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def prune_cache_directory(directory: Path, max_bytes: int) -> int:
    """按 mtime 从旧到新淘汰目录内文件，直到总大小不超过 ``max_bytes``。

    用于表情包缓存、朋友圈 blob、图片解密临时副本等可再生缓存目录，
    防止长期使用后无界增长。递归遍历（含子目录）；被淘汰的缓存条目
    在下次访问时会自动重建。返回删除的文件数量；
    目录不存在或未超限时不动任何文件。
    """

    max_bytes = max(0, int(max_bytes))
    if max_bytes <= 0 or not directory.is_dir():
        return 0

    entries = []
    total = 0
    try:
        for root, _dirs, files in os.walk(directory):
            for name in files:
                path = os.path.join(root, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                entries.append((info.st_mtime_ns, path, info.st_size))
                total += info.st_size
    except OSError:
        return 0

    if total <= max_bytes:
        return 0

    removed = 0
    for _, path, size in sorted(entries, key=lambda item: item[0]):
        if total <= max_bytes:
            break
        try:
            os.unlink(path)
        except OSError:
            continue
        total -= size
        removed += 1
    return removed
