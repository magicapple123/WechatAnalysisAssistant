"""Account-isolated, SSRF-resistant resolver for WeChat custom stickers.

Raw CDN addresses and encryption material stay on the backend. The browser
only receives local proxy URLs created by :mod:`backend.api`.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

from Crypto.Cipher import AES

from .app_paths import app_data_path
from .moments import (
    DownloadedMedia,
    MomentsMediaDownloadError,
    SafeMomentsMediaDownloader,
    _PinnedHTTPSConnection,
    _REDIRECT_STATUSES,
)


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SPECIAL_CACHE_VERSION = 3
_SPECIAL_CONTAINER_MAGICS = (b"wxgf", b"wxam")
_SPECIAL_MAX_TOTAL_PIXELS = 80_000_000
_SPECIAL_DECODE_SECONDS = 15.0
_SPECIAL_MAX_PACKETS = 8_000
_SPECIAL_MAX_PARTITIONS = 1_200
_SPECIAL_MAX_CONTAINER_BYTES = 32 * 1024 * 1024
_SPECIAL_DECODE_SEMAPHORE = threading.BoundedSemaphore(value=2)
_PILLOW_SPECIAL_FORMATS = {
    "AVIF",
    "BMP",
    "DIB",
    "HEIC",
    "HEIF",
    "ICO",
    "JPEG2000",
    "TIFF",
}
_HEIF_BRANDS = {
    b"avif",
    b"avis",
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"mif1",
    b"msf1",
}
_AVIF_BRANDS = {b"avif", b"avis"}
_ALLOWED_STICKER_DOMAINS = (
    "emoji.qpic.cn",
    "mmbiz.qpic.cn",
    "mmsns.qpic.cn",
    "shmmsns.qpic.cn",
    "szmmsns.qpic.cn",
    "res.wx.qq.com",
    "vweixinf.tc.qq.com",
    "wxapp.tc.qq.com",
    "weixin.qq.com",
    "weixincdn.com",
)
# This historical WeChat emoticon CDN still publishes ``http://`` URLs and
# does not present a certificate valid for its hostname on port 443.  Keep the
# exception exact (no subdomains) and require one unambiguous ``m=<md5>``
# integrity contract before any plaintext request is made.
_PLAINTEXT_STICKER_DOMAINS = frozenset({"vweixinf.tc.qq.com"})


class StickerResolutionError(Exception):
    """A sticker cannot be safely resolved for local display."""

    def __init__(self, message: str, status_code: int = 404):
        super().__init__(message)
        self.status_code = int(status_code)


@dataclass(frozen=True)
class ResolvedSticker:
    data: bytes
    mime_type: str
    extension: str
    md5: str


@dataclass(frozen=True)
class _StickerCandidate:
    """One private resource candidate and its independent integrity contract."""

    kind: str
    url: str
    encrypted: bool
    decoded_md5: str
    raw_md5: str
    persist: bool = True
    allow_direct_fallback: bool = False


def _unique_url_md5(url: object) -> str:
    """Return a trustworthy ``m`` query digest only when it is unambiguous."""
    value = str(url or "").strip()
    if not value or len(value) > 4096:
        return ""
    try:
        matches = [
            str(item).strip().lower()
            for name, item in parse_qsl(
                urlsplit(value).query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=128,
            )
            if str(name).lower() == "m"
        ]
    except (TypeError, ValueError, UnicodeError):
        return ""
    if len(matches) != 1 or not _MD5_RE.fullmatch(matches[0]):
        return ""
    return matches[0]


def _annex_b_nal_headers(
    data: bytes,
    *,
    max_headers: int = _SPECIAL_MAX_PACKETS,
) -> list[tuple[int, int]]:
    """Return ``(start_offset, nal_unit_type)`` pairs from an Annex-B stream.

    WeChat files have been observed with both three- and four-byte start codes,
    and the second HEVC header byte is not guaranteed to be ``0x01``.  Parsing
    the NAL unit type is therefore safer than searching for one exact byte
    signature.
    """
    header_limit = max(1, int(max_headers))
    headers: list[tuple[int, int]] = []
    offset = 0
    size = len(data)
    while offset + 4 <= size:
        four = data.find(b"\x00\x00\x00\x01", offset)
        three = data.find(b"\x00\x00\x01", offset)
        if four < 0 and three < 0:
            break
        if four >= 0 and (three < 0 or four <= three):
            start, prefix_size = four, 4
        else:
            start, prefix_size = three, 3
        header_offset = start + prefix_size
        if header_offset >= size:
            break
        if len(headers) >= header_limit:
            raise StickerResolutionError("特殊表情数据包数量超过安全限制", 422)
        headers.append((start, (data[header_offset] >> 1) & 0x3F))
        offset = header_offset + 1
    return headers


def _annex_b_prefix_size_at(data: bytes, offset: int) -> int:
    if data.startswith(b"\x00\x00\x00\x01", offset):
        return 4
    if data.startswith(b"\x00\x00\x01", offset):
        return 3
    return 0


def _parse_wxgf_partitions(data: bytes) -> list[bytes]:
    """Parse length-prefixed HEVC partitions from a WXGF/WXAM container.

    WeChat stores a big-endian partition length in the four bytes immediately
    before the partition's first Annex-B start code.  A real HEVC partition
    itself contains many further start codes; accepted ranges are therefore
    kept ordered and non-overlapping.  Gaps and a short container trailer are
    allowed because real WXGF variants can include metadata between streams.
    """
    if not isinstance(data, bytes) or data[:4].lower() not in _SPECIAL_CONTAINER_MAGICS:
        return []
    if len(data) > _SPECIAL_MAX_CONTAINER_BYTES:
        raise StickerResolutionError("WXGF/WXAM 容器超过安全大小限制", 422)

    declared_header_length = int(data[4]) if len(data) > 4 else 0
    scan_start = (
        declared_header_length
        if 5 <= declared_header_length < len(data)
        else 4
    )
    # Do not mix prefix widths in one pass.  Every four-byte start code also
    # contains a three-byte sequence at +1, and compressed partition bytes can
    # contain further accidental sequences.  WeChat/chatlog semantics prefer
    # a complete four-byte pass and only fall back to three bytes when it found
    # no valid partition.  A valid hit advances over its declared payload.
    scanned_candidates = 0
    for pattern in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        partitions: list[bytes] = []
        search_offset = scan_start
        prefix_size = len(pattern)
        while search_offset < len(data):
            stream_start = data.find(pattern, search_offset)
            if stream_start < 0:
                break
            scanned_candidates += 1
            if scanned_candidates > _SPECIAL_MAX_PACKETS:
                raise StickerResolutionError(
                    "WXGF/WXAM 分区候选数量超过安全限制", 422
                )
            if stream_start < 4:
                search_offset = stream_start + 1
                continue
            declared_length = int.from_bytes(
                data[stream_start - 4:stream_start], "big"
            )
            stream_end = stream_start + declared_length
            if (
                declared_length < prefix_size + 2
                or stream_end > len(data)
            ):
                search_offset = stream_start + 1
                continue
            if len(partitions) >= _SPECIAL_MAX_PARTITIONS:
                raise StickerResolutionError(
                    "WXGF/WXAM 分区数量超过安全限制", 422
                )
            partitions.append(data[stream_start:stream_end])
            search_offset = stream_end
        if partitions:
            return partitions
    return []


def _wxgf_partitions_form_dual_stream(partitions: list[bytes]) -> bool:
    """Return whether even/odd partitions are independently decodable streams.

    A WXGF file may split one HEVC stream into several length-prefixed chunks;
    only the first chunk then contains VPS/SPS/PPS data.  Treating every
    multi-partition file as alternating mask/colour video corrupts that common
    shape.  A real dual stream must carry independent SPS/PPS parameter sets
    on both the even (mask) and odd (colour) sides.
    """
    if len(partitions) < 2:
        return False
    parameter_types = (set(), set())
    remaining_headers = _SPECIAL_MAX_PACKETS
    for index, partition in enumerate(partitions):
        if remaining_headers <= 0:
            raise StickerResolutionError(
                "WXGF/WXAM 数据包数量超过安全限制", 422
            )
        headers = _annex_b_nal_headers(
            partition,
            max_headers=remaining_headers,
        )
        remaining_headers -= len(headers)
        parameter_types[index % 2].update(
            unit_type for _, unit_type in headers if unit_type in (32, 33, 34)
        )
        if all({33, 34}.issubset(types) for types in parameter_types):
            return True
    return False


def _extract_hevc_annex_b(data: bytes) -> bytes:
    """Extract the browser-incompatible HEVC stream from WXGF/WXAM data."""
    if not isinstance(data, bytes) or len(data) < 8:
        raise StickerResolutionError("特殊表情文件过短或已损坏", 422)
    wrapped = data[:4].lower() in _SPECIAL_CONTAINER_MAGICS
    partitions = _parse_wxgf_partitions(data) if wrapped else []
    if partitions:
        # Static WXGF variants can still use a length-prefixed partition.  The
        # largest partition is the colour stream and is the safest fallback.
        return max(partitions, key=len)
    headers = _annex_b_nal_headers(data)
    vps_offsets = [offset for offset, unit_type in headers if unit_type == 32]
    sps_offsets = [offset for offset, unit_type in headers if unit_type == 33]
    if not sps_offsets:
        raise StickerResolutionError("WXGF/WXAM 中没有可识别的 HEVC SPS", 422)
    usable_vps = [
        vps
        for vps in vps_offsets
        if any(sps >= vps for sps in sps_offsets)
    ]
    start = min(usable_vps) if usable_vps else min(sps_offsets)
    # Bare HEVC is accepted for older CDN rows, but only when its parameter
    # sets are near the beginning.  A wrapper magic permits an ICC/profile
    # block before the actual Annex-B stream.
    if not wrapped and start > 4096:
        raise StickerResolutionError("表情数据不是受支持的 WXGF/WXAM/HEVC 格式", 422)
    stream = data[start:]
    if len(stream) < 8:
        raise StickerResolutionError("WXGF/WXAM 中的 HEVC 数据不完整", 422)
    return stream


def _encode_browser_frames(
    images,
    durations: list[int],
    *,
    max_output_bytes: int,
) -> DownloadedMedia:
    """Encode one or more Pillow frames as WebP, with a static PNG fallback."""
    if not images:
        raise StickerResolutionError("特殊表情没有可编码的图片帧", 422)
    output_limit = max(1, int(max_output_bytes))
    first, rest = images[0], images[1:]
    webp_error: Optional[BaseException] = None
    for quality in (88, 76, 64):
        output = io.BytesIO()
        try:
            first.save(
                output,
                format="WEBP",
                save_all=bool(rest),
                append_images=rest,
                duration=durations,
                loop=0,
                quality=quality,
                method=4,
            )
            encoded = output.getvalue()
            if encoded and len(encoded) <= output_limit:
                return DownloadedMedia(encoded, "image/webp", ".webp")
        except (OSError, ValueError) as exc:
            webp_error = exc

    # PNG is universally understood by the target browser and avoids a lossy
    # JPEG-only fallback for static stickers with transparency or text.
    if not rest:
        output = io.BytesIO()
        try:
            first.save(output, format="PNG", optimize=True)
        except (OSError, ValueError) as exc:
            raise StickerResolutionError("特殊表情 PNG 转换失败", 422) from exc
        encoded = output.getvalue()
        if encoded and len(encoded) <= output_limit:
            return DownloadedMedia(encoded, "image/png", ".png")
    if webp_error is not None:
        raise StickerResolutionError(
            "当前 Pillow 缺少动态 WebP 编码支持，无法转换特殊表情", 503
        ) from webp_error
    raise StickerResolutionError("特殊表情转换结果超过大小限制", 422)


def _heif_brands(data: bytes) -> set[bytes]:
    if len(data) < 16 or data[4:8] != b"ftyp":
        return set()
    return {
        data[index:index + 4].lower()
        for index in range(8, min(len(data), 80) - 3, 4)
    }


def _is_heif_family(data: bytes) -> bool:
    return bool(_heif_brands(data) & _HEIF_BRANDS)


def _is_avif_family(data: bytes) -> bool:
    return bool(_heif_brands(data) & _AVIF_BRANDS)


def _looks_like_pillow_special(data: bytes) -> bool:
    return bool(
        data.startswith((b"BM", b"II*\x00", b"MM\x00*", b"\x00\x00\x01\x00"))
        or data.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n")
        or _is_heif_family(data)
    )


def _transcode_pillow_special(
    data: bytes,
    *,
    max_pixels: int,
    max_frames: int,
    max_output_bytes: int,
) -> Optional[DownloadedMedia]:
    """Transcode a Pillow-supported non-web format to WebP or static PNG."""
    looks_special = _looks_like_pillow_special(data)
    # Pillow 11.3+ ships AVIF support in its Windows wheels.  pillow-heif is
    # registered only for HEIC/HEIF, because its modern releases deliberately
    # leave AVIF to Pillow itself.
    if _is_heif_family(data) and not _is_avif_family(data):
        try:
            import pillow_heif
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise StickerResolutionError(
                "缺少 pillow-heif，无法显示 HEIC/HEIF 特殊表情", 503
            ) from exc
        try:
            pillow_heif.register_heif_opener()
        except Exception as exc:
            raise StickerResolutionError(
                "pillow-heif 初始化失败，无法显示 HEIC/HEIF 特殊表情", 503
            ) from exc

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise StickerResolutionError("缺少 Pillow，无法转换特殊表情", 503) from exc

    images = []
    durations: list[int] = []
    pixel_limit = max(1, int(max_pixels))
    frame_limit = max(1, int(max_frames))
    total_limit = min(_SPECIAL_MAX_TOTAL_PIXELS, pixel_limit * frame_limit)
    total_pixels = 0
    _acquire_special_decoder()
    deadline = time.monotonic() + _SPECIAL_DECODE_SECONDS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                source = Image.open(io.BytesIO(data))
            except Exception as exc:
                if looks_special:
                    raise StickerResolutionError("特殊表情图片结构无效或已损坏", 422) from exc
                return None
            with source:
                image_format = str(source.format or "").upper()
                if image_format not in _PILLOW_SPECIAL_FORMATS:
                    return None
                try:
                    declared_frames = int(getattr(source, "n_frames", 1) or 1)
                except (TypeError, ValueError):
                    declared_frames = 1
                if declared_frames < 1 or declared_frames > frame_limit:
                    raise StickerResolutionError("特殊表情帧数超过安全限制", 422)

                for index in range(declared_frames):
                    if time.monotonic() > deadline:
                        raise StickerResolutionError("特殊表情解码超过安全时限", 422)
                    source.seek(index)
                    width, height = map(int, source.size)
                    pixels = width * height
                    if width <= 0 or height <= 0 or pixels > pixel_limit:
                        raise StickerResolutionError("特殊表情像素数量超过安全限制", 422)
                    total_pixels += pixels
                    if total_pixels > total_limit:
                        raise StickerResolutionError("特殊表情解码总像素量超过安全限制", 422)
                    frame = source.convert("RGBA")
                    if frame.size != (width, height):
                        frame.close()
                        raise StickerResolutionError("特殊表情解码后的帧尺寸异常", 422)
                    images.append(frame)
                    try:
                        duration = int(source.info.get("duration", 80) or 80)
                    except (TypeError, ValueError):
                        duration = 80
                    durations.append(max(20, min(1000, duration)))

        return _encode_browser_frames(
            images,
            durations,
            max_output_bytes=max_output_bytes,
        )
    except StickerResolutionError:
        raise
    except Exception as exc:
        raise StickerResolutionError("特殊表情图片转换失败", 422) from exc
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass
        _SPECIAL_DECODE_SEMAPHORE.release()


def _decode_hevc_frames(
    annex_b: bytes,
    *,
    max_pixels: int,
    max_frames: int,
    image_mode: str = "RGB",
    deadline: Optional[float] = None,
    max_total_pixels: Optional[int] = None,
    max_packets: int = _SPECIAL_MAX_PACKETS,
):
    """Decode a bounded Annex-B stream and return owned Pillow frames."""
    try:
        import av
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise StickerResolutionError(
            "缺少 PyAV/FFmpeg 解码组件，无法显示 WXGF/WXAM 特殊表情", 503
        ) from exc

    if image_mode not in {"RGB", "RGBA", "L"}:
        raise ValueError("unsupported HEVC output mode")
    if not isinstance(annex_b, bytes) or not annex_b:
        raise StickerResolutionError("WXGF/WXAM 中的 HEVC 数据为空", 422)

    pixel_limit = max(1, int(max_pixels))
    frame_limit = max(1, int(max_frames))
    packet_limit = max(1, int(max_packets))
    default_total_limit = min(_SPECIAL_MAX_TOTAL_PIXELS, pixel_limit * frame_limit)
    total_pixel_limit = max(
        1,
        min(
            default_total_limit,
            int(max_total_pixels) if max_total_pixels is not None else default_total_limit,
        ),
    )
    images = []
    timestamps_ms: list[Optional[float]] = []
    explicit_durations_ms: list[Optional[float]] = []
    decoded_pixels = 0
    packet_count = 0
    decode_deadline = (
        float(deadline)
        if deadline is not None
        else time.monotonic() + _SPECIAL_DECODE_SECONDS
    )
    succeeded = False

    def check_deadline() -> None:
        if time.monotonic() > decode_deadline:
            raise StickerResolutionError("特殊表情解码超过安全时限", 422)

    def consume(frames) -> None:
        nonlocal decoded_pixels
        for frame in frames:
            check_deadline()
            if len(images) >= frame_limit:
                raise StickerResolutionError("特殊表情帧数超过安全限制", 422)
            try:
                width, height = int(frame.width), int(frame.height)
            except (AttributeError, TypeError, ValueError) as exc:
                raise StickerResolutionError("特殊表情包含无效视频帧", 422) from exc
            pixels = width * height
            if width <= 0 or height <= 0 or pixels > pixel_limit:
                raise StickerResolutionError("特殊表情像素数量超过安全限制", 422)
            decoded_pixels += pixels
            if decoded_pixels > total_pixel_limit:
                raise StickerResolutionError("特殊表情解码总像素量超过安全限制", 422)

            source_image = frame.to_image()
            try:
                image = source_image.convert(image_mode)
            finally:
                try:
                    source_image.close()
                except Exception:
                    pass
            if image.size != (width, height):
                image.close()
                raise StickerResolutionError("特殊表情解码后的帧尺寸异常", 422)
            images.append(image)

            timestamp = None
            duration = None
            try:
                if frame.pts is not None and frame.time_base is not None:
                    timestamp = float(frame.pts * frame.time_base) * 1000.0
                if frame.duration is not None and frame.time_base is not None:
                    duration = float(frame.duration * frame.time_base) * 1000.0
            except (AttributeError, TypeError, ValueError, OverflowError):
                timestamp = None
                duration = None
            timestamps_ms.append(timestamp)
            explicit_durations_ms.append(duration)

    codec = None
    try:
        codec = av.CodecContext.create("hevc", "r")
        try:
            codec.thread_count = 1
        except Exception:
            pass
        for packet in codec.parse(annex_b):
            packet_count += 1
            if packet_count > packet_limit:
                raise StickerResolutionError("特殊表情数据包数量超过安全限制", 422)
            check_deadline()
            consume(codec.decode(packet))
        for packet in codec.parse(None):
            packet_count += 1
            if packet_count > packet_limit:
                raise StickerResolutionError("特殊表情数据包数量超过安全限制", 422)
            check_deadline()
            consume(codec.decode(packet))
        consume(codec.decode(None))
        if not images:
            raise StickerResolutionError("WXGF/WXAM 中没有可解码的图片帧", 422)

        deltas = [
            timestamps_ms[index + 1] - timestamps_ms[index]
            for index in range(len(timestamps_ms) - 1)
            if timestamps_ms[index] is not None
            and timestamps_ms[index + 1] is not None
            and 1 <= timestamps_ms[index + 1] - timestamps_ms[index] <= 2000
        ]
        fallback_duration = sorted(deltas)[len(deltas) // 2] if deltas else 80.0
        durations: list[int] = []
        for index, explicit in enumerate(explicit_durations_ms):
            candidate = explicit
            if not candidate or not 1 <= candidate <= 2000:
                if index + 1 < len(timestamps_ms):
                    current = timestamps_ms[index]
                    following = timestamps_ms[index + 1]
                    candidate = (
                        following - current
                        if current is not None and following is not None
                        else fallback_duration
                    )
                else:
                    candidate = fallback_duration
            durations.append(max(20, min(1000, int(round(candidate)))))

        succeeded = True
        return images, durations
    except StickerResolutionError:
        raise
    except Exception as exc:
        raise StickerResolutionError("WXGF/WXAM/HEVC 特殊表情解码失败", 422) from exc
    finally:
        if not succeeded:
            for image in images:
                try:
                    image.close()
                except Exception:
                    pass
        if codec is not None:
            try:
                codec.close()
            except Exception:
                pass


def _acquire_special_decoder() -> None:
    if not _SPECIAL_DECODE_SEMAPHORE.acquire(timeout=_SPECIAL_DECODE_SECONDS):
        raise StickerResolutionError("特殊表情解码任务繁忙，请稍后重试", 503)


def _decode_hevc_to_browser_image(
    annex_b: bytes,
    *,
    max_pixels: int,
    max_frames: int,
    max_output_bytes: int,
) -> DownloadedMedia:
    """Decode one HEVC stream and encode a browser-safe WebP/PNG image."""
    images = []
    _acquire_special_decoder()
    try:
        images, durations = _decode_hevc_frames(
            annex_b,
            max_pixels=max_pixels,
            max_frames=max_frames,
            deadline=time.monotonic() + _SPECIAL_DECODE_SECONDS,
        )
        return _encode_browser_frames(
            images,
            durations,
            max_output_bytes=max_output_bytes,
        )
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass
        _SPECIAL_DECODE_SEMAPHORE.release()


def _decode_wxgf_to_browser_image(
    data: bytes,
    *,
    max_pixels: int,
    max_frames: int,
    max_output_bytes: int,
) -> DownloadedMedia:
    """Decode static or dual-stream animated WXGF/WXAM content.

    Animated containers alternate mask and colour partitions.  Both HEVC
    streams are decoded in-process by PyAV, then each grayscale mask frame is
    installed as the alpha channel of the matching colour frame.
    """
    partitions = _parse_wxgf_partitions(data)
    if not _wxgf_partitions_form_dual_stream(partitions):
        # Multiple partitions are often consecutive chunks of one HEVC
        # animation.  Preserve their original order so later slice-only chunks
        # can reuse the first chunk's parameter sets and reference frames.
        single_stream = b"".join(partitions) if partitions else data
        return _decode_hevc_to_browser_image(
            _extract_hevc_annex_b(single_stream),
            max_pixels=max_pixels,
            max_frames=max_frames,
            max_output_bytes=max_output_bytes,
        )

    mask_stream = b"".join(partitions[0::2])
    colour_stream = b"".join(partitions[1::2])
    if not mask_stream or not colour_stream:
        raise StickerResolutionError("WXGF/WXAM 动画缺少遮罩或颜色数据", 422)
    # Later partitions may contain slices only, so validate parameter sets only
    # after concatenating all partitions belonging to the same logical stream.
    mask_stream = _extract_hevc_annex_b(mask_stream)
    colour_stream = _extract_hevc_annex_b(colour_stream)

    masks = []
    colours = []
    _acquire_special_decoder()
    try:
        common_deadline = time.monotonic() + _SPECIAL_DECODE_SECONDS
        total_pixel_limit = min(
            _SPECIAL_MAX_TOTAL_PIXELS,
            max(1, int(max_pixels)) * max(1, int(max_frames)),
        )
        per_stream_pixel_limit = max(1, total_pixel_limit // 2)
        per_stream_packet_limit = max(1, _SPECIAL_MAX_PACKETS // 2)
        masks, _ = _decode_hevc_frames(
            mask_stream,
            max_pixels=max_pixels,
            max_frames=max_frames,
            image_mode="L",
            deadline=common_deadline,
            max_total_pixels=per_stream_pixel_limit,
            max_packets=per_stream_packet_limit,
        )
        colours, durations = _decode_hevc_frames(
            colour_stream,
            max_pixels=max_pixels,
            max_frames=max_frames,
            image_mode="RGBA",
            deadline=common_deadline,
            max_total_pixels=per_stream_pixel_limit,
            max_packets=per_stream_packet_limit,
        )
        if len(masks) not in (len(colours), len(colours) + 1):
            raise StickerResolutionError("WXGF/WXAM 动画的遮罩与颜色帧数不一致", 422)
        # FFmpeg's alphamerge follows the colour stream as its primary
        # timeline.  Some real WXGF files end with one surplus mask frame;
        # it has no corresponding colour output and is intentionally ignored.
        for mask, colour in zip(masks[:len(colours)], colours):
            if mask.size != colour.size:
                raise StickerResolutionError("WXGF/WXAM 动画的遮罩与颜色尺寸不一致", 422)
            colour.putalpha(mask)
        return _encode_browser_frames(
            colours,
            durations,
            max_output_bytes=max_output_bytes,
        )
    finally:
        for image in masks + colours:
            try:
                image.close()
            except Exception:
                pass
        _SPECIAL_DECODE_SEMAPHORE.release()


def _normalise_sticker_payload(
    data: bytes,
    *,
    expected_md5: str,
    downloader,
) -> tuple[DownloadedMedia, bool, str]:
    """Validate the original bytes, then optionally transcode for the browser.

    The returned digest always belongs to the original CDN/decrypted payload,
    never to its WebP/PNG representation.
    """
    if not isinstance(data, bytes) or not data:
        raise StickerResolutionError("表情资源响应为空", 422)
    max_file_bytes = max(1, int(getattr(downloader, "max_file_bytes", 15 * 1024 * 1024)))
    if len(data) > max_file_bytes:
        raise StickerResolutionError("表情资源超过大小限制", 422)
    source_md5 = hashlib.md5(data).hexdigest()
    if expected_md5 and source_md5 != expected_md5:
        raise StickerResolutionError("表情完整性校验失败", 422)

    native_error: Optional[MomentsMediaDownloadError] = None
    try:
        mime_type, extension = downloader._detect_image(data)
        downloader._validate_image_safety(data)
        return DownloadedMedia(data, mime_type, extension), False, source_md5
    except MomentsMediaDownloadError as exc:
        native_error = exc

    headers = _annex_b_nal_headers(data)
    looks_hevc = data[:4].lower() in _SPECIAL_CONTAINER_MAGICS or any(
        offset <= 4096 and unit_type in (32, 33)
        for offset, unit_type in headers
    )
    if looks_hevc:
        decode_options = {
            "max_pixels": getattr(downloader, "max_pixels", 40_000_000),
            "max_frames": getattr(downloader, "max_frames", 600),
            "max_output_bytes": max_file_bytes,
        }
        if data[:4].lower() in _SPECIAL_CONTAINER_MAGICS:
            media = _decode_wxgf_to_browser_image(data, **decode_options)
        else:
            media = _decode_hevc_to_browser_image(
                _extract_hevc_annex_b(data),
                **decode_options,
            )
    else:
        media = _transcode_pillow_special(
            data,
            max_pixels=getattr(downloader, "max_pixels", 40_000_000),
            max_frames=getattr(downloader, "max_frames", 600),
            max_output_bytes=max_file_bytes,
        )
        if media is None:
            raise native_error or MomentsMediaDownloadError(
                "表情响应不是受支持的图片格式"
            )

    detected_mime, detected_extension = downloader._detect_image(media.data)
    downloader._validate_image_safety(media.data)
    if (detected_mime, detected_extension) != (media.mime_type, media.extension):
        raise StickerResolutionError("特殊表情转码结果格式校验失败", 422)
    return media, True, source_md5


class _StickerFlight:
    """One in-process single-flight result shared by concurrent callers."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[ResolvedSticker] = None
        self.error: Optional[BaseException] = None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to one pre-validated public IP while preserving the Host header."""

    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # pragma: no cover - covered with a fake socket
        self.sock = self._create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )


class SafeStickerDownloader(SafeMomentsMediaDownloader):
    """Reuse the project's pinned-DNS downloader with a sticker allow-list."""

    @staticmethod
    def _host_allowed(hostname: str) -> bool:
        host = str(hostname or "").lower().rstrip(".")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_STICKER_DOMAINS
        )

    def _validate_url(self, url: str) -> tuple[object, str, int, list[str]]:
        """Validate a sticker URL, including one tightly scoped legacy CDN.

        All ordinary HTTP URLs retain the parent downloader's HTTPS-upgrade
        behaviour.  Only the exact legacy emoticon host may use port 80, and
        only when its URL carries one unambiguous MD5 integrity contract.
        """
        try:
            parsed = urlsplit(str(url or "").strip())
            explicit_port = parsed.port
        except (TypeError, ValueError, UnicodeError) as exc:
            raise MomentsMediaDownloadError("表情 URL 无效") from exc

        hostname = (parsed.hostname or "").lower().rstrip(".")
        scheme = parsed.scheme.lower()
        if not hostname or parsed.username or parsed.password:
            raise MomentsMediaDownloadError("表情 URL 主机或端口无效")
        if not self._host_allowed(hostname):
            raise MomentsMediaDownloadError("表情 URL 不属于允许的微信 CDN 域名")

        if scheme == "http" and hostname in _PLAINTEXT_STICKER_DOMAINS:
            port = explicit_port or 80
            if port != 80 or not _unique_url_md5(url):
                raise MomentsMediaDownloadError(
                    "历史表情 URL 缺少有效完整性参数或端口无效"
                )
        elif scheme == "http":
            # Preserve the existing secure behaviour for every other host.
            if explicit_port not in (None, 443):
                raise MomentsMediaDownloadError("表情 URL 主机或端口无效")
            parsed = parsed._replace(scheme="https")
            port = 443
        elif scheme == "https":
            port = explicit_port or 443
            if port != 443:
                raise MomentsMediaDownloadError("表情 URL 主机或端口无效")
        else:
            raise MomentsMediaDownloadError("表情仅允许通过 HTTP/HTTPS 下载")

        return parsed, hostname, port, self._resolve_public_ips(hostname, port)

    @staticmethod
    def _open_pinned_connection(
        parsed: object,
        hostname: str,
        port: int,
        pinned_ip: str,
        timeout: float,
    ) -> http.client.HTTPConnection:
        if getattr(parsed, "scheme", "") == "http":
            return _PinnedHTTPConnection(
                hostname, port, pinned_ip, timeout
            )
        return _PinnedHTTPSConnection(
            hostname, port, pinned_ip, timeout
        )

    def download_encrypted(self, url: str) -> bytes:
        """Download a bounded encrypted blob without treating it as an image."""
        current_url = str(url or "").strip()
        for redirect_count in range(self.max_redirects + 1):
            parsed, hostname, port, addresses = self._validate_url(current_url)
            redirect_url = ""
            last_network_error: Optional[BaseException] = None
            for address in addresses:
                remaining = self._remaining_seconds()
                connection = self._open_pinned_connection(
                    parsed,
                    hostname,
                    port,
                    address,
                    min(self.timeout_seconds, remaining),
                )
                try:
                    target = parsed.path or "/"
                    if parsed.query:
                        target += "?" + parsed.query
                    connection.request(
                        "GET",
                        target,
                        headers={
                            "User-Agent": "Mozilla/5.0 WeChatAnalysisAssistant/1.0",
                            "Accept": "application/octet-stream,*/*;q=0.1",
                            "Referer": "https://weixin.qq.com/",
                            "Connection": "close",
                        },
                    )
                    response = connection.getresponse()
                    if response.status in _REDIRECT_STATUSES:
                        location = response.getheader("Location")
                        if not location or redirect_count >= self.max_redirects:
                            raise MomentsMediaDownloadError(
                                "表情资源重定向次数过多"
                            )
                        redirect_url = urljoin(current_url, location)
                        break
                    if response.status != 200:
                        raise MomentsMediaDownloadError(
                            f"表情资源服务器返回 HTTP {response.status}"
                        )
                    content_length = response.getheader("Content-Length")
                    if content_length:
                        try:
                            announced = int(content_length)
                        except ValueError:
                            announced = 0
                        if announced < 0 or announced > self.max_file_bytes:
                            raise MomentsMediaDownloadError(
                                "表情资源超过大小限制"
                            )
                        if self.total_downloaded + announced > self.max_total_bytes:
                            raise MomentsMediaDownloadError(
                                "表情下载总量超过限制"
                            )

                    data = bytearray()
                    while True:
                        remaining = self._remaining_seconds()
                        if connection.sock is not None:
                            connection.sock.settimeout(
                                min(self.timeout_seconds, remaining)
                            )
                        chunk = response.read(
                            min(
                                64 * 1024,
                                self.max_file_bytes + 1 - len(data),
                            )
                        )
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) > self.max_file_bytes:
                            raise MomentsMediaDownloadError(
                                "表情资源超过大小限制"
                            )
                        if self.total_downloaded + len(data) > self.max_total_bytes:
                            raise MomentsMediaDownloadError(
                                "表情下载总量超过限制"
                            )
                    if not data:
                        raise MomentsMediaDownloadError("表情资源响应为空")
                    payload = bytes(data)
                    if (
                        getattr(parsed, "scheme", "") == "http"
                        and hashlib.md5(payload).hexdigest()
                        != _unique_url_md5(current_url)
                    ):
                        raise StickerResolutionError(
                            "历史表情下载数据完整性校验失败", 422
                        )
                    self.total_downloaded += len(data)
                    return payload
                except MomentsMediaDownloadError:
                    raise
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    # Some DNS answers point at temporarily unreachable CDN
                    # edges. Try the remaining already-validated public IPs;
                    # never re-resolve between validation and connection.
                    last_network_error = exc
                finally:
                    connection.close()

            if redirect_url:
                current_url = redirect_url
                continue
            raise MomentsMediaDownloadError(
                "表情资源下载失败"
            ) from last_network_error
        raise MomentsMediaDownloadError("表情资源重定向次数过多")


    # Direct special-format resources need the same bounded, DNS-pinned raw
    # download path as encrypted blobs. Keep the old name for ciphertext.
    download_blob = download_encrypted


class StickerService:
    """Resolve and cache one account's custom stickers."""

    def __init__(
        self,
        account_id: str,
        *,
        cache_root: Optional[Path | str] = None,
        downloader: Optional[SafeStickerDownloader] = None,
        downloader_factory: Optional[Callable[[], SafeStickerDownloader]] = None,
        emoticon_db_path: Optional[Path | str] = None,
    ) -> None:
        account = str(account_id or "").strip()
        if not account:
            raise StickerResolutionError("请先选择微信账号", 400)
        if cache_root is None:
            cache_root = app_data_path("stickers")
        account_hash = hashlib.sha256(account.encode("utf-8")).hexdigest()[:24]
        self.cache_dir = Path(cache_root) / account_hash
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.emoticon_db_path = (
            Path(emoticon_db_path) if emoticon_db_path else None
        )
        self._database_lookup_cache: dict[str, dict[str, str]] = {}
        if downloader is not None and downloader_factory is not None:
            raise ValueError("downloader 与 downloader_factory 不能同时设置")
        self._downloader_options = {
            "timeout_seconds": 12,
            "max_file_bytes": 15 * 1024 * 1024,
            "max_total_bytes": 30 * 1024 * 1024,
            # Legacy sticker CDN responses can be deliberately throttled; a
            # real 670 KiB GIF may take about 36 seconds while making steady
            # progress. Socket reads remain capped independently.
            "max_total_seconds": 90,
            "max_pixels": 40_000_000,
            "max_frames": 600,
        }
        self._shared_downloader_lock = threading.Lock()
        self._uses_shared_downloader = downloader is not None
        if downloader_factory is not None:
            self._downloader_factory = downloader_factory
        elif downloader is not None:
            self._downloader_factory = lambda: downloader
        else:
            self._downloader_factory = lambda: SafeStickerDownloader(
                **self._downloader_options
            )
        self._flights_lock = threading.Lock()
        self._flights: dict[str, _StickerFlight] = {}

    @staticmethod
    def _normalize_md5(value: object) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if _MD5_RE.fullmatch(candidate) else ""

    @staticmethod
    def _source_urls(source: Mapping[str, object]) -> list[str]:
        values = []
        for key in ("cdn_url", "encrypt_url", "extern_url", "thumb_url"):
            value = str(source.get(key) or "").strip()
            if value and len(value) <= 4096 and value not in values:
                values.append(value)
        return values

    def _resource_candidates(
        self,
        source: Mapping[str, object],
        logical_md5: str,
    ) -> list[_StickerCandidate]:
        """Build quality-ordered candidates without exposing private fields."""
        extern_md5 = self._normalize_md5(source.get("extern_md5"))
        definitions = (
            ("cdn", "cdn_url", False, logical_md5, True, False),
            ("encrypt", "encrypt_url", True, logical_md5, True, False),
            ("extern", "extern_url", True, extern_md5, True, True),
            # A thumbnail must never become a persistent substitute for a main
            # resource that may become reachable on the next request.
            ("thumb", "thumb_url", False, "", False, False),
        )
        candidates = []
        for kind, field, encrypted, decoded_md5, persist, direct_fallback in definitions:
            url = str(source.get(field) or "").strip()
            if not url or len(url) > 4096:
                continue
            raw_md5 = _unique_url_md5(url)
            expected_decoded = decoded_md5
            if not encrypted and not expected_decoded:
                expected_decoded = raw_md5
            candidates.append(_StickerCandidate(
                kind=kind,
                url=url,
                encrypted=encrypted,
                decoded_md5=expected_decoded,
                raw_md5=raw_md5,
                persist=persist,
                allow_direct_fallback=direct_fallback,
            ))
        return candidates

    def _cache_key(self, source: Mapping[str, object]) -> str:
        md5 = self._normalize_md5(source.get("md5"))
        if md5:
            return md5
        urls = self._source_urls(source)
        identity_url = urls[0] if urls else ""
        if not identity_url:
            return ""
        identity = identity_url + "\0" + str(source.get("aes_key") or "").strip()
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _quoted_identifier(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'

    @staticmethod
    def _find_table(
        connection: sqlite3.Connection, *candidates: str
    ) -> Optional[str]:
        wanted = {str(value).lower() for value in candidates}
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return next(
            (str(row[0]) for row in rows if str(row[0]).lower() in wanted),
            None,
        )

    @classmethod
    def _columns(
        cls, connection: sqlite3.Connection, table: str
    ) -> dict[str, str]:
        return {
            str(row[1]).lower(): str(row[1])
            for row in connection.execute(
                f"PRAGMA table_info({cls._quoted_identifier(table)})"
            ).fetchall()
        }

    def _lookup_from_database(self, md5: str) -> dict[str, str]:
        """Resolve a sticker MD5 from the current account's 4.x emoticon DB."""
        if md5 in self._database_lookup_cache:
            return dict(self._database_lookup_cache[md5])
        result: dict[str, str] = {}
        database = self.emoticon_db_path
        if not md5 or database is None or not database.is_file():
            self._database_lookup_cache[md5] = result
            return result

        connection = None
        try:
            uri = database.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            non_store = self._find_table(
                connection, "kNonStoreEmoticonTable"
            )
            if non_store:
                columns = self._columns(connection, non_store)

                def choose(*names: str) -> Optional[str]:
                    return next(
                        (columns[name] for name in names if name in columns),
                        None,
                    )

                md5_column = choose("md5", "md5_")
                selected = {
                    "aes_key": choose("aes_key", "aeskey"),
                    "cdn_url": choose("cdn_url", "cdnurl"),
                    "encrypt_url": choose("encrypt_url", "encrypturl"),
                    "extern_url": choose("extern_url", "externurl"),
                    "extern_md5": choose("extern_md5", "externmd5"),
                    "thumb_url": choose("thumb_url", "thumburl"),
                    "product_id": choose("product_id", "productid"),
                }
                if md5_column:
                    fields = [
                        (name, column)
                        for name, column in selected.items()
                        if column is not None
                    ]
                    if fields:
                        row = connection.execute(
                            f"SELECT {', '.join(self._quoted_identifier(column) for _, column in fields)} "
                            f"FROM {self._quoted_identifier(non_store)} "
                            f"WHERE lower(CAST({self._quoted_identifier(md5_column)} AS TEXT))=? "
                            "LIMIT 1",
                            (md5,),
                        ).fetchone()
                        if row:
                            for index, (name, _) in enumerate(fields):
                                value = str(row[index] or "").strip()
                                limit = 4096 if name.endswith("_url") else 512
                                if value:
                                    result[name] = value[:limit]

            # Store-package rows sometimes omit a direct URL. Reuse a template
            # from the same package, replacing only its explicit m=<md5> query.
            if not result.get("cdn_url"):
                store = self._find_table(
                    connection, "kStoreEmoticonFilesTable"
                )
                if store and non_store:
                    store_columns = self._columns(connection, store)
                    non_store_columns = self._columns(connection, non_store)
                    store_md5 = store_columns.get("md5_") or store_columns.get("md5")
                    store_package = (
                        store_columns.get("package_id_")
                        or store_columns.get("package_id")
                    )
                    non_store_package = (
                        non_store_columns.get("product_id")
                        or non_store_columns.get("productid")
                    )
                    non_store_url = (
                        non_store_columns.get("cdn_url")
                        or non_store_columns.get("cdnurl")
                    )
                    if all((
                        store_md5,
                        store_package,
                        non_store_package,
                        non_store_url,
                    )):
                        package_row = connection.execute(
                            f"SELECT {self._quoted_identifier(store_package)} "
                            f"FROM {self._quoted_identifier(store)} "
                            f"WHERE lower(CAST({self._quoted_identifier(store_md5)} AS TEXT))=? "
                            "LIMIT 1",
                            (md5,),
                        ).fetchone()
                        if package_row and package_row[0]:
                            template_row = connection.execute(
                                f"SELECT {self._quoted_identifier(non_store_url)} "
                                f"FROM {self._quoted_identifier(non_store)} "
                                f"WHERE CAST({self._quoted_identifier(non_store_package)} AS TEXT)=? "
                                f"AND length(COALESCE({self._quoted_identifier(non_store_url)}, '')) > 0 "
                                "LIMIT 1",
                                (str(package_row[0]),),
                            ).fetchone()
                            if template_row and template_row[0]:
                                template = str(template_row[0])[:4096]
                                constructed, substitutions = re.subn(
                                    r"([?&]m=)[0-9a-fA-F]{32}(?=&|$)",
                                    rf"\g<1>{md5}",
                                    template,
                                    count=1,
                                )
                                if substitutions:
                                    result["cdn_url"] = constructed
        except (OSError, sqlite3.Error, ValueError):
            result = {}
        finally:
            if connection is not None:
                connection.close()
        self._database_lookup_cache[md5] = dict(result)
        return result

    def _read_cache(
        self,
        key: str,
        expected_md5: str,
        downloader: SafeStickerDownloader,
    ) -> Optional[ResolvedSticker]:
        manifest_path = self.cache_dir / f"{key}.transcoded.json"
        try:
            manifest_raw = manifest_path.read_bytes()
            if len(manifest_raw) > 4096:
                raise ValueError("manifest too large")
            manifest = json.loads(manifest_raw.decode("utf-8"))
            logical_md5 = self._normalize_md5(manifest.get("logical_md5"))
            source_md5 = self._normalize_md5(manifest.get("source_md5"))
            source_kind = str(manifest.get("source_kind") or "").lower()
            output_sha256 = str(manifest.get("output_sha256") or "").lower()
            extension = str(manifest.get("extension") or "").lower()
            if (
                int(manifest.get("version", 0)) != _SPECIAL_CACHE_VERSION
                or not logical_md5
                or not source_md5
                or (expected_md5 and logical_md5 != expected_md5)
                or source_kind not in {
                    "cdn", "encrypt", "extern", "extern_direct", "transcoded"
                }
                or not re.fullmatch(r"[0-9a-f]{64}", output_sha256)
                or extension not in (".gif", ".webp", ".png", ".jpg")
            ):
                raise ValueError("invalid manifest")
            data = (self.cache_dir / f"{key}.transcoded{extension}").read_bytes()
            if hashlib.sha256(data).hexdigest() != output_sha256:
                raise ValueError("transcoded digest mismatch")
            mime_type, detected_extension = downloader._detect_image(data)
            downloader._validate_image_safety(data)
            if detected_extension != extension:
                raise ValueError("transcoded extension mismatch")
            return ResolvedSticker(
                data,
                mime_type,
                detected_extension,
                expected_md5 or logical_md5,
            )
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            MomentsMediaDownloadError,
        ):
            pass

        for extension in (".gif", ".webp", ".png", ".jpg"):
            path = self.cache_dir / f"{key}{extension}"
            try:
                data = path.read_bytes()
                mime_type, detected_extension = downloader._detect_image(data)
                downloader._validate_image_safety(data)
                if expected_md5 and hashlib.md5(data).hexdigest() != expected_md5:
                    continue
                return ResolvedSticker(
                    data,
                    mime_type,
                    detected_extension,
                    expected_md5 or hashlib.md5(data).hexdigest(),
                )
            except (OSError, MomentsMediaDownloadError):
                continue
        return None

    def _store(self, key: str, media: DownloadedMedia) -> None:
        target = self.cache_dir / f"{key}{media.extension}"
        self._atomic_write(target, media.data)

    def _atomic_write(self, target: Path, data: bytes) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix="sticker_", suffix=".tmp", dir=str(self.cache_dir)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        finally:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass

    def _store_transcoded(
        self,
        key: str,
        media: DownloadedMedia,
        source_md5: str,
        *,
        logical_md5: str = "",
        source_kind: str = "transcoded",
    ) -> None:
        normalized_source = self._normalize_md5(source_md5)
        normalized_logical = self._normalize_md5(logical_md5) or normalized_source
        normalized_kind = str(source_kind or "").strip().lower()
        if (
            media.extension not in (".gif", ".webp", ".png", ".jpg")
            or not normalized_source
            or not normalized_logical
            or normalized_kind not in {
                "cdn", "encrypt", "extern", "extern_direct", "transcoded"
            }
        ):
            raise StickerResolutionError("特殊表情缓存元数据无效", 500)
        output_path = self.cache_dir / f"{key}.transcoded{media.extension}"
        manifest_path = self.cache_dir / f"{key}.transcoded.json"
        manifest = {
            "version": _SPECIAL_CACHE_VERSION,
            "logical_md5": normalized_logical,
            "source_md5": normalized_source,
            "source_kind": normalized_kind,
            "output_sha256": hashlib.sha256(media.data).hexdigest(),
            "extension": media.extension,
        }
        self._atomic_write(output_path, media.data)
        self._atomic_write(
            manifest_path,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    @staticmethod
    def _decrypt_aes_cbc(data: bytes, value: object) -> bytes:
        raw_key = str(value or "").strip()
        try:
            key = bytes.fromhex(raw_key)
        except ValueError:
            key = raw_key.encode("utf-8", errors="strict")
        if len(key) not in (16, 24, 32):
            ascii_key = raw_key.encode("utf-8", errors="strict")
            if len(ascii_key) in (16, 24, 32):
                key = ascii_key
        if len(key) not in (16, 24, 32) or len(data) == 0 or len(data) % 16:
            raise StickerResolutionError("表情加密参数或数据长度无效", 422)
        try:
            decrypted = AES.new(key, AES.MODE_CBC, iv=key[:16]).decrypt(data)
        except (TypeError, ValueError) as exc:
            raise StickerResolutionError("表情 AES 解密失败", 422) from exc
        if decrypted:
            padding = decrypted[-1]
            if (
                1 <= padding <= 16
                and decrypted[-padding:] == bytes([padding]) * padding
            ):
                decrypted = decrypted[:-padding]
        if not decrypted:
            raise StickerResolutionError("表情 AES 解密结果为空", 422)
        return decrypted

    def _resolve_with_downloader(
        self,
        effective_source: Mapping[str, object],
        key: str,
        expected_md5: str,
        downloader: SafeStickerDownloader,
    ) -> ResolvedSticker:
        cached = self._read_cache(key, expected_md5, downloader)
        if cached:
            return cached

        # One bounded budget covers every quality-ordered candidate.
        downloader.reset_budget()
        candidates = self._resource_candidates(effective_source, expected_md5)
        aes_key = str(effective_source.get("aes_key") or "").strip()
        last_error: Optional[BaseException] = None
        best_resolution_error: Optional[StickerResolutionError] = None

        def download_raw(candidate: _StickerCandidate) -> bytes:
            if candidate.encrypted and (
                aes_key or not candidate.allow_direct_fallback
            ):
                return downloader.download_encrypted(candidate.url)
            download_blob = getattr(downloader, "download_blob", None)
            if callable(download_blob):
                return download_blob(candidate.url)
            downloaded = downloader.download(candidate.url)
            return downloaded.data

        def validate_raw(data: bytes, candidate: _StickerCandidate) -> None:
            if (
                candidate.raw_md5
                and hashlib.md5(data).hexdigest() != candidate.raw_md5
            ):
                raise StickerResolutionError("表情下载数据完整性校验失败", 422)

        def finish(
            candidate: _StickerCandidate,
            media: DownloadedMedia,
            transcoded: bool,
            source_md5: str,
            *,
            source_kind: Optional[str] = None,
        ) -> ResolvedSticker:
            logical_md5 = expected_md5 or source_md5
            if candidate.persist:
                if transcoded or source_md5 != logical_md5:
                    self._store_transcoded(
                        key,
                        media,
                        source_md5,
                        logical_md5=logical_md5,
                        source_kind=source_kind or candidate.kind,
                    )
                else:
                    self._store(key, media)
            return ResolvedSticker(
                media.data,
                media.mime_type,
                media.extension,
                logical_md5,
            )

        for candidate in candidates:
            # encrypt_url has no safe interpretation without its AES key.
            if candidate.kind == "encrypt" and not aes_key:
                continue
            try:
                raw_data = download_raw(candidate)
                validate_raw(raw_data, candidate)
            except (MomentsMediaDownloadError, StickerResolutionError) as exc:
                last_error = exc
                if isinstance(exc, StickerResolutionError):
                    best_resolution_error = exc
                continue

            if candidate.encrypted and aes_key:
                aes_error: Optional[BaseException] = None
                try:
                    decrypted = self._decrypt_aes_cbc(raw_data, aes_key)
                    media, transcoded, source_md5 = _normalise_sticker_payload(
                        decrypted,
                        expected_md5=candidate.decoded_md5,
                        downloader=downloader,
                    )
                    return finish(
                        candidate,
                        media,
                        transcoded,
                        source_md5,
                    )
                except (MomentsMediaDownloadError, StickerResolutionError) as exc:
                    aes_error = exc
                    last_error = exc
                    if isinstance(exc, StickerResolutionError):
                        best_resolution_error = exc
                if not candidate.allow_direct_fallback:
                    continue

                # Some historical 4.x records store an already-decoded image
                # in extern_url.  Try that interpretation only after the
                # authenticated AES/WXGF contract fails.
                try:
                    direct_md5 = candidate.raw_md5 or candidate.decoded_md5
                    media, transcoded, source_md5 = _normalise_sticker_payload(
                        raw_data,
                        expected_md5=direct_md5,
                        downloader=downloader,
                    )
                    return finish(
                        candidate,
                        media,
                        transcoded,
                        source_md5,
                        source_kind="extern_direct",
                    )
                except (MomentsMediaDownloadError, StickerResolutionError):
                    # Preserve the AES/decode error: it usually explains a
                    # missing codec or a corrupt modern extern payload better
                    # than treating its ciphertext as a direct image.
                    last_error = aes_error
                    continue

            try:
                direct_expected_md5 = candidate.decoded_md5
                if candidate.kind == "extern" and not aes_key:
                    direct_expected_md5 = (
                        candidate.raw_md5 or candidate.decoded_md5
                    )
                media, transcoded, source_md5 = _normalise_sticker_payload(
                    raw_data,
                    expected_md5=direct_expected_md5,
                    downloader=downloader,
                )
                return finish(
                    candidate,
                    media,
                    transcoded,
                    source_md5,
                    source_kind=(
                        "extern_direct"
                        if candidate.kind == "extern"
                        else None
                    ),
                )
            except (MomentsMediaDownloadError, StickerResolutionError) as exc:
                last_error = exc
                if isinstance(exc, StickerResolutionError):
                    best_resolution_error = exc

        if not candidates:
            raise StickerResolutionError("该表情没有可用的本地或安全下载资源", 404)
        if best_resolution_error is not None:
            raise best_resolution_error
        if isinstance(last_error, StickerResolutionError):
            raise last_error
        raise StickerResolutionError(
            "表情资源下载失败，请检查网络后重试", 502
        ) from last_error

    def _resolve_once(
        self,
        source: Mapping[str, object],
        key: str,
    ) -> ResolvedSticker:
        effective_source = dict(source)
        expected_md5 = self._normalize_md5(effective_source.get("md5"))
        if expected_md5:
            for name, value in self._lookup_from_database(expected_md5).items():
                if not effective_source.get(name):
                    effective_source[name] = value

        downloader = self._downloader_factory()
        if self._uses_shared_downloader:
            with self._shared_downloader_lock:
                return self._resolve_with_downloader(
                    effective_source, key, expected_md5, downloader
                )
        return self._resolve_with_downloader(
            effective_source, key, expected_md5, downloader
        )

    def resolve(self, source: Mapping[str, object]) -> ResolvedSticker:
        if not isinstance(source, Mapping):
            raise StickerResolutionError("表情资源信息无效", 404)
        source_copy = dict(source)
        key = self._cache_key(source_copy)
        if not key:
            raise StickerResolutionError("该表情没有可用的本地或安全下载资源", 404)

        with self._flights_lock:
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = _StickerFlight()
                self._flights[key] = flight

        if not leader:
            flight.event.wait()
            if flight.result is not None:
                return flight.result
            if isinstance(flight.error, StickerResolutionError):
                raise StickerResolutionError(
                    str(flight.error), flight.error.status_code
                ) from flight.error
            if flight.error is not None:
                raise flight.error
            raise StickerResolutionError("表情资源解析任务异常结束", 500)

        try:
            flight.result = self._resolve_once(source_copy, key)
            return flight.result
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            flight.event.set()
            with self._flights_lock:
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
