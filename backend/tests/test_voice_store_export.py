import csv
import json
import tempfile
import unittest
from pathlib import Path

from backend.exporter import ChatExporter
from backend.voice_store import VoiceTranscriptionStore


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


def voice_message(*, server_id=700, content="[语音]", message_type=34):
    return {
        "id": 17,
        "server_id": server_id,
        "type": message_type,
        "type_name": "语音",
        "is_sender": False,
        "sender_name": "好友",
        "content": content,
        "content_preview": content,
        "create_time": 1_700_000_123,
        "time_str": "2023-11-14 22:15:23",
        "date_str": "2023年11月14日",
    }


class VoiceTranscriptionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = VoiceTranscriptionStore(
            Path(self.tmp.name) / "voice-transcriptions.sqlite3"
        )

    def save(self, *, account_id="account-a", transcription="你好", **overrides):
        values = {
            "account_id": account_id,
            "talker": "friend",
            "message_id": 17,
            "create_time": 1_700_000_123,
            "server_id": 700,
            "audio_sha256": "ab" * 32,
            "transcription": transcription,
            "status": "success",
            "provider": "openai_compatible",
            "model": "whisper-1",
            "language": "zh",
            "settings_hash": "settings-v1",
        }
        values.update(overrides)
        return self.store.save(**values)

    def test_exact_identity_and_account_are_isolated(self):
        self.save(transcription="账号 A")
        self.save(account_id="account-b", transcription="账号 B")
        self.save(server_id=701, transcription="另一条语音")

        records = self.store.get_many(
            "account-a",
            "friend",
            [voice_message(server_id=700), voice_message(server_id=701)],
        )

        self.assertEqual(records[(17, 1_700_000_123, "700")]["transcription"], "账号 A")
        self.assertEqual(records[(17, 1_700_000_123, "701")]["transcription"], "另一条语音")
        self.assertEqual(
            self.store.get("account-b", "friend", 17, 1_700_000_123, 700)[
                "transcription"
            ],
            "账号 B",
        )

    def test_reuse_requires_account_audio_and_model_signature(self):
        self.save()

        reusable = self.store.find_reusable(
            "account-a", "ab" * 32, "openai_compatible", "whisper-1", "zh", "settings-v1"
        )
        wrong_account = self.store.find_reusable(
            "account-b", "ab" * 32, "openai_compatible", "whisper-1", "zh", "settings-v1"
        )
        wrong_model = self.store.find_reusable(
            "account-a", "ab" * 32, "openai_compatible", "small", "zh", "settings-v1"
        )

        self.assertEqual(reusable["transcription"], "你好")
        self.assertIsNone(wrong_account)
        self.assertIsNone(wrong_model)


class VoiceTranscriptionExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.store = VoiceTranscriptionStore(base / "voice.sqlite3")
        self.message = voice_message()
        self.store.save(
            account_id="account-a",
            talker="friend",
            message_id=self.message["id"],
            create_time=self.message["create_time"],
            server_id=self.message["server_id"],
            audio_sha256="cd" * 32,
            transcription="你好 <世界> & 再见",
            status="success",
            provider="openai_compatible",
            model="whisper-1",
            language="zh",
        )
        self.exporter = ChatExporter(
            FakeParser([self.message]),
            base / "exports",
            account_id="account-a",
            voice_store=self.store,
        )

    def test_html_txt_and_csv_can_replace_voice_placeholder(self):
        html_path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="html",
            replace_voices_with_transcriptions=True,
        )
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("[语音转文字] 你好 &lt;世界&gt; &amp; 再见", html)
        self.assertNotIn("你好 <世界>", html)

        txt_path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="txt",
            replace_voices_with_transcriptions=True,
        )
        self.assertIn(
            "[语音转文字] 你好 <世界> & 再见",
            txt_path.read_text(encoding="utf-8"),
        )

        csv_path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="csv",
            replace_voices_with_transcriptions=True,
        )
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[1][4], "[语音转文字] 你好 <世界> & 再见")

    def test_disabled_replacement_preserves_voice_placeholder(self):
        path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="txt",
            replace_voices_with_transcriptions=False,
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("[语音]", text)
        self.assertNotIn("[语音转文字]", text)

    def test_json_keeps_original_content_and_structured_transcription(self):
        path = self.exporter.export_chat(
            "friend",
            "Friend",
            fmt="json",
            replace_voices_with_transcriptions=True,
        )
        exported = json.loads(path.read_text(encoding="utf-8"))["messages"][0]

        self.assertEqual(exported["content"], "[语音]")
        self.assertEqual(exported["voice_transcription"], "你好 <世界> & 再见")
        self.assertEqual(exported["voice_transcription_status"], "success")
        self.assertEqual(
            exported["message_key"],
            "friend:700:1700000123:17",
        )

    def test_composite_voice_type_is_supported(self):
        message = voice_message(message_type=(9 << 32) | 34)
        exporter = ChatExporter(
            FakeParser([message]),
            Path(self.tmp.name) / "composite",
            account_id="account-a",
            voice_store=self.store,
        )
        path = exporter.export_chat(
            "friend",
            "Friend",
            fmt="txt",
            replace_voices_with_transcriptions=True,
        )
        self.assertIn("[语音转文字]", path.read_text(encoding="utf-8"))

    def test_reused_local_id_uses_exact_server_id(self):
        other = voice_message(server_id=701)
        self.store.save(
            account_id="account-a",
            talker="friend",
            message_id=other["id"],
            create_time=other["create_time"],
            server_id=other["server_id"],
            transcription="第二条",
            status="success",
        )
        exporter = ChatExporter(
            FakeParser([self.message, other]),
            Path(self.tmp.name) / "exact",
            account_id="account-a",
            voice_store=self.store,
        )
        path = exporter.export_chat("friend", "Friend", fmt="json")
        messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
        by_server = {str(item["server_id"]): item for item in messages}
        self.assertEqual(by_server["700"]["voice_transcription"], "你好 <世界> & 再见")
        self.assertEqual(by_server["701"]["voice_transcription"], "第二条")


if __name__ == "__main__":
    unittest.main()
