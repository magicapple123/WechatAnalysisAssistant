import base64
import hashlib
import io
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util import Padding
from PIL import Image

from backend.image_service import (
    ImageResolutionError,
    V2_MAGIC,
    WeChatImageService,
    decrypt_dat_bytes,
)


AES_KEY = b"1234567890abcdef"
XOR_KEY = 0x37
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def build_v2(plain: bytes, aes_size: int = 32, xor_size: int = 16) -> bytes:
    aes_plain = plain[:aes_size]
    raw_plain = plain[aes_size:len(plain) - xor_size]
    xor_plain = plain[len(plain) - xor_size:]
    encrypted_aes = AES.new(AES_KEY, AES.MODE_ECB).encrypt(
        Padding.pad(aes_plain, AES.block_size)
    )
    encrypted_xor = bytes(value ^ XOR_KEY for value in xor_plain)
    return (
        V2_MAGIC
        + struct.pack("<II", aes_size, xor_size)
        + b"\x00"
        + encrypted_aes
        + raw_plain
        + encrypted_xor
    )


def packed(file_md5: str) -> bytes:
    return b"prefix" + b"\x12\x22\x0a\x20" + file_md5.encode("ascii") + b"suffix"


class ImageDecryptTests(unittest.TestCase):
    def test_v2_round_trip_with_auto_xor(self):
        decrypted, image_format, mime = decrypt_dat_bytes(
            build_v2(PNG), aes_key=AES_KEY, xor_key="auto"
        )
        self.assertEqual(decrypted, PNG)
        self.assertEqual(image_format, "png")
        self.assertEqual(mime, "image/png")

    def test_v2_rejects_wrong_aes_key(self):
        with self.assertRaises(ImageResolutionError):
            decrypt_dat_bytes(
                build_v2(PNG), aes_key=b"wrongkey00000000", xor_key=XOR_KEY
            )

    def test_legacy_xor_round_trip(self):
        encrypted = bytes(value ^ XOR_KEY for value in PNG)
        decrypted, image_format, _ = decrypt_dat_bytes(encrypted)
        self.assertEqual(decrypted, PNG)
        self.assertEqual(image_format, "png")

    def test_rejects_malformed_v2_lengths(self):
        malformed = V2_MAGIC + struct.pack("<II", 0xFFFFFFFF, 3) + b"\x00" + b"x"
        with self.assertRaises(ImageResolutionError):
            decrypt_dat_bytes(malformed, aes_key=AES_KEY, xor_key=XOR_KEY)

    @staticmethod
    def _jpeg() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (3, 2), "purple").save(output, format="JPEG")
        return output.getvalue()

    def test_v2_accepts_and_strips_verified_sns_jpeg_footer(self):
        jpeg = self._jpeg()
        framed = jpeg + b"12345678" + hashlib.md5(jpeg).digest()

        decrypted, image_format, mime = decrypt_dat_bytes(
            build_v2(framed, xor_size=32),
            aes_key=AES_KEY,
            xor_key="auto",
        )

        self.assertEqual(decrypted, jpeg)
        self.assertEqual(image_format, "jpg")
        self.assertEqual(mime, "image/jpeg")

    def test_v2_rejects_unverified_jpeg_trailing_data(self):
        jpeg = self._jpeg()
        framed = jpeg + b"12345678" + (b"x" * 16)
        with self.assertRaises(ImageResolutionError):
            decrypt_dat_bytes(
                build_v2(framed, xor_size=32),
                aes_key=AES_KEY,
                xor_key="auto",
            )


class ImageResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.account_root = root / "xwechat_files" / "wxid_test_abcd"
        self.resource_db = root / "message_resource.db"
        self.talker = "wxid_friend"
        self.local_id = 42
        self.old_time = 1_700_000_000
        self.new_time = 1_800_000_000
        self.old_md5 = hashlib.md5(b"old").hexdigest()
        self.new_md5 = hashlib.md5(b"new").hexdigest()

        conn = sqlite3.connect(self.resource_db)
        conn.executescript(
            """
            CREATE TABLE ChatName2Id (user_name TEXT);
            CREATE TABLE MessageResourceInfo (
                chat_id INTEGER,
                message_local_id INTEGER,
                message_local_type INTEGER,
                message_create_time INTEGER,
                message_svr_id INTEGER,
                packed_info BLOB
            );
            """
        )
        conn.execute("INSERT INTO ChatName2Id(user_name) VALUES (?)", (self.talker,))
        chat_id = conn.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name=?", (self.talker,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO MessageResourceInfo VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, self.local_id, 3, self.old_time, 111, packed(self.old_md5)),
        )
        conn.execute(
            "INSERT INTO MessageResourceInfo VALUES (?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                self.local_id,
                (7 << 32) | 3,
                self.new_time,
                222,
                packed(self.new_md5),
            ),
        )
        conn.commit()
        conn.close()

        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = self.account_root / "msg" / "attach" / talker_hash / "2026-01" / "Img"
        image_dir.mkdir(parents=True)
        (image_dir / f"{self.old_md5}.dat").write_bytes(
            bytes(value ^ 0x12 for value in PNG)
        )
        (image_dir / f"{self.new_md5}.dat").write_bytes(
            bytes(value ^ 0x34 for value in PNG)
        )
        self.service = WeChatImageService(
            account_id="wxid_test_abcd",
            account_root=self.account_root,
            resource_db_path=self.resource_db,
        )
        self.addCleanup(self.service.close)

    def test_server_id_disambiguates_reused_local_id(self):
        old = self.service.get_image(
            talker=self.talker,
            message_id=self.local_id,
            create_time=self.new_time,
            server_id=111,
            purpose="analysis",
        )
        self.assertEqual(old.file_md5, self.old_md5)
        self.assertEqual(old.path.read_bytes(), PNG)

    def test_create_time_disambiguates_reused_local_id(self):
        new = self.service.get_image(
            talker=self.talker,
            message_id=self.local_id,
            create_time=self.new_time,
            purpose="analysis",
        )
        self.assertEqual(new.file_md5, self.new_md5)
        self.assertEqual(new.path.read_bytes(), PNG)

    def test_thumbnail_and_best_quality_use_different_candidate_order(self):
        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = (
            self.account_root / "msg" / "attach" / talker_hash / "2026-01" / "Img"
        )
        thumbnail = image_dir / f"{self.new_md5}_t.dat"
        hd = image_dir / f"{self.new_md5}_h.dat"
        thumbnail.write_bytes(b"t" * 100)
        hd.write_bytes(b"h" * 1000)

        thumbnail_first = self.service._candidate_files(
            self.talker, self.new_md5, "display", quality="thumbnail"
        )
        best_first = self.service._candidate_files(
            self.talker, self.new_md5, "display", quality="best"
        )

        self.assertEqual(thumbnail_first[0], thumbnail)
        self.assertEqual(best_first[0], hd)

    def test_best_quality_automatically_falls_back_to_thumbnail(self):
        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = (
            self.account_root / "msg" / "attach" / talker_hash / "2026-01" / "Img"
        )
        (image_dir / f"{self.new_md5}.dat").unlink()
        (image_dir / f"{self.new_md5}_h.dat").write_bytes(b"broken" * 200)
        thumbnail = image_dir / f"{self.new_md5}_t.dat"
        thumbnail.write_bytes(bytes(value ^ 0x56 for value in PNG))

        result = self.service.get_image(
            talker=self.talker,
            message_id=self.local_id,
            create_time=self.new_time,
            purpose="display",
            quality="best",
        )

        self.assertEqual(result.source_path, thumbnail)
        self.assertEqual(result.path.read_bytes(), PNG)

    def test_best_quality_refreshes_candidates_downloaded_after_first_view(self):
        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = (
            self.account_root / "msg" / "attach" / talker_hash / "2026-01" / "Img"
        )
        (image_dir / f"{self.new_md5}.dat").unlink()
        thumbnail = image_dir / f"{self.new_md5}_t.dat"
        thumbnail.write_bytes(bytes(value ^ 0x56 for value in PNG))

        first = self.service.get_image(
            talker=self.talker,
            message_id=self.local_id,
            create_time=self.new_time,
            purpose="display",
            quality="best",
        )
        self.assertEqual(first.source_path, thumbnail)

        hd = image_dir / f"{self.new_md5}_h.dat"
        hd.write_bytes(bytes(value ^ 0x57 for value in PNG))
        second = self.service.get_image(
            talker=self.talker,
            message_id=self.local_id,
            create_time=self.new_time,
            purpose="display",
            quality="best",
        )
        self.assertEqual(second.source_path, hd)

    def test_best_quality_converts_wxgf_instead_of_using_thumbnail(self):
        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = (
            self.account_root / "msg" / "attach" / talker_hash / "2026-01" / "Img"
        )
        (image_dir / f"{self.new_md5}.dat").unlink()
        thumbnail = image_dir / f"{self.new_md5}_t.dat"
        thumbnail.write_bytes(bytes(value ^ 0x56 for value in PNG))
        hd = image_dir / f"{self.new_md5}_h.dat"
        wxgf = b"wxgf" + b"\x00" * 124
        hd.write_bytes(build_v2(wxgf))
        self.service.aes_key = AES_KEY
        self.service.xor_key = XOR_KEY
        converted_jpeg = b"\xff\xd8\xffconverted-high-resolution\xff\xd9"

        with patch(
            "backend.image_service.convert_wxgf_to_jpeg",
            return_value=converted_jpeg,
        ) as convert_mock:
            result = self.service.get_image(
                talker=self.talker,
                message_id=self.local_id,
                create_time=self.new_time,
                purpose="display",
                quality="best",
            )

        convert_mock.assert_called_once()
        self.assertEqual(result.source_path, hd)
        self.assertEqual(result.format, "jpg")
        self.assertEqual(result.path.read_bytes(), converted_jpeg)


if __name__ == "__main__":
    unittest.main()
