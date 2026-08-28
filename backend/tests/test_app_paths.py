import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import app_paths, key_extractor, settings


class ApplicationPathTests(unittest.TestCase):
    def test_windows_app_data_path_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = app_paths.get_app_data_dir(
                {"LOCALAPPDATA": str(root / "Local")},
                platform="win32",
                home=root / "Home",
            )
            explicit = app_paths.get_app_data_dir(
                {app_paths.DATA_DIR_ENV: str(root / "Portable")},
                platform="win32",
                home=root / "Home",
            )

            self.assertEqual(default, (root / "Local" / app_paths.APP_NAME).resolve())
            self.assertEqual(explicit, (root / "Portable").resolve())

    def test_relative_environment_paths_never_resolve_against_install_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "Home"
            result = app_paths.get_app_data_dir(
                {
                    app_paths.DATA_DIR_ENV: ".",
                    "LOCALAPPDATA": "relative-local-data",
                },
                platform="win32",
                home=home,
            )

            self.assertEqual(
                result,
                (home / "AppData" / "Local" / app_paths.APP_NAME).resolve(),
            )

    def test_atomic_write_preserves_old_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "settings.json"
            destination.write_text("old", encoding="utf-8")
            with patch.object(app_paths.os, "replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    app_paths.atomic_write_text(destination, "new")

            self.assertEqual(destination.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(destination.parent.glob(".settings.json.*.tmp")), [])

    def test_json_migration_is_validated_non_destructive_and_one_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy" / "settings.json"
            destination = root / "data" / "settings.json"
            source.parent.mkdir()
            source.write_text('{"ui": {"image_quality": "smart"}}', encoding="utf-8")

            migrated = app_paths.migrate_legacy_json_file(destination, (source,))
            self.assertEqual(migrated, source)
            self.assertTrue(source.exists())
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))["ui"]["image_quality"],
                "smart",
            )

            source.write_text('{"ui": {"image_quality": "thumbnail"}}', encoding="utf-8")
            self.assertIsNone(
                app_paths.migrate_legacy_json_file(destination, (source,))
            )
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))["ui"]["image_quality"],
                "smart",
            )

    def test_invalid_json_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "legacy.json"
            destination = root / "data" / "settings.json"
            source.write_text("not-json", encoding="utf-8")

            self.assertIsNone(
                app_paths.migrate_legacy_json_file(destination, (source,))
            )
            self.assertFalse(destination.exists())

    def test_pyinstaller_resource_root_is_supported(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            app_paths.sys,
            "_MEIPASS",
            temporary,
            create=True,
        ):
            self.assertEqual(
                app_paths.resource_path("frontend", "dist"),
                Path(temporary).resolve() / "frontend" / "dist",
            )


class LegacyStorageIntegrationTests(unittest.TestCase):
    def test_relative_legacy_export_directory_falls_back_to_absolute_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            settings_file = root / "settings.json"
            fallback = root / "Downloads"
            settings_file.write_text(
                json.dumps({"export": {"directory": "relative-export"}}),
                encoding="utf-8",
            )
            with patch.object(settings, "SETTINGS_FILE", settings_file), patch.dict(
                settings.DEFAULTS["export"], {"directory": str(fallback)}
            ):
                resolved = settings.get_export_dir()

            self.assertEqual(resolved, fallback)
            self.assertTrue(fallback.is_dir())

    def test_settings_are_loaded_from_one_time_legacy_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy-settings.json"
            destination = root / "data" / "settings.json"
            legacy.write_text(
                json.dumps({"ui": {"image_quality": "hd"}}),
                encoding="utf-8",
            )
            with patch.multiple(
                settings,
                DEFAULT_SETTINGS_FILE=destination,
                SETTINGS_FILE=destination,
                LEGACY_SETTINGS_FILES=(legacy,),
            ):
                loaded = settings.load_settings()

            self.assertEqual(loaded["ui"]["image_quality"], "hd")
            self.assertTrue(legacy.exists())
            self.assertTrue(destination.exists())

    def test_account_keys_migrate_without_writing_the_legacy_root_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_data = root / "app-data"
            legacy_keys = root / "backend" / "wechat_keys.json"
            legacy_single = root / "backend" / "wechat_key.txt"
            legacy_readable = root / "wechat_keys.txt"
            legacy_keys.parent.mkdir()
            legacy_keys.write_text(
                json.dumps({"wxid_test": {"key": "a" * 64}}),
                encoding="utf-8",
            )
            legacy_readable.write_text("legacy-readable-copy", encoding="utf-8")
            destination_keys = app_data / "wechat_keys.json"
            destination_single = app_data / "wechat_key.txt"
            destination_readable = app_data / "wechat_keys.txt"

            with patch.multiple(
                key_extractor,
                DEFAULT_KEYS_FILE=destination_keys,
                DEFAULT_KEY_FILE=destination_single,
                DEFAULT_READABLE_KEY_FILE=destination_readable,
                KEYS_FILE=destination_keys,
                KEY_FILE=destination_single,
                ROOT_KEY_FILE=destination_readable,
                LEGACY_KEYS_FILE=legacy_keys,
                LEGACY_KEY_FILE=legacy_single,
                LEGACY_READABLE_KEY_FILE=legacy_readable,
            ):
                self.assertEqual(key_extractor.load_key("wxid_test_suffix"), "a" * 64)
                key_extractor.save_key("b" * 64, "wxid_second_suffix")

            stored = json.loads(destination_keys.read_text(encoding="utf-8"))
            self.assertEqual(stored["wxid_test"]["key"], "a" * 64)
            self.assertEqual(stored["wxid_second"]["key"], "b" * 64)
            self.assertEqual(
                legacy_readable.read_text(encoding="utf-8"),
                "legacy-readable-copy",
            )
            self.assertNotEqual(
                destination_readable.read_text(encoding="utf-8"),
                "legacy-readable-copy",
            )

if __name__ == "__main__":
    unittest.main()
