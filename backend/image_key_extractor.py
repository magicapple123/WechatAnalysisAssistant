"""Derive or extract and verify the WeChat 4.x V2 image AES key.

The memory fallback is adapted from ``wechat-decrypt-main/find_image_key.py``.
The preferred metadata derivation follows the verified WeChat 4.x relation
also documented by ``find_image_key_macos.py``.  No candidate is returned
unless it decrypts multiple local V2 image samples successfully.
"""

from __future__ import annotations

import ctypes
import glob
import hashlib
import heapq
import os
import re
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil
from Crypto.Cipher import AES

from .image_service import ImageResolutionError, decrypt_dat_bytes


V2_MAGIC = b"\x07\x08V2\x08\x07"
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
READABLE_PROTECTIONS = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
WRITABLE_PROTECTIONS = {0x04, 0x08, 0x40, 0x80}
CHUNK_SIZE = 4 * 1024 * 1024
RE_KEY_32 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])")
RE_KEY_16 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])")


class ImageKeyExtractionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class V2ImageSample:
    path: Path
    ciphertext: bytes
    encrypted_tail: bytes
    xor_size: int = 0


@dataclass(frozen=True)
class ImageKeyExtractionResult:
    aes_key: str
    xor_key: Optional[int]
    verified_format: str
    samples_found: int
    processes_scanned: int
    extraction_method: str = "memory"


class _DeadlineExceeded(RuntimeError):
    pass


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _image_format_from_block(block: bytes) -> str:
    if block[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if block[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if block[:4] == b"RIFF" and block[8:12] == b"WEBP":
        return "WEBP"
    if block[:4] == b"wxgf":
        return "WXGF"
    if block[:3] == b"GIF":
        return "GIF"
    if block[:2] == b"BM":
        return "BMP"
    if block[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    return ""


def _candidate_format(key: bytes, samples: list[V2ImageSample]) -> str:
    if len(key) != 16 or not samples:
        return ""
    matches = []
    for sample in samples[:4]:
        try:
            block = AES.new(key, AES.MODE_ECB).decrypt(sample.ciphertext)
        except (ValueError, KeyError):
            return ""
        image_format = _image_format_from_block(block)
        if image_format:
            matches.append(image_format)
    required = 2 if len(samples[:4]) >= 2 else 1
    return matches[0] if len(matches) >= required else ""


def _scan_buffer_for_key(
    data: bytes,
    samples: list[V2ImageSample],
    seen: Optional[set[bytes]] = None,
) -> tuple[Optional[bytes], str]:
    """Test candidate ASCII strings in one memory buffer."""
    seen = seen if seen is not None else set()
    candidates = []
    for match in RE_KEY_32.finditer(data):
        value = match.group()
        candidates.extend((value[:16], value[16:]))
    candidates.extend(match.group() for match in RE_KEY_16.finditer(data))

    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        image_format = _candidate_format(candidate, samples)
        if image_format:
            return candidate, image_format
    return None, ""


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_recent_v2_samples(
    attach_dir: Path,
    *,
    max_samples: int = 8,
    max_candidates: int = 160,
) -> list[V2ImageSample]:
    """Return V2 validation samples already present in the account data.

    The common WeChat directory shape is searched first.  If it does not
    yield enough independent samples, fall back to a recursive search so the
    caller does not need to open images merely to refresh the cache.
    """
    attach_dir = Path(attach_dir)
    if not attach_dir.is_dir():
        return []

    candidates: dict[str, Path] = {}
    patterns = (
        str(attach_dir / "*" / "*" / "Img" / "*_t.dat"),
        str(attach_dir / "*" / "*" / "Img" / "*_h.dat"),
        str(attach_dir / "*" / "*" / "Img" / "*.dat"),
    )
    for pattern in patterns:
        recent = heapq.nlargest(
            max_candidates,
            (Path(item) for item in glob.iglob(pattern)),
            key=_safe_mtime,
        )
        for path in recent:
            candidates[str(path)] = path

    samples = []
    seen_ciphertexts = set()

    def collect(paths) -> None:
        ordered = sorted(paths, key=_safe_mtime, reverse=True)
        for path in ordered:
            if len(samples) >= max_samples:
                return
            try:
                size = path.stat().st_size
                if size < 31:
                    continue
                with open(path, "rb") as handle:
                    header = handle.read(31)
                    if header[:6] != V2_MAGIC:
                        continue
                    handle.seek(max(0, size - 12))
                    tail = handle.read(12)
                ciphertext = header[15:31]
                _aes_size, xor_size = struct.unpack_from("<II", header, 6)
                if len(ciphertext) != AES.block_size:
                    continue
                if ciphertext in seen_ciphertexts:
                    continue
                seen_ciphertexts.add(ciphertext)
                samples.append(V2ImageSample(path, ciphertext, tail, xor_size))
            except (OSError, struct.error):
                continue

    collect(candidates.values())

    # Older/newer WeChat builds may add directory levels or use a different
    # image subdirectory name.  Only run the broader walk when the fast path
    # did not provide the two independent samples preferred for validation.
    if len(samples) < 2:
        recursive = heapq.nlargest(
            max(max_candidates, 1000),
            (
                path
                for path in attach_dir.rglob("*.dat")
                if str(path) not in candidates
            ),
            key=_safe_mtime,
        )
        collect(recursive)
    return samples


def derive_xor_key(samples: list[V2ImageSample]) -> Optional[int]:
    counts: dict[int, int] = {}
    png_trailer = b"IEND\xaeB`\x82"
    for sample in samples:
        tail = sample.encrypted_tail
        if sample.xor_size >= 2 and len(tail) >= 2:
            candidate = tail[-2] ^ 0xFF
            if (tail[-1] ^ candidate) == 0xD9:
                counts[candidate] = counts.get(candidate, 0) + 1
        if sample.xor_size >= len(png_trailer) and len(tail) >= len(png_trailer):
            encrypted = tail[-len(png_trailer):]
            candidate = encrypted[0] ^ png_trailer[0]
            if all(
                (value ^ candidate) == expected
                for value, expected in zip(encrypted, png_trailer)
            ):
                counts[candidate] = counts.get(candidate, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _account_id_candidates(account_id: str) -> list[str]:
    """Return plausible wxid values without assuming one folder suffix shape."""
    value = str(account_id or "").strip()
    if not value:
        return []
    candidates = [value]
    parts = value.split("_")
    # WeChat account directories may append a short hash to the real wxid.
    # Generate progressively shorter forms and let V2 decryption decide which
    # one is correct instead of trusting a particular client version's shape.
    while len(parts) > 2:
        parts = parts[:-1]
        candidate = "_".join(parts)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _collect_kvcomm_codes(roaming_root: Optional[Path] = None) -> list[int]:
    """Collect local WeChat numeric account codes from kvcomm filenames."""
    if roaming_root is not None:
        roots = [Path(roaming_root)]
    else:
        roots = []
        for environment_name in ("APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(environment_name, "").strip()
            if value:
                candidate = Path(value) / "Tencent"
                if candidate not in roots:
                    roots.append(candidate)

    codes = set()
    filename_pattern = re.compile(r"^key_(\d+)_", re.IGNORECASE)

    def ignore_walk_error(_error) -> None:
        return None

    for roaming_path in roots:
        if not roaming_path.is_dir():
            continue
        for root, directories, files in os.walk(
            roaming_path, onerror=ignore_walk_error
        ):
            if Path(root).name.lower() != "kvcomm":
                continue
            # kvcomm subdirectories do not contain additional account code
            # files, so there is no reason to descend further from here.
            directories.clear()
            for filename in files:
                match = filename_pattern.match(filename)
                if not match:
                    continue
                try:
                    code = int(match.group(1))
                except ValueError:
                    continue
                if 0 < code <= 0xFFFFFFFF:
                    codes.add(code)
    return sorted(codes)


def _derive_verified_image_key(
    samples: list[V2ImageSample],
    account_id: str,
    codes: list[int],
) -> Optional[tuple[bytes, int, str, int]]:
    """Derive an account key and accept it only after full image validation."""
    matches: dict[bytes, tuple[int, str, int]] = {}
    required = 2 if len(samples) >= 2 else 1
    for code in codes:
        for wxid in _account_id_candidates(account_id):
            candidate = hashlib.md5(
                f"{code}{wxid}".encode("utf-8")
            ).hexdigest()[:16].encode("ascii")
            block_format = _candidate_format(candidate, samples)
            if not block_format:
                continue
            xor_key = code & 0xFF
            verified_format, verified_count = _verify_full_image(
                candidate, samples, xor_key
            )
            if verified_count >= required:
                matches[candidate] = (
                    xor_key,
                    verified_format or block_format,
                    verified_count,
                )

    # More than one distinct verified key means the local metadata is
    # ambiguous. Fall back to memory scanning instead of selecting silently.
    if len(matches) != 1:
        return None
    key, (xor_key, image_format, verified_count) = next(iter(matches.items()))
    return key, xor_key, image_format, verified_count


def _verify_full_image(
    key: bytes,
    samples: list[V2ImageSample],
    xor_key: Optional[int],
) -> tuple[str, int]:
    verified_format = ""
    verified = 0
    for sample in samples:
        try:
            data = sample.path.read_bytes()
            decoded, image_format, _mime = decrypt_dat_bytes(
                data,
                aes_key=key,
                xor_key=xor_key if xor_key is not None else "auto",
            )
            if decoded and image_format:
                verified_format = verified_format or image_format.upper()
                verified += 1
        except (OSError, ImageResolutionError):
            continue
    return verified_format, verified


def _configure_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _enumerate_regions(kernel32, process_handle) -> list[tuple[int, int, int]]:
    address = 0
    maximum = 0x7FFFFFFFFFFF if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x7FFFFFFF
    regions = []
    while address < maximum:
        info = MEMORY_BASIC_INFORMATION()
        queried = kernel32.VirtualQueryEx(
            process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not queried:
            break
        base = int(info.BaseAddress or address)
        size = int(info.RegionSize or 0)
        if size <= 0:
            break
        protection = int(info.Protect or 0)
        base_protection = protection & 0xFF
        if (
            info.State == MEM_COMMIT
            and base_protection in READABLE_PROTECTIONS
            and base_protection != PAGE_NOACCESS
            and not (protection & PAGE_GUARD)
        ):
            regions.append((base, size, base_protection))
        next_address = base + size
        if next_address <= address:
            break
        address = next_address
    return regions


def _scan_regions(
    kernel32,
    process_handle,
    regions: list[tuple[int, int, int]],
    samples: list[V2ImageSample],
    seen: set[bytes],
    deadline: float,
) -> tuple[Optional[bytes], str]:
    for base, region_size, _protection in regions:
        offset = 0
        overlap = b""
        while offset < region_size:
            if time.monotonic() >= deadline:
                raise _DeadlineExceeded()
            requested = min(CHUNK_SIZE, region_size - offset)
            buffer = ctypes.create_string_buffer(requested)
            bytes_read = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                process_handle,
                ctypes.c_void_p(base + offset),
                buffer,
                requested,
                ctypes.byref(bytes_read),
            )
            if bytes_read.value:
                data = overlap + buffer.raw[:bytes_read.value]
                key, image_format = _scan_buffer_for_key(data, samples, seen)
                if key:
                    return key, image_format
                overlap = data[-31:]
            else:
                overlap = b""
            offset += requested
            if not ok and not bytes_read.value:
                break
    return None, ""


def _wechat_processes() -> list[tuple[int, float]]:
    processes = []
    for process in psutil.process_iter(
        ["pid", "name", "create_time", "memory_info"]
    ):
        try:
            name = str(process.info.get("name") or "").lower()
            if name != "weixin.exe":
                continue
            processes.append(
                (
                    int(process.info["pid"]),
                    float(
                        getattr(process.info.get("memory_info"), "rss", 0) or 0
                    ),
                )
            )
        except (psutil.Error, TypeError, ValueError):
            continue
    return sorted(processes, key=lambda item: item[1], reverse=True)


def extract_image_aes_key(
    attach_dir: Path,
    *,
    account_id: str = "",
    max_seconds: int = 90,
) -> ImageKeyExtractionResult:
    """Find, validate, and return the current account's 16-byte image key."""
    if sys.platform != "win32":
        raise ImageKeyExtractionError(
            "UNSUPPORTED_PLATFORM", "自动获取图片密钥目前仅支持 Windows"
        )
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ImageKeyExtractionError(
            "BITNESS_MISMATCH", "自动获取图片密钥需要使用 64 位 Python"
        )

    samples = find_recent_v2_samples(Path(attach_dir))
    if not samples:
        raise ImageKeyExtractionError(
            "NO_V2_IMAGE_CACHE",
            "当前账号的本地数据中没有可用于验证的 V2 图片文件，暂时无法安全确认图片密钥",
        )

    derived = _derive_verified_image_key(
        samples,
        account_id,
        _collect_kvcomm_codes(),
    )
    if derived:
        key, xor_key, verified_format, _verified_count = derived
        return ImageKeyExtractionResult(
            aes_key=key.decode("ascii"),
            xor_key=xor_key,
            verified_format=verified_format,
            samples_found=len(samples),
            processes_scanned=0,
            extraction_method="metadata",
        )

    processes = _wechat_processes()
    if not processes:
        raise ImageKeyExtractionError(
            "WECHAT_NOT_RUNNING", "未找到运行中的微信 4.x 进程 Weixin.exe"
        )

    kernel32 = _configure_kernel32()
    deadline = time.monotonic() + max(10, int(max_seconds))
    seen: set[bytes] = set()
    opened_pids = set()
    scanned_pids = set()
    region_cache: dict[int, list[tuple[int, int, int]]] = {}
    try:
        # Search likely string-bearing writable memory in every Weixin process
        # before spending the remaining budget on read-only regions.
        for writable_only in (True, False):
            for pid, _rss in processes:
                if time.monotonic() >= deadline:
                    raise _DeadlineExceeded()
                process_handle = kernel32.OpenProcess(
                    PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
                )
                if not process_handle:
                    continue
                opened_pids.add(pid)
                try:
                    regions = region_cache.get(pid)
                    if regions is None:
                        regions = _enumerate_regions(kernel32, process_handle)
                        region_cache[pid] = regions
                    phase = [
                        item
                        for item in regions
                        if (item[2] in WRITABLE_PROTECTIONS) == writable_only
                    ]
                    key, block_format = _scan_regions(
                        kernel32,
                        process_handle,
                        phase,
                        samples,
                        seen,
                        deadline,
                    )
                    scanned_pids.add(pid)
                    if not key:
                        continue
                    # The requested secret is the 16-byte AES key.  Keep XOR
                    # on the safer per-image auto mode instead of persisting a
                    # guess derived from one cache file.
                    xor_key = None
                    verified_format, verified_count = _verify_full_image(
                        key, samples, xor_key
                    )
                    required = 2 if len(samples) >= 2 else 1
                    if verified_count < required:
                        continue
                    return ImageKeyExtractionResult(
                        aes_key=key.decode("ascii"),
                        xor_key=xor_key,
                        verified_format=verified_format or block_format,
                        samples_found=len(samples),
                        processes_scanned=len(scanned_pids),
                        extraction_method="memory",
                    )
                finally:
                    kernel32.CloseHandle(process_handle)
    except _DeadlineExceeded as exc:
        raise ImageKeyExtractionError(
            "SCAN_TIMEOUT",
            "自动扫描微信内存超时，请重试；如果持续超时，可尝试以管理员身份运行本工具",
        ) from exc

    if not opened_pids:
        raise ImageKeyExtractionError(
            "PROCESS_ACCESS_DENIED",
            "无法读取微信进程内存，请尝试以管理员身份运行本工具",
        )
    raise ImageKeyExtractionError(
        "KEY_NOT_FOUND",
        "未能从当前账号元数据或微信进程中获取并验证图片密钥；请确认登录的是所选账号，必要时以管理员身份运行本工具",
    )
