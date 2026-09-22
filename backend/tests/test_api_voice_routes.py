import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from backend import api


CLOUD_SETTINGS = {
    "transcription": {
        "provider": "openai_compatible",
        "base_url": "https://speech.example.test/v1",
        "api_key": "test-key",
        "model": "whisper-1",
        "language": "auto",
        "max_voices_per_task": 20,
    }
}


class VoiceRouteSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_cancel_require_local_client_header(self):
        with self.assertRaises(HTTPException) as create_error:
            api.transcribe_chat_voices(
                "friend",
                api.VoiceTranscriptionRequest(all_voices=True),
                None,
            )
        self.assertEqual(create_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as cancel_error:
            await api.cancel_voice_transcription_task("task", None)
        self.assertEqual(cancel_error.exception.status_code, 403)

    async def test_explicit_empty_and_implicit_unbounded_scopes_are_rejected(self):
        with self.assertRaises(HTTPException) as empty:
            api.transcribe_chat_voices(
                "friend",
                api.VoiceTranscriptionRequest(message_refs=[]),
                "1",
            )
        self.assertEqual(empty.exception.status_code, 400)

        with self.assertRaises(HTTPException) as implicit_all:
            api.transcribe_chat_voices(
                "friend",
                api.VoiceTranscriptionRequest(),
                "1",
            )
        self.assertEqual(implicit_all.exception.status_code, 400)

    async def test_cloud_provider_requires_confirmation_before_reading_messages(self):
        cloud = {
            "transcription": {
                "provider": "openai_compatible",
                "base_url": "https://speech.example.test/v1",
                "api_key": "secret",
                "model": "whisper-1",
                "language": "auto",
            }
        }
        with patch.object(api, "load_settings", return_value=cloud), \
                patch.object(api, "get_parser") as parser:
            with self.assertRaises(HTTPException) as caught:
                api.transcribe_chat_voices(
                    "friend",
                    api.VoiceTranscriptionRequest(all_voices=True),
                    "1",
                )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("上传", str(caught.exception.detail))
        parser.assert_not_called()

    async def test_exact_message_reference_starts_bounded_cloud_task(self):
        message = {
            "id": 7,
            "type": 34,
            "create_time": 100,
            "server_id": 700,
        }
        manager = api._voice_transcription_manager
        fake_service = SimpleNamespace()
        with patch.object(api, "load_settings", return_value=CLOUD_SETTINGS), \
                patch.object(api, "get_parser", return_value=SimpleNamespace()), \
                patch.object(api, "_find_voice_message", return_value=message) as find, \
                patch.object(api, "get_voice_service", return_value=fake_service), \
                patch.object(
                    manager,
                    "start",
                    return_value={"task_id": "voice-task", "status": "queued", "total": 1},
                ) as start:
            response = api.transcribe_chat_voices(
                "friend",
                api.VoiceTranscriptionRequest(
                    message_refs=[
                        api.MessageReference(
                            id=7,
                            create_time=100,
                            message_key="friend:700:100:7",
                        )
                    ],
                    cloud_upload_confirmed=True,
                ),
                "1",
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["task_id"], "voice-task")
        find.assert_called_once_with(
            unittest.mock.ANY,
            "friend",
            7,
            100,
            expected_key="friend:700:100:7",
        )
        started_messages = start.call_args.kwargs["messages"]
        self.assertEqual(started_messages[0]["message_key"], "friend:700:100:7")
        self.assertIs(start.call_args.kwargs["voice_service"], fake_service)

    async def test_transcription_secret_is_retained_and_never_returned(self):
        existing = {
            "transcription": {
                "provider": "openai_compatible",
                "base_url": "https://speech.example.test/v1",
                "api_key": "do-not-leak",
                "model": "whisper-1",
                "language": "auto",
            },
            "vision": {},
            "images": {"accounts": {}},
        }
        saved = {}

        def fake_save(update):
            saved.update(update)
            merged = dict(existing)
            for section, values in update.items():
                current = dict(merged.get(section, {}))
                current.update(values)
                merged[section] = current
            return merged

        with patch.object(api, "load_settings", return_value=existing), \
                patch.object(api, "save_settings", side_effect=fake_save), \
                patch.object(api, "_close_runtime_caches"):
            response = await api.update_settings(
                api.SettingsUpdate(transcription={"language": "zh"})
            )

        self.assertEqual(saved["transcription"]["api_key"], "do-not-leak")
        public = response["data"]["transcription"]
        self.assertTrue(public["has_api_key"])
        self.assertNotIn("api_key", public)
        self.assertNotIn("do-not-leak", str(response))


if __name__ == "__main__":
    unittest.main()
