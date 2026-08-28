import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.transcription import TranscriptionConfig
from backend.voice_store import VoiceTranscriptionStore
from backend.voice_transcription import (
    VoiceTranscriptionManager,
    transcription_settings_hash,
)


class NeverDecodeVoiceService:
    def decode_voice(self, *_args, **_kwargs):
        raise AssertionError("exact transcription cache should avoid voice decoding")


class FakeVoiceService:
    def __init__(self):
        self.calls = 0

    def decode_voice(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            wav_data=b"RIFF" + b"\x00" * 40,
            resource=SimpleNamespace(audio_sha256="a" * 64),
            duration_seconds=3.5,
        )


class FakeClient:
    def __init__(self, text="转写完成"):
        self.text = text
        self.calls = 0

    def transcribe(self, _wav):
        self.calls += 1
        return self.text


class VoiceTranscriptionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = VoiceTranscriptionStore(Path(self.temp.name) / "voice.sqlite3")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def _wait(self, manager, task_id):
        for _ in range(200):
            job = manager.get(task_id)
            if job and job["status"] in ("completed", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.01)
        self.fail("voice transcription task did not finish")

    async def test_exact_cache_hit_does_not_decode_or_transcribe(self):
        config = TranscriptionConfig(provider="openai_compatible", base_url="https://api.example.test/v1", api_key="test-key", model="whisper-1")
        settings_hash = transcription_settings_hash(config)
        self.store.save(
            account_id="account-a",
            talker="friend",
            message_id=1,
            create_time=2,
            server_id="3",
            audio_sha256="b" * 64,
            transcription="已有转写",
            status="success",
            provider=config.protocol,
            model=config.model,
            language=config.language,
            settings_hash=settings_hash,
        )
        manager = VoiceTranscriptionManager(self.store)
        with patch("backend.voice_transcription.create_transcription_client", return_value=FakeClient()):
            job = manager.start(
                account_id="account-a",
                talker="friend",
                messages=[{"id": 1, "create_time": 2, "server_id": "3", "message_key": "k"}],
                voice_service=NeverDecodeVoiceService(),
                transcription_config=config,
            )
            completed = await self._wait(manager, job["task_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["cached"], 1)
        self.assertEqual(completed["failed"], 0)
        self.assertEqual(completed["results"][0]["transcription"], "已有转写")

    async def test_new_transcription_is_saved_and_returned(self):
        config = TranscriptionConfig(provider="openai_compatible", base_url="https://api.example.test/v1", api_key="test-key", model="whisper-1")
        client = FakeClient("新的转写")
        service = FakeVoiceService()
        manager = VoiceTranscriptionManager(self.store)
        with patch("backend.voice_transcription.create_transcription_client", return_value=client):
            job = manager.start(
                account_id="account-a",
                talker="friend",
                messages=[{"id": 7, "create_time": 8, "server_id": "9", "message_key": "stable"}],
                voice_service=service,
                transcription_config=config,
            )
            completed = await self._wait(manager, job["task_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["results"][0]["status"], "success")
        self.assertEqual(completed["results"][0]["message_key"], "stable")
        self.assertEqual(service.calls, 1)
        self.assertEqual(client.calls, 1)
        record = self.store.get("account-a", "friend", 7, 8, server_id="9")
        self.assertEqual(record["transcription"], "新的转写")
        self.assertEqual(record["status"], "success")


if __name__ == "__main__":
    unittest.main()
