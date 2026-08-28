import hashlib
import sqlite3
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.voice_service import (
    SILK_MAGIC,
    VoiceServiceError,
    WeChatVoiceService,
    discover_media_databases,
    normalize_silk_data,
)


class VoiceServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.msg_dir = Path(self.tmp.name)
        self.payload_a = b"\x02" + SILK_MAGIC + b"voice-a"
        self.payload_b = b"\x02" + SILK_MAGIC + b"voice-b"
        self._create_media_db(self.msg_dir / "media_0.db")

    def _create_media_db(self, path):
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT NOT NULL)")
            conn.execute("INSERT INTO Name2Id(user_name) VALUES ('friend')")
            conn.execute(
                """
                CREATE TABLE VoiceInfo(
                    chat_name_id INTEGER NOT NULL,
                    create_time INTEGER NOT NULL,
                    local_id INTEGER NOT NULL,
                    svr_id INTEGER NOT NULL,
                    voice_data BLOB NOT NULL,
                    data_index INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT INTO VoiceInfo VALUES (1, 1700000123, 17, 700, ?, 0)",
                (self.payload_a,),
            )
            conn.execute(
                "INSERT INTO VoiceInfo VALUES (1, 1700000123, 17, 701, ?, 0)",
                (self.payload_b,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_discovers_shards_in_numeric_order(self):
        (self.msg_dir / "media_10.db").write_bytes(b"not-empty")
        (self.msg_dir / "media_2.db").write_bytes(b"not-empty")
        (self.msg_dir / "media_x.db").write_bytes(b"ignored")
        (self.msg_dir / "media_1.db").write_bytes(b"")

        self.assertEqual(
            [path.name for path in discover_media_databases(self.msg_dir)],
            ["media_0.db", "media_2.db", "media_10.db"],
        )

    def test_exact_server_identity_resolves_reused_local_id(self):
        with WeChatVoiceService("account-a", self.msg_dir, "unused-for-plain-db") as service:
            resource = service.get_voice("friend", 17, 1_700_000_123, 701)

        self.assertEqual(resource.voice_data, self.payload_b)
        self.assertEqual(resource.server_id, "701")
        self.assertEqual(resource.source_shard, "media_0.db")
        self.assertEqual(resource.audio_sha256, hashlib.sha256(self.payload_b).hexdigest())

    def test_ambiguous_weak_identity_is_rejected(self):
        with WeChatVoiceService("account-a", self.msg_dir, "unused") as service:
            with self.assertRaises(VoiceServiceError) as caught:
                service.get_voice("friend", 17, 1_700_000_123)

        self.assertEqual(caught.exception.code, "AMBIGUOUS_VOICE")

    def test_mock_silk_decoder_produces_valid_wav(self):
        def decode(source, output, sample_rate):
            self.assertTrue(source.read().startswith(SILK_MAGIC))
            self.assertEqual(sample_rate, 24_000)
            output.write(b"\x01\x00" * 24_000)

        fake_pysilk = SimpleNamespace(decode=decode)
        with patch.dict(sys.modules, {"pysilk": fake_pysilk}):
            with WeChatVoiceService("account-a", self.msg_dir, "unused") as service:
                decoded = service.decode_voice("friend", 17, 1_700_000_123, 700)

        self.assertAlmostEqual(decoded.duration_seconds, 1.0)
        self.assertTrue(decoded.wav_data.startswith(b"RIFF"))
        with wave.open(__import__("io").BytesIO(decoded.wav_data), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24_000)
            self.assertEqual(wav.getnframes(), 24_000)

    def test_normalizes_wechat_silk_wrapper_and_rejects_other_audio(self):
        self.assertEqual(normalize_silk_data(self.payload_a), SILK_MAGIC + b"voice-a")
        with self.assertRaises(VoiceServiceError) as caught:
            normalize_silk_data(b"not-silk")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_AUDIO")


if __name__ == "__main__":
    unittest.main()
