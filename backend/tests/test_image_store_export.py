import json
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from backend import api
from backend.exporter import ChatExporter
from backend.image_store import ImageDescriptionStore
from backend.settings import public_settings


class FakeParser:
    def __init__(self, messages):
        self.messages = messages

    def get_messages(self, _talker, **_kwargs):
        return {
            "messages": [dict(message) for message in self.messages],
            "total": len(self.messages),
            "total_pages": 1,
        }

    def get_contacts(self):
        return []


class FakeImageService:
    def __init__(self, payload=b"embedded-image-bytes"):
        self.payload = payload
        self.calls = []

    def get_image_bytes(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(mime_type="image/png"), self.payload


class DescriptionExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = ImageDescriptionStore(base / "descriptions.sqlite3")
        self.message = {
            "id": 7,
            "server_id": 70,
            "type": 3,
            "type_name": "图片",
            "is_sender": True,
            "content": "[图片]",
            "content_preview": "[图片]",
            "create_time": 1_700_000_000,
            "time_str": "2023-11-14 00:00:00",
            "date_str": "2023年11月14日",
        }
        self.store.save(
            account_id="account-a",
            talker="friend",
            message_id=7,
            create_time=self.message["create_time"],
            server_id=self.message["server_id"],
            description="一只猫坐在窗边。",
            status="success",
        )
        self.exporter = ChatExporter(
            FakeParser([self.message]),
            base / "exports",
            description_store=self.store,
            account_id="account-a",
        )

    def test_json_contains_description_and_replaced_content(self):
        path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="json",
            replace_images_with_descriptions=True,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        exported = data["messages"][0]
        self.assertEqual(exported["image_description"], "一只猫坐在窗边。")
        self.assertEqual(exported["content"], "[图片内容] 一只猫坐在窗边。")

    def test_export_without_replace_preserves_placeholder(self):
        path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="txt",
            replace_images_with_descriptions=False,
        )
        self.assertIn("[图片]", path.read_text(encoding="utf-8"))

    def test_export_format_is_normalized_and_rejected_before_work(self):
        path = self.exporter.export_chat("friend", "Friend", fmt=" JSON ")
        self.assertEqual(path.suffix, ".json")

        with self.assertRaisesRegex(ValueError, "不支持的导出格式"):
            self.exporter.export_chat("friend", "Friend", fmt="pdf")
        with self.assertRaisesRegex(ValueError, "不支持的导出格式"):
            self.exporter.export_all_chats(fmt="pdf")

    def test_html_embeds_images_by_default_with_best_quality(self):
        image_service = FakeImageService()
        exporter = ChatExporter(
            FakeParser([self.message]),
            Path(self.tmp.name) / "html-exports",
            description_store=self.store,
            account_id="account-a",
            image_service=image_service,
        )

        path = exporter.export_chat("friend", "Friend", fmt="html")
        html = path.read_text(encoding="utf-8")
        encoded = base64.b64encode(image_service.payload).decode("ascii")

        self.assertIn(f"data:image/png;base64,{encoded}", html)
        self.assertIn("一只猫坐在窗边。", html)
        self.assertEqual(image_service.calls[0]["quality"], "best")
        self.assertNotIn(str(Path(self.tmp.name)), html)

    def test_html_thumbnail_option_is_forwarded_to_image_service(self):
        image_service = FakeImageService()
        exporter = ChatExporter(
            FakeParser([self.message]),
            Path(self.tmp.name) / "thumbnail-exports",
            image_service=image_service,
        )

        exporter.export_chat(
            "friend",
            "Friend",
            fmt="html",
            html_image_quality="thumbnail",
        )

        self.assertEqual(image_service.calls[0]["quality"], "thumbnail")

    def test_description_replacement_disables_html_image_embedding(self):
        image_service = FakeImageService()
        exporter = ChatExporter(
            FakeParser([self.message]),
            Path(self.tmp.name) / "description-html-exports",
            description_store=self.store,
            account_id="account-a",
            image_service=image_service,
        )

        path = exporter.export_chat(
            "friend",
            "Friend",
            fmt="html",
            replace_images_with_descriptions=True,
        )
        html = path.read_text(encoding="utf-8")

        self.assertIn("[图片内容] 一只猫坐在窗边。", html)
        self.assertNotIn("data:image/", html)
        self.assertEqual(image_service.calls, [])

    def test_same_local_id_and_second_are_isolated_by_server_id(self):
        other = dict(self.message, server_id=71)
        self.store.save(
            account_id="account-a",
            talker="friend",
            message_id=7,
            create_time=self.message["create_time"],
            server_id=71,
            description="另一张图片。",
            status="success",
        )
        records = self.store.get_many(
            "account-a", "friend", [self.message, other]
        )
        self.assertEqual(
            records[(7, self.message["create_time"], "70")]["description"],
            "一只猫坐在窗边。",
        )
        self.assertEqual(
            records[(7, self.message["create_time"], "71")]["description"],
            "另一张图片。",
        )

    def test_exact_export_reference_disambiguates_reused_local_id(self):
        other = dict(self.message, server_id=71, content="[图片-另一条]")
        exporter = ChatExporter(
            FakeParser([self.message, other]),
            Path(self.tmp.name) / "exact-exports",
            description_store=self.store,
            account_id="account-a",
        )
        path = exporter.export_chat(
            "friend",
            "Friend",
            fmt="json",
            message_refs=[{
                "id": 7,
                "create_time": self.message["create_time"],
                "message_key": (
                    f"friend:71:{self.message['create_time']}:7"
                ),
            }],
        )
        exported = json.loads(path.read_text(encoding="utf-8"))["messages"]
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["content"], "[图片-另一条]")


class PublicSettingsTests(unittest.TestCase):
    def test_secrets_are_masked_and_account_scoped(self):
        settings = {
            "vision": {
                "base_url": "https://api.example/v1",
                "model": "vision-model",
                "api_key": "secret-value",
            },
            "images": {
                "accounts": {
                    "account-a": {"aes_key": "1234567890abcdef", "xor_key": "0x37"},
                    "account-b": {"aes_key": "another-secret-key", "xor_key": "0x88"},
                }
            },
        }
        public = public_settings(settings, "account-a")
        self.assertNotIn("api_key", public["vision"])
        self.assertTrue(public["vision"]["has_api_key"])
        self.assertEqual(public["vision"]["api_key_hint"], "••••alue")
        self.assertNotIn("accounts", public["images"])
        self.assertTrue(public["images"]["has_aes_key"])
        self.assertEqual(public["images"]["xor_key"], "0x37")
        self.assertNotIn("another-secret-key", json.dumps(public, ensure_ascii=False))


class ExportRouteValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_format_is_rejected_before_database_access(self):
        request = api.ExportRequest(
            talker="friend",
            display_name="Friend",
            format="pdf",
        )
        with patch.object(api, "get_parser") as get_parser:
            with self.assertRaises(HTTPException) as single_rejected:
                api.export_chat(request)
            with self.assertRaises(HTTPException) as all_rejected:
                api.export_all(fmt="pdf")

        self.assertEqual(single_rejected.exception.status_code, 400)
        self.assertEqual(all_rejected.exception.status_code, 400)
        get_parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
