import io
import json
import unittest
import wave
from unittest.mock import patch

from backend.transcription import (
    CustomMultipartTranscriptionClient,
    OpenAICompatibleTranscriptionClient,
    TranscriptionAPIError,
    TranscriptionConfig,
)


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(b"\x00\x00" * 240)
    return output.getvalue()


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


class TranscriptionClientTests(unittest.TestCase):
    def test_remote_plain_http_is_rejected_but_loopback_is_allowed(self):
        with self.assertRaises(TranscriptionAPIError):
            TranscriptionConfig(
                provider="openai_compatible",
                base_url="http://api.example.test/v1",
                api_key="secret",
                model="whisper-1",
            ).validate()
        TranscriptionConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:8000/v1",
            api_key="secret",
            model="whisper-1",
        ).validate()

    def test_openai_compatible_sends_bounded_multipart_and_parses_text(self):
        client = OpenAICompatibleTranscriptionClient(
            TranscriptionConfig(
                provider="openai_compatible",
                base_url="https://api.example.test/v1",
                api_key="private-key",
                model="whisper-large-v3",
                language="zh",
                retry_count=0,
            )
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["authorization"] = request.headers.get("Authorization")
            captured["content_type"] = request.headers.get("Content-type")
            captured["body"] = request.data
            return FakeResponse({"text": "  这是一段微信语音。  "})

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            result = client.transcribe(wav_bytes())

        self.assertEqual(result, "这是一段微信语音。")
        self.assertEqual(captured["url"], "https://api.example.test/v1/audio/transcriptions")
        self.assertEqual(captured["authorization"], "Bearer private-key")
        self.assertIn("multipart/form-data", captured["content_type"])
        self.assertIn(b'name="model"', captured["body"])
        self.assertIn(b"whisper-large-v3", captured["body"])
        self.assertIn(b'name="language"', captured["body"])
        self.assertNotIn(b"private-key", captured["body"])

    def test_azure_client_sends_api_key_header_in_actual_request(self):
        client = OpenAICompatibleTranscriptionClient(
            TranscriptionConfig(
                provider="openai_compatible",
                interface_preset="azure_openai",
                base_url="https://resource.openai.azure.com/openai/v1",
                api_key="azure-secret",
                model="audio-deployment",
                retry_count=0,
            )
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["api_key"] = request.headers.get("Api-key")
            captured["authorization"] = request.headers.get("Authorization")
            return FakeResponse({"text": "Azure 转写结果"})

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            self.assertEqual(client.transcribe(wav_bytes()), "Azure 转写结果")

        self.assertEqual(captured["api_key"], "azure-secret")
        self.assertIsNone(captured["authorization"])

    def test_custom_multipart_fields_headers_and_response_path(self):
        client = CustomMultipartTranscriptionClient(
            TranscriptionConfig(
                provider="custom_multipart",
                custom_name="内部语音接口",
                base_url="https://speech.example.test/transcribe",
                api_key="custom-secret",
                model="speech-model",
                language="zh",
                custom_api_key_header="X-Speech-Key",
                custom_api_key_prefix="Token ",
                custom_extra_headers=json.dumps({"X-API-Version": "v2"}),
                custom_audio_field="audio_blob",
                custom_model_field="engine",
                custom_language_field="locale",
                custom_extra_form_fields=json.dumps({"mode": "chat"}),
                custom_response_path="result.items.0.text",
                retry_count=0,
            )
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["key"] = request.headers.get("X-speech-key")
            captured["version"] = request.headers.get("X-api-version")
            captured["body"] = request.data
            return FakeResponse({"result": {"items": [{"text": "自定义结果"}]}})

        with patch("urllib.request.OpenerDirector.open", side_effect=fake_urlopen):
            result = client.transcribe(wav_bytes())

        self.assertEqual(result, "自定义结果")
        self.assertEqual(captured["key"], "Token custom-secret")
        self.assertEqual(captured["version"], "v2")
        self.assertIn(b'name="audio_blob"', captured["body"])
        self.assertIn(b'name="engine"', captured["body"])
        self.assertIn(b'name="locale"', captured["body"])
        self.assertIn(b'name="mode"', captured["body"])

if __name__ == "__main__":
    unittest.main()
