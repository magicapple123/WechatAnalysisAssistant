import base64
import hashlib
import io
import json
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from backend.moments import SafeMomentsMediaDownloader
from backend.sticker_service import (
    DownloadedMedia,
    StickerResolutionError,
    StickerService,
    _annex_b_nal_headers,
    _annex_b_prefix_size_at,
    _decode_hevc_to_browser_image,
    _decode_wxgf_to_browser_image,
    _extract_hevc_annex_b,
    _normalise_sticker_payload,
    _parse_wxgf_partitions,
    _transcode_pillow_special,
)


def make_image_bytes(image_format, *, frames=1):
    output = io.BytesIO()
    images = [
        Image.new("RGBA", (4, 3), (index * 80, 20, 255 - index * 80, 255))
        for index in range(frames)
    ]
    try:
        images[0].save(
            output,
            format=image_format,
            save_all=frames > 1,
            append_images=images[1:],
            duration=[40] * frames,
            loop=0,
        )
        return output.getvalue()
    finally:
        for image in images:
            image.close()


WEBP = make_image_bytes("WEBP")
HEVC_TWO_FRAME = base64.b64decode(
    "AAAAAUABDAH//wQIAAADAJ+oAAADAAAeugJAAAAAAUIBAQQIAAADAJ+oAAADAAAeo"
    "IhFluqvK5oCAAADAAIAAAMAFBAAAAABRAHBc8CJAAABKAGvePcEA/9PPf6W1D0nf"
    "gAAAAFAAQwB//8ECAAAAwCfqAAAAwAAHroCQAAAAAFCAQEECAAAAwCfqAAAAwAAHq"
    "CIRZbqryuaAgAAAwACAAADABQQAAAAAUQBwXPAiQAAASgBrx+A94TD/6CMf+VB/+x"
    "/wA=="
)


def make_wxgf(*partitions):
    return b"wxgf\x05" + b"".join(
        len(partition).to_bytes(4, "big") + partition
        for partition in partitions
    )


class BlobDownloader:
    max_file_bytes = 2 * 1024 * 1024
    max_pixels = 1_000_000
    max_frames = 30

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.resets = 0

    def reset_budget(self):
        self.resets += 1

    def download_blob(self, url):
        self.calls.append(url)
        return self.payload

    def download_encrypted(self, url):
        self.calls.append(url)
        return self.payload

    @staticmethod
    def _detect_image(data):
        return SafeMomentsMediaDownloader._detect_image(data)

    def _validate_image_safety(self, data):
        return SafeMomentsMediaDownloader._validate_image_safety(self, data)


class FakeFrame:
    def __init__(self, color, pts):
        self.width = 4
        self.height = 3
        self.pts = pts
        self.duration = 50
        self.time_base = Fraction(1, 1000)
        self.color = color

    def to_image(self):
        return Image.new("RGB", (self.width, self.height), self.color)


class FakeCodec:
    def __init__(self, frames):
        self.frames = frames
        self.decoded = False
        self.closed = False
        self.thread_count = 0

    def parse(self, data):
        return [object()] if data is not None and not self.decoded else []

    def decode(self, packet):
        if packet is not None and not self.decoded:
            self.decoded = True
            return self.frames
        return []

    def close(self):
        self.closed = True


class StickerSpecialFormatTests(unittest.TestCase):
    def test_extracts_wxgf_and_wxam_with_variable_annex_b_start_codes(self):
        vps = b"\x00\x00\x01" + bytes([(32 << 1), 0x02]) + b"vps"
        sps = b"\x00\x00\x00\x01" + bytes([(33 << 1), 0x05]) + b"sps"
        for magic in (b"wxgf", b"WXGF", b"wxam", b"WXAM"):
            with self.subTest(magic=magic):
                raw = magic + (b"profile" * 7) + vps + sps
                self.assertEqual(_extract_hevc_annex_b(raw), vps + sps)

    def test_rejects_wrapper_without_hevc_sequence_parameter_set(self):
        with self.assertRaises(StickerResolutionError) as raised:
            _extract_hevc_annex_b(b"wxgf" + b"\x00\x00\x00\x01\x40\x01bad")
        self.assertEqual(raised.exception.status_code, 422)

    def test_wxgf_length_prefixed_partitions_are_parsed_without_inner_nals(self):
        raw = make_wxgf(HEVC_TWO_FRAME, HEVC_TWO_FRAME)
        self.assertEqual(
            _parse_wxgf_partitions(raw),
            [HEVC_TWO_FRAME, HEVC_TWO_FRAME],
        )
        self.assertEqual(_extract_hevc_annex_b(raw), HEVC_TWO_FRAME)

    def test_wxgf_parser_allows_metadata_gaps_trailer_and_slice_only_parts(self):
        slice_only = b"\x00\x00\x00\x01\x26\x01slice"
        raw = (
            b"wxgf\x05"
            + len(HEVC_TWO_FRAME).to_bytes(4, "big")
            + HEVC_TWO_FRAME
            + b"meta"
            + len(HEVC_TWO_FRAME).to_bytes(4, "big")
            + HEVC_TWO_FRAME
            + b"gap"
            + len(slice_only).to_bytes(4, "big")
            + slice_only
            + b"more"
            + len(slice_only).to_bytes(4, "big")
            + slice_only
            + b"trailer"
        )
        self.assertEqual(
            _parse_wxgf_partitions(raw),
            [HEVC_TWO_FRAME, HEVC_TWO_FRAME, slice_only, slice_only],
        )

    def test_wxgf_partition_count_limit_is_enforced(self):
        tiny_sps_partition = b"\x00\x00\x00\x01\x42\x01x"
        raw = make_wxgf(*([tiny_sps_partition] * 1_201))
        with self.assertRaises(StickerResolutionError) as raised:
            _parse_wxgf_partitions(raw)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("分区数量", str(raised.exception))

    def test_wxgf_invalid_partition_candidate_scan_is_bounded(self):
        # Every marker has an invalid one-byte declared length.  The parser
        # must stop before an attacker can force millions of failed searches.
        raw = b"wxgf\x05" + (b"\x00\x00\x00\x01" * 8_002)
        with self.assertRaises(StickerResolutionError) as raised:
            _parse_wxgf_partitions(raw)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("候选数量", str(raised.exception))

    def test_odd_partitions_of_one_hevc_stream_are_joined_in_order(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV is not installed")
        headers = _annex_b_nal_headers(HEVC_TWO_FRAME)
        # Normalise every NAL to a four-byte Annex-B start code.  The fixture
        # itself uses three-byte start codes for the VCL units, while real
        # WXGF partition boundaries are located via their four-byte markers.
        units = []
        for index, (offset, _unit_type) in enumerate(headers):
            end = headers[index + 1][0] if index + 1 < len(headers) else len(HEVC_TWO_FRAME)
            prefix_size = _annex_b_prefix_size_at(HEVC_TWO_FRAME, offset)
            units.append(b"\x00\x00\x00\x01" + HEVC_TWO_FRAME[offset + prefix_size:end])

        raw = make_wxgf(
            b"".join(units[:3]),
            units[3],
            b"".join(units[4:]),
        )
        self.assertEqual(len(_parse_wxgf_partitions(raw)), 3)

        media = _decode_wxgf_to_browser_image(
            raw,
            max_pixels=1_000,
            max_frames=10,
            max_output_bytes=100_000,
        )

        with Image.open(io.BytesIO(media.data)) as decoded:
            self.assertEqual(decoded.size, (16, 16))
            self.assertEqual(decoded.n_frames, 2)

    def test_dual_stream_allows_one_surplus_trailing_mask_frame(self):
        slice_only = b"\x00\x00\x00\x01\x02\x01tail"
        raw = make_wxgf(HEVC_TWO_FRAME, HEVC_TWO_FRAME, slice_only)
        masks = [
            Image.new("L", (4, 3), value)
            for value in (32, 160, 255)
        ]
        colours = [
            Image.new("RGBA", (4, 3), colour)
            for colour in ((255, 0, 0, 255), (0, 255, 0, 255))
        ]
        with patch(
            "backend.sticker_service._decode_hevc_frames",
            side_effect=[
                (masks, [40, 40, 40]),
                (colours, [40, 40]),
            ],
        ):
            media = _decode_wxgf_to_browser_image(
                raw,
                max_pixels=1_000,
                max_frames=10,
                max_output_bytes=100_000,
            )

        with Image.open(io.BytesIO(media.data)) as decoded:
            self.assertEqual(decoded.n_frames, 2)

    def test_dual_stream_rejects_fewer_masks_than_colour_frames(self):
        raw = make_wxgf(HEVC_TWO_FRAME, HEVC_TWO_FRAME)
        masks = [Image.new("L", (4, 3), 128)]
        colours = [
            Image.new("RGBA", (4, 3), colour)
            for colour in ((255, 0, 0, 255), (0, 255, 0, 255))
        ]
        with patch(
            "backend.sticker_service._decode_hevc_frames",
            side_effect=[(masks, [40]), (colours, [40, 40])],
        ), self.assertRaises(StickerResolutionError) as raised:
            _decode_wxgf_to_browser_image(
                raw,
                max_pixels=1_000,
                max_frames=10,
                max_output_bytes=100_000,
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("帧数不一致", str(raised.exception))

    def test_pyav_frames_are_preserved_as_animated_webp(self):
        codec = FakeCodec([
            FakeFrame((255, 0, 0), 0),
            FakeFrame((0, 255, 0), 50),
        ])
        fake_av = SimpleNamespace(
            CodecContext=SimpleNamespace(create=lambda *_: codec)
        )
        with patch.dict(sys.modules, {"av": fake_av}):
            media = _decode_hevc_to_browser_image(
                b"annex-b",
                max_pixels=100,
                max_frames=4,
                max_output_bytes=100_000,
            )

        self.assertEqual((media.mime_type, media.extension), ("image/webp", ".webp"))
        with Image.open(io.BytesIO(media.data)) as decoded:
            self.assertEqual(decoded.n_frames, 2)
        self.assertTrue(codec.closed)
        self.assertEqual(codec.thread_count, 1)

    def test_real_pyav_decodes_embedded_two_frame_hevc_fixture(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV is not installed")
        # Two 16x16 intra frames generated as a raw Annex-B HEVC stream.  The
        # fixture exercises the actual bundled FFmpeg decoder, not the fake
        # codec used by the unit-level timing and limit tests above.
        media = _decode_hevc_to_browser_image(
            HEVC_TWO_FRAME,
            max_pixels=1_000,
            max_frames=10,
            max_output_bytes=100_000,
        )
        self.assertEqual((media.mime_type, media.extension), ("image/webp", ".webp"))
        with Image.open(io.BytesIO(media.data)) as decoded:
            self.assertEqual(decoded.size, (16, 16))
            self.assertEqual(decoded.n_frames, 2)

    def test_real_dual_stream_wxgf_preserves_mask_as_animated_alpha(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV is not installed")
        media = _decode_wxgf_to_browser_image(
            make_wxgf(HEVC_TWO_FRAME, HEVC_TWO_FRAME),
            max_pixels=1_000,
            max_frames=10,
            max_output_bytes=100_000,
        )
        self.assertEqual((media.mime_type, media.extension), ("image/webp", ".webp"))
        with Image.open(io.BytesIO(media.data)) as decoded:
            self.assertEqual(decoded.n_frames, 2)
            for index in range(decoded.n_frames):
                decoded.seek(index)
                alpha_extrema = decoded.convert("RGBA").getchannel("A").getextrema()
                self.assertLess(alpha_extrema[1], 255)

    def test_global_decode_limit_rejects_busy_work_without_starting_pyav(self):
        busy = Mock()
        busy.acquire.return_value = False
        with patch("backend.sticker_service._SPECIAL_DECODE_SEMAPHORE", busy):
            with self.assertRaises(StickerResolutionError) as raised:
                _decode_hevc_to_browser_image(
                    HEVC_TWO_FRAME,
                    max_pixels=1_000,
                    max_frames=10,
                    max_output_bytes=100_000,
                )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("繁忙", str(raised.exception))
        busy.release.assert_not_called()

    def test_pyav_frame_limit_is_enforced_before_encoding(self):
        codec = FakeCodec([
            FakeFrame((255, 0, 0), 0),
            FakeFrame((0, 255, 0), 50),
        ])
        fake_av = SimpleNamespace(
            CodecContext=SimpleNamespace(create=lambda *_: codec)
        )
        with patch.dict(sys.modules, {"av": fake_av}):
            with self.assertRaises(StickerResolutionError) as raised:
                _decode_hevc_to_browser_image(
                    b"annex-b",
                    max_pixels=100,
                    max_frames=1,
                    max_output_bytes=100_000,
                )
        self.assertIn("帧数", str(raised.exception))

    def test_annex_b_header_scan_has_a_packet_limit(self):
        excessive_stream = b"".join(
            b"\x00\x00\x01\x40\x01x" for _ in range(8_001)
        )
        with self.assertRaises(StickerResolutionError) as raised:
            _extract_hevc_annex_b(excessive_stream)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("数据包数量", str(raised.exception))

    def test_bmp_and_multiframe_tiff_are_transcoded_for_browser(self):
        downloader = BlobDownloader(b"")
        for image_format, frame_count in (("BMP", 1), ("TIFF", 2)):
            with self.subTest(image_format=image_format):
                raw = make_image_bytes(image_format, frames=frame_count)
                media, transcoded, original_md5 = _normalise_sticker_payload(
                    raw,
                    expected_md5=hashlib.md5(raw).hexdigest(),
                    downloader=downloader,
                )
                self.assertTrue(transcoded)
                self.assertEqual(original_md5, hashlib.md5(raw).hexdigest())
                self.assertIn(media.extension, (".webp", ".png"))
                with Image.open(io.BytesIO(media.data)) as decoded:
                    self.assertEqual(decoded.n_frames, frame_count)

    def test_pillow_pixel_limit_is_enforced(self):
        raw = make_image_bytes("BMP")
        with self.assertRaises(StickerResolutionError) as raised:
            _transcode_pillow_special(
                raw,
                max_pixels=1,
                max_frames=5,
                max_output_bytes=100_000,
            )
        self.assertIn("像素", str(raised.exception))

    def test_heif_opener_is_registered_lazily_when_available(self):
        heif = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"bad"
        register_heif = Mock()
        fake_plugin = SimpleNamespace(register_heif_opener=register_heif)
        with patch.dict(sys.modules, {"pillow_heif": fake_plugin}):
            with self.assertRaises(StickerResolutionError):
                _transcode_pillow_special(
                    heif,
                    max_pixels=100,
                    max_frames=5,
                    max_output_bytes=100_000,
                )
        register_heif.assert_called_once_with()

    def test_real_pillow_heif_and_avif_payloads_are_transcoded(self):
        try:
            import pillow_heif
        except ImportError:
            self.skipTest("pillow-heif is not installed")
        pillow_heif.register_heif_opener()

        downloader = BlobDownloader(b"")
        source_image = Image.new("RGBA", (8, 6), (240, 30, 90, 180))
        exercised = set()
        try:
            for image_format in ("HEIF", "AVIF"):
                output = io.BytesIO()
                try:
                    save_image = (
                        source_image.convert("RGB")
                        if image_format == "HEIF"
                        else source_image
                    )
                    try:
                        save_image.save(output, format=image_format, quality=80)
                    finally:
                        if save_image is not source_image:
                            save_image.close()
                except (KeyError, OSError, ValueError) as exc:
                    self.fail(f"declared dependency cannot encode {image_format}: {exc}")
                raw = output.getvalue()
                with self.subTest(image_format=image_format):
                    media, transcoded, source_md5 = _normalise_sticker_payload(
                        raw,
                        expected_md5=hashlib.md5(raw).hexdigest(),
                        downloader=downloader,
                    )
                    self.assertTrue(transcoded)
                    self.assertEqual(source_md5, hashlib.md5(raw).hexdigest())
                    self.assertIn(media.extension, (".webp", ".png"))
                    with Image.open(io.BytesIO(media.data)) as decoded:
                        self.assertEqual(decoded.size, source_image.size)
                    exercised.add(image_format)
        finally:
            source_image.close()
        self.assertEqual(exercised, {"HEIF", "AVIF"})

    def test_native_cache_without_declared_md5_returns_computed_digest(self):
        source = {"cdn_url": "https://emoji.qpic.cn/native.webp"}
        expected = hashlib.md5(WEBP).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            first_service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=BlobDownloader(WEBP),
            )
            first = first_service.resolve(source)
            self.assertEqual(first.md5, expected)

            cached_downloader = BlobDownloader(b"must-not-be-downloaded")
            restarted = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=cached_downloader,
            )
            cached = restarted.resolve(source)
            self.assertEqual(cached.md5, expected)
            self.assertEqual(cached_downloader.calls, [])

    def test_source_md5_is_checked_before_hevc_decoder(self):
        raw = (
            b"WXGFheader"
            b"\x00\x00\x00\x01\x40\x02vps"
            b"\x00\x00\x00\x01\x42\x02sps"
        )
        downloader = BlobDownloader(raw)
        with patch(
            "backend.sticker_service._decode_hevc_to_browser_image"
        ) as decode:
            with self.assertRaises(StickerResolutionError) as raised:
                _normalise_sticker_payload(
                    raw,
                    expected_md5=hashlib.md5(b"different").hexdigest(),
                    downloader=downloader,
                )
        self.assertEqual(raised.exception.status_code, 422)
        decode.assert_not_called()

    def test_transcoded_cache_sidecar_binds_source_md5_and_output_sha256(self):
        raw = (
            b"WXGFprofile"
            b"\x00\x00\x00\x01\x40\x02vps"
            b"\x00\x00\x00\x01\x42\x02sps"
        )
        original_md5 = hashlib.md5(raw).hexdigest()
        source = {
            "md5": original_md5,
            "cdn_url": "https://emoji.qpic.cn/special.wxgf",
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.sticker_service._decode_hevc_to_browser_image",
            return_value=DownloadedMedia(WEBP, "image/webp", ".webp"),
        ) as decode:
            downloader = BlobDownloader(raw)
            service = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            first = service.resolve(source)
            self.assertEqual(first.data, WEBP)
            self.assertEqual(first.md5, original_md5)
            self.assertNotEqual(hashlib.md5(WEBP).hexdigest(), original_md5)
            decode.assert_called_once()

            manifest_path = service.cache_dir / f"{original_md5}.transcoded.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["version"], 3)
            self.assertEqual(manifest["logical_md5"], original_md5)
            self.assertEqual(manifest["source_md5"], original_md5)
            self.assertEqual(manifest["source_kind"], "cdn")
            self.assertEqual(manifest["output_sha256"], hashlib.sha256(WEBP).hexdigest())

            restarted_downloader = BlobDownloader(b"must-not-be-downloaded")
            restarted = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=restarted_downloader,
            )
            cached = restarted.resolve(source)
            self.assertEqual(cached.data, WEBP)
            self.assertEqual(cached.md5, original_md5)
            self.assertEqual(restarted_downloader.calls, [])


if __name__ == "__main__":
    unittest.main()
