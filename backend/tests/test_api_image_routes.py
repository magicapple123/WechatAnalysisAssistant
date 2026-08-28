import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend import api
from backend import settings as settings_module
from backend.config import WeChatAccount
from backend.image_key_extractor import ImageKeyExtractionResult


class ImageRouteSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_old_settings_file_receives_hd_automation_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "settings.json"
            settings_file.write_text(
                json.dumps({"ui": {"show_chat_images": True}}),
                encoding="utf-8",
            )
            with patch.object(settings_module, "SETTINGS_FILE", settings_file):
                loaded = settings_module.load_settings()

        self.assertEqual(loaded["ui"]["hd_automation_timeout_seconds"], 10)
        self.assertEqual(loaded["ui"]["hd_automation_min_dwell_seconds"], 0.5)

    async def test_partial_ui_update_preserves_saved_image_quality(self):
        existing = {
            "ui": {
                "show_chat_images": False,
                "image_quality": "high",
                "hd_automation_timeout_seconds": 42,
                "hd_automation_min_dwell_seconds": 1.25,
            },
            "vision": {},
            "images": {"accounts": {}},
        }

        def fake_save(update):
            return {**existing, **update}

        with patch.object(api, "load_settings", return_value=existing), \
                patch.object(api, "save_settings", side_effect=fake_save):
            response = await api.update_settings(
                api.SettingsUpdate(ui={"show_chat_images": True})
            )

        self.assertTrue(response["data"]["ui"]["show_chat_images"])
        self.assertEqual(response["data"]["ui"]["image_quality"], "high")
        self.assertEqual(
            response["data"]["ui"]["hd_automation_timeout_seconds"], 42
        )
        self.assertEqual(
            response["data"]["ui"]["hd_automation_min_dwell_seconds"], 1.25
        )

    async def test_hd_automation_timing_settings_accept_zero_dwell(self):
        existing = {
            "ui": {"show_chat_images": False, "image_quality": "smart"},
            "vision": {},
            "images": {"accounts": {}},
        }

        def fake_save(update):
            return {**existing, **update}

        with patch.object(api, "load_settings", return_value=existing), \
                patch.object(api, "save_settings", side_effect=fake_save):
            response = await api.update_settings(
                api.SettingsUpdate(
                    ui={
                        "hd_automation_timeout_seconds": 25,
                        "hd_automation_min_dwell_seconds": 0,
                    }
                )
            )

        self.assertEqual(
            response["data"]["ui"]["hd_automation_timeout_seconds"], 25
        )
        self.assertEqual(
            response["data"]["ui"]["hd_automation_min_dwell_seconds"], 0
        )

    async def test_hd_automation_timing_settings_reject_invalid_values(self):
        existing = {
            "ui": {"show_chat_images": False, "image_quality": "smart"},
            "vision": {},
            "images": {"accounts": {}},
        }
        invalid_updates = (
            {"hd_automation_timeout_seconds": 4},
            {"hd_automation_timeout_seconds": 10.5},
            {"hd_automation_min_dwell_seconds": -0.1},
            {"hd_automation_min_dwell_seconds": 5.1},
        )
        with patch.object(api, "load_settings", return_value=existing):
            for update in invalid_updates:
                with self.subTest(update=update), self.assertRaises(HTTPException) as invalid:
                    await api.update_settings(api.SettingsUpdate(ui=update))
                self.assertEqual(invalid.exception.status_code, 400)

    async def test_rejects_unknown_chat_image_quality(self):
        existing = {
            "ui": {"show_chat_images": False, "image_quality": "smart"},
            "vision": {},
            "images": {"accounts": {}},
        }
        with patch.object(api, "load_settings", return_value=existing):
            with self.assertRaises(HTTPException) as invalid:
                await api.update_settings(
                    api.SettingsUpdate(ui={"image_quality": "original-network"})
                )
        self.assertEqual(invalid.exception.status_code, 400)

    async def test_image_key_reveal_is_explicit_account_scoped_and_not_cached(self):
        stored = {
            "images": {
                "accounts": {
                    "wxid_current": {"aes_key": "1234567890abcdef"},
                    "wxid_other": {"aes_key": "fedcba0987654321"},
                }
            }
        }
        account = SimpleNamespace(wxid="wxid_current")
        with patch.object(api.config, "accounts", [account]), \
                patch.object(api.config, "active_index", 0), \
                patch.object(api, "load_settings", return_value=stored):
            response = await api.reveal_chat_image_key("1")

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["aes_key"], "1234567890abcdef")
        self.assertNotIn("account_id", payload)
        self.assertNotIn("fedcba0987654321", response.body.decode("utf-8"))
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_image_key_reveal_requires_custom_header(self):
        with self.assertRaises(HTTPException) as missing_header:
            await api.reveal_chat_image_key(None)
        self.assertEqual(missing_header.exception.status_code, 403)

    async def test_image_key_extraction_saves_current_account_without_leaking_key(self):
        secret = "1234567890abcdef"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            account_root = root / "wxid_test"
            message_dir = account_root / "db_storage" / "message"
            attach_dir = account_root / "msg" / "attach"
            message_dir.mkdir(parents=True)
            attach_dir.mkdir(parents=True)
            account = WeChatAccount(
                "wxid_test", root, msg_dir=message_dir
            )
            existing = {
                "vision": {"api_key": "", "base_url": "", "model": ""},
                "images": {
                    "accounts": {
                        "wxid_test": {"xor_key": "0x37"},
                        "wxid_other": {
                            "aes_key": "other-account-key",
                            "xor_key": "0x37",
                        }
                    }
                },
            }
            saved = {}

            def fake_save(update):
                saved.update(update)
                return {**existing, **update}

            result = ImageKeyExtractionResult(
                aes_key=secret,
                xor_key=None,
                verified_format="JPEG",
                samples_found=2,
                processes_scanned=1,
            )
            with patch.object(api.config, "accounts", [account]), \
                    patch.object(api.config, "active_index", 0), \
                    patch.object(api, "extract_image_aes_key", return_value=result), \
                    patch.object(api, "load_settings", return_value=existing), \
                    patch.object(api, "save_settings", side_effect=fake_save), \
                    patch.object(api, "_close_runtime_caches"):
                response = await api.extract_chat_image_key("1")

        accounts = saved["images"]["accounts"]
        self.assertEqual(accounts["wxid_test"]["aes_key"], secret)
        self.assertEqual(accounts["wxid_test"]["xor_key"], "0x37")
        self.assertEqual(accounts["wxid_other"]["aes_key"], "other-account-key")
        self.assertTrue(response["data"]["images"]["has_aes_key"])
        self.assertNotIn(secret, json.dumps(response, ensure_ascii=False))
        self.assertNotIn("accounts", response["data"]["images"])

    async def test_image_key_endpoint_requires_custom_local_client_header(self):
        with self.assertRaises(HTTPException) as missing_header:
            await api.extract_chat_image_key(None)
        self.assertEqual(missing_header.exception.status_code, 403)

    async def test_image_key_endpoint_rejects_duplicate_scan(self):
        api._image_key_extraction_running = True
        try:
            with self.assertRaises(HTTPException) as busy:
                await api.extract_chat_image_key("1")
            self.assertEqual(busy.exception.status_code, 409)
        finally:
            api._image_key_extraction_running = False

    async def test_image_key_result_is_not_saved_after_account_switch(self):
        result = ImageKeyExtractionResult(
            aes_key="1234567890abcdef",
            xor_key=None,
            verified_format="JPEG",
            samples_found=2,
            processes_scanned=1,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            accounts = []
            for name in ("wxid_first", "wxid_second"):
                account_root = root / name
                message_dir = account_root / "db_storage" / "message"
                (account_root / "msg" / "attach").mkdir(parents=True)
                message_dir.mkdir(parents=True)
                accounts.append(WeChatAccount(name, root, msg_dir=message_dir))

            def switch_account(*_args, **_kwargs):
                api.config.active_index = 1
                return result

            with patch.object(api.config, "accounts", accounts), \
                    patch.object(api.config, "active_index", 0), \
                    patch.object(api, "extract_image_aes_key", side_effect=switch_account), \
                    patch.object(api, "save_settings") as save_mock:
                with self.assertRaises(HTTPException) as switched:
                    await api.extract_chat_image_key("1")
                self.assertEqual(switched.exception.status_code, 409)
                save_mock.assert_not_called()

    async def test_rejects_explicit_empty_or_implicit_unbounded_scope(self):
        with self.assertRaises(HTTPException) as empty:
            await api.recognize_chat_images(
                "friend", api.ImageRecognitionRequest(message_refs=[])
            )
        self.assertEqual(empty.exception.status_code, 400)

        with self.assertRaises(HTTPException) as implicit_all:
            await api.recognize_chat_images(
                "friend", api.ImageRecognitionRequest()
            )
        self.assertEqual(implicit_all.exception.status_code, 400)

    async def test_decrypted_image_response_is_never_browser_cached(self):
        calls = {}
        fake_image = SimpleNamespace(
            mime_type="image/png",
            source_path=Path("sample_h.dat"),
        )
        def get_image_bytes(**kwargs):
            calls.update(kwargs)
            return fake_image, b"png-bytes"
        fake_service = SimpleNamespace(
            get_image_bytes=get_image_bytes
        )
        message = {
            "id": 7,
            "type": 3,
            "create_time": 100,
            "server_id": 700,
        }
        with patch.object(api, "get_parser", return_value=object()), \
                patch.object(api, "_find_image_message", return_value=message), \
                patch.object(api, "get_image_service", return_value=fake_service):
            response = await api.get_chat_image(
                "friend", 7, create_time=100, message_key=None, quality="best"
            )
        self.assertEqual(response.body, b"png-bytes")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-image-quality"], "enhanced")
        self.assertEqual(calls["quality"], "best")

    async def test_hd_automation_requires_explicit_local_client_header(self):
        request = api.ImageHDAutomationRequest(all_images=True)
        with self.assertRaises(HTTPException) as missing_header:
            await api.start_image_hd_automation(
                "friend", request, x_wechat_assistant=None
            )
        self.assertEqual(missing_header.exception.status_code, 403)

        with self.assertRaises(HTTPException) as pause_header:
            await api.pause_image_hd_automation_task(
                "task", x_wechat_assistant=None
            )
        self.assertEqual(pause_header.exception.status_code, 403)

        with self.assertRaises(HTTPException) as cancel_header:
            await api.cancel_image_hd_automation_task(
                "task", x_wechat_assistant=None
            )
        self.assertEqual(cancel_header.exception.status_code, 403)

        with self.assertRaises(HTTPException) as navigation_header:
            await api.set_image_hd_automation_navigation_mode(
                "task",
                api.ImageHDNavigationModeRequest(mode="manual"),
                x_wechat_assistant=None,
            )
        self.assertEqual(navigation_header.exception.status_code, 403)

    async def test_hd_automation_navigation_mode_endpoint_updates_paused_task(self):
        paused = {
            "task_id": "task",
            "status": "paused",
            "navigation_mode": "auto",
            "can_switch_to_manual": True,
        }
        switched = {**paused, "navigation_mode": "manual"}
        with patch.object(
            api._image_hd_automation_manager,
            "get",
            side_effect=[paused, switched],
        ), patch.object(
            api._image_hd_automation_manager,
            "set_navigation_mode",
            return_value=True,
        ) as set_mode:
            response = await api.set_image_hd_automation_navigation_mode(
                "task",
                api.ImageHDNavigationModeRequest(mode="manual"),
                x_wechat_assistant="1",
            )

        set_mode.assert_called_once_with("task", "manual")
        self.assertEqual(response["navigation_mode"], "manual")

    async def test_hd_automation_navigation_mode_endpoint_rejects_running_task(self):
        with patch.object(
            api._image_hd_automation_manager,
            "get",
            return_value={"task_id": "task", "status": "running"},
        ), patch.object(
            api._image_hd_automation_manager,
            "set_navigation_mode",
            side_effect=RuntimeError("只能在任务暂停时切换翻页模式"),
        ):
            with self.assertRaises(HTTPException) as conflict:
                await api.set_image_hd_automation_navigation_mode(
                    "task",
                    api.ImageHDNavigationModeRequest(mode="manual"),
                    x_wechat_assistant="1",
                )
        self.assertEqual(conflict.exception.status_code, 409)

    def test_hd_automation_request_validates_task_timing(self):
        request = api.ImageHDAutomationRequest(
            all_images=True,
            per_image_timeout=10,
            min_dwell_seconds=0,
        )
        self.assertEqual(request.min_dwell_seconds, 0)

        for update in (
            {"per_image_timeout": 4},
            {"per_image_timeout": 121},
            {"min_dwell_seconds": -0.1},
            {"min_dwell_seconds": 5.1},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                api.ImageHDAutomationRequest(all_images=True, **update)

    async def test_hd_automation_rejects_unknown_direction_before_ui_actions(self):
        request = api.ImageHDAutomationRequest(
            all_images=True,
            direction="diagonal",
        )
        with self.assertRaises(HTTPException) as invalid:
            await api.start_image_hd_automation(
                "friend", request, x_wechat_assistant="1"
            )
        self.assertEqual(invalid.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
