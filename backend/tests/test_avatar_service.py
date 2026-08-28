import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from backend.avatar_service import (
    AvatarNotFound,
    AvatarService,
    AvatarValidationError,
    _SafeAvatarDownloader,
)
from backend.config import WeChatAccount
from backend.moments import MomentsMediaDownloadError


def make_image_bytes(image_format: str = "PNG", *, color=(20, 120, 220)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format=image_format)
    return output.getvalue()


def make_head_image_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE head_image (
                username TEXT,
                md5 TEXT,
                image_buffer BLOB,
                update_time INTEGER
            )
            """
        )
        conn.executemany("INSERT INTO head_image VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def make_contact_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE contact (
                username TEXT,
                small_head_url TEXT,
                big_head_url TEXT,
                head_img_md5 TEXT
            )
            """
        )
        conn.executemany("INSERT INTO contact VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


class AvatarServiceTests(unittest.TestCase):
    def test_valid_local_blob_is_preferred_and_mime_is_detected_from_bytes(self):
        png = make_image_bytes("PNG")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            head_db = root / "head_image.db"
            contact_db = root / "contact.db"
            make_head_image_db(
                head_db,
                [("wxid_friend", "ignored", png, 100)],
            )
            make_contact_db(
                contact_db,
                [(
                    "wxid_friend",
                    "https://wx.qlogo.cn/fallback",
                    "",
                    "remote-md5",
                )],
            )

            def forbidden_downloader():
                self.fail("a valid local avatar must prevent network access")

            service = AvatarService(
                account_id="wxid_account_abcd",
                contact_db_path=contact_db,
                head_image_db_path=head_db,
                cache_root=root / "cache",
                downloader_factory=forbidden_downloader,
            )
            avatar = service.get_avatar("wxid_friend")

        self.assertEqual(avatar.data, png)
        self.assertEqual(avatar.mime_type, "image/png")
        self.assertEqual(avatar.source, "local")
        self.assertEqual(avatar.etag, hashlib.sha256(png).hexdigest())

    def test_invalid_local_blob_is_ignored_and_valid_network_image_is_used(self):
        jpeg = make_image_bytes("JPEG")
        calls = []

        class Downloader:
            @staticmethod
            def download(url):
                calls.append(url)
                return SimpleNamespace(data=jpeg)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            head_db = root / "head_image.db"
            contact_db = root / "contact.db"
            make_head_image_db(
                head_db,
                [("wxid_friend", "bad", b"not-an-image", 100)],
            )
            make_contact_db(
                contact_db,
                [(
                    "wxid_friend",
                    "https://wx.qlogo.cn/remote-avatar",
                    "",
                    "remote-md5",
                )],
            )
            service = AvatarService(
                account_id="wxid_account_abcd",
                contact_db_path=contact_db,
                head_image_db_path=head_db,
                cache_root=root / "cache",
                downloader_factory=Downloader,
            )
            avatar = service.get_avatar("wxid_friend")

        self.assertEqual(avatar.mime_type, "image/jpeg")
        self.assertEqual(avatar.source, "network")
        self.assertEqual(calls, ["https://wx.qlogo.cn/remote-avatar"])

    def test_validation_rejects_non_image_payload(self):
        with self.assertRaises(AvatarValidationError):
            AvatarService._validate(b"<html>not an avatar</html>", source="test")

    def test_network_result_is_persisted_and_reused_without_redownload(self):
        jpeg = make_image_bytes("JPEG")
        calls = []

        class Downloader:
            @staticmethod
            def download(url):
                calls.append(url)
                return SimpleNamespace(data=jpeg)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact_db = root / "contact.db"
            cache_root = root / "cache"
            make_contact_db(
                contact_db,
                [(
                    "wxid_friend",
                    "https://thirdwx.qlogo.cn/avatar",
                    "",
                    "avatar-v1",
                )],
            )
            service = AvatarService(
                account_id="wxid_account_abcd",
                contact_db_path=contact_db,
                cache_root=cache_root,
                downloader_factory=Downloader,
            )
            first = service.get_avatar("wxid_friend")

            class ForbiddenDownloader:
                @staticmethod
                def download(_url):
                    raise AssertionError("the validated disk cache must be reused")

            restarted_service = AvatarService(
                account_id="wxid_account_abcd",
                contact_db_path=contact_db,
                cache_root=cache_root,
                downloader_factory=ForbiddenDownloader,
            )
            second = restarted_service.get_avatar("wxid_friend")

            cache_files = list(cache_root.rglob("*.img"))

        self.assertEqual(first.source, "network")
        self.assertEqual(second.source, "network-cache")
        self.assertEqual(first.data, second.data)
        self.assertEqual(calls, ["https://thirdwx.qlogo.cn/avatar"])
        self.assertEqual(len(cache_files), 1)

    def test_network_404_becomes_avatar_not_found_and_is_not_cached(self):
        class NotFoundDownloader:
            @staticmethod
            def download(_url):
                raise MomentsMediaDownloadError("HTTP 404")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact_db = root / "contact.db"
            cache_root = root / "cache"
            make_contact_db(
                contact_db,
                [(
                    "wxid_missing",
                    "https://wx.qlogo.cn/missing",
                    "",
                    "missing-v1",
                )],
            )
            service = AvatarService(
                account_id="wxid_account_abcd",
                contact_db_path=contact_db,
                cache_root=cache_root,
                downloader_factory=NotFoundDownloader,
            )
            with self.assertRaises(AvatarNotFound):
                service.get_avatar("wxid_missing")

            cache_files = list(cache_root.rglob("*.img"))

        self.assertEqual(cache_files, [])

    def test_avatar_downloader_accepts_only_exact_wechat_domain_boundaries(self):
        for hostname in (
            "wx.qlogo.cn",
            "a.wx.qlogo.cn",
            "thirdwx.qlogo.cn",
            "wework.qpic.cn",
            "mmhead.c2c.wechat.com",
            "mmhead.hk.wechat.com",
            "p.qpic.cn",
        ):
            with self.subTest(hostname=hostname):
                self.assertTrue(_SafeAvatarDownloader._host_allowed(hostname))

        for hostname in (
            "wx.qlogo.cn.evil.example",
            "evilwx.qlogo.cn",
            "qlogo.cn",
            "localhost",
            "127.0.0.1",
        ):
            with self.subTest(hostname=hostname):
                self.assertFalse(_SafeAvatarDownloader._host_allowed(hostname))

        downloader = _SafeAvatarDownloader()
        with self.assertRaises(MomentsMediaDownloadError):
            downloader._validate_url("https://wx.qlogo.cn.evil.example/avatar")

    def test_self_alias_resolves_the_unsuffixed_wechat_username(self):
        png = make_image_bytes("PNG")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            head_db = root / "head_image.db"
            make_head_image_db(
                head_db,
                [("wxid_account", "self", png, 100)],
            )
            service = AvatarService(
                account_id="wxid_account_abcd",
                head_image_db_path=head_db,
                cache_root=root / "cache",
            )

            avatar = service.get_avatar("__self__")

        self.assertEqual(avatar.source, "local")
        self.assertEqual(avatar.data, png)


class AvatarConfigTests(unittest.TestCase):
    def test_wechat_4_account_discovers_nonempty_head_image_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            message_dir = root / "wxid_test" / "db_storage" / "message"
            head_db = message_dir.parent / "head_image" / "head_image.db"
            message_dir.mkdir(parents=True)
            head_db.parent.mkdir()
            head_db.write_bytes(b"database")
            account = WeChatAccount("wxid_test", root, msg_dir=message_dir)

            self.assertEqual(account.head_image_db, head_db)

if __name__ == "__main__":
    unittest.main()
