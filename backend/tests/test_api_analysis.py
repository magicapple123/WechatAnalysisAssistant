import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend import api
from backend.settings import public_settings


def analysis_settings():
    return {
        "analysis": {
            "provider": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "api_key": "analysis-secret",
            "model": "summary-model",
            "timeout_seconds": 30,
            "presets": [
                {
                    "id": "balanced-review",
                    "name": "标准分析",
                    "strength": "balanced",
                    "detail": "standard",
                    "requirements": "先按时间梳理。",
                }
            ],
        }
    }


class ConnectedRequest:
    async def is_disconnected(self):
        return False


class AnalysisSettingsTests(unittest.TestCase):
    def test_public_settings_masks_analysis_api_key(self):
        public = public_settings(analysis_settings())
        self.assertNotIn("api_key", public["analysis"])
        self.assertTrue(public["analysis"]["has_api_key"])
        self.assertEqual(public["analysis"]["api_key_hint"], "••••cret")

    def test_request_rejects_oversized_requirements(self):
        with self.assertRaises(ValidationError):
            api.ChatAnalysisRequest(
                display_name="好友",
                all_messages=True,
                requirements="x" * 4001,
                cloud_upload_confirmed=True,
            )

    def test_settings_request_bounds_incomplete_custom_payloads(self):
        """Size checks run even before a provider has all required fields."""
        oversized_template = "界" * (api.MAX_CUSTOM_TEMPLATE_BYTES // 3 + 1)
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={
                "custom_request_template": oversized_template,
            })

        oversized_headers = "界" * (api.MAX_EXTRA_HEADERS_BYTES // 3 + 1)
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={
                "custom_extra_headers": oversized_headers,
            })

        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={"api_key": "k" * 16385})

    def test_settings_request_rejects_wrong_types_and_unbounded_presets(self):
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={"timeout_seconds": "90"})
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={"clear_api_key": 1})
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={"presets": {"id": "not-a-list"}})
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={
                "presets": [
                    {
                        "id": f"preset-{index}",
                        "name": "预设",
                        "requirements": "",
                    }
                    for index in range(51)
                ],
            })

    def test_settings_request_bounds_presets_and_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={
                "presets": [{
                    "id": "oversized",
                    "name": "预设",
                    "requirements": "x" * 4001,
                }],
            })
        with self.assertRaises(ValidationError):
            api.SettingsUpdate(analysis={"unexpected": "x"})

    def test_settings_request_keeps_existing_partial_update_shape(self):
        request = api.SettingsUpdate(analysis={
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model": "analysis-model",
            "timeout_seconds": 45,
            "presets": analysis_settings()["analysis"]["presets"],
        })
        payload = request.analysis.model_dump(exclude_none=True)
        self.assertEqual(payload["timeout_seconds"], 45)
        self.assertEqual(payload["presets"][0]["id"], "balanced-review")
        self.assertNotIn("api_key", payload)

    def test_chat_analysis_rejects_missing_empty_and_invalid_message_keys(self):
        invalid_references = (
            {"id": 7, "create_time": 500},
            {"id": 7, "create_time": 500, "message_key": ""},
            {"id": 7, "create_time": 500, "message_key": "not-an-exact-key"},
            {
                "id": 7,
                "create_time": 500,
                "message_key": "wxid_friend:101:501:7",
            },
            {
                "id": 7,
                "create_time": 500,
                "message_key": "x" * 1025,
            },
        )
        for reference in invalid_references:
            with self.subTest(reference=reference), self.assertRaises(ValidationError):
                api.ChatAnalysisRequest(
                    display_name="好友",
                    message_refs=[reference],
                    cloud_upload_confirmed=True,
                )

    def test_saved_preset_and_per_run_requirements_are_merged(self):
        request = api.ChatAnalysisRequest(
            display_name="好友",
            all_messages=True,
            preset_id="balanced-review",
            strength="deep",
            requirements="重点关注行动项。",
            cloud_upload_confirmed=True,
        )
        resolved = api._resolve_analysis_options(request, analysis_settings())
        self.assertEqual(resolved[1], "deep")
        self.assertEqual(resolved[2], "standard")
        self.assertEqual(
            resolved[3], "先按时间梳理。\n\n重点关注行动项。"
        )
        self.assertEqual(resolved[4], "标准分析")

    def test_exact_chat_reference_disambiguates_reused_local_id(self):
        talker = "wxid_friend"
        expected = {
            "id": 7,
            "server_id": 101,
            "create_time": 500,
            "type": 1,
            "type_name": "文本",
            "content": "正确消息",
        }
        other = {**expected, "server_id": 202, "content": "错误分库消息"}

        class Parser:
            @staticmethod
            def get_messages(_talker, **_kwargs):
                return {
                    "total": 2,
                    "total_pages": 1,
                    "messages": [other, expected],
                }

        reference = api.AnalysisMessageReference(
            id=7,
            create_time=500,
            message_key=api._message_key(talker, expected),
        )
        with patch.object(api, "_enrich_image_descriptions", return_value=None):
            messages = api._collect_chat_analysis_messages(
                Parser(), talker, message_refs=[reference]
            )
        self.assertEqual([item["content"] for item in messages], ["正确消息"])


class FakeChatParser:
    def get_messages(self, _talker, **_kwargs):
        return {
            "total": 2,
            "total_pages": 1,
            "messages": [
                {
                    "id": 1,
                    "server_id": 101,
                    "create_time": 100,
                    "is_sender": True,
                    "sender_name": "",
                    "type": 1,
                    "type_name": "文本",
                    "content": "你好",
                },
                {
                    "id": 2,
                    "server_id": 102,
                    "create_time": 101,
                    "is_sender": False,
                    "sender_name": "",
                    "type": 1,
                    "type_name": "文本",
                    "content": "收到",
                },
            ],
        }


class AnalysisReportStorageTests(unittest.TestCase):
    def test_same_second_reservations_are_unique_and_save_in_place(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            api, "get_export_dir", return_value=Path(temporary)
        ), patch.object(api, "datetime") as datetime_mock:
            datetime_mock.now.return_value.strftime.return_value = (
                "20260716_120000"
            )

            first = api._reserve_analysis_report_path("测试报告")
            second = api._reserve_analysis_report_path("测试报告")

            self.assertNotEqual(first, second)
            self.assertEqual(first.name, "测试报告_20260716_120000.md")
            self.assertEqual(second.name, "测试报告_20260716_120000(1).md")
            api._save_analysis_report(
                "# 第一份", "测试报告", destination=first
            )
            api._save_analysis_report(
                "# 第二份", "测试报告", destination=second
            )
            self.assertEqual(first.read_text(encoding="utf-8"), "# 第一份")
            self.assertEqual(second.read_text(encoding="utf-8"), "# 第二份")


class AnalysisRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_update_preserves_existing_analysis_key(self):
        previous = analysis_settings()
        incoming = api.SettingsUpdate(analysis={
            "provider": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "model": "analysis-model",
            "timeout_seconds": 45,
            "presets": previous["analysis"]["presets"],
        })
        saved = {}

        def fake_save(data):
            saved.update(data)
            merged = {**previous, **data}
            merged["analysis"] = {**previous["analysis"], **data["analysis"]}
            return merged

        with patch.object(api, "load_settings", return_value=previous), patch.object(
            api, "save_settings", side_effect=fake_save
        ):
            response = await api.update_settings(incoming)

        self.assertEqual(saved["analysis"]["api_key"], "analysis-secret")
        self.assertEqual(saved["analysis"]["provider"], "openai_compatible")
        self.assertNotIn("api_key", response["data"]["analysis"])
        self.assertTrue(response["data"]["analysis"]["has_api_key"])

    async def test_chat_analysis_requires_header_and_per_run_confirmation(self):
        request = api.ChatAnalysisRequest(
            display_name="好友", all_messages=True, cloud_upload_confirmed=True
        )
        with self.assertRaises(HTTPException) as missing_header:
            await api.analyze_chat_records(
                "wxid_friend", request, ConnectedRequest(), None
            )
        self.assertEqual(missing_header.exception.status_code, 403)

        unconfirmed = api.ChatAnalysisRequest(
            display_name="好友", all_messages=True
        )
        with self.assertRaises(HTTPException) as confirmation:
            await api.analyze_chat_records(
                "wxid_friend", unconfirmed, ConnectedRequest(), "1"
            )
        self.assertEqual(confirmation.exception.status_code, 400)

    async def test_chat_analysis_uses_exact_text_projection_and_saves_report(self):
        captured = {}

        def fake_analyze(records, _config, **kwargs):
            captured["records"] = records
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                markdown="# 报告\n\n完成。",
                metadata={"request_count": 1},
            )

        request = api.ChatAnalysisRequest(
            display_name="好友",
            all_messages=True,
            preset_id="balanced-review",
            requirements="列出结论。",
            cloud_upload_confirmed=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.md"
            with patch.object(
                api, "load_settings", return_value=analysis_settings()
            ), patch.object(
                api, "get_parser", return_value=FakeChatParser()
            ), patch.object(
                api, "_enrich_image_descriptions", return_value=None
            ), patch.object(
                api, "analyze_records", side_effect=fake_analyze
            ), patch.object(
                api, "_preflight_analysis_report", return_value=destination
            ), patch.object(
                api,
                "_finish_analysis_report",
                return_value=(destination, ""),
            ):
                response = await api.analyze_chat_records(
                    "wxid_friend", request, ConnectedRequest(), "1"
                )

        self.assertTrue(response["success"])
        self.assertEqual(response["report_markdown"], "# 报告\n\n完成。")
        self.assertEqual(len(captured["records"]), 2)
        self.assertEqual(captured["records"][1]["sender_name"], "好友")
        self.assertEqual(captured["kwargs"]["source_type"], "chat")
        self.assertIn("先按时间梳理", captured["kwargs"]["custom_requirements"])
        self.assertEqual(
            captured["kwargs"]["total_timeout_seconds"],
            api.AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS,
        )
        self.assertFalse(captured["kwargs"]["cancel_event"].is_set())
        self.assertNotIn("analysis-secret", str(response))

    async def test_chat_analysis_preflights_export_before_model_request(self):
        request = api.ChatAnalysisRequest(
            display_name="好友",
            all_messages=True,
            cloud_upload_confirmed=True,
        )
        with patch.object(
            api, "load_settings", return_value=analysis_settings()
        ), patch.object(
            api, "get_parser", return_value=FakeChatParser()
        ), patch.object(
            api, "_enrich_image_descriptions", return_value=None
        ), patch.object(
            api,
            "_reserve_analysis_report_path",
            side_effect=PermissionError("read only"),
        ), patch.object(api, "analyze_records") as analyze_mock:
            with self.assertRaises(HTTPException) as error:
                await api.analyze_chat_records(
                    "wxid_friend", request, ConnectedRequest(), "1"
                )

        self.assertEqual(error.exception.status_code, 500)
        analyze_mock.assert_not_called()

    async def test_chat_analysis_returns_markdown_when_auto_save_fails(self):
        request = api.ChatAnalysisRequest(
            display_name="好友",
            all_messages=True,
            cloud_upload_confirmed=True,
        )
        result = SimpleNamespace(
            markdown="# 已生成报告\n\n正文。",
            metadata={"request_count": 1},
        )
        with tempfile.TemporaryDirectory() as temporary:
            reservation = Path(temporary) / "reserved.md"
            reservation.write_text("", encoding="utf-8")
            with patch.object(
                api, "load_settings", return_value=analysis_settings()
            ), patch.object(
                api, "get_parser", return_value=FakeChatParser()
            ), patch.object(
                api, "_enrich_image_descriptions", return_value=None
            ), patch.object(
                api, "_preflight_analysis_report", return_value=reservation
            ), patch.object(
                api, "analyze_records", return_value=result
            ), patch.object(
                api,
                "_save_analysis_report",
                side_effect=PermissionError("disk full"),
            ):
                response = await api.analyze_chat_records(
                    "wxid_friend", request, ConnectedRequest(), "1"
                )

            self.assertFalse(reservation.exists())

        self.assertTrue(response["success"])
        self.assertEqual(response["report_markdown"], result.markdown)
        self.assertEqual(response["path"], "")
        self.assertEqual(response["filename"], "")
        self.assertIn("仍可预览和下载", response["warning"])

    async def test_chat_analysis_requires_exactly_one_scope(self):
        request = api.ChatAnalysisRequest(
            display_name="好友",
            all_messages=True,
            start_time=1,
            cloud_upload_confirmed=True,
        )
        with self.assertRaises(HTTPException) as error:
            await api.analyze_chat_records(
                "wxid_friend", request, ConnectedRequest(), "1"
            )
        self.assertEqual(error.exception.status_code, 400)

    async def test_analysis_concurrency_limit_rejects_without_model_call(self):
        acquired = 0
        try:
            for _ in range(api.AI_ANALYSIS_MAX_CONCURRENT_TASKS):
                self.assertTrue(api._analysis_task_slots.acquire(blocking=False))
                acquired += 1
            with patch.object(api, "analyze_records") as analyze_mock:
                with self.assertRaises(HTTPException) as error:
                    await api._run_analysis_with_limits(
                        ConnectedRequest(),
                        [{"content": "hello"}],
                        api.AnalysisConfig.from_settings(analysis_settings()),
                        source_type="chat",
                    )
            self.assertEqual(error.exception.status_code, 429)
            analyze_mock.assert_not_called()
        finally:
            for _ in range(acquired):
                api._analysis_task_slots.release()

    async def test_disconnected_client_cancels_analysis_worker(self):
        class DisconnectedRequest:
            async def is_disconnected(self):
                return True

        def wait_for_cancel(_records, _config, **kwargs):
            cancel_event = kwargs["cancel_event"]
            self.assertTrue(cancel_event.wait(2))
            raise api.AIAnalysisCancelled("AI 分析任务已取消")

        with patch.object(api, "analyze_records", side_effect=wait_for_cancel):
            with self.assertRaises(api.AIAnalysisCancelled):
                await api._run_analysis_with_limits(
                    DisconnectedRequest(),
                    [{"content": "hello"}],
                    api.AnalysisConfig.from_settings(analysis_settings()),
                    source_type="chat",
                )

    async def test_moments_analysis_projects_comments_without_media_bytes(self):
        captured = {}

        class FakeMomentsService:
            @staticmethod
            def get_contacts():
                return [{"username": "friend", "display_name": "好友备注"}]

            @staticmethod
            def get_posts(_usernames, start_time=None, end_time=None, tids=None):
                return [{
                    "tid": "1",
                    "username": "friend",
                    "create_time": 100,
                    "content": "今天去公园",
                    "media": [{"url": "https://example.test/private.jpg"}],
                    "comments": [
                        {"type": 1, "from_display_name": "小王"},
                        {
                            "type": 2,
                            "from_display_name": "我",
                            "content": "天气真好",
                        },
                    ],
                }]

        def fake_analyze(records, _config, **kwargs):
            captured["records"] = records
            captured["kwargs"] = kwargs
            return SimpleNamespace(markdown="# 朋友圈报告", metadata={})

        request = api.MomentsAnalysisRequest(
            usernames=["friend"],
            preset_id="balanced-review",
            cloud_upload_confirmed=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "moments.md"
            with patch.object(
                api, "load_settings", return_value=analysis_settings()
            ), patch.object(
                api, "get_moments_service", return_value=FakeMomentsService()
            ), patch.object(
                api, "analyze_records", side_effect=fake_analyze
            ), patch.object(
                api, "_preflight_analysis_report", return_value=destination
            ), patch.object(
                api,
                "_finish_analysis_report",
                return_value=(destination, ""),
            ):
                response = await api.analyze_moments_records(
                    request, ConnectedRequest(), "1"
                )

        record = captured["records"][0]
        self.assertEqual(record["author_name"], "好友备注")
        self.assertEqual(record["likes"], [{"display_name": "小王"}])
        self.assertEqual(record["comments"][0]["content"], "天气真好")
        self.assertNotIn("private.jpg", str(record))
        self.assertEqual(captured["kwargs"]["source_type"], "moments")
        self.assertEqual(
            captured["kwargs"]["total_timeout_seconds"],
            api.AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS,
        )
        self.assertTrue(response["success"])


if __name__ == "__main__":
    unittest.main()
