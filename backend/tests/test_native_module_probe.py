import os
import subprocess
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend import api, main
from backend.subprocess_env import DESKTOP_ONLY_ENV_VARS


class NativeModuleProbeTests(unittest.TestCase):
    def test_main_probe_imports_only_the_allowlisted_module(self):
        with patch.object(main.importlib, "import_module") as import_module:
            self.assertEqual(main.probe_native_module("wx_key"), 0)

        import_module.assert_called_once_with("wx_key")

    def test_source_and_frozen_probe_commands_use_the_correct_entrypoint(self):
        with patch.object(api.sys, "frozen", False, create=True), patch.object(
            api.sys, "executable", r"C:\Python\python.exe"
        ):
            self.assertEqual(
                api._native_module_probe_command("wx_key"),
                [
                    r"C:\Python\python.exe",
                    "-m",
                    "backend.main",
                    "--probe-native-module",
                    "wx_key",
                ],
            )

        with patch.object(api.sys, "frozen", True, create=True), patch.object(
            api.sys, "executable", r"C:\App\Backend.exe"
        ):
            self.assertEqual(
                api._native_module_probe_command("wx_key"),
                [r"C:\App\Backend.exe", "--probe-native-module", "wx_key"],
            )

    def test_failed_native_probe_becomes_http_error_without_crashing_api(self):
        completed = subprocess.CompletedProcess(
            ["backend.exe", "--probe-native-module", "wx_key"],
            returncode=-1073741819,
            stdout="",
            stderr="",
        )
        environment = {
            **os.environ,
            "WECHAT_ASSISTANT_DESKTOP_TOKEN": "secret-token",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "subprocess.run", return_value=completed
        ) as run, self.assertRaises(HTTPException) as captured:
            api._probe_wx_key_runtime()

        self.assertEqual(captured.exception.status_code, 500)
        self.assertIn("错误码", captured.exception.detail)
        child_environment = run.call_args.kwargs["env"]
        normalized = {name.upper() for name in child_environment}
        self.assertTrue(DESKTOP_ONLY_ENV_VARS.isdisjoint(normalized))

    def test_desktop_self_test_marks_missing_hook_as_optional(self):
        with patch.object(
            api,
            "_probe_wx_key_runtime",
            side_effect=HTTPException(500, "optional module missing"),
        ):
            self.assertEqual(
                api._optional_wx_key_self_test_label(),
                "wx-key-hook:optional-skipped",
            )

        with patch.object(api, "_probe_wx_key_runtime"):
            self.assertEqual(
                api._optional_wx_key_self_test_label(),
                "wx-key-hook:optional",
            )

    def test_smoke_self_test_never_probes_the_optional_native_hook(self):
        with patch.dict(
            os.environ,
            {"WECHAT_ASSISTANT_SMOKE_TEST": "1"},
            clear=False,
        ), patch.object(api, "_probe_wx_key_runtime") as probe:
            self.assertEqual(
                api._optional_wx_key_self_test_label(),
                "wx-key-hook:optional-skipped",
            )

        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
