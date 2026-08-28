import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend import api


class APIKeyRevealTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = {
            "vision": {
                "api_key": "vision-secret-value",
                "base_url": "https://vision.example.test/v1",
                "model": "vision-model",
            },
            "transcription": {
                "api_key": "speech-secret-value",
                "base_url": "https://speech.example.test/v1",
                "model": "speech-model",
            },
            "analysis": {
                "api_key": "analysis-secret-value",
                "base_url": "https://analysis.example.test/v1",
                "model": "analysis-model",
            },
            "images": {"accounts": {}},
        }

    async def test_reveals_saved_vision_api_key_without_cache(self):
        with patch.object(api, "load_settings", return_value=self.settings):
            response = await api.reveal_vision_api_key("1")

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload, {
            "success": True,
            "api_key": "vision-secret-value",
        })
        cache_control = response.headers["cache-control"]
        for directive in ("no-store", "no-cache", "private"):
            self.assertIn(directive, cache_control)
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(
            response.headers["x-content-type-options"], "nosniff"
        )

    async def test_reveals_saved_transcription_api_key_without_cache(self):
        with patch.object(api, "load_settings", return_value=self.settings):
            response = await api.reveal_transcription_api_key("1")

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload, {
            "success": True,
            "api_key": "speech-secret-value",
        })
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn("no-cache", response.headers["cache-control"])
        self.assertIn("private", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(
            response.headers["x-content-type-options"], "nosniff"
        )

    async def test_reveals_saved_analysis_api_key_without_cache(self):
        with patch.object(api, "load_settings", return_value=self.settings):
            response = await api.reveal_analysis_api_key("1")

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload, {
            "success": True,
            "api_key": "analysis-secret-value",
        })
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_reveal_endpoints_require_local_client_marker(self):
        for reveal in (
            api.reveal_vision_api_key,
            api.reveal_transcription_api_key,
            api.reveal_analysis_api_key,
        ):
            with self.subTest(endpoint=reveal.__name__):
                with self.assertRaises(HTTPException) as caught:
                    await reveal(None)
                self.assertEqual(caught.exception.status_code, 403)

    async def test_regular_settings_response_never_contains_raw_api_keys(self):
        with patch.object(api, "load_settings", return_value=self.settings):
            response = await api.get_settings()

        serialized = json.dumps(response, ensure_ascii=False)
        self.assertNotIn("vision-secret-value", serialized)
        self.assertNotIn("speech-secret-value", serialized)
        self.assertNotIn("analysis-secret-value", serialized)
        self.assertNotIn("api_key", response["data"]["vision"])
        self.assertNotIn("api_key", response["data"]["transcription"])
        self.assertNotIn("api_key", response["data"]["analysis"])
        self.assertTrue(response["data"]["vision"]["has_api_key"])
        self.assertTrue(response["data"]["transcription"]["has_api_key"])
        self.assertTrue(response["data"]["analysis"]["has_api_key"])


if __name__ == "__main__":
    unittest.main()
