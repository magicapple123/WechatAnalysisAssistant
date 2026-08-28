import asyncio
import threading
import unittest
from types import SimpleNamespace

from backend.image_recognition import ImageRecognitionManager
from backend.vision import VisionConfig


class FakeStore:
    def get(self, *_args, **_kwargs):
        return None

    def find_reusable(self, *_args, **_kwargs):
        return {"description": "缓存描述"}

    def save(self, **kwargs):
        return kwargs


class BlockingImageService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def get_image(self, **_kwargs):
        self.started.set()
        self.release.wait(timeout=5)
        return SimpleNamespace(
            sha256="hash",
            file_md5="md5",
            path=None,
        )


class RecognitionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_waits_for_inflight_worker_after_cancel(self):
        manager = ImageRecognitionManager(FakeStore())
        service = BlockingImageService()
        job = manager.start(
            account_id="account",
            talker="friend",
            messages=[{"id": 1, "create_time": 2, "server_id": 3}],
            image_service=service,
            vision_config=VisionConfig(
                base_url="http://127.0.0.1:1/v1",
                api_key="test-key",
                model="test-model",
            ),
        )
        for _ in range(100):
            if service.started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(service.started.is_set())

        cleaned = threading.Event()
        manager.cancel_all()
        manager.defer_cleanup(cleaned.set)
        await asyncio.sleep(0.02)
        self.assertFalse(cleaned.is_set())

        service.release.set()
        for _ in range(100):
            if cleaned.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(manager.get(job["task_id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
