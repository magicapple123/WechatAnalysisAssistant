import io
import json
import threading
import unittest
import urllib.error

from backend.ai_analysis import (
    MAX_INPUT_CHARS,
    MAX_REPORT_CHARS,
    AIAnalysisCancelled,
    AIAnalysisDeadlineExceeded,
    AIAnalysisError,
    AnalysisConfig,
    AnalysisRunControl,
    JSONTransport,
    analyze_records,
    analyze_text,
    create_analysis_client,
    records_to_analysis_text,
)


SECRET = "sk-analysis-secret-never-leak"


def make_config(**changes):
    values = {
        "provider": "openai_compatible",
        "base_url": "https://api.example.test/v1",
        "api_key": SECRET,
        "model": "analysis-model",
        "retry_count": 0,
        "chunk_chars": 1000,
    }
    values.update(changes)
    return AnalysisConfig(**values)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post_json(self, endpoint, payload, headers):
        self.calls.append(
            {
                "endpoint": endpoint,
                "payload": payload,
                "headers": dict(headers),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum):
        return self.payload[:maximum]


class SequenceOpener:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class RecordingClient:
    def __init__(self, *, long_map=False, final_text="# 分析报告\n\n完成"):
        self.calls = []
        self.long_map = long_map
        self.final_text = final_text

    def complete(
        self,
        system_prompt,
        user_prompt,
        *,
        max_output_tokens=None,
        source_type="text",
        strength=None,
        detail_level=None,
    ):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_prompt,
                "tokens": max_output_tokens,
                "source_type": source_type,
                "strength": strength,
                "detail_level": detail_level,
            }
        )
        if "请分析第" in user_prompt:
            return ("分块事实。" * 5000) if self.long_map else "分块事实与证据。"
        if "合并成一个更紧凑" in user_prompt:
            return "归并后的跨分块事实。"
        return self.final_text


class RaisingClient:
    def complete(self, *_args, **_kwargs):
        raise RuntimeError("provider exposed " + SECRET)


class AnalysisConfigTests(unittest.TestCase):
    def test_from_settings_reads_analysis_section_and_hides_secret(self):
        settings = {
            "analysis": {
                "provider": "custom",
                "custom_name": "内部模型",
                "custom_protocol": "custom_json",
                "base_url": "http://127.0.0.1:9000/analyze",
                "api_key": SECRET,
                "model": "local-model",
                "analysis_strength": "deep",
                "detail_level": "detailed",
                "custom_requirements": "关注时间变化",
                "retry_count": 3,
                "timeout_seconds": 300,
            }
        }
        config = AnalysisConfig.from_settings(settings)
        config.validate()

        self.assertEqual(config.protocol, "custom_json")
        self.assertEqual(config.strength, "deep")
        self.assertEqual(config.detail_level, "detailed")
        self.assertNotIn(SECRET, repr(config))
        self.assertNotIn(SECRET, json.dumps(config.public_metadata()))

    def test_remote_http_is_rejected_but_loopback_http_is_allowed(self):
        with self.assertRaises(AIAnalysisError):
            make_config(base_url="http://api.example.test/v1").validate()

        make_config(base_url="http://127.0.0.2:8080/v1").validate()
        make_config(base_url="http://[::1]:8080/v1").validate()
        make_config(base_url="http://localhost:8080/v1").validate()

    def test_url_userinfo_fragment_and_invalid_port_are_rejected(self):
        urls = (
            "https://user:password@example.test/v1",
            "https://example.test/v1#fragment",
            "https://example.test:not-a-port/v1",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(AIAnalysisError):
                make_config(base_url=url).validate()

    def test_custom_headers_template_and_response_path_are_strict(self):
        base = {
            "provider": "custom_json",
            "custom_name": "自定义",
            "base_url": "https://custom.example.test/analyze",
            "api_key": SECRET,
            "model": "custom-model",
        }
        invalid = (
            {"custom_api_key_prefix": "Bearer\r\nInjected: yes"},
            {
                "custom_extra_headers": json.dumps(
                    {"Authorization": "duplicate"}
                )
            },
            {"custom_request_template": json.dumps({"model": "{{model}}"})},
            {
                "custom_request_template": json.dumps(
                    {"input": "{{prompt}}", "bad": "{{api_key}}"}
                )
            },
            {"custom_response_path": "choices..content"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(AIAnalysisError):
                AnalysisConfig(**base, **changes).validate()


class ProviderAdapterTests(unittest.TestCase):
    def test_protocol_endpoints_append_to_path_and_preserve_query(self):
        cases = (
            (
                make_config(
                    base_url=(
                        "https://api.example.test/v1"
                        "?api-version=2026-01-01&region=cn"
                    )
                ),
                (
                    "https://api.example.test/v1/chat/completions"
                    "?api-version=2026-01-01&region=cn"
                ),
            ),
            (
                make_config(
                    provider="anthropic",
                    base_url="https://api.example.test/v1/messages?beta=true",
                ),
                "https://api.example.test/v1/messages?beta=true",
            ),
            (
                make_config(
                    provider="gemini",
                    model="models/gemini-test",
                    base_url="https://api.example.test/v1beta?tenant=a%2Fb",
                ),
                (
                    "https://api.example.test/v1beta/models/"
                    "gemini-test:generateContent?tenant=a%2Fb"
                ),
            ),
        )
        for config, expected in cases:
            with self.subTest(protocol=config.protocol):
                client = create_analysis_client(
                    config, transport=FakeTransport()
                )
                self.assertEqual(client.endpoint, expected)

    def test_openai_compatible_request_and_response(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": "# OpenAI 报告"}}]}
        )
        client = create_analysis_client(make_config(), transport=transport)

        result = client.complete("system", "records", max_output_tokens=777)

        self.assertEqual(result, "# OpenAI 报告")
        call = transport.calls[0]
        self.assertEqual(
            call["endpoint"], "https://api.example.test/v1/chat/completions"
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + SECRET)
        self.assertEqual(call["payload"]["max_completion_tokens"], 777)

    def test_azure_client_sends_api_key_header_in_actual_request(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": "# Azure 报告"}}]}
        )
        client = create_analysis_client(
            make_config(
                interface_preset="azure_openai",
                base_url="https://resource.openai.azure.com/openai/v1",
            ),
            transport=transport,
        )

        self.assertEqual(client.complete("system", "records"), "# Azure 报告")
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["api-key"], SECRET)
        self.assertNotIn("Authorization", headers)

    def test_openai_compatible_falls_back_to_legacy_token_field_once(self):
        transport = FakeTransport(
            AIAnalysisError("文本分析 API 请求失败（HTTP 400）", http_status=400),
            {"choices": [{"message": {"content": "# 兼容接口报告"}}]},
        )
        client = create_analysis_client(make_config(), transport=transport)

        result = client.complete("system", "records", max_output_tokens=777)

        self.assertEqual(result, "# 兼容接口报告")
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("max_completion_tokens", transport.calls[0]["payload"])
        self.assertNotIn("max_completion_tokens", transport.calls[1]["payload"])
        self.assertEqual(transport.calls[1]["payload"]["max_tokens"], 777)

    def test_provider_response_cannot_echo_api_key_into_result(self):
        transport = FakeTransport(
            {"choices": [{"message": {"content": "报告 " + SECRET}}]}
        )
        client = create_analysis_client(make_config(), transport=transport)

        result = client.complete("system", "records")

        self.assertNotIn(SECRET, result)

    def test_anthropic_messages_request_and_response(self):
        transport = FakeTransport(
            {"content": [{"type": "text", "text": "# Anthropic 报告"}]}
        )
        config = make_config(provider="anthropic")
        client = create_analysis_client(config, transport=transport)

        result = client.complete("system", "records")

        self.assertEqual(result, "# Anthropic 报告")
        call = transport.calls[0]
        self.assertEqual(call["endpoint"], "https://api.example.test/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], SECRET)
        self.assertEqual(call["payload"]["system"], "system")

    def test_gemini_request_and_response(self):
        transport = FakeTransport(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "# Gemini 报告"}]}}
                ]
            }
        )
        config = make_config(provider="gemini", model="models/gemini-test")
        client = create_analysis_client(config, transport=transport)

        result = client.complete("system", "records")

        self.assertEqual(result, "# Gemini 报告")
        call = transport.calls[0]
        self.assertEqual(
            call["endpoint"],
            "https://api.example.test/v1/models/gemini-test:generateContent",
        )
        self.assertEqual(call["headers"]["x-goog-api-key"], SECRET)
        self.assertEqual(
            call["payload"]["systemInstruction"]["parts"][0]["text"],
            "system",
        )

    def test_custom_json_uses_typed_template_headers_and_response_path(self):
        template = json.dumps(
            {
                "model_name": "{{model}}",
                "input": "{{prompt}}",
                "system": "{{system_prompt}}",
                "limit": "{{max_output_tokens}}",
                "temperature": "{{temperature}}",
                "source": "{{source_type}}",
                "mode": "{{analysis_strength}}/{{detail_level}}",
            }
        )
        config = make_config(
            provider="custom_json",
            custom_name="自定义",
            base_url="https://custom.example.test/analyze",
            custom_api_key_header="X-API-Key",
            custom_api_key_prefix="Token ",
            custom_extra_headers=json.dumps({"X-Client": "wechat-assistant"}),
            custom_request_template=template,
            custom_response_path="data.report",
            strength="deep",
            detail_level="detailed",
        )
        transport = FakeTransport({"data": {"report": "# 自定义报告"}})
        client = create_analysis_client(config, transport=transport)

        result = client.complete(
            "system",
            "records",
            max_output_tokens=888,
            source_type="moments",
            strength="deep",
            detail_level="detailed",
        )

        self.assertEqual(result, "# 自定义报告")
        call = transport.calls[0]
        self.assertEqual(call["endpoint"], config.base_url)
        self.assertEqual(call["headers"]["X-API-Key"], "Token " + SECRET)
        self.assertEqual(call["payload"]["limit"], 888)
        self.assertIsInstance(call["payload"]["limit"], int)
        self.assertIsInstance(call["payload"]["temperature"], float)
        self.assertEqual(call["payload"]["source"], "moments")


class TransportSafetyTests(unittest.TestCase):
    def test_retryable_http_error_is_retried_without_leaking_secret(self):
        error = urllib.error.HTTPError(
            "https://api.example.test/v1",
            500,
            "server echoed " + SECRET,
            None,
            io.BytesIO(("body " + SECRET).encode()),
        )
        success = FakeResponse(json.dumps({"ok": True}).encode())
        opener = SequenceOpener(error, success)
        sleeps = []
        config = make_config(retry_count=1)
        transport = JSONTransport(config, opener=opener, sleeper=sleeps.append)

        result = transport.post_json(
            "https://api.example.test/analyze",
            {"input": "safe"},
            {"Authorization": "Bearer " + SECRET},
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(len(sleeps), 1)

    def test_non_retryable_error_is_sanitized(self):
        error = urllib.error.HTTPError(
            "https://api.example.test/v1",
            400,
            "bad key " + SECRET,
            None,
            io.BytesIO(SECRET.encode()),
        )
        transport = JSONTransport(
            make_config(retry_count=3),
            opener=SequenceOpener(error),
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(AIAnalysisError) as captured:
            transport.post_json(
                "https://api.example.test/analyze",
                {"input": "safe"},
                {"Authorization": "Bearer " + SECRET},
            )

        self.assertNotIn(SECRET, str(captured.exception))
        self.assertEqual(captured.exception.__cause__, None)

    def test_task_deadline_clamps_each_http_request_timeout(self):
        now = [100.0]
        control = AnalysisRunControl.for_timeout(3, clock=lambda: now[0])
        opener = SequenceOpener(FakeResponse(b'{"ok": true}'))
        transport = JSONTransport(make_config(timeout_seconds=90), opener=opener)

        result = transport.post_json(
            "https://api.example.test/v1/analyze",
            {"prompt": "hello"},
            {"Authorization": "Bearer " + SECRET},
            run_control=control,
        )

        self.assertEqual({"ok": True}, result)
        self.assertLessEqual(opener.requests[0][1], 3.0)


class RunControlTests(unittest.TestCase):
    def test_cancelled_run_never_calls_model(self):
        cancel_event = threading.Event()
        cancel_event.set()
        client = RecordingClient()

        with self.assertRaises(AIAnalysisCancelled):
            analyze_text(
                "待分析内容",
                make_config(),
                client=client,
                cancel_event=cancel_event,
                total_timeout_seconds=30,
            )

        self.assertEqual([], client.calls)

    def test_deadline_stops_run_after_current_model_call(self):
        now = [0.0]

        class AdvancingClient(RecordingClient):
            def complete(self, *args, **kwargs):
                value = super().complete(*args, **kwargs)
                now[0] += 6.0
                return value

        with self.assertRaises(AIAnalysisDeadlineExceeded):
            analyze_text(
                "待分析内容",
                make_config(),
                client=AdvancingClient(),
                total_timeout_seconds=5,
                clock=lambda: now[0],
            )


class RecordSerializationTests(unittest.TestCase):
    def test_chat_records_include_descriptions_but_ignore_unknown_secret_fields(self):
        text = records_to_analysis_text(
            [
                {
                    "time_str": "2026-07-16 10:00:00",
                    "sender_name": "小王",
                    "type_name": "图片",
                    "content": "[图片]",
                    "image_description": "一只猫坐在窗边",
                    "api_key": SECRET,
                },
                {
                    "is_self": True,
                    "type_name": "语音",
                    "voice_transcription": "明天上午见",
                },
            ],
            source_type="chat",
        )

        self.assertIn("小王", text)
        self.assertIn("图片内容：一只猫坐在窗边", text)
        self.assertIn("语音转写：明天上午见", text)
        self.assertNotIn(SECRET, text)

    def test_moments_records_include_media_likes_and_comments(self):
        text = records_to_analysis_text(
            [
                {
                    "create_time_str": "2026-07-15",
                    "author_name": "小李",
                    "content": "周末爬山",
                    "media": [{"description": "山顶日出"}],
                    "likes": [{"display_name": "小王"}, "我"],
                    "comments": [
                        {
                            "display_name": "我",
                            "reply_to_name": "小李",
                            "content": "风景真好",
                        }
                    ],
                }
            ],
            source_type="moments",
        )

        self.assertIn("媒体：山顶日出", text)
        self.assertIn("点赞：小王、我", text)
        self.assertIn("我 回复 小李：风景真好", text)

    def test_api_projected_record_shapes_keep_sender_author_and_interactions(self):
        chat = records_to_analysis_text(
            [
                {
                    "sequence": 1,
                    "timestamp": "2026-07-16 10:00:00",
                    "sender": "我",
                    "type": "文本",
                    "content": "确认方案",
                }
            ],
            source_type="chat",
        )
        moments = records_to_analysis_text(
            [
                {
                    "sequence": 1,
                    "timestamp": "2026-07-16 11:00:00",
                    "author": "小李",
                    "content_type": "图文",
                    "content": "周末出游",
                    "is_pinned": True,
                    "location": "西湖",
                    "media_count": 3,
                    "interactions": ["点赞：小王", "评论：我：真好看"],
                }
            ],
            source_type="moments",
        )

        self.assertIn("我 (文本): 确认方案", chat)
        self.assertIn("小李", moments)
        self.assertIn("状态：置顶", moments)
        self.assertIn("位置：西湖", moments)
        self.assertIn("媒体数量：3", moments)
        self.assertIn("评论：我：真好看", moments)


class MapReduceTests(unittest.TestCase):
    def test_structured_records_are_chunked_and_overrides_reach_metadata(self):
        records = [
            {
                "time_str": f"2026-07-{index:02d}",
                "sender_name": "小王",
                "content": ("关于项目进度的讨论。" * 60),
            }
            for index in range(1, 8)
        ]
        client = RecordingClient()
        result = analyze_records(
            records,
            make_config(strength="quick", detail_level="brief"),
            source_type="chat",
            title="项目沟通分析",
            strength="deep",
            detail_level="detailed",
            custom_requirements="重点关注承诺是否兑现",
            source_metadata={"contact": "小王", "keyword": "项目"},
            client=client,
        )

        self.assertTrue(result.markdown.startswith("#"))
        self.assertGreater(result.metadata["chunk_count"], 1)
        self.assertEqual(result.metadata["map_requests"], result.metadata["chunk_count"])
        self.assertEqual(result.metadata["request_count"], len(client.calls))
        self.assertEqual(result.metadata["strength"], "deep")
        self.assertEqual(result.metadata["detail_level"], "detailed")
        self.assertEqual(result.metadata["source"]["record_count"], len(records))
        self.assertEqual(result.metadata["source"]["keyword"], "项目")
        self.assertTrue(
            all(call["source_type"] == "chat" for call in client.calls)
        )
        self.assertTrue(
            all("重点关注承诺是否兑现" in call["user"] for call in client.calls)
        )
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertNotIn(SECRET, serialized)

    def test_short_text_uses_one_direct_request_and_adds_markdown_heading(self):
        client = RecordingClient(final_text="核心结论")
        result = analyze_text(
            "[10:00] 小王：你好",
            make_config(),
            source_type="chat",
            client=client,
        )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result.metadata["chunk_count"], 1)
        self.assertEqual(result.metadata["map_requests"], 0)
        self.assertEqual(result.metadata["request_count"], 1)
        self.assertTrue(result.markdown.startswith("# 微信内容分析报告"))

    def test_large_intermediate_summaries_use_hierarchical_reduce(self):
        client = RecordingClient(long_map=True)
        text = "\n".join(("记录内容" * 200) for _ in range(5))
        result = analyze_text(
            text,
            make_config(chunk_chars=1000),
            source_type="moments",
            client=client,
        )

        self.assertGreater(result.metadata["chunk_count"], 2)
        self.assertGreaterEqual(result.metadata["reduce_rounds"], 2)
        self.assertGreater(
            result.metadata["request_count"], result.metadata["chunk_count"] + 1
        )

    def test_input_report_and_metadata_boundaries_are_enforced(self):
        client = RecordingClient()
        with self.assertRaises(AIAnalysisError):
            analyze_text(
                "x" * (MAX_INPUT_CHARS + 1),
                make_config(),
                client=client,
            )
        self.assertEqual(client.calls, [])

        too_long = RecordingClient(final_text="x" * (MAX_REPORT_CHARS + 1))
        with self.assertRaises(AIAnalysisError):
            analyze_text("short", make_config(), client=too_long)

        with self.assertRaises(AIAnalysisError) as captured:
            analyze_text(
                "short",
                make_config(),
                source_metadata={"api_key": SECRET},
                client=RecordingClient(),
            )
        self.assertNotIn(SECRET, str(captured.exception))

    def test_injected_client_output_and_error_cannot_leak_api_key(self):
        echoed = analyze_text(
            "short",
            make_config(),
            client=RecordingClient(final_text="报告中包含 " + SECRET),
        )
        self.assertNotIn(SECRET, echoed.markdown)

        with self.assertRaises(AIAnalysisError) as captured:
            analyze_text("short", make_config(), client=RaisingClient())
        self.assertNotIn(SECRET, str(captured.exception))
        self.assertEqual(captured.exception.__cause__, None)


if __name__ == "__main__":
    unittest.main()
