import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import api


class PrivateTemporaryFileCleanupTests(unittest.TestCase):
    def test_cleanup_preserves_live_owner_and_recent_legacy_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / f"wechat_db_{os.getpid()}_live.db"
            dead = root / "wechat_db_999999_dead.db"
            recent_legacy = root / "wechat_recent.db"
            old_legacy = root / "wechat_old.db"
            for path in (live, dead, recent_legacy, old_legacy):
                path.write_bytes(b"private")
            old_timestamp = time.time() - 90000
            os.utime(old_legacy, (old_timestamp, old_timestamp))

            with patch.object(
                api.tempfile, "gettempdir", return_value=str(root)
            ), patch(
                "psutil.pid_exists",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                api._cleanup_temp_files()

            self.assertTrue(live.exists())
            self.assertFalse(dead.exists())
            self.assertTrue(recent_legacy.exists())
            self.assertFalse(old_legacy.exists())


if __name__ == "__main__":
    unittest.main()
