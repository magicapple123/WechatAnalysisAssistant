import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from backend import api, wcdb_key_scanner
from backend.subprocess_env import (
    DESKTOP_ONLY_ENV_VARS,
    sanitized_subprocess_env,
)
from backend.wechat_viewer_automation import _authenticode_is_tencent


SECRETS = {
    "WECHAT_ASSISTANT_DESKTOP_TOKEN": "desktop-secret",
    "WECHAT_ASSISTANT_DESKTOP_PORT": "49152",
    "WECHAT_ASSISTANT_DESKTOP_MODE": "1",
    "WECHAT_ASSISTANT_PARENT_PID": "1234",
    "WECHAT_ASSISTANT_APP_VERSION": "1.2.3",
    "WECHAT_ASSISTANT_DESKTOP_API_VERSION": "7",
    "WECHAT_ASSISTANT_DESKTOP_USER_DATA_DIR": "isolated-desktop-data",
    "WECHAT_ASSISTANT_SMOKE_TEST": "1",
    "WECHAT_ASSISTANT_SMOKE_TIMEOUT_MS": "90000",
}


class SanitizedSubprocessEnvironmentTests(unittest.TestCase):
    def assert_desktop_metadata_removed(self, environment):
        normalized = {key.upper() for key in environment}
        self.assertTrue(DESKTOP_ONLY_ENV_VARS.isdisjoint(normalized))

    def test_removes_all_desktop_metadata_and_preserves_unrelated_values(self):
        source = {**SECRETS, "PATH": "example-path", "WAA_USER_VALUE": "kept"}

        result = sanitized_subprocess_env(source)

        self.assert_desktop_metadata_removed(result)
        self.assertEqual(result["PATH"], "example-path")
        self.assertEqual(result["WAA_USER_VALUE"], "kept")
        self.assertEqual(source["WECHAT_ASSISTANT_DESKTOP_TOKEN"], "desktop-secret")

    def test_removal_is_case_insensitive_and_result_is_an_independent_copy(self):
        source = {
            "wechat_assistant_desktop_token": "secret",
            "WeChat_Assistant_Parent_Pid": "1234",
            "UNCHANGED": "before",
        }

        result = sanitized_subprocess_env(source)
        result["UNCHANGED"] = "after"

        self.assert_desktop_metadata_removed(result)
        self.assertEqual(source["UNCHANGED"], "before")

    def test_tasklist_receives_a_sanitized_environment(self):
        completed = SimpleNamespace(stdout='"INFO","321","Console","1","1,024 K"\n')
        with patch.dict(os.environ, SECRETS, clear=False), patch(
            "subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(wcdb_key_scanner.get_weixin_pids(), [(321, 1024)])

        self.assert_desktop_metadata_removed(run.call_args.kwargs["env"])

    def test_powershell_receives_a_sanitized_environment(self):
        payload = {"Status": "Valid", "Subject": "Tencent Technology"}
        completed = SimpleNamespace(
            stdout=json.dumps(payload),
            returncode=0,
        )
        with patch.dict(os.environ, SECRETS, clear=False), patch(
            "backend.wechat_viewer_automation.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertTrue(_authenticode_is_tencent(Path("Weixin.exe")))

        environment = run.call_args.kwargs["env"]
        self.assert_desktop_metadata_removed(environment)
        self.assertEqual(environment["WAA_WEIXIN_EXE"], "Weixin.exe")


class WeChatLaunchEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_receives_a_sanitized_environment(self):
        process = SimpleNamespace(
            info={"pid": 4321, "name": "Weixin.exe", "exe": ""},
            terminate=lambda: None,
        )
        wx_key = SimpleNamespace(
            initialize_hook=lambda _pid: False,
            get_last_error_msg=lambda: "expected test stop",
            poll_key_data=lambda: None,
            cleanup_hook=lambda: None,
        )

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Weixin.exe"
            executable.touch()
            process.info["exe"] = str(executable)

            with patch.dict(os.environ, SECRETS, clear=False), patch.dict(
                sys.modules, {"wx_key": wx_key}
            ), patch.object(
                api, "_probe_wx_key_runtime"
            ), patch("psutil.process_iter", return_value=[process]), patch(
                "subprocess.Popen"
            ) as popen, patch("time.sleep"):
                with self.assertRaises(HTTPException):
                    await api.extract_key()

        environment = popen.call_args.kwargs["env"]
        self.assert_desktop_metadata_removed(environment)

    def assert_desktop_metadata_removed(self, environment):
        normalized = {key.upper() for key in environment}
        self.assertTrue(DESKTOP_ONLY_ENV_VARS.isdisjoint(normalized))


if __name__ == "__main__":
    unittest.main()
