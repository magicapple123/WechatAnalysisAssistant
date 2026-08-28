"""Conservative keyboard automation for an explicitly opened WeChat viewer."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

from .subprocess_env import sanitized_subprocess_env


class ViewerAutomationError(RuntimeError):
    pass


class ViewerAutomationInterrupted(ViewerAutomationError):
    pass


class ViewerNavigationSentButUnconfirmed(ViewerAutomationError):
    """The arrow was sent; callers must never resend it for the same step."""

    pass


if os.name == "nt":
    import psutil
    import win32api
    import win32con
    import win32gui
    import win32process

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi

    ULONG_PTR = wintypes.WPARAM

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        )

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        )

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class INPUTUNION(ctypes.Union):
        _fields_ = (
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        )

    class INPUT(ctypes.Structure):
        _fields_ = (("type", wintypes.DWORD), ("union", INPUTUNION))

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


HOTKEY_START_RESUME = 0x51A1
HOTKEY_PAUSE = 0x51A2
HOTKEY_STOP = 0x51A3
HOTKEY_LABELS = {
    "start_resume": "Ctrl+Alt+F9",
    "pause": "Ctrl+Alt+F10",
    "stop": "Ctrl+Alt+F11",
}
_INPUT_SENTINEL = 0x57414149

# WeChat 4.x exposes its self-drawn viewer as a separate top-level Qt window.
# The image pixels are not an identity signal: the toolbar can be light and the
# pixels necessarily change after every navigation. The native title is stable.
_VIEWER_TITLE_MARKERS = (
    "图片和视频",
    "圖片和視頻",
    "圖片和影片",
    "images and videos",
    "images & videos",
    "photos and videos",
    "photos & videos",
    "image and video",
    "photo and video",
    "画像と動画",
    "사진 및 동영상",
)


def _authenticode_is_tencent(executable: Path) -> bool:
    env = sanitized_subprocess_env()
    env["WAA_WEIXIN_EXE"] = str(executable)
    script = (
        "$s=Get-AuthenticodeSignature -LiteralPath $env:WAA_WEIXIN_EXE;"
        "$o=[PSCustomObject]@{Status=[string]$s.Status;"
        "Subject=[string]$s.SignerCertificate.Subject};"
        "$o|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(completed.stdout.strip() or "{}")
        subject = str(payload.get("Subject") or "").lower()
        return completed.returncode == 0 and payload.get("Status") == "Valid" and (
            "tencent" in subject or "腾讯" in subject
        )
    except Exception:
        return False


def _file_version(executable: Path) -> str:
    try:
        info = win32api.GetFileVersionInfo(str(executable), "\\")
        ms = int(info["FileVersionMS"])
        ls = int(info["FileVersionLS"])
        return f"{ms >> 16}.{ms & 0xffff}.{ls >> 16}.{ls & 0xffff}"
    except Exception:
        return ""


class WeChatViewerController:
    """Send a single arrow only while the calibrated viewer remains foreground."""

    def __init__(self):
        if os.name != "nt":
            raise ViewerAutomationError("批量触发高清图片仅支持 Windows")
        self.hwnd = 0
        self.pid = 0
        self.executable = Path()
        self.version = ""
        self.window_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.dpi = 0
        self.top_windows: set[int] = set()
        self.frame: Optional[list[int]] = None
        self.window_title = ""
        self.window_class = ""
        self.cursor_position: tuple[int, int] = (0, 0)
        self.last_input_tick = 0
        self._hotkeys_registered = False

    def register_hotkeys(self) -> None:
        # Force creation of this worker thread's Win32 message queue before
        # registering thread-bound global hotkeys.
        message = wintypes.MSG()
        user32.PeekMessageW(
            ctypes.byref(message), None, 0, 0, 0
        )
        modifiers = win32con.MOD_CONTROL | win32con.MOD_ALT | 0x4000
        registered = []
        try:
            for hotkey_id, virtual_key in (
                (HOTKEY_START_RESUME, win32con.VK_F9),
                (HOTKEY_PAUSE, win32con.VK_F10),
                (HOTKEY_STOP, win32con.VK_F11),
            ):
                if not user32.RegisterHotKey(
                    None, hotkey_id, modifiers, virtual_key
                ):
                    raise ViewerAutomationError(
                        "全局快捷键已被其他程序占用，请关闭冲突程序后重试"
                    )
                registered.append(hotkey_id)
            self._hotkeys_registered = True
        except Exception:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
            raise

    def close(self) -> None:
        if self._hotkeys_registered:
            for hotkey_id in (
                HOTKEY_START_RESUME,
                HOTKEY_PAUSE,
                HOTKEY_STOP,
            ):
                user32.UnregisterHotKey(None, hotkey_id)
            self._hotkeys_registered = False

    def poll_hotkey(self) -> Optional[int]:
        message = wintypes.MSG()
        if user32.PeekMessageW(
            ctypes.byref(message),
            None,
            win32con.WM_HOTKEY,
            win32con.WM_HOTKEY,
            win32con.PM_REMOVE,
        ):
            return int(message.wParam)
        return None

    @staticmethod
    def _root_window(hwnd: int) -> int:
        return int(user32.GetAncestor(hwnd, 2))

    @staticmethod
    def _window_pid(hwnd: int) -> int:
        _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)

    @staticmethod
    def _window_dpi(hwnd: int) -> int:
        try:
            return int(user32.GetDpiForWindow(hwnd))
        except Exception:
            return 0

    @staticmethod
    def _is_cloaked(hwnd: int) -> bool:
        cloaked = wintypes.DWORD(0)
        try:
            result = dwmapi.DwmGetWindowAttribute(
                hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            return result == 0 and bool(cloaked.value)
        except Exception:
            return False

    @staticmethod
    def _last_input_time() -> int:
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if user32.GetLastInputInfo(ctypes.byref(info)):
            return int(info.dwTime)
        return 0

    @staticmethod
    def _large_top_windows(pid: int) -> set[int]:
        result: set[int] = set()

        def callback(hwnd, _extra):
            try:
                if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                    return
                if WeChatViewerController._window_pid(hwnd) != pid:
                    return
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if max(0, right - left) * max(0, bottom - top) >= 80_000:
                    result.add(int(hwnd))
            except Exception:
                return

        win32gui.EnumWindows(callback, None)
        return result

    def _capture_frame(self) -> list[int]:
        from PIL import ImageGrab

        left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
        image = ImageGrab.grab(
            bbox=(left, top, right, bottom), all_screens=True
        ).convert("L")
        width, height = image.size
        # Ignore the mostly static title/toolbar and window borders. This
        # makes page changes behind WeChat's common "expired" overlay visible
        # to the comparison instead of diluting them across the whole window.
        image = image.crop(
            (
                int(width * 0.06),
                int(height * 0.10),
                max(int(width * 0.94), int(width * 0.06) + 1),
                max(int(height * 0.96), int(height * 0.10) + 1),
            )
        ).resize((80, 80))
        return list(image.getdata())

    @staticmethod
    def _frame_difference(first: list[int], second: list[int]) -> tuple[float, float]:
        if len(first) != len(second) or not first:
            return 255.0, 1.0
        differences = [abs(a - b) for a, b in zip(first, second)]
        return (
            sum(differences) / len(differences),
            sum(value >= 12 for value in differences) / len(differences),
        )

    @staticmethod
    def _frame_changed(first: list[int], second: list[int]) -> bool:
        average, ratio = WeChatViewerController._frame_difference(first, second)
        # A page may retain the same toolbar and expired-image mask while only
        # the underlying thumbnail changes. Either a small broad change or a
        # stronger local change is sufficient after the frame was stable.
        return average >= 0.8 or ratio >= 0.02

    @staticmethod
    def _title_looks_like_viewer(title: str) -> bool:
        normalized = " ".join(str(title or "").strip().lower().split())
        return any(marker in normalized for marker in _VIEWER_TITLE_MARKERS)

    def _viewer_heuristic(self, require_calibrated_identity: bool = False) -> bool:
        try:
            title = str(win32gui.GetWindowText(self.hwnd) or "")
            window_class = str(win32gui.GetClassName(self.hwnd) or "")
        except Exception:
            return False
        if not (
            window_class.startswith("Qt")
            and self._title_looks_like_viewer(title)
        ):
            return False
        if require_calibrated_identity:
            return title == self.window_title and window_class == self.window_class
        return True

    def calibrate_foreground_viewer(self) -> dict:
        foreground = int(win32gui.GetForegroundWindow())
        hwnd = self._root_window(foreground)
        if not hwnd or not win32gui.IsWindow(hwnd):
            raise ViewerAutomationError("没有检测到前台微信图片查看器")
        pid = self._window_pid(hwnd)
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe()).resolve()
        except Exception as exc:
            raise ViewerAutomationError("无法验证前台程序身份") from exc
        if executable.name.lower() != "weixin.exe":
            raise ViewerAutomationError("前台窗口不是电脑版微信")
        if not _authenticode_is_tencent(executable):
            raise ViewerAutomationError("电脑版微信数字签名校验失败")
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            raise ViewerAutomationError("微信图片查看器不可见或已最小化")
        if self._is_cloaked(hwnd) or user32.IsHungAppWindow(hwnd):
            raise ViewerAutomationError("微信图片查看器当前不可用")
        rect = tuple(int(value) for value in win32gui.GetWindowRect(hwnd))
        if (rect[2] - rect[0]) < 600 or (rect[3] - rect[1]) < 400:
            raise ViewerAutomationError("微信图片查看器窗口过小")

        self.hwnd = hwnd
        self.pid = pid
        self.executable = executable
        self.version = _file_version(executable)
        self.window_rect = rect
        self.dpi = self._window_dpi(hwnd)
        self.top_windows = self._large_top_windows(pid)
        if not self._viewer_heuristic():
            self.hwnd = 0
            raise ViewerAutomationError(
                "前台微信窗口不像图片查看器，请先点开第一张图片"
            )
        self.window_title = str(win32gui.GetWindowText(hwnd) or "")
        self.window_class = str(win32gui.GetClassName(hwnd) or "")
        self.frame = self._wait_until_stable()
        time.sleep(0.55)
        self.arm_input_guard()
        return {"wechat_version": self.version}

    def arm_input_guard(self) -> None:
        self.cursor_position = tuple(win32gui.GetCursorPos())
        self.last_input_tick = self._last_input_time()

    def resume_foreground_viewer(self) -> None:
        """Re-arm only when the user has manually returned to the same viewer."""
        self._validate_window(require_foreground=True)
        if not self._viewer_heuristic(require_calibrated_identity=True):
            raise ViewerAutomationError("当前微信窗口不再是已校准的图片查看器")
        time.sleep(0.55)
        self.arm_input_guard()
        self.frame = self._wait_until_stable(guarded=True)

    def _validate_window(self, require_foreground: bool = True) -> None:
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            raise ViewerAutomationError("微信图片查看器已经关闭")
        if self._window_pid(self.hwnd) != self.pid:
            raise ViewerAutomationError("微信图片查看器进程已经变化")
        try:
            current_executable = Path(psutil.Process(self.pid).exe()).resolve()
        except Exception as exc:
            raise ViewerAutomationError("无法重新验证微信进程") from exc
        if current_executable != self.executable:
            raise ViewerAutomationError("微信程序路径已经变化")
        if not win32gui.IsWindowVisible(self.hwnd) or win32gui.IsIconic(self.hwnd):
            raise ViewerAutomationError("微信图片查看器不可见或已最小化")
        if self._is_cloaked(self.hwnd) or user32.IsHungAppWindow(self.hwnd):
            raise ViewerAutomationError("微信图片查看器无响应")
        if require_foreground:
            foreground = self._root_window(int(win32gui.GetForegroundWindow()))
            if foreground != self.hwnd:
                raise ViewerAutomationError("微信图片查看器已失去前台焦点")
        rect = tuple(int(value) for value in win32gui.GetWindowRect(self.hwnd))
        if any(abs(a - b) > 2 for a, b in zip(rect, self.window_rect)):
            raise ViewerAutomationError("微信图片查看器的位置或大小发生变化")
        if self._window_dpi(self.hwnd) != self.dpi:
            raise ViewerAutomationError("微信图片查看器的显示缩放发生变化")
        if self._large_top_windows(self.pid) != self.top_windows:
            raise ViewerAutomationError("微信出现了新的窗口或弹窗")

    def validate_safety(self) -> None:
        self._validate_window(require_foreground=True)
        if tuple(win32gui.GetCursorPos()) != self.cursor_position:
            raise ViewerAutomationError("检测到鼠标移动")
        if self._last_input_time() != self.last_input_tick:
            raise ViewerAutomationError("检测到用户键盘或鼠标操作")

    def _wait_until_stable(
        self,
        timeout: float = 4.0,
        interrupt: Optional[Callable[[], bool]] = None,
        guarded: bool = False,
    ) -> list[int]:
        deadline = time.monotonic() + timeout
        previous = self._capture_frame()
        stable_count = 0
        while time.monotonic() < deadline:
            if interrupt is not None and not interrupt():
                raise ViewerAutomationInterrupted("自动翻页已暂停")
            if guarded:
                self.validate_safety()
            time.sleep(0.12)
            current = self._capture_frame()
            average, ratio = self._frame_difference(previous, current)
            if average < 1.5 and ratio < 0.02:
                stable_count += 1
                if stable_count >= 2:
                    return current
            else:
                stable_count = 0
            previous = current
        return previous

    def _send_virtual_key(self, virtual_key: int) -> None:
        # Last possible foreground/PID gate immediately before global input.
        self._validate_window(require_foreground=True)
        if not self._viewer_heuristic(require_calibrated_identity=True):
            raise ViewerAutomationError("微信图片查看器画面身份校验失败")
        key_down = INPUT(
            1,
            INPUTUNION(
                ki=KEYBDINPUT(
                    virtual_key, 0, 0, 0, _INPUT_SENTINEL
                )
            ),
        )
        key_up = INPUT(
            1,
            INPUTUNION(
                ki=KEYBDINPUT(
                    virtual_key, 0, win32con.KEYEVENTF_KEYUP, 0, _INPUT_SENTINEL
                )
            ),
        )
        inputs = (INPUT * 2)(key_down, key_up)
        sent = user32.SendInput(2, inputs, ctypes.sizeof(INPUT))
        if sent != 2:
            raise ViewerAutomationError("方向键发送失败")

    def navigate(
        self,
        direction: str,
        interrupt: Optional[Callable[[], bool]] = None,
    ) -> bool:
        self.validate_safety()
        before = self._wait_until_stable(
            timeout=2.0, interrupt=interrupt, guarded=True
        )
        self.validate_safety()
        virtual_key = win32con.VK_RIGHT if direction == "next" else win32con.VK_LEFT
        self._send_virtual_key(virtual_key)
        try:
            time.sleep(0.05)
            self.arm_input_guard()
            deadline = time.monotonic() + 1.5
            changed = None
            while time.monotonic() < deadline:
                if interrupt is not None and not interrupt():
                    raise ViewerAutomationInterrupted("自动翻页已暂停")
                self.validate_safety()
                current = self._capture_frame()
                if self._frame_changed(before, current):
                    changed = current
                    break
                time.sleep(0.08)
        except ViewerAutomationInterrupted:
            raise
        except ViewerAutomationError as exc:
            raise ViewerNavigationSentButUnconfirmed(str(exc)) from exc
        if changed is None:
            self.frame = self._capture_frame()
            self.arm_input_guard()
            return False
        try:
            self.frame = self._wait_until_stable(
                timeout=2.0, interrupt=interrupt, guarded=True
            )
        except ViewerAutomationInterrupted:
            raise
        except ViewerAutomationError as exc:
            raise ViewerNavigationSentButUnconfirmed(str(exc)) from exc
        self.arm_input_guard()
        return True
