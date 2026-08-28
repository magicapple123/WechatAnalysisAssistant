import json
import unittest
from unittest.mock import patch

from backend.vision import (
    AnthropicVisionClient,
    CustomJSONVisionClient,
    GeminiVisionClient,
    OpenAICompatibleVisionClient,
    VisionAPIError,
    VisionConfig,
    create_vision_client,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class VisionClientTests(unittest.TestCase):
    def test_rejects_unknown_provider(self):
        with self.assertRaises(VisionAPIError):
            VisionConfig(
                base_url="https://api.example.test/v1",
                api_key="test-secret",
                model="vision-model",
                provider="unknown",
            ).validate()

    def test_plain_http_is_only_allowed_for_loopback(self):
        with self.assertRaises(VisionAPIError):
            VisionConfig(
                base_url="http://api.example.test/v1",
                api_key="test-secret",
                model="vision-model",
            ).validate()
        VisionConfig(
            base_url="http://127.0.0.1:11434/v1",
            api_key="local-key",
            model="local-model",
        ).validate()

    def test_sends_base64_data_url_and_parses_description(self):
        config = VisionConfig(
            base_url="https://api.example.test/v1",
            api_key="test-secret",
            model="vision-model",
        )
        client = OpenAICompatibleVisionClient(config)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["authorization"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(
                {"choices": [{"message": {"content": "一张包含蓝天的风景照片。"}}]}
            )

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            description = client.describe_image(b"sample-image", "image/jpeg")

        self.assertEqual(description, "一张包含蓝天的风景照片。")
        self.assertEqual(captured["url"], "https://api.example.test/v1/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-secret")
        content = captured["body"]["messages"][1]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertNotIn("test-secret", json.dumps(captured["body"]))

    def test_azure_client_sends_api_key_header_in_actual_request(self):
        client = OpenAICompatibleVisionClient(
            VisionConfig(
                provider="openai_compatible",
                interface_preset="azure_openai",
                base_url="https://resource.openai.azure.com/openai/v1",
                api_key="azure-secret",
                model="vision-deployment",
            )
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["api_key"] = request.headers.get("Api-key")
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse(
                {"choices": [{"message": {"content": "Azure 图片描述"}}]}
            )

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            self.assertEqual(
                client.describe_image(b"sample-image", "image/jpeg"),
                "Azure 图片描述",
            )

        self.assertEqual(captured["api_key"], "azure-secret")
        self.assertIsNone(captured["authorization"])

    def test_anthropic_messages_request_and_response(self):
        config = VisionConfig(
            base_url="https://api.anthropic.com/v1",
            api_key="anthropic-secret",
            model="claude-vision-model",
            provider="anthropic",
        )
        client = create_vision_client(config)
        self.assertIsInstance(client, AnthropicVisionClient)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.headers.get("X-api-key")
            captured["version"] = request.headers.get("Anthropic-version")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"content": [{"type": "text", "text": "一张风景照片。"}]})

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            description = client.describe_image(b"sample-image", "image/png")

        self.assertEqual(description, "一张风景照片。")
        self.assertEqual(captured["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(captured["key"], "anthropic-secret")
        self.assertEqual(captured["version"], "2023-06-01")
        source = captured["body"]["messages"][0]["content"][0]["source"]
        self.assertEqual(source["type"], "base64")
        self.assertEqual(source["media_type"], "image/png")

    def test_gemini_generate_content_request_and_response(self):
        config = VisionConfig(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="gemini-secret",
            model="gemini-vision-model",
            provider="gemini",
        )
        client = create_vision_client(config)
        self.assertIsInstance(client, GeminiVisionClient)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.headers.get("X-goog-api-key")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({
                "candidates": [{"content": {"parts": [{"text": "一张聊天截图。"}]}}]
            })

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            description = client.describe_image(b"sample-image", "image/jpeg")

        self.assertEqual(description, "一张聊天截图。")
        self.assertEqual(
            captured["url"],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-vision-model:generateContent",
        )
        self.assertEqual(captured["key"], "gemini-secret")
        inline = captured["body"]["contents"][0]["parts"][0]["inline_data"]
        self.assertEqual(inline["mime_type"], "image/jpeg")

    def test_fully_custom_json_protocol_replaces_fields_and_reads_path(self):
        template = json.dumps({
            "engine": "{{model}}",
            "input": {
                "instruction": "{{prompt}}",
                "image": "{{image_base64}}",
                "mime": "{{mime_type}}",
                "quality": "{{detail}}",
            },
        })
        config = VisionConfig(
            base_url="https://vision.example.test/analyze",
            api_key="custom-secret",
            model="custom-model",
            provider="custom",
            custom_name="内部视觉接口",
            custom_protocol="custom_json",
            custom_api_key_header="X-Custom-Key",
            custom_api_key_prefix="Token ",
            custom_extra_headers=json.dumps({"X-API-Version": "v2"}),
            custom_request_template=template,
            custom_response_path="result.items.0.caption",
            detail="high",
        )
        client = create_vision_client(config)
        self.assertIsInstance(client, CustomJSONVisionClient)
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["key"] = request.headers.get("X-custom-key")
            captured["version"] = request.headers.get("X-api-version")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({
                "result": {"items": [{"caption": "自定义接口返回的描述。"}]}
            })

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            description = client.describe_image(b"sample-image", "image/png")

        self.assertEqual(description, "自定义接口返回的描述。")
        self.assertEqual(captured["url"], "https://vision.example.test/analyze")
        self.assertEqual(captured["key"], "Token custom-secret")
        self.assertEqual(captured["version"], "v2")
        self.assertEqual(captured["body"]["engine"], "custom-model")
        self.assertEqual(captured["body"]["input"]["mime"], "image/png")
        self.assertEqual(captured["body"]["input"]["quality"], "high")

    def test_custom_named_interface_can_reuse_a_builtin_protocol(self):
        config = VisionConfig(
            base_url="https://proxy.example.test/v1",
            api_key="proxy-secret",
            model="proxy-model",
            provider="custom",
            custom_name="我的代理",
            custom_protocol="openai_compatible",
        )
        self.assertIsInstance(create_vision_client(config), OpenAICompatibleVisionClient)


if __name__ == "__main__":
    unittest.main()
