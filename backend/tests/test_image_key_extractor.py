import os
import hashlib
import struct
import tempfile
import time
import unittest
import ctypes
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util import Padding

from backend.image_key_extractor import (
    V2ImageSample,
    V2_MAGIC,
    _scan_regions,
    _scan_buffer_for_key,
    _account_id_candidates,
    _collect_kvcomm_codes,
    _derive_verified_image_key,
    _verify_full_image,
    derive_xor_key,
    find_recent_v2_samples,
)


class ImageKeyCandidateTests(unittest.TestCase):
    def setUp(self):
        self.key = b"1234567890abcdef"
        png_block = b"\x89PNG\r\n\x1a\n" + b"12345678"
        jpg_block = b"\xff\xd8\xffsynthetic-jpg"
        self.samples = [
            V2ImageSample(
                Path("first.dat"),
                AES.new(self.key, AES.MODE_ECB).encrypt(png_block),
                b"",
            ),
            V2ImageSample(
                Path("second.dat"),
                AES.new(self.key, AES.MODE_ECB).encrypt(jpg_block),
                b"",
            ),
        ]

    def test_finds_verified_16_byte_key_in_synthetic_memory(self):
        memory = b"noise\x00" + self.key + b"\x00more-noise"
        found, image_format = _scan_buffer_for_key(memory, self.samples)
        self.assertEqual(found, self.key)
        self.assertIn(image_format, ("PNG", "JPEG"))

    def test_uses_first_half_of_32_character_candidate(self):
        memory = b"\x00" + self.key + b"fedcba0987654321" + b"\x00"
        found, _image_format = _scan_buffer_for_key(memory, self.samples)
        self.assertEqual(found, self.key)

    def test_rejects_unverified_candidate(self):
        found, image_format = _scan_buffer_for_key(
            b"\x00abcdefghijklmnop\x00", self.samples
        )
        self.assertIsNone(found)
        self.assertEqual(image_format, "")

    def test_region_scanner_finds_key_across_chunk_boundary(self):
        memory = b"x" * 27 + b"\x00" + self.key + b"\x00tail"
        base = 0x1000

        class FakeKernel32:
            @staticmethod
            def ReadProcessMemory(_handle, address, buffer, requested, bytes_read):
                start = int(address.value) - base
                chunk = memory[start:start + int(requested)]
                if chunk:
                    ctypes.memmove(buffer, chunk, len(chunk))
                bytes_read._obj.value = len(chunk)
                return True

        with patch("backend.image_key_extractor.CHUNK_SIZE", 32):
            found, _image_format = _scan_regions(
                FakeKernel32(),
                object(),
                [(base, len(memory), 0x04)],
                self.samples,
                set(),
                time.monotonic() + 2,
            )
        self.assertEqual(found, self.key)

    def test_derives_only_a_consistent_jpeg_xor_key(self):
        xor_key = 0x88
        encrypted_tail = bytes(value ^ xor_key for value in b"\xff\xd9")
        samples = [
            V2ImageSample(
                Path("tail.dat"),
                self.samples[0].ciphertext,
                encrypted_tail,
                xor_size=2,
            )
        ]
        self.assertEqual(derive_xor_key(samples), xor_key)
        self.assertIsNone(
            derive_xor_key([
                V2ImageSample(
                    Path("bad.dat"),
                    self.samples[0].ciphertext,
                    b"\x00\x00",
                    xor_size=2,
                )
            ])
        )

    def test_full_v2_decryption_verifies_multiple_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            samples = []
            for index in range(2):
                image = (
                    b"\xff\xd8\xff"
                    + bytes([65 + index]) * 20
                    + b"\xff\xd9"
                )
                encrypted = AES.new(self.key, AES.MODE_ECB).encrypt(
                    Padding.pad(image, AES.block_size)
                )
                payload = (
                    V2_MAGIC
                    + struct.pack("<II", len(image), 0)
                    + b"\x00"
                    + encrypted
                )
                path = Path(temporary) / f"full_{index}.dat"
                path.write_bytes(payload)
                samples.append(
                    V2ImageSample(path, encrypted[:16], encrypted[-12:])
                )
            image_format, verified = _verify_full_image(
                self.key, samples, None
            )
            self.assertEqual(image_format, "JPG")
            self.assertEqual(verified, 2)


class ImageKeySampleDiscoveryTests(unittest.TestCase):
    def test_discovers_recent_v2_files_and_skips_other_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            image_dir = Path(temporary) / "attach" / "chat" / "month" / "Img"
            image_dir.mkdir(parents=True)
            key = b"1234567890abcdef"
            blocks = [
                b"\x89PNG\r\n\x1a\n12345678",
                b"\xff\xd8\xffsynthetic-jpg",
            ]
            for index, block in enumerate(blocks):
                ciphertext = AES.new(key, AES.MODE_ECB).encrypt(block)
                payload = V2_MAGIC + struct.pack("<II", 16, 0) + b"\x00" + ciphertext
                path = image_dir / f"sample_{index}_t.dat"
                path.write_bytes(payload)
                os.utime(path, (100 + index, 100 + index))
            (image_dir / "not_v2_t.dat").write_bytes(b"legacy-data")

            samples = find_recent_v2_samples(Path(temporary) / "attach")
            self.assertEqual(len(samples), 2)
            self.assertTrue(all(len(item.ciphertext) == 16 for item in samples))
            self.assertEqual(samples[0].path.name, "sample_1_t.dat")

    def test_recursively_finds_existing_v2_data_without_expected_img_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            attach_dir = Path(temporary) / "attach"
            unusual_dir = attach_dir / "legacy" / "nested" / "image_cache"
            unusual_dir.mkdir(parents=True)
            key = b"1234567890abcdef"
            blocks = (
                b"\x89PNG\r\n\x1a\n12345678",
                b"\xff\xd8\xffsynthetic-jpg",
            )
            for index, block in enumerate(blocks):
                ciphertext = AES.new(key, AES.MODE_ECB).encrypt(block)
                payload = V2_MAGIC + struct.pack("<II", 16, 0) + b"\x00" + ciphertext
                (unusual_dir / f"cached_{index}.dat").write_bytes(payload)

            samples = find_recent_v2_samples(attach_dir)

            self.assertEqual(len(samples), 2)
            self.assertEqual({sample.path.name for sample in samples}, {
                "cached_0.dat",
                "cached_1.dat",
            })


class ImageKeyMetadataDerivationTests(unittest.TestCase):
    def test_collects_codes_only_from_wechat_kvcomm_key_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Tencent"
            kvcomm = root / "xwechat" / "net" / "kvcomm"
            kvcomm.mkdir(parents=True)
            (kvcomm / "key_123456789_any.statistic").write_bytes(b"")
            (kvcomm / "key_reportnow_987654321_any.statistic").write_bytes(b"")
            (root / "unrelated").mkdir()
            (root / "unrelated" / "key_999_other.statistic").write_bytes(b"")

            self.assertEqual(_collect_kvcomm_codes(root), [123456789])

    def test_derives_and_fully_verifies_key_without_process_memory(self):
        code = 123456789
        folder_id = "wxid_example_abcd"
        actual_wxid = "wxid_example"
        key = hashlib.md5(
            f"{code}{actual_wxid}".encode("utf-8")
        ).hexdigest()[:16].encode("ascii")
        samples = []
        with tempfile.TemporaryDirectory() as temporary:
            for index in range(2):
                image = b"\xff\xd8\xff" + bytes([65 + index]) * 20 + b"\xff\xd9"
                encrypted = AES.new(key, AES.MODE_ECB).encrypt(
                    Padding.pad(image, AES.block_size)
                )
                path = Path(temporary) / f"derived_{index}.dat"
                path.write_bytes(
                    V2_MAGIC
                    + struct.pack("<II", len(image), 0)
                    + b"\x00"
                    + encrypted
                )
                samples.append(V2ImageSample(path, encrypted[:16], encrypted[-12:]))

            result = _derive_verified_image_key(samples, folder_id, [code])

        self.assertIsNotNone(result)
        derived_key, xor_key, image_format, verified = result
        self.assertEqual(derived_key, key)
        self.assertEqual(xor_key, code & 0xFF)
        self.assertEqual(image_format, "JPG")
        self.assertEqual(verified, 2)
        self.assertIn(actual_wxid, _account_id_candidates(folder_id))


if __name__ == "__main__":
    unittest.main()
