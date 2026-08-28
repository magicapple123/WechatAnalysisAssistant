"""User-triggered batch jobs that navigate an already-open WeChat image viewer."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from typing import Callable, Optional

from .image_hd_resource import ImageHDTarget, WeChatHDResourceMonitor
from .wechat_viewer_automation import (
    HOTKEY_LABELS,
    HOTKEY_PAUSE,
    HOTKEY_START_RESUME,
    HOTKEY_STOP,
    ViewerAutomationError,
    ViewerAutomationInterrupted,
    ViewerNavigationSentButUnconfirmed,
    WeChatViewerController,
)


FINISHED_STATUSES = {"completed", "cancelled", "failed"}


class ImageHDAutomationManager:
    def __init__(
        self,
        controller_factory: Callable[[], WeChatViewerController] = WeChatViewerController,
    ):
        self.controller_factory = controller_factory
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def has_active_runs(self) -> bool:
        return any(
            job.get("status") not in FINISHED_STATUSES
            for job in self._jobs.values()
        )

    def start(
        self,
        *,
        account_id: str,
        talker: str,
        sequence: list[dict],
        monitor: WeChatHDResourceMonitor,
        direction: str,
        per_image_timeout: int,
        min_dwell_seconds: float = 0.5,
        pre_skipped: int = 0,
        total_images: Optional[int] = None,
    ) -> dict:
        if self.has_active_runs:
            raise RuntimeError("已有批量高清任务正在运行")
        self._prune()
        image_items = [item for item in sequence if item.get("kind") == "image"]
        if not image_items:
            raise ValueError("所选范围内没有图片")
        min_dwell_seconds = float(min_dwell_seconds)
        if not 0 <= min_dwell_seconds <= 5:
            raise ValueError("验证成功后的最短停留时间必须在 0–5 秒之间")
        task_id = uuid.uuid4().hex
        first_target: ImageHDTarget = image_items[0]["target"]
        now = int(time.time())
        job = {
            "task_id": task_id,
            "account_id": account_id,
            "talker": talker,
            "status": "awaiting_viewer",
            "direction": direction,
            "per_image_timeout": int(per_image_timeout),
            "min_dwell_seconds": min_dwell_seconds,
            "total": int(total_images or len(image_items)),
            "completed": int(pre_skipped),
            "downloaded": 0,
            "skipped": int(pre_skipped),
            "expired_count": 0,
            "failed_count": 0,
            "unconfirmed_navigation_count": 0,
            "navigation_warning": "",
            "navigation_mode": "auto",
            "navigation_pause_kind": "",
            "can_switch_to_manual": False,
            "navigation_sent": False,
            "manual_navigation_count": 0,
            "results": [],
            "current": None,
            "first_target": first_target.public_reference(),
            "hotkeys": dict(HOTKEY_LABELS),
            "error": "",
            "pause_reason": "",
            "pause_requested": False,
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
        }
        self._jobs[task_id] = job
        if all(item["target"].initially_verified for item in image_items):
            job.update(
                status="completed",
                completed=job["total"],
                skipped=job["total"],
                current=None,
                updated_at=int(time.time()),
            )
            return self.get(task_id) or {}
        task = asyncio.create_task(
            asyncio.to_thread(
                self._run_sync,
                job,
                sequence,
                monitor,
                per_image_timeout,
                min_dwell_seconds,
            )
        )
        self._tasks[task_id] = task
        task.add_done_callback(
            lambda _task, job_id=task_id: self._tasks.pop(job_id, None)
        )
        return self.get(task_id) or {}

    def get(self, task_id: str) -> Optional[dict]:
        job = self._jobs.get(task_id)
        if not job:
            return None
        public = copy.deepcopy(job)
        for private_key in (
            "account_id",
            "talker",
            "pause_requested",
            "cancel_requested",
        ):
            public.pop(private_key, None)
        return public

    def get_active(self) -> Optional[dict]:
        active = [
            job
            for job in self._jobs.values()
            if job.get("status") not in FINISHED_STATUSES
        ]
        if not active:
            return None
        latest = max(active, key=lambda item: int(item.get("created_at") or 0))
        return self.get(str(latest.get("task_id") or ""))

    def pause(self, task_id: str) -> bool:
        job = self._jobs.get(task_id)
        if not job or job.get("status") in FINISHED_STATUSES:
            return False
        job["pause_requested"] = True
        job["pause_reason"] = "已从工具界面请求暂停"
        job["updated_at"] = int(time.time())
        return True

    def set_navigation_mode(self, task_id: str, mode: str) -> bool:
        if mode not in ("auto", "manual"):
            raise ValueError("翻页模式必须是 auto 或 manual")
        job = self._jobs.get(task_id)
        if not job or job.get("status") in FINISHED_STATUSES:
            return False
        if job.get("status") != "paused":
            raise RuntimeError("只能在任务暂停时切换翻页模式")
        if (
            mode == "manual"
            and job.get("navigation_mode") != "manual"
            and not job.get("can_switch_to_manual")
        ):
            raise RuntimeError("当前暂停不是自动翻页失败，暂时不能切换为手动翻页")
        job["navigation_mode"] = mode
        if mode == "manual":
            job["can_switch_to_manual"] = False
        pause_kind = str(job.get("navigation_pause_kind") or "")
        if mode == "manual":
            if pause_kind == "auto_before_send":
                job["pause_reason"] = (
                    "已切换为手动翻页：请在微信查看器中手动切换一项媒体，"
                    "然后按启动热键继续"
                )
            elif pause_kind == "auto_after_send":
                job["pause_reason"] = (
                    "已切换为手动翻页；本次方向键已经发出，请先确认是否已翻页，"
                    "必要时手动补翻一项，再按启动热键继续"
                )
            else:
                job["pause_reason"] = (
                    "已切换为手动翻页；恢复当前步骤后，程序会在每次翻页前暂停等待你操作"
                )
        elif pause_kind == "auto_before_send":
            job["pause_reason"] = (
                "已切回自动翻页；本次方向键尚未发出，回到微信查看器并按启动热键重试"
            )
        elif pause_kind == "auto_after_send":
            job["pause_reason"] = (
                "已切回自动翻页；本次方向键已经发出，请核对当前位置后按启动热键继续"
            )
        elif pause_kind == "manual_required":
            job["pause_reason"] = (
                "已切回自动翻页；请不要手动翻页，回到微信查看器并按启动热键继续"
            )
        job["updated_at"] = int(time.time())
        return True

    @staticmethod
    def _clear_navigation_pause(job: dict) -> None:
        job["navigation_pause_kind"] = ""
        job["can_switch_to_manual"] = False
        job["navigation_sent"] = False
        job["updated_at"] = int(time.time())

    def cancel(self, task_id: str) -> bool:
        job = self._jobs.get(task_id)
        if not job or job.get("status") in FINISHED_STATUSES:
            return False
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["updated_at"] = int(time.time())
        return True

    def cancel_all(self) -> None:
        for task_id in list(self._jobs):
            self.cancel(task_id)

    @staticmethod
    def _set_status(job: dict, status: str, **updates) -> None:
        job["status"] = status
        job.update(updates)
        job["updated_at"] = int(time.time())

    def _handle_hotkey(self, job: dict, hotkey: Optional[int]) -> None:
        if hotkey == HOTKEY_STOP:
            job["cancel_requested"] = True
            self._set_status(job, "cancelling")
        elif hotkey == HOTKEY_PAUSE:
            job["pause_requested"] = True
            job["pause_reason"] = "已按下暂停快捷键"

    def _wait_until_running(
        self, job: dict, controller: WeChatViewerController
    ) -> bool:
        while not job.get("cancel_requested"):
            hotkey = controller.poll_hotkey()
            self._handle_hotkey(job, hotkey)
            if job.get("cancel_requested"):
                return False
            if hotkey == HOTKEY_START_RESUME:
                try:
                    if controller.hwnd:
                        controller.resume_foreground_viewer()
                        metadata = {"wechat_version": controller.version}
                    else:
                        metadata = controller.calibrate_foreground_viewer()
                    job["pause_requested"] = False
                    self._set_status(
                        job,
                        "running",
                        error="",
                        pause_reason="",
                        **metadata,
                    )
                    return True
                except ViewerAutomationError as exc:
                    self._set_status(
                        job,
                        "paused" if controller.hwnd else "awaiting_viewer",
                        error=str(exc),
                        pause_reason=str(exc),
                    )
            elif job.get("pause_requested") or job.get("status") == "paused":
                self._set_status(job, "paused")
            time.sleep(0.05)
        return False

    def _control_point(
        self, job: dict, controller: WeChatViewerController
    ) -> bool:
        hotkey = controller.poll_hotkey()
        self._handle_hotkey(job, hotkey)
        if job.get("cancel_requested"):
            return False
        if job.get("pause_requested"):
            self._set_status(job, "paused")
            return self._wait_until_running(job, controller)
        try:
            controller.validate_safety()
            return True
        except ViewerAutomationError as exc:
            job["pause_requested"] = True
            self._set_status(
                job,
                "paused",
                pause_reason=f"安全暂停：{exc}",
                error="",
            )
            return self._wait_until_running(job, controller)

    def _navigate(
        self, job: dict, controller: WeChatViewerController
    ) -> bool:
        while not job.get("cancel_requested"):
            if job.get("navigation_mode") == "manual":
                direction_label = (
                    "下一项" if job.get("direction") == "next" else "上一项"
                )
                job["pause_requested"] = True
                self._set_status(
                    job,
                    "paused",
                    navigation_pause_kind="manual_required",
                    can_switch_to_manual=False,
                    navigation_sent=False,
                    pause_reason=(
                        f"手动翻页模式：请在微信查看器中切换到{direction_label}媒体"
                        "（图片或视频），然后按启动热键继续"
                    ),
                    error="",
                )
                if not self._wait_until_running(job, controller):
                    return False
                if job.get("navigation_mode") == "manual":
                    job["manual_navigation_count"] += 1
                    self._clear_navigation_pause(job)
                    return True
                self._clear_navigation_pause(job)

            try:
                confirmed = controller.navigate(
                    job["direction"],
                    interrupt=lambda: self._control_point(job, controller),
                )
                if not confirmed:
                    job["unconfirmed_navigation_count"] += 1
                    job["navigation_warning"] = (
                        "方向键已发送，但未能确认画面变化；请核对当前位置后继续"
                    )
                    job["pause_requested"] = True
                    self._set_status(
                        job,
                        "paused",
                        navigation_pause_kind="auto_after_send",
                        can_switch_to_manual=True,
                        navigation_sent=True,
                        pause_reason=(
                            "自动方向键已经发出，但未确认画面是否变化。"
                            "若仍是原媒体，请手动切换一项；若已经切换，请不要再翻页"
                        ),
                        error="",
                    )
                    if not self._wait_until_running(job, controller):
                        return False
                    self._clear_navigation_pause(job)
                return True
            except ViewerAutomationInterrupted:
                if job.get("cancel_requested"):
                    return False
            except ViewerNavigationSentButUnconfirmed as exc:
                job["unconfirmed_navigation_count"] += 1
                job["navigation_warning"] = (
                    "方向键发送后查看器状态发生变化；恢复后会从下一项继续，不会重复翻页"
                )
                job["pause_requested"] = True
                self._set_status(
                    job,
                    "paused",
                    navigation_pause_kind="auto_after_send",
                    can_switch_to_manual=True,
                    navigation_sent=True,
                    pause_reason=f"自动翻页安全暂停：{exc}",
                    error="",
                )
                if not self._wait_until_running(job, controller):
                    return False
                self._clear_navigation_pause(job)
                return True
            except ViewerAutomationError as exc:
                job["pause_requested"] = True
                self._set_status(
                    job,
                    "paused",
                    navigation_pause_kind="auto_before_send",
                    can_switch_to_manual=True,
                    navigation_sent=False,
                    pause_reason=(
                        f"自动翻页暂停（方向键尚未发出）：{exc}。"
                        "可重试自动翻页，或在工具中切换为手动翻页"
                    ),
                    error="",
                )
                if not self._wait_until_running(job, controller):
                    return False
                if job.get("navigation_mode") == "manual":
                    job["manual_navigation_count"] += 1
                    self._clear_navigation_pause(job)
                    return True
                self._clear_navigation_pause(job)
        return False

    def _process_image(
        self,
        job: dict,
        controller: WeChatViewerController,
        monitor: WeChatHDResourceMonitor,
        target: ImageHDTarget,
        timeout: int,
    ) -> bool:
        job["current"] = target.public_reference()
        job["updated_at"] = int(time.time())
        result = {
            **target.public_reference(),
            "status": "failed",
            "error": "",
        }
        if target.initially_verified:
            result["status"] = "skipped"
            job["skipped"] += 1
        elif monitor.is_expired(target):
            result["status"] = "expired"
            result["error"] = "微信已标记图片过期或已清理"
            job["expired_count"] += 1
        else:
            remaining = max(0.0, float(timeout))
            while remaining > 0:
                if not self._control_point(job, controller):
                    return False
                active_started = time.monotonic()
                if monitor.is_verified_high(job["talker"], target):
                    result["status"] = "downloaded"
                    job["downloaded"] += 1
                    break
                if monitor.is_expired(target):
                    result["status"] = "expired"
                    result["error"] = "微信已标记图片过期或已清理"
                    job["expired_count"] += 1
                    break
                remaining -= max(0.0, time.monotonic() - active_started)
                if remaining <= 0:
                    break
                interval = min(0.3, remaining)
                sleep_started = time.monotonic()
                time.sleep(interval)
                # Paused time is spent inside _control_point and therefore does
                # not consume this active verification budget.
                remaining -= max(interval, time.monotonic() - sleep_started)
            if (
                result["status"] == "failed"
                and not self._control_point(job, controller)
            ):
                return False
            if result["status"] == "failed":
                try:
                    monitor.force_refresh()
                    if monitor.is_verified_high(job["talker"], target):
                        result["status"] = "downloaded"
                        job["downloaded"] += 1
                    elif monitor.is_expired(target):
                        result["status"] = "expired"
                        result["error"] = "微信已标记图片过期或已清理"
                        job["expired_count"] += 1
                except Exception:
                    pass
            if result["status"] == "failed":
                # A refresh/decrypt check may take long enough for a pause or
                # stop request to arrive. Honour it before committing failure.
                if not self._control_point(job, controller):
                    return False
                try:
                    if monitor.is_verified_high(job["talker"], target):
                        result["status"] = "downloaded"
                        job["downloaded"] += 1
                    elif monitor.is_expired(target):
                        result["status"] = "expired"
                        result["error"] = "微信已标记图片过期或已清理"
                        job["expired_count"] += 1
                except Exception:
                    pass
            if result["status"] == "failed":
                result["error"] = "等待高清文件落盘并验证超时"
                job["failed_count"] += 1

        job["results"].append(result)
        if len(job["results"]) > 500:
            job["results"] = job["results"][-500:]
        job["completed"] += 1
        job["updated_at"] = int(time.time())
        return True

    def _wait_minimum_dwell(
        self,
        job: dict,
        controller: WeChatViewerController,
        seconds: float,
    ) -> bool:
        """Keep a newly verified image visible without delaying other outcomes."""
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            if not self._control_point(job, controller):
                return False
            interval = min(0.1, remaining)
            sleep_started = time.monotonic()
            time.sleep(interval)
            # Count only active wait slices. Time spent blocked in a paused
            # control point is deliberately excluded from the configured dwell.
            remaining -= max(interval, time.monotonic() - sleep_started)
        return True

    def _confirm_first_target(
        self,
        job: dict,
        controller: WeChatViewerController,
        monitor: WeChatHDResourceMonitor,
        target: ImageHDTarget,
        timeout: int,
    ) -> bool:
        if target.initially_verified:
            return True
        if monitor.is_expired(target):
            job["pause_requested"] = True
            self._set_status(
                job,
                "paused",
                pause_reason=(
                    "任务首图在创建前就已被微信标记为过期，无法通过资源变化确认位置。"
                    "请确认当前仍是任务提示的第一张图片，再按一次启动热键明确继续；"
                    "程序尚未发送方向键"
                ),
                error="",
            )
            return self._wait_until_running(job, controller)
        while not job.get("cancel_requested"):
            remaining = float(max(5, int(timeout)))
            while remaining > 0:
                if not self._control_point(job, controller):
                    return False
                active_started = time.monotonic()
                if (
                    monitor.is_verified_high(job["talker"], target)
                    or monitor.has_activity(job["talker"], target)
                ):
                    return True
                remaining -= max(0.0, time.monotonic() - active_started)
                if remaining <= 0:
                    break
                interval = min(0.25, remaining)
                sleep_started = time.monotonic()
                time.sleep(interval)
                remaining -= max(interval, time.monotonic() - sleep_started)
            if not self._control_point(job, controller):
                return False
            if (
                monitor.is_verified_high(job["talker"], target)
                or monitor.has_activity(job["talker"], target)
            ):
                return True
            job["pause_requested"] = True
            self._set_status(
                job,
                "paused",
                pause_reason=(
                    "微信没有提供这张图片的资源访问变化，这不一定表示图片打开错误。"
                    "请确认当前仍是任务提示的第一张图片，再按一次启动热键明确继续；"
                    "程序尚未发送方向键"
                ),
                error="",
            )
            if not self._wait_until_running(job, controller):
                return False
            # A second explicit start hotkey is the user's confirmation when
            # this WeChat build does not publish any per-resource access event.
            # Window/process/viewer identity is still revalidated by resume.
            return True
        return False

    def _run_sync(
        self,
        job: dict,
        sequence: list[dict],
        monitor: WeChatHDResourceMonitor,
        per_image_timeout: int,
        min_dwell_seconds: float,
    ) -> None:
        controller = None
        try:
            controller = self.controller_factory()
            controller.register_hotkeys()
            self._set_status(job, "awaiting_viewer")
            if not self._wait_until_running(job, controller):
                self._set_status(job, "cancelled")
                return

            first_image = next(
                (item["target"] for item in sequence if item.get("kind") == "image"),
                None,
            )
            if first_image is not None and not self._confirm_first_target(
                job, controller, monitor, first_image, per_image_timeout
            ):
                self._set_status(job, "cancelled")
                return

            for index, item in enumerate(sequence):
                if not self._control_point(job, controller):
                    break
                if item.get("kind") == "image":
                    if not self._process_image(
                        job,
                        controller,
                        monitor,
                        item["target"],
                        per_image_timeout,
                    ):
                        break
                else:
                    # Videos may be part of WeChat's media-viewer sequence.
                    time.sleep(0.5)
                if index < len(sequence) - 1:
                    if (
                        item.get("kind") == "image"
                        and job.get("results")
                        and job["results"][-1].get("status") == "downloaded"
                        and not self._wait_minimum_dwell(
                            job,
                            controller,
                            min_dwell_seconds,
                        )
                    ):
                        break
                    if not self._navigate(job, controller):
                        break

            if job.get("cancel_requested"):
                self._set_status(job, "cancelled")
            elif job.get("completed") >= job.get("total"):
                self._set_status(job, "completed", current=None)
            elif job.get("status") not in FINISHED_STATUSES:
                self._set_status(job, "paused")
        except Exception as exc:
            self._set_status(
                job,
                "failed",
                error=(
                    str(exc)[:500]
                    if isinstance(exc, (ViewerAutomationError, ValueError))
                    else f"批量高清任务失败：{type(exc).__name__}"
                ),
            )
        finally:
            if controller is not None:
                controller.close()
            job["updated_at"] = int(time.time())

    def _prune(self) -> None:
        cutoff = int(time.time()) - 24 * 60 * 60
        for task_id, job in list(self._jobs.items()):
            if (
                job.get("updated_at", 0) < cutoff
                and job.get("status") in FINISHED_STATUSES
            ):
                self._jobs.pop(task_id, None)
