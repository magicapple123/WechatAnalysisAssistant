import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import api, key_extractor
from backend.config import WeChatAccount


class AccountKeyStorageTests(unittest.TestCase):
    def test_invalid_persisted_or_new_keys_are_never_returned_or_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys_file = root / "wechat_keys.json"
            keys_file.write_text(
                json.dumps({"wxid_test": {"key": "not-a-database-key"}}),
                encoding="utf-8",
            )
            with patch.multiple(
                key_extractor,
                KEYS_FILE=keys_file,
                KEY_FILE=root / "wechat_key.txt",
                ROOT_KEY_FILE=root / "wechat_keys.txt",
            ):
                self.assertIsNone(
                    key_extractor.load_key("wxid_test_suffix")
                )
                with self.assertRaises(ValueError):
                    key_extractor.save_key("not-a-database-key", "wxid_test_suffix")

            stored = json.loads(keys_file.read_text(encoding="utf-8"))
            self.assertEqual(stored["wxid_test"]["key"], "not-a-database-key")

    def test_delete_key_removes_only_target_account_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys_file = root / "wechat_keys.json"
            key_file = root / "wechat_key.txt"
            readable_file = root / "wechat_keys.txt"
            keys_file.write_text(
                json.dumps({
                    "wxid_first": {
                        "key": "a" * 64,
                        "display_name": "第一个账号",
                    },
                    "wxid_second": {
                        "key": "b" * 64,
                        "display_name": "第二个账号",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.multiple(
                key_extractor,
                KEYS_FILE=keys_file,
                KEY_FILE=key_file,
                ROOT_KEY_FILE=readable_file,
            ):
                removed = key_extractor.delete_key("wxid_first_suffix")
                stored = json.loads(keys_file.read_text(encoding="utf-8"))

                self.assertTrue(removed)
                self.assertNotIn("key", stored["wxid_first"])
                self.assertEqual(stored["wxid_first"]["display_name"], "第一个账号")
                self.assertEqual(stored["wxid_second"]["key"], "b" * 64)
                self.assertIsNone(key_extractor.load_key("wxid_first_suffix"))
                self.assertEqual(
                    key_extractor.load_key("wxid_second_suffix"),
                    "b" * 64,
                )
                self.assertNotIn("a" * 64, readable_file.read_text(encoding="utf-8"))

    def test_delete_key_blocks_legacy_fallback_without_touching_other_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keys_file = root / "wechat_keys.json"
            keys_file.write_text(
                json.dumps({
                    "_default": {"key": "a" * 64},
                    "wxid_second": {"key": "b" * 64},
                }),
                encoding="utf-8",
            )
            with patch.multiple(
                key_extractor,
                KEYS_FILE=keys_file,
                KEY_FILE=root / "wechat_key.txt",
                ROOT_KEY_FILE=root / "wechat_keys.txt",
            ):
                self.assertEqual(
                    key_extractor.load_key("wxid_first_suffix"),
                    "a" * 64,
                )
                self.assertTrue(key_extractor.delete_key("wxid_first_suffix"))
                stored = json.loads(keys_file.read_text(encoding="utf-8"))

                self.assertEqual(stored["_default"]["key"], "a" * 64)
                self.assertEqual(stored["wxid_second"]["key"], "b" * 64)
                self.assertEqual(stored["wxid_first"], {})
                self.assertIsNone(key_extractor.load_key("wxid_first_suffix"))


class AccountKeyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_hook_falls_back_without_controlling_wechat(self):
        candidate = "9" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            msg_dir = root / "wxid_current_suffix" / "db_storage" / "message"
            msg_dir.mkdir(parents=True)
            (msg_dir / "message_0.db").write_bytes(b"encrypted")
            account = WeChatAccount(
                "wxid_current_suffix",
                root,
                msg_dir=msg_dir,
            )
            with patch.object(
                api.config, "accounts", [account]
            ), patch.object(
                api.config, "active_index", 0
            ), patch.object(
                api.config, "key", None
            ), patch.object(
                api, "_load_optional_wx_key_runtime", return_value=None
            ), patch.object(
                api, "find_key_auto", return_value=candidate
            ) as find_key, patch.object(
                api, "test_key", return_value=True
            ) as test_key, patch.object(
                api, "save_key"
            ) as save_key, patch.object(
                api, "_close_runtime_caches"
            ) as close_caches, patch(
                "subprocess.Popen"
            ) as popen:
                response = await api.extract_key()

        self.assertTrue(response["success"])
        self.assertTrue(response["verified"])
        self.assertFalse(response["hook_available"])
        self.assertEqual(response["source"], "python_memory_fallback")
        find_key.assert_called_once_with(persist=False)
        test_key.assert_called_once()
        save_key.assert_called_once_with(candidate, "wxid_current_suffix")
        close_caches.assert_called_once_with(cancel_recognition=True)
        popen.assert_not_called()

    async def test_fallback_never_saves_an_unverified_candidate(self):
        candidate = "8" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            msg_dir = root / "wxid_current_suffix" / "db_storage" / "message"
            msg_dir.mkdir(parents=True)
            (msg_dir / "message_0.db").write_bytes(b"encrypted")
            account = WeChatAccount(
                "wxid_current_suffix",
                root,
                msg_dir=msg_dir,
            )
            with patch.object(
                api.config, "accounts", [account]
            ), patch.object(
                api.config, "active_index", 0
            ), patch.object(
                api.config, "key", None
            ), patch.object(
                api, "_load_optional_wx_key_runtime", return_value=None
            ), patch.object(
                api, "find_key_auto", return_value=candidate
            ), patch.object(
                api, "test_key", return_value=False
            ), patch.object(
                api, "save_key"
            ) as save_key, patch(
                "subprocess.Popen"
            ) as popen:
                with self.assertRaises(HTTPException) as rejected:
                    await api.extract_key()

        self.assertEqual(rejected.exception.status_code, 422)
        save_key.assert_not_called()
        popen.assert_not_called()

    async def test_fallback_discards_verified_result_after_account_switch(self):
        candidate = "7" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            msg_dir = root / "wxid_current_suffix" / "db_storage" / "message"
            msg_dir.mkdir(parents=True)
            (msg_dir / "message_0.db").write_bytes(b"encrypted")
            account = WeChatAccount(
                "wxid_current_suffix",
                root,
                msg_dir=msg_dir,
            )

            def verify_then_switch(*_args):
                api.config.active_index = -1
                return True

            with patch.object(
                api.config, "accounts", [account]
            ), patch.object(
                api.config, "active_index", 0
            ), patch.object(
                api.config, "key", None
            ), patch.object(
                api, "_load_optional_wx_key_runtime", return_value=None
            ), patch.object(
                api, "find_key_auto", return_value=candidate
            ), patch.object(
                api, "test_key", side_effect=verify_then_switch
            ), patch.object(
                api, "save_key"
            ) as save_key:
                with self.assertRaises(HTTPException) as rejected:
                    await api.extract_key()

        self.assertEqual(rejected.exception.status_code, 409)
        save_key.assert_not_called()

    async def test_delete_endpoint_targets_current_account(self):
        account = WeChatAccount(
            "wxid_current_suffix",
            Path("xwechat_files"),
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            api.config, "accounts", [account]
        ), patch.object(
            api.config, "active_index", 0
        ), patch.object(
            api.config, "key", "c" * 64
        ), patch.object(
            key_extractor, "KEY_FILE", Path(temporary) / "wechat_key.txt"
        ), patch.object(
            api, "delete_saved_key", return_value=True
        ) as delete_saved, patch.object(
            api, "_close_runtime_caches"
        ) as close_caches:
            response = await api.delete_key()
            cleared_runtime_key = api.config.key

        delete_saved.assert_called_once_with("wxid_current_suffix")
        close_caches.assert_called_once_with(cancel_recognition=True)
        self.assertIsNone(cleared_runtime_key)
        self.assertTrue(response["success"])
        self.assertTrue(response["removed"])
        self.assertEqual(response["wxid"], "wxid_current_suffix")

    async def test_auto_detect_clears_stale_memory_key_on_failed_validation(self):
        account = WeChatAccount(
            "wxid_current_suffix",
            Path("xwechat_files"),
        )
        with patch.object(
            api.config, "accounts", [account]
        ), patch.object(
            api.config, "active_index", 0
        ), patch.object(
            api.config, "key", "d" * 64
        ), patch.object(
            api.config, "_load_cached_names"
        ), patch.object(
            api.config, "detect_databases", return_value=True
        ), patch.object(
            api.config, "get_all_msg_dbs", return_value=[Path("message_0.db")]
        ), patch.object(
            api, "load_key", return_value="e" * 64
        ), patch.object(
            api, "test_key", return_value=False
        ), patch.object(
            api, "delete_saved_key", return_value=True
        ) as delete_saved:
            response = await api.auto_detect()

        self.assertFalse(response["data"]["key_found"])
        self.assertIsNone(api.config.key)
        delete_saved.assert_called_once_with("wxid_current_suffix")

    async def test_auto_detect_persists_heuristic_key_only_after_validation(self):
        account = WeChatAccount(
            "wxid_current_suffix",
            Path("xwechat_files"),
        )
        candidate = "f" * 64
        with patch.object(
            api.config, "accounts", [account]
        ), patch.object(
            api.config, "active_index", 0
        ), patch.object(
            api.config, "_load_cached_names"
        ), patch.object(
            api.config, "detect_databases", return_value=True
        ), patch.object(
            api.config, "get_all_msg_dbs", return_value=[Path("message_0.db")]
        ), patch.object(
            api, "load_key", return_value=None
        ), patch.object(
            api, "find_key_auto", return_value=candidate
        ) as find_key, patch.object(
            api, "test_key", return_value=True
        ), patch.object(
            api, "save_key"
        ) as save_key:
            response = await api.auto_detect()

        self.assertTrue(response["data"]["key_found"])
        self.assertEqual(response["data"]["key_source"], "memory")
        find_key.assert_called_once_with(persist=False)
        save_key.assert_called_once_with(candidate, "wxid_current_suffix")


if __name__ == "__main__":
    unittest.main()
