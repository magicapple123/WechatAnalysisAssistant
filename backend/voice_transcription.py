"""Explicit background jobs for WeChat voice-message transcription."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from typing import Optional

from .transcription import (
    TranscriptionAPIError,
    TranscriptionConfig,
    create_transcription_client,
)
from .model_interfaces import interface_label, normalize_interface_preset
from .voice_service import VoiceServiceError, WeChatVoiceService
from .voice_store import VoiceTranscriptionStore, build_transcription_signature


def transcription_interface_preset(config: TranscriptionConfig) -> str:
    return normalize_interface_preset(
        "transcription", config.interface_preset, config.protocol, config.base_url
    )


def transcription_provider_label(config: TranscriptionConfig) -> str:
    if config.protocol == "custom_multipart" and config.custom_name:
        return config.custom_name
    preset = transcription_interface_preset(config)
    return interface_label(preset, config.protocol)


async def _to_thread_safely(function, /, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            pass
        raise


def _transcription_settings_payload(
    config: TranscriptionConfig, *, include_preset: bool
) -> dict:
    payload = {
        "protocol": config.protocol,
        "base_url": config.base_url.rstrip("/"),
        "custom_name": config.custom_name,
        "custom_api_key_header": config.custom_api_key_header,
        "custom_api_key_prefix": config.custom_api_key_prefix,
        "custom_extra_headers": config.custom_extra_headers,
        "custom_audio_field": config.custom_audio_field,
        "custom_model_field": config.custom_model_field,
        "custom_language_field": config.custom_language_field,
        "custom_extra_form_fields": config.custom_extra_form_fields,
        "custom_response_path": config.custom_response_path,
        "custom_filename": config.custom_filename,
    }
    if include_preset:
        payload["interface_preset"] = transcription_interface_preset(config)
    return payload


def _hash_transcription_settings(payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def transcription_settings_hash(config: TranscriptionConfig) -> str:
    """Hash behavior-affecting settings without including the API key."""

    return _hash_transcription_settings(
        _transcription_settings_payload(config, include_preset=True)
    )


def legacy_transcription_settings_hash(config: TranscriptionConfig) -> str:
    """Return the settings hash used before interface presets were added."""

    return _hash_transcription_settings(
        _transcription_settings_payload(config, include_preset=False)
    )


class VoiceTranscriptionManager:
    def __init__(self, store: VoiceTranscriptionStore):
        self.store = store
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_runs = 0
        self._deferred_cleanups = []

    @property
    def has_active_runs(self) -> bool:
        return self._active_runs > 0

    def start(
        self,
        *,
        account_id: str,
        talker: str,
        messages: list[dict],
        voice_service: WeChatVoiceService,
        transcription_config: TranscriptionConfig,
        force: bool = False,
    ) -> dict:
        self._prune()
        task_id = uuid.uuid4().hex
        interface_preset = transcription_interface_preset(
            transcription_config
        )
        job = {
            "task_id": task_id,
            "account_id": account_id,
            "talker": talker,
            "status": "queued",
            "provider": transcription_provider_label(transcription_config),
            "interface_preset": interface_preset,
            "is_cloud": True,
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
                voice_service=voice_service,
                transcription_config=transcription_config,
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

    def get(self, task_id: str) -> Optional[dict]:
        job = self._jobs.get(task_id)
        if not job:
            return None
        public = copy.deepcopy(job)
        public.pop("account_id", None)
        public.pop("talker", None)
        public.pop("cancel_requested", None)
        return public

    def cancel(self, task_id: str) -> bool:
        job = self._jobs.get(task_id)
        if not job or job.get("status") not in ("queued", "running"):
            return False
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["updated_at"] = int(time.time())
        return True

    def cancel_all(self) -> None:
        for task_id in list(self._jobs):
            self.cancel(task_id)

    def defer_cleanup(self, callback) -> None:
        if self._active_runs:
            self._deferred_cleanups.append(callback)
            return
        try:
            callback()
        except Exception:
            pass

    async def _run(
        self,
        job: dict,
        *,
        messages: list[dict],
        voice_service: WeChatVoiceService,
        transcription_config: TranscriptionConfig,
        force: bool,
    ) -> None:
        if job.get("cancel_requested"):
            job["status"] = "cancelled"
            return
        job["status"] = "running"
        job["updated_at"] = int(time.time())
        settings_hash = transcription_settings_hash(transcription_config)
        legacy_settings_hash = legacy_transcription_settings_hash(
            transcription_config
        )
        provider = transcription_config.protocol
        expected_signature = build_transcription_signature(
            provider,
            transcription_config.model,
            transcription_config.language,
            settings_hash,
        )
        legacy_signature = build_transcription_signature(
            provider,
            transcription_config.model,
            transcription_config.language,
            legacy_settings_hash,
        )
        try:
            client = create_transcription_client(transcription_config)
            for message in messages:
                if job.get("cancel_requested"):
                    job["status"] = "cancelled"
                    break
                message_id = int(message.get("id") or 0)
                create_time = int(message.get("create_time") or 0)
                server_id = str(message.get("server_id") or "")
                result = {
                    "message_id": message_id,
                    "create_time": create_time,
                    "server_id": server_id,
                    "message_key": str(message.get("message_key") or ""),
                    "status": "failed",
                    "transcription": "",
                    "error": "",
                }
                existing = self.store.get(
                    job["account_id"],
                    job["talker"],
                    message_id,
                    create_time,
                    server_id=server_id,
                )
                try:
                    if (
                        not force
                        and existing
                        and existing.get("status") == "success"
                        and existing.get("transcription_signature") in {
                            expected_signature,
                            legacy_signature,
                        }
                    ):
                        result.update(
                            status="cached",
                            transcription=existing.get("transcription") or "",
                        )
                        job["cached"] += 1
                    else:
                        decoded = await _to_thread_safely(
                            voice_service.decode_voice,
                            job["talker"],
                            message_id,
                            create_time,
                            server_id,
                        )
                        reusable = None
                        if not force:
                            reusable = self.store.find_reusable(
                                job["account_id"],
                                decoded.resource.audio_sha256,
                                provider,
                                transcription_config.model,
                                transcription_config.language,
                                settings_hash,
                            )
                            if reusable is None:
                                reusable = self.store.find_reusable(
                                    job["account_id"],
                                    decoded.resource.audio_sha256,
                                    provider,
                                    transcription_config.model,
                                    transcription_config.language,
                                    legacy_settings_hash,
                                )
                        if reusable:
                            text = str(reusable.get("transcription") or "")
                            result.update(status="cached", transcription=text)
                            job["cached"] += 1
                        else:
                            text = await _to_thread_safely(
                                client.transcribe, decoded.wav_data
                            )
                            result.update(status="success", transcription=text)
                        self.store.save(
                            account_id=job["account_id"],
                            talker=job["talker"],
                            message_id=message_id,
                            create_time=create_time,
                            server_id=server_id,
                            audio_sha256=decoded.resource.audio_sha256,
                            transcription=result["transcription"],
                            status="success",
                            provider=provider,
                            model=transcription_config.model,
                            language=transcription_config.language,
                            settings_hash=settings_hash,
                            duration_seconds=decoded.duration_seconds,
                        )
                except (VoiceServiceError, TranscriptionAPIError) as exc:
                    result["error"] = str(exc)[:500]
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
                            model=transcription_config.model,
                            language=transcription_config.language,
                            settings_hash=settings_hash,
                        )
                except Exception as exc:
                    result["error"] = f"语音转写失败: {type(exc).__name__}"
                    job["failed"] += 1
                finally:
                    job["results"].append(result)
                    job["completed"] += 1
                    job["updated_at"] = int(time.time())

            if job.get("cancel_requested"):
                job["status"] = "cancelled"
            elif job["status"] not in ("cancelled", "failed"):
                job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except (TranscriptionAPIError, ValueError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)[:500]
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"语音转写任务失败: {type(exc).__name__}"
        finally:
            job["updated_at"] = int(time.time())

    def _prune(self) -> None:
        cutoff = int(time.time()) - 24 * 60 * 60
        for task_id, job in list(self._jobs.items()):
            if (
                job.get("updated_at", 0) < cutoff
                and job.get("status")
                not in ("queued", "running", "cancelling")
            ):
                self._jobs.pop(task_id, None)
