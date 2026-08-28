import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import api
from backend import settings as settings_module
from backend.ai_analysis import AnalysisConfig
from backend.model_interfaces import (
    api_key_headers,
    api_key_required,
    interface_label,
    protocol_for_interface,
)
from backend.transcription import TranscriptionConfig
from backend.vision import VisionConfig
from backend.image_recognition import (
    legacy_vision_cache_provider,
    vision_cache_provider,
)
from backend.voice_transcription import (
    legacy_transcription_settings_hash,
    transcription_settings_hash,
)


class ModelInterfaceMetadataTests(unittest.TestCase):
    def test_azure_uses_api_key_header(self):
        for module in ("vision", "transcription", "analysis"):
            with self.subTest(module=module):
                headers = api_key_headers(
                    module=module,
                    preset="azure_openai",
                    base_url=(
                        "https://resource.openai.azure.com/openai/v1"
                    ),
                    api_key="azure-secret",
                )
                self.assertEqual(headers, {"api-key": "azure-secret"})
                self.assertNotIn("Authorization", headers)

    def test_local_servers_allow_empty_key_but_send_optional_bearer(self):
        for preset in ("ollama", "lm_studio"):
            for module in ("vision", "analysis"):
                with self.subTest(preset=preset, module=module):
                    base_url = "http://[::1]:11434/v1"
                    self.assertFalse(
                        api_key_required(module, preset, base_url)
                    )
                    self.assertEqual(
                        api_key_headers(
                            module=module,
                            preset=preset,
                            base_url=base_url,
                            api_key="",
                        ),
                        {},
                    )
                    self.assertEqual(
                        api_key_headers(
                            module=module,
                            preset=preset,
                            base_url=base_url,
                            api_key="optional-secret",
                        ),
                        {"Authorization": "Bearer optional-secret"},
                    )

            self.assertTrue(
                api_key_required(
                    "vision", preset, "https://models.example.test/v1"
                )
            )
            self.assertTrue(
                api_key_required(
                    "analysis", preset, "http://127.example.test/v1"
                )
            )

    def test_siliconflow_and_lm_studio_presets_are_mapped_and_labeled(self):
        for module in ("vision", "analysis"):
            with self.subTest(module=module):
                self.assertEqual(
                    protocol_for_interface(module, "siliconflow"),
                    "openai_compatible",
                )
                self.assertEqual(
                    protocol_for_interface(module, "lm_studio"),
                    "openai_compatible",
                )
        self.assertEqual(
            interface_label("siliconflow"), "硅基流动 SiliconFlow"
        )
        self.assertEqual(interface_label("lm_studio"), "LM Studio（本机）")

    def test_cache_identities_separate_presets_but_never_depend_on_key(self):
        base_vision = {
            "provider": "openai_compatible",
            "base_url": "https://models.example.test/v1",
            "model": "vision-model",
        }
        openai = VisionConfig(
            **base_vision, interface_preset="openai", api_key="key-one"
        )
        azure = VisionConfig(
            **base_vision, interface_preset="azure_openai", api_key="key-one"
        )
        openai_new_key = VisionConfig(
            **base_vision, interface_preset="openai", api_key="key-two"
        )
        self.assertNotEqual(
            vision_cache_provider(openai), vision_cache_provider(azure)
        )
        self.assertEqual(
            vision_cache_provider(openai), vision_cache_provider(openai_new_key)
        )
        self.assertNotEqual(
            vision_cache_provider(openai), legacy_vision_cache_provider(openai)
        )

        base_transcription = {
            "provider": "openai_compatible",
            "base_url": "https://speech.example.test/v1",
            "model": "speech-model",
        }
        groq = TranscriptionConfig(
            **base_transcription, interface_preset="groq", api_key="key-one"
        )
        openai_speech = TranscriptionConfig(
            **base_transcription, interface_preset="openai", api_key="key-one"
        )
        groq_new_key = TranscriptionConfig(
            **base_transcription, interface_preset="groq", api_key="key-two"
        )
        self.assertNotEqual(
            transcription_settings_hash(groq),
            transcription_settings_hash(openai_speech),
        )
        self.assertEqual(
            transcription_settings_hash(groq),
            transcription_settings_hash(groq_new_key),
        )
        self.assertNotEqual(
            transcription_settings_hash(groq),
            legacy_transcription_settings_hash(groq),
        )

    def test_old_provider_only_settings_are_migrated_without_openai_override(self):
        saved = {
            "vision": {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "vision-secret",
                "model": "claude-vision",
            },
            "transcription": {
                "provider": "dashscope_asr",
                "base_url": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                "api_key": "speech-secret",
                "model": "qwen3-asr-flash",
            },
            "analysis": {
                "provider": "gemini",
                "base_url": (
                    "https://generativelanguage.googleapis.com/v1beta"
                ),
                "api_key": "analysis-secret",
                "model": "gemini-test",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            settings_file = Path(temporary) / "settings.json"
            settings_file.write_text(
                json.dumps(saved, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(
                settings_module, "SETTINGS_FILE", settings_file
            ):
                loaded = settings_module.load_settings()

        self.assertNotIn("interface_preset", loaded["vision"])
        self.assertNotIn("interface_preset", loaded["transcription"])
        self.assertNotIn("interface_preset", loaded["analysis"])

        vision = VisionConfig.from_settings(loaded)
        transcription = TranscriptionConfig.from_settings(loaded)
        analysis = AnalysisConfig.from_settings(loaded)
        self.assertEqual(vision.interface_preset, "anthropic")
        self.assertEqual(transcription.interface_preset, "dashscope_asr")
        self.assertEqual(analysis.interface_preset, "gemini")
        vision.validate()
        transcription.validate()
        analysis.validate()

        public = settings_module.public_settings(loaded)
        self.assertEqual(public["vision"]["interface_preset"], "anthropic")
        self.assertEqual(
            public["transcription"]["interface_preset"], "dashscope_asr"
        )
        self.assertEqual(public["analysis"]["interface_preset"], "gemini")

    def test_legacy_custom_json_provider_normalizes_to_custom(self):
        config = AnalysisConfig.from_settings(
            {
                "analysis": {
                    "provider": "custom_json",
                    "custom_name": "旧版自定义接口",
                    "base_url": "https://analysis.example.test/run",
                    "api_key": "secret",
                    "model": "custom-model",
                }
            }
        )
        self.assertEqual(config.provider, "custom")
        self.assertEqual(config.interface_preset, "custom")
        config.validate()

    def test_legacy_compatible_urls_are_inferred_without_openai_override(self):
        deepseek = AnalysisConfig.from_settings(
            {
                "analysis": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "secret",
                    "model": "deepseek-chat",
                }
            }
        )
        generic = AnalysisConfig.from_settings(
            {
                "analysis": {
                    "provider": "openai_compatible",
                    "base_url": "https://gateway.example.test/v1",
                    "api_key": "secret",
                    "model": "internal-model",
                }
            }
        )
        official = AnalysisConfig.from_settings(
            {
                "analysis": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "secret",
                    "model": "gpt-test",
                }
            }
        )
        foundry = AnalysisConfig.from_settings(
            {
                "analysis": {
                    "provider": "openai_compatible",
                    "base_url": (
                        "https://example.services.ai.azure.com/openai/v1"
                    ),
                    "api_key": "secret",
                    "model": "deployment-name",
                }
            }
        )
        self.assertEqual(deepseek.interface_preset, "deepseek")
        self.assertEqual(generic.interface_preset, "openai_compatible")
        self.assertEqual(official.interface_preset, "openai")
        self.assertEqual(foundry.interface_preset, "azure_openai")

        legacy_dashscope_multipart = TranscriptionConfig.from_settings(
            {
                "transcription": {
                    "provider": "openai_compatible",
                    "base_url": (
                        "https://dashscope.aliyuncs.com/compatible-mode/v1"
                    ),
                    "api_key": "secret",
                    "model": "legacy-transcription-model",
                }
            }
        )
        self.assertEqual(
            legacy_dashscope_multipart.interface_preset,
            "openai_compatible",
        )
        legacy_dashscope_multipart.validate()


class InterfaceSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _merge(previous, update):
        merged = {key: dict(value) if isinstance(value, dict) else value
                  for key, value in previous.items()}
        for section, values in update.items():
            if isinstance(values, dict):
                current = dict(merged.get(section, {}))
                current.update(values)
                merged[section] = current
            else:
                merged[section] = values
        return merged

    async def test_azure_preset_is_saved_with_derived_protocol(self):
        previous = {
            "vision": {
                "provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "old-model",
            }
        }
        saved = {}

        def fake_save(update):
            saved.update(update)
            return self._merge(previous, update)

        request = api.SettingsUpdate(
            vision={
                "provider": "openai_compatible",
                "interface_preset": "azure_openai",
                "base_url": (
                    "https://resource.openai.azure.com/openai/v1"
                ),
                "api_key": "azure-secret",
                "model": "vision-deployment",
            }
        )
        with patch.object(api, "load_settings", return_value=previous), \
                patch.object(api, "save_settings", side_effect=fake_save), \
                patch.object(api, "_close_runtime_caches"):
            response = await api.update_settings(request)

        self.assertEqual(
            saved["vision"]["interface_preset"], "azure_openai"
        )
        self.assertEqual(
            saved["vision"]["provider"], "openai_compatible"
        )
        self.assertEqual(
            response["data"]["vision"]["interface_label"],
            "Azure OpenAI / Microsoft Foundry",
        )

    async def test_provider_change_never_reuses_the_previous_service_key(self):
        previous = {
            "analysis": {
                "provider": "openai_compatible",
                "interface_preset": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "existing-secret",
                "model": "old-model",
                "presets": [
                    {
                        "id": "summary",
                        "name": "摘要",
                        "strength": "quick",
                        "detail": "brief",
                        "requirements": "",
                    }
                ],
            }
        }
        saved = {}

        def fake_save(update):
            saved.update(update)
            return self._merge(previous, update)

        request = api.SettingsUpdate(
            analysis={
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "model": "claude-test",
            }
        )
        with patch.object(api, "load_settings", return_value=previous), \
                patch.object(api, "save_settings", side_effect=fake_save) as save:
            with self.assertRaises(HTTPException) as caught:
                await api.update_settings(request)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("新 API Key", str(caught.exception.detail))
        save.assert_not_called()

        replacement = api.SettingsUpdate(
            analysis={
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "anthropic-secret",
                "model": "claude-test",
            }
        )
        with patch.object(api, "load_settings", return_value=previous), \
                patch.object(api, "save_settings", side_effect=fake_save):
            await api.update_settings(replacement)

        self.assertEqual(saved["analysis"]["provider"], "anthropic")
        self.assertEqual(saved["analysis"]["interface_preset"], "anthropic")
        self.assertEqual(saved["analysis"]["api_key"], "anthropic-secret")

    async def test_provider_only_update_preserves_same_protocol_preset(self):
        previous = {
            "analysis": {
                "provider": "openai_compatible",
                "interface_preset": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "deepseek-secret",
                "model": "deepseek-chat",
                "presets": [
                    {
                        "id": "summary",
                        "name": "摘要",
                        "strength": "quick",
                        "detail": "brief",
                        "requirements": "",
                    }
                ],
            }
        }
        saved = {}

        def fake_save(update):
            saved.update(update)
            return self._merge(previous, update)

        request = api.SettingsUpdate(
            analysis={"provider": "openai_compatible"}
        )
        with patch.object(api, "load_settings", return_value=previous), \
                patch.object(api, "save_settings", side_effect=fake_save):
            await api.update_settings(request)

        self.assertEqual(saved["analysis"]["interface_preset"], "deepseek")
        self.assertEqual(saved["analysis"]["api_key"], "deepseek-secret")

    async def test_unknown_or_mismatched_preset_is_rejected(self):
        previous = {
            "vision": {
                "provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "secret",
                "model": "vision-model",
            }
        }
        requests = (
            api.SettingsUpdate(
                vision={"interface_preset": "unknown-provider"}
            ),
            api.SettingsUpdate(
                vision={
                    "interface_preset": "azure_openai",
                    "provider": "anthropic",
                }
            ),
        )
        for request in requests:
            with self.subTest(request=request), patch.object(
                api, "load_settings", return_value=previous
            ), patch.object(api, "save_settings") as save:
                with self.assertRaises(HTTPException) as caught:
                    await api.update_settings(request)
                self.assertEqual(caught.exception.status_code, 400)
                save.assert_not_called()

    async def test_local_ollama_and_lm_studio_can_be_saved_without_key(self):
        for preset, port in (("ollama", 11434), ("lm_studio", 1234)):
            previous = {
                "analysis": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "",
                    "model": "old-model",
                    "presets": [
                        {
                            "id": "summary",
                            "name": "摘要",
                            "strength": "quick",
                            "detail": "brief",
                            "requirements": "",
                        }
                    ],
                }
            }
            saved = {}

            def fake_save(update):
                saved.update(update)
                return self._merge(previous, update)

            request = api.SettingsUpdate(
                analysis={
                    "provider": "openai_compatible",
                    "interface_preset": preset,
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "model": "local-model",
                    "clear_api_key": True,
                }
            )
            with self.subTest(preset=preset), patch.object(
                api, "load_settings", return_value=previous
            ), patch.object(
                api, "save_settings", side_effect=fake_save
            ):
                response = await api.update_settings(request)

            self.assertEqual(saved["analysis"]["api_key"], "")
            self.assertFalse(
                response["data"]["analysis"]["requires_api_key"]
            )


if __name__ == "__main__":
    unittest.main()
