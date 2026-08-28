"""Explicit, user-triggered background jobs for chat image recognition."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from typing import Optional

from .image_service import ImageResolutionError, WeChatImageService
from .image_store import ImageDescriptionStore
from .model_interfaces import interface_label, normalize_interface_preset
from .vision import (
    VisionAPIError,
    VisionConfig,
    create_vision_client,
    normalize_image_for_vision,
)


def vision_interface_preset(config: VisionConfig) -> str:
    return normalize_interface_preset(
        "vision", config.interface_preset, config.provider, config.base_url
    )


def vision_cache_provider(config: VisionConfig) -> str:
    """Build a credential-free cache identity for the selected interface."""

    preset = vision_interface_preset(config)
    payload = {
        "preset": preset,
        "protocol": config.protocol,
        "base_url": config.base_url.rstrip("/"),
    }
    if config.provider == "custom":
        payload["custom"] = {
            "protocol": config.custom_protocol,
            "api_key_header": config.custom_api_key_header,
            "api_key_prefix": config.custom_api_key_prefix,
            "extra_headers": config.custom_extra_headers,
            "request_template": config.custom_request_template,
            "response_path": config.custom_response_path,
        }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{preset}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def legacy_vision_cache_provider(config: VisionConfig) -> Optional[str]:
    """Return the pre-preset cache identity for one safe built-in adapter."""

    if config.provider == "custom":
        return None
    base_hash = hashlib.sha256(
        config.base_url.rstrip("/").encode("utf-8")
    ).hexdigest()[:16]
    return f"{config.provider}:{base_hash}"


def vision_provider_label(config: VisionConfig) -> str:
    if config.provider == "custom" and config.custom_name:
        return config.custom_name
    preset = vision_interface_preset(config)
    return interface_label(preset, config.provider)


async def _to_thread_safely(function, /, *args, **kwargs):
    """Wait for a running worker before propagating coroutine cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            pass
        raise


class ImageRecognitionManager:
    """In-process job manager with per-image progress and persistent results."""

    def __init__(self, store: ImageDescriptionStore):
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_runs = 0
        self._deferred_cleanups = []

    def start(
        self,
        *,
        account_id: str,
        talker: str,
        messages: list[dict],
        image_service: WeChatImageService,
        vision_config: VisionConfig,
        force: bool = False,
    ) -> dict:
        self._prune()
        task_id = uuid.uuid4().hex
        interface_preset = vision_interface_preset(vision_config)
        job = {
            "task_id": task_id,
            "account_id": account_id,
            "talker": talker,
            "status": "queued",
            "provider": vision_provider_label(vision_config),
            "interface_preset": interface_preset,
            "total": len(messages),
            "completed": 0,
            "cached": 0,
            "failed": 0,
            "results": [],
            "error": "",
            "created_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        self._jobs[task_id] = job
        self._active_runs += 1
        task = asyncio.create_task(
            self._run(
                job,
                messages=messages,
                image_service=image_service,
                vision_config=vision_config,
                force=force,
            )
        )
        self._tasks[task_id] = task
        task.add_done_callback(
            lambda _task, job_id=task_id: self._task_finished(job_id)
        )
        return self.get(task_id) or {}

    def _task_finished(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        self._finish_run()

    def get(self, task_id: str) -> Optional[dict]:
        job = self._jobs.get(task_id)
        if not job:
            return None
        public = copy.deepcopy(job)
        public.pop("account_id", None)
        public.pop("talker", None)
        public.pop("cancel_requested", None)
        return public

    def cancel_all(self) -> None:
        """Request cancellation between images without abandoning a thread."""
        for job in self._jobs.values():
            if job["status"] in ("queued", "running"):
                job["cancel_requested"] = True
                job["status"] = "cancelling"
                job["updated_at"] = int(time.time())

    @property
    def has_active_runs(self) -> bool:
        return self._active_runs > 0

    def defer_cleanup(self, callback) -> None:
        if self._active_runs > 0:
            self._deferred_cleanups.append(callback)
            return
        try:
            callback()
        except Exception:
            pass

    def _finish_run(self) -> None:
        self._active_runs = max(0, self._active_runs - 1)
        if self._active_runs:
            return
        callbacks = self._deferred_cleanups
        self._deferred_cleanups = []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    async def _run(
        self,
        job: dict,
        *,
        messages: list[dict],
        image_service: WeChatImageService,
        vision_config: VisionConfig,
        force: bool,
    ) -> None:
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
            job["updated_at"] = int(time.time())
            return
        job["status"] = "running"
        job["updated_at"] = int(time.time())
        try:
            client = create_vision_client(vision_config)
            prompt_hash = hashlib.sha256(
                f"{vision_config.detail}\0{vision_config.prompt}".encode("utf-8")
            ).hexdigest()
            provider = vision_cache_provider(vision_config)
            legacy_provider = legacy_vision_cache_provider(vision_config)

            for message in messages:
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    break
                message_id = int(message.get("id") or 0)
                create_time = int(message.get("create_time") or 0)
                server_id = message.get("server_id") or ""
                result = {
                    "message_id": message_id,
                    "create_time": create_time,
                    "status": "failed",
                    "description": "",
                    "error": "",
                }
                existing = None
                try:
                    existing = self.store.get(
                        job["account_id"],
                        job["talker"],
                        message_id,
                        create_time,
                        server_id=server_id,
                    )
                    cache_matches = (
                        existing
                        and existing.get("status") == "success"
                        and existing.get("provider") in {
                            provider,
                            legacy_provider,
                        }
                        and existing.get("model") == vision_config.model
                        and existing.get("prompt_hash") == prompt_hash
                    )
                    if not force and cache_matches:
                        result.update(
                            status="cached",
                            description=existing.get("description") or "",
                        )
                        job["cached"] += 1
                    else:
                        image = await _to_thread_safely(
                            image_service.get_image,
                            talker=job["talker"],
                            message_id=message_id,
                            create_time=create_time,
                            server_id=server_id,
                            purpose="analysis",
                        )
                        if job.get("cancel_requested"):
                            result["status"] = "cancelled"
                            continue

                        reusable = None
                        if not force:
                            reusable = self.store.find_reusable(
                                job["account_id"],
                                image.sha256,
                                provider,
                                vision_config.model,
                                prompt_hash,
                            )
                            if reusable is None and legacy_provider:
                                reusable = self.store.find_reusable(
                                    job["account_id"],
                                    image.sha256,
                                    legacy_provider,
                                    vision_config.model,
                                    prompt_hash,
                                )
                        if reusable:
                            description = reusable.get("description") or ""
                            result.update(status="cached", description=description)
                            job["cached"] += 1
                        else:
                            normalized, mime_type = await _to_thread_safely(
                                lambda path=image.path: normalize_image_for_vision(
                                    path.read_bytes(),
                                    max_edge={
                                        "low": 1024,
                                        "auto": 2048,
                                        "high": 4096,
                                    }.get(vision_config.detail, 2048),
                                )
                            )
                            if job.get("cancel_requested"):
                                result["status"] = "cancelled"
                                continue
                            description = await _to_thread_safely(
                                client.describe_image, normalized, mime_type
                            )
                            result.update(status="success", description=description)

                        self.store.save(
                            account_id=job["account_id"],
                            talker=job["talker"],
                            message_id=message_id,
                            create_time=create_time,
                            server_id=server_id,
                            image_md5=image.file_md5,
                            image_sha256=image.sha256,
                            description=result["description"],
                            status="success",
                            provider=provider,
                            model=vision_config.model,
                            prompt_hash=prompt_hash,
                        )
                except (ImageResolutionError, VisionAPIError) as exc:
                    error = str(exc)[:500]
                    result["error"] = error
                    job["failed"] += 1
                    if not existing or existing.get("status") != "success":
                        self.store.save(
                            account_id=job["account_id"],
                            talker=job["talker"],
                            message_id=message_id,
                            create_time=create_time,
                            server_id=server_id,
                            status="failed",
                            error=error,
                            provider=provider,
                            model=vision_config.model,
                            prompt_hash=prompt_hash,
                        )
                except Exception as exc:
                    # Do not expose request bodies, API keys, or local paths.
                    result["error"] = f"图片识别失败: {type(exc).__name__}"
                    job["failed"] += 1
                    if not existing or existing.get("status") != "success":
                        self.store.save(
                            account_id=job["account_id"],
                            talker=job["talker"],
                            message_id=message_id,
                            create_time=create_time,
                            server_id=server_id,
                            status="failed",
                            error=result["error"],
                            provider=provider,
                            model=vision_config.model,
                            prompt_hash=prompt_hash,
                        )
                finally:
                    job["results"].append(result)
                    job["completed"] += 1
                    job["updated_at"] = int(time.time())

            if job.get("cancel_requested"):
                job["status"] = "cancelled"
            elif job["status"] != "cancelled":
                job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except (VisionAPIError, ValueError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)[:500]
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"识别任务失败: {type(exc).__name__}"
        finally:
            job["updated_at"] = int(time.time())

    def _prune(self) -> None:
        cutoff = int(time.time()) - 24 * 60 * 60
        removable = [
            task_id
            for task_id, job in self._jobs.items()
            if job.get("updated_at", 0) < cutoff
            and job.get("status") not in ("queued", "running", "cancelling")
        ]
        for task_id in removable:
            self._jobs.pop(task_id, None)
