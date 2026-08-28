"""Command-line entry point for browser development and Electron sidecar use."""

from __future__ import annotations

import argparse
import importlib
import os
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


# Keep ``python backend/main.py`` and the PyInstaller script entry working.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.version import APP_VERSION
from backend.config import is_smoke_test_environment


DESKTOP_TOKEN_ENV = "WECHAT_ASSISTANT_DESKTOP_TOKEN"
DESKTOP_PORT_ENV = "WECHAT_ASSISTANT_DESKTOP_PORT"
PARENT_PID_ENV = "WECHAT_ASSISTANT_PARENT_PID"
DESKTOP_MODE_ENV = "WECHAT_ASSISTANT_DESKTOP_MODE"


@dataclass(frozen=True)
class RuntimeOptions:
    desktop: bool
    host: str
    port: int
    session_token: str
    parent_pid: Optional[int]
    open_browser: bool


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _pid_argument(value: str) -> int:
    try:
        pid = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("parent pid must be an integer") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("parent pid must be positive")
    return pid


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WechatAnalysisAssistant backend")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument(
        "--port",
        type=_port_argument,
        default=None,
        help="listen port; 0 lets the operating system choose one",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="run as an authenticated Electron sidecar",
    )
    parser.add_argument(
        "--session-token",
        default=None,
        help="desktop session token (prefer the environment variable)",
    )
    parser.add_argument(
        "--parent-pid",
        type=_pid_argument,
        default=None,
        help="exit automatically when this Electron process exits",
    )
    parser.add_argument(
        "--probe-native-module",
        choices=("wx_key",),
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def probe_native_module(module_name: str) -> int:
    """Import one allow-listed native module in an isolated process."""

    if module_name != "wx_key":
        print("Unsupported native module probe", file=sys.stderr, flush=True)
        return 2
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        print(
            f"Native module probe failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"Native module probe passed: {module_name}", flush=True)
    return 0


def resolve_runtime_options(
    args: argparse.Namespace,
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimeOptions:
    """Normalize CLI/environment settings without starting the server."""

    env = os.environ if environ is None else environ
    environment_token = str(env.get(DESKTOP_TOKEN_ENV, "") or "").strip()
    token = str(args.session_token or environment_token or "").strip()
    environment_desktop = str(env.get(DESKTOP_MODE_ENV, "") or "").strip().lower()
    desktop = bool(
        args.desktop
        or args.session_token
        or environment_token
        or environment_desktop in {"1", "true", "yes", "on"}
    )

    if desktop and not token:
        raise ValueError(
            f"desktop mode requires --session-token or {DESKTOP_TOKEN_ENV}"
        )
    if desktop and len(token) < 32:
        raise ValueError("desktop session token must contain at least 32 characters")
    if len(token) > 4096 or any(ord(char) < 33 or ord(char) == 127 for char in token):
        raise ValueError("desktop session token contains invalid characters")

    raw_port = args.port
    if raw_port is None:
        environment_port = str(env.get(DESKTOP_PORT_ENV, "") or "").strip()
        raw_port = _port_argument(environment_port) if environment_port else 8520

    parent_pid = args.parent_pid
    if parent_pid is None:
        environment_pid = str(env.get(PARENT_PID_ENV, "") or "").strip()
        parent_pid = _pid_argument(environment_pid) if environment_pid else None

    # A desktop sidecar must never be exposed to the LAN, even when an old
    # shortcut or manually supplied argument requests a wildcard address.
    host = "127.0.0.1" if desktop else str(args.host or "127.0.0.1")
    return RuntimeOptions(
        desktop=desktop,
        host=host,
        port=int(raw_port),
        session_token=token,
        parent_pid=parent_pid,
        open_browser=not (args.no_browser or desktop),
    )


def _create_listening_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(2048)
        return listener
    except Exception:
        listener.close()
        raise


def _process_identity(pid: int) -> Optional[float]:
    """Return a stable process creation time, or ``None`` if it is gone."""

    try:
        import psutil

        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return float(process.create_time())
    except Exception:
        return None


def start_parent_monitor(
    server,
    parent_pid: int,
    interval: float = 0.75,
    hard_exit_after: Optional[float] = 15.0,
) -> threading.Thread:
    """Stop Uvicorn if the Electron parent exits or the PID is reused."""

    expected_identity = _process_identity(parent_pid)

    def monitor() -> None:
        if expected_identity is None:
            server.should_exit = True
            return
        while not server.should_exit:
            time.sleep(max(0.1, interval))
            current_identity = _process_identity(parent_pid)
            if current_identity is None or current_identity != expected_identity:
                server.should_exit = True
                if hard_exit_after is not None and hard_exit_after > 0:
                    def force_exit() -> None:
                        time.sleep(max(1.0, float(hard_exit_after)))
                        os._exit(0)

                    threading.Thread(
                        target=force_exit,
                        name="electron-parent-hard-exit",
                        daemon=True,
                    ).start()
                return

    thread = threading.Thread(
        target=monitor,
        name="electron-parent-monitor",
        daemon=True,
    )
    thread.start()
    return thread


def _detect_local_wechat() -> None:
    from backend.config import config
    from backend.key_extractor import load_key

    print("[INFO] Detecting local WeChat data...", flush=True)
    config.detect_wxid()
    config.detect_databases()
    if config.wxid:
        print("[INFO] A local WeChat account was detected", flush=True)
    else:
        print("[WARN] No local WeChat 4.x data directory was detected", flush=True)

    saved_key = load_key(config.wxid)
    if saved_key:
        config.key = saved_key
        print("[INFO] Loaded the saved database key", flush=True)
    else:
        print("[INFO] No saved database key is available", flush=True)


def run_server(options: RuntimeOptions) -> int:
    import uvicorn
    from backend import api as api_module

    listener = _create_listening_socket(options.host, options.port)
    actual_port = int(listener.getsockname()[1])

    if options.desktop:
        os.environ[DESKTOP_MODE_ENV] = "1"
        os.environ[DESKTOP_TOKEN_ENV] = options.session_token
        os.environ[DESKTOP_PORT_ENV] = str(actual_port)
        if options.parent_pid:
            os.environ[PARENT_PID_ENV] = str(options.parent_pid)
        # Machine-readable line consumed by the Electron launcher when port 0
        # is used.  Never print the corresponding authentication token.
        print(f"WECHAT_ASSISTANT_DESKTOP_PORT={actual_port}", flush=True)

    if options.open_browser:
        def open_browser() -> None:
            time.sleep(1)
            webbrowser.open(f"http://127.0.0.1:{actual_port}")

        threading.Thread(target=open_browser, name="browser-opener", daemon=True).start()

    configuration = uvicorn.Config(
        api_module.app,
        host=options.host,
        port=actual_port,
        reload=False,
        log_level="info",
        timeout_graceful_shutdown=10,
    )
    server = uvicorn.Server(configuration)
    api_module.set_desktop_shutdown_callback(
        (lambda: setattr(server, "should_exit", True)) if options.desktop else None
    )
    if options.parent_pid:
        start_parent_monitor(server, options.parent_pid)
    try:
        server.run(sockets=[listener])
    finally:
        try:
            listener.close()
        except OSError:
            pass
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.probe_native_module:
        return probe_native_module(args.probe_native_module)
    try:
        options = resolve_runtime_options(args)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))

    print(f"WechatAnalysisAssistant backend {APP_VERSION}", flush=True)
    if is_smoke_test_environment():
        print(
            "[INFO] Smoke-test isolation enabled; local WeChat discovery is disabled",
            flush=True,
        )
    else:
        _detect_local_wechat()
    return run_server(options)


if __name__ == "__main__":
    raise SystemExit(main())
