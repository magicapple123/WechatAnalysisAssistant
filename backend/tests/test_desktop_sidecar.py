import os
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import api, main
from backend.version import APP_VERSION, DESKTOP_API_VERSION


class DesktopCommandLineTests(unittest.TestCase):
    def test_desktop_environment_forces_loopback_and_disables_browser(self):
        args = main.build_argument_parser().parse_args(
            ["--desktop", "--host", "0.0.0.0", "--port", "0", "--parent-pid", "123"]
        )
        options = main.resolve_runtime_options(
            args,
            {main.DESKTOP_TOKEN_ENV: "test-token-value-with-at-least-32-chars"},
        )

        self.assertTrue(options.desktop)
        self.assertEqual(options.host, "127.0.0.1")
        self.assertEqual(options.port, 0)
        self.assertEqual(options.parent_pid, 123)
        self.assertFalse(options.open_browser)

    def test_environment_can_enable_desktop_mode_and_supply_port(self):
        args = main.build_argument_parser().parse_args([])
        options = main.resolve_runtime_options(
            args,
            {
                main.DESKTOP_TOKEN_ENV: "environment-token-with-at-least-32-chars",
                main.DESKTOP_PORT_ENV: "49152",
                main.PARENT_PID_ENV: "99",
            },
        )

        self.assertTrue(options.desktop)
        self.assertEqual(options.port, 49152)
        self.assertEqual(options.parent_pid, 99)

    def test_desktop_mode_requires_a_session_token(self):
        args = main.build_argument_parser().parse_args(["--desktop"])
        with self.assertRaisesRegex(ValueError, "requires"):
            main.resolve_runtime_options(args, {})

        browser_args = main.build_argument_parser().parse_args([])
        with self.assertRaisesRegex(ValueError, "requires"):
            main.resolve_runtime_options(browser_args, {main.DESKTOP_MODE_ENV: "1"})

    def test_parent_monitor_requests_shutdown_when_parent_disappears(self):
        class Server:
            should_exit = False

        server = Server()
        with patch.object(main, "_process_identity", side_effect=[10.0, None]):
            monitor = main.start_parent_monitor(
                server, 123, interval=0.01, hard_exit_after=None
            )
            monitor.join(timeout=1)

        self.assertFalse(monitor.is_alive())
        self.assertTrue(server.should_exit)

    def test_smoke_mode_skips_startup_wechat_discovery(self):
        with patch.dict(
            os.environ,
            {"WECHAT_ASSISTANT_SMOKE_TEST": "1"},
            clear=False,
        ), patch.object(main, "_detect_local_wechat") as detect, patch.object(
            main, "run_server", return_value=0
        ) as run_server:
            result = main.main(["--no-browser"])

        self.assertEqual(result, 0)
        detect.assert_not_called()
        run_server.assert_called_once()


class DesktopAuthenticationTests(unittest.TestCase):
    token = "desktop-session-token-for-tests-at-least-32"

    def test_health_requires_exact_desktop_session_header(self):
        with patch.dict(
            os.environ,
            {api.DESKTOP_SESSION_TOKEN_ENV: self.token},
            clear=False,
        ), TestClient(api.app) as client:
            missing = client.get("/api/desktop/health")
            wrong = client.get(
                "/api/desktop/health",
                headers={"X-Desktop-Session-Token": "wrong"},
            )
            valid = client.get(
                "/api/desktop/health",
                headers={"X-Desktop-Session-Token": self.token},
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(valid.status_code, 200)
        payload = valid.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["app_version"], APP_VERSION)
        self.assertEqual(payload["api_version"], DESKTOP_API_VERSION)
        self.assertIsInstance(payload["pid"], int)
        self.assertTrue(payload["desktop"])

    def test_browser_development_without_token_remains_compatible(self):
        with patch.dict(
            os.environ,
            {api.DESKTOP_SESSION_TOKEN_ENV: ""},
            clear=False,
        ), TestClient(api.app) as client:
            response = client.get("/api/desktop/health")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["desktop"])

    def test_prepare_exit_rejects_new_work_and_requests_server_shutdown(self):
        callback_called = threading.Event()
        api.set_desktop_shutdown_callback(callback_called.set)
        try:
            with patch.dict(
                os.environ,
                {api.DESKTOP_SESSION_TOKEN_ENV: self.token},
                clear=False,
            ), patch.object(api, "_close_runtime_caches"), TestClient(api.app) as client:
                headers = {"X-Desktop-Session-Token": self.token}
                prepared = client.post("/api/desktop/prepare-exit", headers=headers)
                blocked = client.get("/api/status", headers=headers)
                self.assertTrue(callback_called.wait(timeout=1))

            self.assertEqual(prepared.status_code, 200)
            self.assertEqual(blocked.status_code, 503)
        finally:
            api.set_desktop_shutdown_callback(None)
            api._desktop_shutdown_requested.clear()

    def test_native_runtime_self_test_is_desktop_only(self):
        with patch.dict(
            os.environ,
            {api.DESKTOP_SESSION_TOKEN_ENV: ""},
            clear=False,
        ), TestClient(api.app) as client:
            browser_response = client.post("/api/desktop/self-test")

        with patch.dict(
            os.environ,
            {api.DESKTOP_SESSION_TOKEN_ENV: self.token},
            clear=False,
        ), patch.object(
            api,
            "_desktop_runtime_self_test",
            return_value=["pyav-hevc", "silk-python"],
        ), TestClient(api.app) as client:
            desktop_response = client.post(
                "/api/desktop/self-test",
                headers={"X-Desktop-Session-Token": self.token},
            )

        self.assertEqual(browser_response.status_code, 404)
        self.assertEqual(desktop_response.status_code, 200)
        self.assertEqual(desktop_response.json()["status"], "ok")

    def test_smoke_mode_blocks_routes_that_can_probe_local_wechat(self):
        with patch.dict(
            os.environ,
            {
                api.DESKTOP_SESSION_TOKEN_ENV: self.token,
                "WECHAT_ASSISTANT_SMOKE_TEST": "1",
            },
            clear=False,
        ), patch.object(
            api.config, "detect_and_set_accounts"
        ) as detect_accounts, patch.object(
            api, "find_key_auto"
        ) as find_key, TestClient(api.app) as client:
            blocked_status = client.get(
                "/api/status",
                headers={"X-Desktop-Session-Token": self.token},
            )
            blocked_detect = client.get(
                "/api/auto-detect",
                headers={"X-Desktop-Session-Token": self.token},
            )
            health = client.get(
                "/api/desktop/health",
                headers={"X-Desktop-Session-Token": self.token},
            )

        self.assertEqual(blocked_status.status_code, 503)
        self.assertEqual(blocked_detect.status_code, 503)
        self.assertEqual(health.status_code, 200)
        detect_accounts.assert_not_called()
        find_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
