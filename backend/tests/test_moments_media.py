import base64
import hashlib
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util import Padding
from PIL import Image

from backend.image_service import V2_MAGIC
from backend.moments_media import (
    MomentsMediaNotFound,
    MomentsMediaPathError,
    MomentsMediaResolver,
    MomentsMediaValidationError,
    QUALITY_HIGH,
    QUALITY_THUMBNAIL,
    STATUS_PARTIAL,
    STATUS_READY,
    STATUS_UNBOUND,
    STATUS_UNSUPPORTED,
)


AES_KEY = b"1234567890abcdef"
XOR_KEY = 0x37
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def build_v2(
    plain: bytes,
    *,
    aes_size: int = 32,
    xor_size: int = 16,
    xor_key: int = XOR_KEY,
) -> bytes:
    aes_plain = plain[:aes_size]
    raw_plain = plain[aes_size:len(plain) - xor_size]
    xor_plain = plain[len(plain) - xor_size:]
    encrypted_aes = AES.new(AES_KEY, AES.MODE_ECB).encrypt(
        Padding.pad(aes_plain, AES.block_size)
    )
    encrypted_xor = bytes(value ^ xor_key for value in xor_plain)
    return (
        V2_MAGIC
        + struct.pack("<II", aes_size, xor_size)
        + b"\x00"
        + encrypted_aes
        + raw_plain
        + encrypted_xor
    )


class MomentsMediaResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store_root = self.base / "local-app-data" / "moments-media"
        self.sns_cache = self.base / "account" / "cache"
        self.cache_file = (
            self.sns_cache
            / "2026-07"
            / "Sns"
            / "Img"
            / "aa"
            / ("b" * 30)
        )
        self.cache_file.parent.mkdir(parents=True)
        self.cache_file.write_bytes(build_v2(PNG))
        self.expected_md5 = hashlib.md5(PNG).hexdigest()
        self.resolver = MomentsMediaResolver(
            account_id="wxid_account_a_suffix",
            aes_key=AES_KEY,
            # Deliberately wrong: the resolver must retry safe tail inference.
            xor_key="0x88",
            store_root=self.store_root,
            sns_cache_root=self.sns_cache,
        )

    def test_exact_plaintext_md5_binds_and_persists(self):
        status = self.resolver.bind_cache_file(
            tid="8100000000000000000",
            media_index=0,
            source=self.cache_file,
            expected_md5=self.expected_md5,
            expected_width=1,
            expected_height=1,
        )
        self.assertEqual(status.status, STATUS_READY)
        self.assertEqual(status.quality, QUALITY_HIGH)

        loaded_status, payload = self.resolver.get_bytes(
            "8100000000000000000", 0
        )
        self.assertEqual(payload, PNG)
        self.assertEqual(loaded_status.mime_type, "image/png")

        restarted = MomentsMediaResolver(
            account_id="wxid_account_a_suffix",
            store_root=self.store_root,
        )
        self.assertEqual(
            restarted.get_status("8100000000000000000", 0).status,
            STATUS_READY,
        )

    def test_md5_mismatch_is_never_bound(self):
        status = self.resolver.bind_cache_file(
            tid=7,
            media_index=0,
            source=self.cache_file,
            expected_md5=hashlib.md5(b"another image").hexdigest(),
        )
        self.assertEqual(status.status, STATUS_UNBOUND)
        self.assertEqual(status.reason, "md5_mismatch")
        self.assertEqual(self.resolver.get_status(7, 0).status, STATUS_UNBOUND)
        with self.assertRaises(MomentsMediaNotFound):
            self.resolver.get_bytes(7, 0)

    def test_missing_expected_md5_does_not_guess_from_dimensions_or_time(self):
        status = self.resolver.bind_cache_file(
            tid=8,
            media_index=0,
            source=self.cache_file,
            expected_md5="",
            expected_width=1,
            expected_height=1,
        )
        self.assertEqual(status.status, STATUS_UNBOUND)
        self.assertEqual(status.reason, "expected_md5_missing")

    def test_batch_scan_uses_only_exact_content_md5(self):
        unrelated = [{
            "tid": "9",
            "media": [{
                "type": "2",
                "url_md5": hashlib.md5(b"not this image").hexdigest(),
                "width": "1",
                "height": "1",
                "create_time": 1_700_000_000,
            }],
        }]
        result = self.resolver.scan_exact_cache_files(
            unrelated, [self.cache_file]
        )
        self.assertEqual(result.files_decoded, 1)
        self.assertEqual(result.unmatched_files, 1)
        self.assertEqual(result.media_bound, 0)
        self.assertEqual(self.resolver.get_status(9, 0).status, STATUS_UNBOUND)

        exact = [{
            "tid": "10",
            "media": [{
                "type": "2",
                "url_md5": self.expected_md5,
                "width": "100",
                "height": "100",
            }],
        }]
        matched = self.resolver.scan_exact_cache_files(exact, [self.cache_file])
        self.assertEqual(matched.exact_content_matches, 1)
        self.assertEqual(matched.media_bound, 1)
        status = self.resolver.get_status(10, 0, exact[0]["media"][0])
        self.assertEqual(status.status, STATUS_READY)
        self.assertEqual(status.quality, QUALITY_THUMBNAIL)

    def test_incomplete_encrypted_file_is_partial_not_ready(self):
        broken = self.cache_file.with_name("c" * 30)
        broken.write_bytes(self.cache_file.read_bytes()[:-1])
        status = self.resolver.bind_cache_file(
            tid=11,
            media_index=0,
            source=broken,
            expected_md5=self.expected_md5,
        )
        self.assertEqual(status.status, STATUS_PARTIAL)
        self.assertEqual(self.resolver.get_status(11, 0).status, STATUS_PARTIAL)
        with self.assertRaises(MomentsMediaNotFound):
            self.resolver.get_bytes(11, 0)

    def test_source_path_cannot_escape_account_cache(self):
        outside = self.base / "outside.dat"
        outside.write_bytes(build_v2(PNG))
        with self.assertRaises(MomentsMediaPathError):
            self.resolver.bind_cache_file(
                tid=12,
                media_index=0,
                source=outside,
                expected_md5=self.expected_md5,
            )

    def test_manifest_path_cannot_escape_account_store(self):
        self.resolver.bind_cache_file(
            tid=13,
            media_index=0,
            source=self.cache_file,
            expected_md5=self.expected_md5,
        )
        conn = sqlite3.connect(self.resolver.store.manifest_path)
        try:
            conn.execute(
                "UPDATE moments_media SET relative_path='../../outside.png' "
                "WHERE tid='13' AND media_index=0"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(MomentsMediaPathError):
            self.resolver.get_bytes(13, 0)

    def test_account_storage_and_lookup_are_isolated(self):
        self.resolver.bind_cache_file(
            tid=14,
            media_index=0,
            source=self.cache_file,
            expected_md5=self.expected_md5,
        )
        other = MomentsMediaResolver(
            account_id="wxid_account_b_suffix",
            store_root=self.store_root,
        )
        self.assertNotEqual(
            self.resolver.store.account_root,
            other.store.account_root,
        )
        self.assertNotIn(
            "wxid_account_a_suffix",
            str(self.resolver.store.account_root),
        )
        self.assertEqual(other.get_status(14, 0).status, STATUS_UNBOUND)

    def test_hydrate_posts_reports_ready_unbound_and_unsupported(self):
        self.resolver.bind_cache_file(
            tid=15,
            media_index=0,
            source=self.cache_file,
            expected_md5=self.expected_md5,
        )
        posts = [{
            "tid": "15",
            "content": "kept",
            "media": [
                {"type": "2", "width": 1, "height": 1},
                {"type": "2", "width": 1, "height": 1},
                {"type": "15"},
            ],
        }]
        hydrated = self.resolver.hydrate_posts(posts)
        statuses = [item["local_media"]["status"] for item in hydrated[0]["media"]]
        self.assertEqual(
            statuses,
            [STATUS_READY, STATUS_UNBOUND, STATUS_UNSUPPORTED],
        )
        self.assertEqual(
            hydrated[0]["local_media_summary"],
            {
                "total": 3,
                "ready": 1,
                "partial": 0,
                "unbound": 1,
                "unsupported": 1,
            },
        )
        self.assertNotIn("local_media", posts[0]["media"][0])

    def test_direct_store_enforces_size_and_pixel_caps(self):
        too_small_limit = MomentsMediaResolver(
            account_id="size-limit",
            store_root=self.store_root,
            max_file_bytes=len(PNG) - 1,
        )
        with self.assertRaises(MomentsMediaValidationError):
            too_small_limit.store.save_bytes(1, 0, PNG)

        image_path = self.base / "ten.png"
        Image.new("RGB", (10, 10), "red").save(image_path, format="PNG")
        pixel_limited = MomentsMediaResolver(
            account_id="pixel-limit",
            store_root=self.store_root,
            max_pixels=50,
        )
        with self.assertRaises(MomentsMediaValidationError):
            pixel_limited.store.save_bytes(1, 0, image_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
