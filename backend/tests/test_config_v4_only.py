import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config import (
    SMOKE_TEST_ENV,
    WeChatAccount,
    WeChatConfig,
    detect_all_accounts,
)


class WeChat4OnlyConfigTests(unittest.TestCase):
    def test_release_smoke_mode_never_scans_local_wechat_roots(self):
        with patch.dict(os.environ, {SMOKE_TEST_ENV: "1"}, clear=False), patch(
            "backend.config.os.scandir"
        ) as scan:
            self.assertEqual(detect_all_accounts(), [])

        scan.assert_not_called()

    def test_switching_account_clears_account_scoped_runtime_key(self):
        first = WeChatAccount("wxid_first_suffix", Path("xwechat_files"))
        second = WeChatAccount("wxid_second_suffix", Path("xwechat_files"))
        config = WeChatConfig(
            accounts=[first, second],
            active_index=0,
            key="a" * 64,
            decrypted_db=Path("old-account.db"),
            display_name="旧账号",
            alias="old-alias",
        )

        self.assertTrue(config.set_active_account(1))

        self.assertEqual(config.active_index, 1)
        self.assertIsNone(config.key)
        self.assertIsNone(config.decrypted_db)
        self.assertIsNone(config.display_name)
        self.assertIsNone(config.alias)

    def test_message_database_discovery_ignores_wechat_3_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            msg_dir = root / "wxid_test" / "db_storage" / "message"
            msg_dir.mkdir(parents=True)
            (msg_dir / "message_0.db").write_bytes(b"v4")
            (msg_dir / "MSG.db").write_bytes(b"v3")
            (msg_dir / "MSG1.db").write_bytes(b"v3")

            account = WeChatAccount("wxid_test", root, msg_dir=msg_dir)
            config = WeChatConfig(accounts=[account], active_index=0)

            self.assertEqual(
                [path.name for path in config.get_all_msg_dbs()],
                ["message_0.db"],
            )
            self.assertEqual(config.to_dict()["wechat_version"], "4.x")
            self.assertTrue(config.to_dict()["supports_chat_images"])

    def test_unsupported_root_is_not_reported_as_wechat_4(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unsupported_data"
            msg_dir = root / "wxid_test" / "message"
            msg_dir.mkdir(parents=True)
            account = WeChatAccount("wxid_test", root, msg_dir=msg_dir)
            config = WeChatConfig(accounts=[account], active_index=0)

            self.assertIsNone(config.to_dict()["wechat_version"])

    def test_account_discovery_uses_weixin_configured_v4_root_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            appdata = temporary_root / "profile" / "AppData" / "Roaming"
            config_dir = appdata / "Tencent" / "xwechat" / "config"
            configured_data_root = temporary_root / "custom-data"
            v4_root = configured_data_root / "xwechat_files"
            message_dir = v4_root / "wxid_v4" / "db_storage" / "message"
            contact_dir = v4_root / "wxid_v4" / "db_storage" / "contact"
            message_dir.mkdir(parents=True)
            contact_dir.mkdir(parents=True)
            (message_dir / "message_0.db").write_bytes(b"v4")
            (contact_dir / "contact.db").write_bytes(b"v4")
            config_dir.mkdir(parents=True)
            (config_dir / "data-path.ini").write_text(
                str(configured_data_root), encoding="utf-8"
            )

            legacy_root = temporary_root / "WeChat Files" / "wxid_v3" / "Msg"
            legacy_root.mkdir(parents=True)
            (legacy_root / "MSG.db").write_bytes(b"v3")

            real_is_dir = Path.is_dir

            def isolated_is_dir(path: Path) -> bool:
                try:
                    resolved = path.resolve()
                    if os.path.commonpath((resolved, temporary_root)) != str(temporary_root):
                        return False
                    return real_is_dir(path)
                except (OSError, ValueError):
                    return False

            with patch.dict(
                os.environ,
                {
                    "APPDATA": str(appdata),
                    "USERPROFILE": str(temporary_root / "profile"),
                },
                clear=False,
            ), patch("backend.config.os.path.exists", return_value=False), patch.object(
                Path, "is_dir", isolated_is_dir
            ):
                accounts = detect_all_accounts()

            self.assertEqual([account.wxid for account in accounts], ["wxid_v4"])
            self.assertEqual(accounts[0].wx_root, v4_root)


if __name__ == "__main__":
    unittest.main()
