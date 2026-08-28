import asyncio
import base64
import hashlib
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.image_hd_automation import ImageHDAutomationManager
from backend.image_hd_resource import (
    ImageHDTarget,
    RESOURCE_HIGH,
    RESOURCE_MID,
    RESOURCE_THUMBNAIL,
    WeChatHDResourceMonitor,
)
from backend.wechat_viewer_automation import (
    HOTKEY_START_RESUME,
    HOTKEY_STOP,
    ViewerAutomationError,
    WeChatViewerController,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def encrypted_png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (80, 120, 160)).save(buffer, format="PNG")
    return bytes(value ^ 0x57 for value in buffer.getvalue())


def packed(file_md5: str) -> bytes:
    return b"prefix\x12\x22\x0a\x20" + file_md5.encode("ascii") + b"suffix"


class ImageHDResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.account_root = root / "account"
        self.database = root / "message_resource.db"
        self.talker = "friend"
        self.file_md5 = hashlib.md5(b"image-resource").hexdigest()

        conn = sqlite3.connect(self.database)
        conn.executescript(
            """
            CREATE TABLE ChatName2Id (user_name TEXT);
            CREATE TABLE MessageResourceInfo (
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                message_local_type INTEGER,
                message_create_time INTEGER,
                message_local_id INTEGER,
                message_svr_id INTEGER,
                packed_info BLOB
            );
            CREATE TABLE MessageResourceDetail (
                resource_id INTEGER PRIMARY KEY,
                message_id INTEGER,
                type INTEGER,
                size INTEGER,
                access_time INTEGER,
                status INTEGER
            );
            """
        )
        conn.execute("INSERT INTO ChatName2Id VALUES (?)", (self.talker,))
        chat_id = conn.execute("SELECT rowid FROM ChatName2Id").fetchone()[0]
        for resource_id in (10, 11):
            conn.execute(
                "INSERT INTO MessageResourceInfo VALUES (?, ?, ?, ?, ?, ?, ?)",
                (resource_id, chat_id, 3, 100, 7, 700, packed(self.file_md5)),
            )

        encrypted = bytes(value ^ 0x57 for value in PNG)
        talker_hash = hashlib.md5(self.talker.encode()).hexdigest()
        image_dir = (
            self.account_root / "msg" / "attach" / talker_hash / "bucket" / "Img"
        )
        image_dir.mkdir(parents=True)
        self.high_path = image_dir / f"{self.file_md5}_h.dat"
        self.high_path.write_bytes(encrypted)
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (1, 10, RESOURCE_HIGH, len(encrypted), 1, 0),
        )
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (2, 11, RESOURCE_HIGH, len(encrypted), 2, 1),
        )
        conn.commit()
        conn.close()

    def monitor(self):
        return WeChatHDResourceMonitor(
            account_id="account",
            account_root=self.account_root,
            encrypted_resource_db=self.database,
            database_key="00" * 32,
        )

    def test_duplicate_resource_rows_are_merged_and_file_is_verified(self):
        targets = self.monitor().prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].resource_info_ids, (10, 11))
        self.assertTrue(targets[0].initially_verified)

    def test_detail_size_must_match_stable_file(self):
        conn = sqlite3.connect(self.database)
        conn.execute(
            "UPDATE MessageResourceDetail SET size=size+1 WHERE status=1"
        )
        conn.commit()
        conn.close()

        targets = self.monitor().prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )

        self.assertFalse(targets[0].initially_verified)

    def test_clear_base_dat_is_verified_against_exact_thumbnail(self):
        self.high_path.unlink()
        clear_data = encrypted_png((1280, 1708))
        thumbnail_data = encrypted_png((90, 120))
        clear_path = self.high_path.with_name(f"{self.file_md5}.dat")
        thumbnail_path = self.high_path.with_name(f"{self.file_md5}_t.dat")
        clear_path.write_bytes(clear_data)
        thumbnail_path.write_bytes(thumbnail_data)
        conn = sqlite3.connect(self.database)
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (3, 10, RESOURCE_MID, len(clear_data), 3, 1),
        )
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (4, 10, RESOURCE_THUMBNAIL, len(thumbnail_data), 4, 1),
        )
        conn.commit()
        conn.close()

        targets = self.monitor().prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )

        self.assertTrue(targets[0].initially_verified)

    def test_explicit_unavailable_status_is_classified_as_expired(self):
        conn = sqlite3.connect(self.database)
        conn.execute(
            "UPDATE MessageResourceDetail SET status=3, size=0 "
            "WHERE type=?",
            (RESOURCE_HIGH,),
        )
        conn.commit()
        conn.close()
        monitor = self.monitor()
        targets = monitor.prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )

        self.assertTrue(monitor.is_expired(targets[0]))

    def test_successful_clear_resource_is_not_classified_as_expired(self):
        conn = sqlite3.connect(self.database)
        conn.execute(
            "UPDATE MessageResourceDetail SET status=3, size=0 "
            "WHERE type=?",
            (RESOURCE_HIGH,),
        )
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (5, 10, RESOURCE_MID, 1234, 5, 1),
        )
        conn.commit()
        conn.close()
        monitor = self.monitor()
        targets = monitor.prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )

        self.assertFalse(monitor.is_expired(targets[0]))

    def test_exact_mid_file_creation_confirms_prompted_image_activity(self):
        monitor = self.monitor()
        targets = monitor.prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )
        mid_path = self.high_path.with_name(f"{self.file_md5}.dat")
        mid_path.write_bytes(b"new exact mid resource")

        self.assertTrue(monitor.has_activity(self.talker, targets[0]))

    def test_unrelated_file_does_not_confirm_prompted_image_activity(self):
        monitor = self.monitor()
        targets = monitor.prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )
        unrelated = self.high_path.with_name(f"{'f' * 32}.dat")
        unrelated.write_bytes(b"another image")

        self.assertFalse(monitor.has_activity(self.talker, targets[0]))

    def test_exact_mid_detail_change_confirms_prompted_image_activity(self):
        monitor = self.monitor()
        targets = monitor.prepare_targets(
            self.talker,
            [
                {
                    "id": 7,
                    "create_time": 100,
                    "server_id": 700,
                    "message_key": "friend:700:100:7",
                    "time_str": "1970-01-01 00:01:40",
                }
            ],
        )
        conn = sqlite3.connect(self.database)
        conn.execute(
            "INSERT INTO MessageResourceDetail VALUES (?, ?, ?, ?, ?, ?)",
            (3, 10, RESOURCE_MID, 42, 9, 1),
        )
        conn.commit()
        conn.close()
        time.sleep(0.8)

        self.assertTrue(monitor.has_activity(self.talker, targets[0]))


class FakeController:
    def __init__(self):
        self.hwnd = 0
        self.version = "test"
        self.hotkeys = [HOTKEY_START_RESUME]
        self.navigation_count = 0
        self.closed = False

    def register_hotkeys(self):
        return None

    def close(self):
        self.closed = True

    def poll_hotkey(self):
        return self.hotkeys.pop(0) if self.hotkeys else None

    def calibrate_foreground_viewer(self):
        self.hwnd = 123
        return {"wechat_version": "test"}

    def resume_foreground_viewer(self):
        return None

    def validate_safety(self):
        return None

    def navigate(self, direction, interrupt=None):
        if interrupt is not None:
            self.assert_running = interrupt()
        self.navigation_count += 1
        return True


class UnconfirmedController(FakeController):
    def navigate(self, direction, interrupt=None):
        super().navigate(direction, interrupt=interrupt)
        return False


class FakeMonitor:
    def __init__(self):
        self.polls = 0

    def is_verified_high(self, talker, target):
        self.polls += 1
        return self.polls >= 2

    def force_refresh(self):
        return None

    def has_activity(self, talker, target):
        return True

    def is_expired(self, target):
        return False


class NeverVerifiedMonitor(FakeMonitor):
    def is_verified_high(self, talker, target):
        return False


class ExpiredMonitor(NeverVerifiedMonitor):
    def is_expired(self, target):
        return True


class ImageHDAutomationManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_minimum_dwell_is_split_into_interruptible_slices(self):
        manager = ImageHDAutomationManager()
        controller = FakeController()
        controller.hwnd = 123
        controller.hotkeys = []
        job = {
            "status": "running",
            "pause_requested": False,
            "cancel_requested": False,
        }
        with patch("backend.image_hd_automation.time.sleep") as sleep_mock:
            self.assertTrue(
                manager._wait_minimum_dwell(job, controller, 0.25)
            )

        intervals = [call.args[0] for call in sleep_mock.call_args_list]
        self.assertEqual(len(intervals), 3)
        self.assertAlmostEqual(sum(intervals), 0.25)
        self.assertTrue(all(interval <= 0.1 for interval in intervals))

    async def test_expired_images_do_not_interrupt_remaining_range(self):
        controller = UnconfirmedController()
        controller.hotkeys = [
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
            HOTKEY_START_RESUME,
        ]
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        targets = [
            ImageHDTarget(
                message_id=index,
                create_time=100 + index,
                server_id=str(700 + index),
                message_key=f"friend:{700 + index}:{100 + index}:{index}",
                time_str=f"time-{index}",
                file_md5=str(index) * 32,
                resource_info_ids=(10 + index,),
            )
            for index in range(1, 4)
        ]
        with patch.object(manager, "_wait_minimum_dwell", return_value=True) as dwell, \
                patch("backend.image_hd_automation.time.sleep", return_value=None):
            started = manager.start(
                account_id="account",
                talker="friend",
                sequence=[{"kind": "image", "target": item} for item in targets],
                monitor=ExpiredMonitor(),
                direction="next",
                per_image_timeout=0,
            )
            deadline = time.monotonic() + 3
            job = started
            while time.monotonic() < deadline:
                job = manager.get(started["task_id"])
                if job and job["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["expired_count"], 3)
        self.assertEqual(job["failed_count"], 0)
        self.assertEqual(job["completed"], 3)
        self.assertEqual(controller.navigation_count, 2)
        self.assertEqual(job["unconfirmed_navigation_count"], 2)
        dwell.assert_not_called()

    async def test_auto_failure_can_switch_to_manual_for_remaining_pages(self):
        class FailsBeforeSendController(FakeController):
            def __init__(self):
                super().__init__()
                self.navigation_attempts = 0

            def navigate(self, direction, interrupt=None):
                self.navigation_attempts += 1
                raise ViewerAutomationError("test failure before direction key")

        controller = FailsBeforeSendController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        targets = [
            ImageHDTarget(
                message_id=index,
                create_time=100 + index,
                server_id=str(700 + index),
                message_key=f"friend:{700 + index}:{100 + index}:{index}",
                time_str=f"time-{index}",
                file_md5=str(index) * 32,
                resource_info_ids=(10 + index,),
            )
            for index in range(1, 3)
        ]
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[
                {"kind": "image", "target": targets[0]},
                {"kind": "video"},
                {"kind": "image", "target": targets[1]},
            ],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 3
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job.get("navigation_pause_kind") == "auto_before_send":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job["status"], "paused")
        self.assertTrue(job["can_switch_to_manual"])
        self.assertFalse(job["navigation_sent"])

        self.assertTrue(manager.set_navigation_mode(started["task_id"], "manual"))
        controller.hotkeys.append(HOTKEY_START_RESUME)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job.get("navigation_pause_kind") == "manual_required":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["manual_navigation_count"], 1)
        controller.hotkeys.append(HOTKEY_START_RESUME)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "completed":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["navigation_mode"], "manual")
        self.assertEqual(job["manual_navigation_count"], 2)
        self.assertEqual(job["downloaded"], 2)
        self.assertEqual(controller.navigation_attempts, 1)

    async def test_sent_but_unconfirmed_navigation_is_never_repeated(self):
        controller = UnconfirmedController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[
                {"kind": "image", "target": target},
                {"kind": "video"},
            ],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 3
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job.get("navigation_pause_kind") == "auto_after_send":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job["status"], "paused")
        self.assertTrue(job["navigation_sent"])
        self.assertTrue(job["can_switch_to_manual"])

        self.assertTrue(manager.set_navigation_mode(started["task_id"], "manual"))
        controller.hotkeys.append(HOTKEY_START_RESUME)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "completed":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(controller.navigation_count, 1)
        self.assertEqual(job["unconfirmed_navigation_count"], 1)

    async def test_manual_wait_can_switch_back_to_auto_without_double_page(self):
        class FailsOnceController(FakeController):
            def __init__(self):
                super().__init__()
                self.navigation_attempts = 0

            def navigate(self, direction, interrupt=None):
                self.navigation_attempts += 1
                if self.navigation_attempts == 1:
                    raise ViewerAutomationError("first automatic navigation failed")
                return super().navigate(direction, interrupt=interrupt)

        controller = FailsOnceController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        targets = [
            ImageHDTarget(
                message_id=index,
                create_time=100 + index,
                server_id=str(700 + index),
                message_key=f"friend:{700 + index}:{100 + index}:{index}",
                time_str=f"time-{index}",
                file_md5=str(index) * 32,
                resource_info_ids=(10 + index,),
            )
            for index in range(1, 3)
        ]
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[
                {"kind": "image", "target": targets[0]},
                {"kind": "video"},
                {"kind": "image", "target": targets[1]},
            ],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 3
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job.get("navigation_pause_kind") == "auto_before_send":
                break
            await asyncio.sleep(0.01)
        self.assertTrue(manager.set_navigation_mode(started["task_id"], "manual"))
        controller.hotkeys.append(HOTKEY_START_RESUME)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job.get("navigation_pause_kind") == "manual_required":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job["manual_navigation_count"], 1)
        self.assertTrue(manager.set_navigation_mode(started["task_id"], "auto"))
        controller.hotkeys.append(HOTKEY_START_RESUME)

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "completed":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["navigation_mode"], "auto")
        self.assertEqual(job["manual_navigation_count"], 1)
        self.assertEqual(controller.navigation_attempts, 2)
        self.assertEqual(controller.navigation_count, 1)

    async def test_unknown_timeouts_also_continue_through_remaining_range(self):
        controller = FakeController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        targets = [
            ImageHDTarget(
                message_id=index,
                create_time=200 + index,
                server_id=str(800 + index),
                message_key=f"friend:{800 + index}:{200 + index}:{index}",
                time_str=f"unknown-{index}",
                file_md5=str(index + 3) * 32,
                resource_info_ids=(20 + index,),
            )
            for index in range(1, 4)
        ]
        with patch("backend.image_hd_automation.time.sleep", return_value=None):
            started = manager.start(
                account_id="account",
                talker="friend",
                sequence=[{"kind": "image", "target": item} for item in targets],
                monitor=NeverVerifiedMonitor(),
                direction="next",
                per_image_timeout=0,
            )
            deadline = time.monotonic() + 3
            job = started
            while time.monotonic() < deadline:
                job = manager.get(started["task_id"])
                if job and job["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["expired_count"], 0)
        self.assertEqual(job["failed_count"], 3)
        self.assertEqual(controller.navigation_count, 2)

    async def test_stop_at_verification_deadline_does_not_create_failure(self):
        controller = FakeController()
        controller.hotkeys = [
            HOTKEY_START_RESUME,
            None,  # first-target confirmation
            None,  # outer loop control point
            None,  # verification loop before its final sleep
            HOTKEY_STOP,  # post-deadline control point
        ]
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[{"kind": "image", "target": target}],
            monitor=NeverVerifiedMonitor(),
            direction="next",
            per_image_timeout=0.01,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 2
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["failed_count"], 0)
        self.assertEqual(job["completed"], 0)
        self.assertEqual(job["results"], [])

    async def test_stop_during_final_refresh_does_not_create_failure(self):
        controller = FakeController()

        class StopDuringRefreshMonitor(NeverVerifiedMonitor):
            def force_refresh(self):
                controller.hotkeys.append(HOTKEY_STOP)

        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[{"kind": "image", "target": target}],
            monitor=StopDuringRefreshMonitor(),
            direction="next",
            per_image_timeout=0,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 2
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["failed_count"], 0)
        self.assertEqual(job["completed"], 0)
        self.assertEqual(job["results"], [])

    async def test_hotkey_starts_job_and_resource_verification_completes_it(self):
        controller = FakeController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="1970-01-01 00:01:40",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[
                {"kind": "image", "target": target},
                {"kind": "video"},
            ],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
            min_dwell_seconds=0,
        )

        deadline = time.monotonic() + 3
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "completed":
                break
            await asyncio.sleep(0.05)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["downloaded"], 1)
        self.assertEqual(job["completed"], 1)
        self.assertEqual(controller.navigation_count, 1)
        self.assertTrue(controller.closed)

    async def test_downloaded_image_dwells_before_navigation(self):
        events = []

        class RecordingController(FakeController):
            def navigate(self, direction, interrupt=None):
                events.append("navigate")
                return super().navigate(direction, interrupt=interrupt)

        controller = RecordingController()
        manager = ImageHDAutomationManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )

        def record_dwell(_job, _controller, seconds):
            events.append(("dwell", seconds))
            return True

        with patch.object(manager, "_wait_minimum_dwell", side_effect=record_dwell):
            started = manager.start(
                account_id="account",
                talker="friend",
                sequence=[
                    {"kind": "image", "target": target},
                    {"kind": "video"},
                ],
                monitor=FakeMonitor(),
                direction="next",
                per_image_timeout=5,
                min_dwell_seconds=1.25,
            )
            deadline = time.monotonic() + 3
            job = started
            while time.monotonic() < deadline:
                job = manager.get(started["task_id"])
                if job and job["status"] == "completed":
                    break
                await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["min_dwell_seconds"], 1.25)
        self.assertEqual(events, [("dwell", 1.25), "navigate"])

    async def test_cancel_during_dwell_keeps_downloaded_result(self):
        class BlockingDwellManager(ImageHDAutomationManager):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.dwell_started = threading.Event()

            def _wait_minimum_dwell(self, job, controller, seconds):
                self.dwell_started.set()
                while not job.get("cancel_requested"):
                    time.sleep(0.01)
                return False

        controller = FakeController()
        manager = BlockingDwellManager(controller_factory=lambda: controller)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
        )
        started = manager.start(
            account_id="account",
            talker="friend",
            sequence=[
                {"kind": "image", "target": target},
                {"kind": "video"},
            ],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
            min_dwell_seconds=5,
        )
        self.assertTrue(
            await asyncio.to_thread(manager.dwell_started.wait, 1),
            "task never reached the dwell phase",
        )
        self.assertTrue(manager.cancel(started["task_id"]))

        deadline = time.monotonic() + 2
        job = started
        while time.monotonic() < deadline:
            job = manager.get(started["task_id"])
            if job and job["status"] == "cancelled":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["downloaded"], 1)
        self.assertEqual(job["completed"], 1)
        self.assertEqual(controller.navigation_count, 0)

    async def test_all_verified_range_completes_without_registering_hotkeys(self):
        controller_created = False

        def factory():
            nonlocal controller_created
            controller_created = True
            return FakeController()

        manager = ImageHDAutomationManager(controller_factory=factory)
        target = ImageHDTarget(
            message_id=7,
            create_time=100,
            server_id="700",
            message_key="friend:700:100:7",
            time_str="time",
            file_md5="0" * 32,
            resource_info_ids=(10,),
            initially_verified=True,
        )
        job = manager.start(
            account_id="account",
            talker="friend",
            sequence=[{"kind": "image", "target": target}],
            monitor=FakeMonitor(),
            direction="next",
            per_image_timeout=5,
        )
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["skipped"], 1)
        self.assertFalse(controller_created)


class ViewerFrameTests(unittest.TestCase):
    def test_viewer_titles_are_recognized_without_using_image_brightness(self):
        self.assertTrue(
            WeChatViewerController._title_looks_like_viewer("图片和视频")
        )
        self.assertTrue(
            WeChatViewerController._title_looks_like_viewer("Images & Videos")
        )
        self.assertFalse(WeChatViewerController._title_looks_like_viewer("微信"))
        self.assertFalse(WeChatViewerController._title_looks_like_viewer("Weixin"))

    def test_frame_difference_detects_change_without_capturing_screen(self):
        average, ratio = WeChatViewerController._frame_difference(
            [0] * 100, [0] * 50 + [100] * 50
        )
        self.assertGreater(average, 3)
        self.assertGreater(ratio, 0.06)

    def test_small_local_page_change_is_not_mistaken_for_boundary(self):
        first = [100] * 100
        second = list(first)
        second[:3] = [125, 125, 125]

        self.assertTrue(WeChatViewerController._frame_changed(first, second))
        self.assertFalse(WeChatViewerController._frame_changed(first, first))


if __name__ == "__main__":
    unittest.main()
