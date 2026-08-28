"""Bounded speech-to-text clients for decoded WeChat WAV audio.

The module supports cloud-only transcription via OpenAI-compatible and custom
multipart APIs.  Audio is uploaded only when the caller explicitly creates a
cloud client.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .model_interfaces import (
    api_key_headers as built_in_api_key_headers,
    api_key_required,
    normalize_interface_preset,
    normalize_provider,
    protocol_for_interface,
)


DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_ALLOWED_AUDIO_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 100_000
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_FORM_FIELD_RE = re.compile(r"[0-9A-Za-z_.\-\[\]]{1,128}")
_LANGUAGE_RE = re.compile(r"[0-9A-Za-z_.-]{1,32}")

AudioInput = Union[bytes, bytearray, memoryview, str, os.PathLike]


class TranscriptionAPIError(RuntimeError):
    """Sanitized transcription error that is safe to display in the UI."""


# Backwards-friendly short name for callers that do not distinguish transports.
TranscriptionError = TranscriptionAPIError


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class TranscriptionConfig:
    """Configuration shared by cloud transcription backends."""

    provider: str = "openai_compatible"
    interface_preset: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = "whisper-1"
    language: str = "auto"
    timeout_seconds: int = 120
    retry_count: int = 2
    max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES
    custom_name: str = ""
    custom_api_key_header: str = "Authorization"
    custom_api_key_prefix: str = "Bearer "
    custom_extra_headers: str = "{}"
    custom_audio_field: str = "file"
    custom_model_field: str = "model"
    custom_language_field: str = "language"
    custom_extra_form_fields: str = "{}"
    custom_response_path: str = "text"
    custom_filename: str = "audio.wav"

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "TranscriptionConfig":
        raw: Mapping[str, Any] = {}
        if isinstance(settings, Mapping):
            candidate = settings.get("transcription", {})
            if isinstance(candidate, Mapping):
                raw = candidate
        provider = normalize_provider(
            "transcription", raw.get("provider") or "openai_compatible"
        )
        base_url = str(raw.get("base_url") or "").strip()
        default_model = "whisper-1"
        return cls(
            provider=provider,
            interface_preset=normalize_interface_preset(
                "transcription", raw.get("interface_preset"), provider, base_url
            ),
            base_url=base_url,
            api_key=str(raw.get("api_key") or "").strip(),
            model=str(raw.get("model") or default_model).strip(),
            language=str(raw.get("language") or "auto").strip().lower(),
            timeout_seconds=_bounded_int(raw.get("timeout_seconds"), 120, 5, 600),
            retry_count=_bounded_int(raw.get("retry_count"), 2, 0, 3),
            max_audio_bytes=_bounded_int(
                raw.get("max_audio_bytes"),
                DEFAULT_MAX_AUDIO_BYTES,
                1024,
                MAX_ALLOWED_AUDIO_BYTES,
            ),
            custom_name=str(raw.get("custom_name") or "").strip(),
            custom_api_key_header=str(
                raw.get("custom_api_key_header") or "Authorization"
            ).strip(),
            custom_api_key_prefix=str(
                raw.get("custom_api_key_prefix")
                if raw.get("custom_api_key_prefix") is not None
                else "Bearer "
            ),
            custom_extra_headers=str(raw.get("custom_extra_headers") or "{}"),
            custom_audio_field=str(raw.get("custom_audio_field") or "file").strip(),
            custom_model_field=str(
                raw.get("custom_model_field")
                if raw.get("custom_model_field") is not None
                else "model"
            ).strip(),
            custom_language_field=str(
                raw.get("custom_language_field")
                if raw.get("custom_language_field") is not None
                else "language"
            ).strip(),
            custom_extra_form_fields=str(
                raw.get("custom_extra_form_fields") or "{}"
            ),
            custom_response_path=str(
                raw.get("custom_response_path") or "text"
            ).strip(),
            custom_filename=str(raw.get("custom_filename") or "audio.wav").strip(),
        )

    @property
    def protocol(self) -> str:
        aliases = {
            "custom": "custom_multipart",
        }
        return aliases.get(self.provider, self.provider)

    @property
    def normalized_language(self) -> Optional[str]:
        return None if self.language in ("", "auto") else self.language

    def validate(self) -> None:
        if self.protocol not in (
            "openai_compatible",
            "custom_multipart",
            "dashscope_asr",
        ):
            raise TranscriptionAPIError("不支持的语音转写接口协议")
        if self.interface_preset:
            try:
                expected_provider = protocol_for_interface(
                    "transcription", self.interface_preset
                )
            except ValueError as exc:
                raise TranscriptionAPIError("不支持的语音转写接口预设") from exc
            if expected_provider != self.protocol:
                raise TranscriptionAPIError("语音转写接口预设与请求协议不匹配")
        if not self.model:
            raise TranscriptionAPIError("请填写语音转写模型名称")
        if len(self.model) > 256 or _contains_control_character(self.model):
            raise TranscriptionAPIError("语音转写模型名称无效")
        if self.language not in ("", "auto") and not _LANGUAGE_RE.fullmatch(
            self.language
        ):
            raise TranscriptionAPIError("语音语言代码无效")
        if not 5 <= self.timeout_seconds <= 600:
            raise TranscriptionAPIError("语音转写超时时间必须在 5 到 600 秒之间")
        if not 0 <= self.retry_count <= 3:
            raise TranscriptionAPIError("语音转写重试次数必须在 0 到 3 次之间")
        if not 1024 <= self.max_audio_bytes <= MAX_ALLOWED_AUDIO_BYTES:
            raise TranscriptionAPIError("语音上传大小限制无效")

        _validate_api_url(self.base_url)
        if self.requires_api_key and not self.api_key:
            raise TranscriptionAPIError("请填写语音转写 API Key")
        if len(self.api_key) > 16_384 or _contains_control_character(self.api_key):
            raise TranscriptionAPIError("语音转写 API Key 无效")

        if self.protocol == "custom_multipart":
            if not self.custom_name:
                raise TranscriptionAPIError("请填写自定义语音转写接口名称")
            _validate_header_name(self.custom_api_key_header)
            _validate_header_value(self.custom_api_key_prefix)
            _validate_form_field(self.custom_audio_field, required=True)
            _validate_form_field(self.custom_model_field, required=False)
            _validate_form_field(self.custom_language_field, required=False)
            _validate_filename(self.custom_filename)
            if not self.custom_response_path:
                raise TranscriptionAPIError("请填写自定义转写响应文本路径")
            _parse_extra_headers(
                self.custom_extra_headers,
                reserved_api_key_header=self.custom_api_key_header,
            )
            _parse_extra_form_fields(self.custom_extra_form_fields)

    @property
    def requires_api_key(self) -> bool:
        return api_key_required(
            "transcription", self.interface_preset, self.base_url
        )

    def api_key_headers(self) -> dict[str, str]:
        return built_in_api_key_headers(
            module="transcription",
            preset=self.interface_preset,
            base_url=self.base_url,
            api_key=self.api_key,
        )


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_api_url(value: str) -> None:
    if not value:
        raise TranscriptionAPIError("请填写语音转写 API 地址")
    try:
        parsed = urllib.parse.urlparse(value)
        # Accessing port also validates malformed/non-numeric ports.
        _ = parsed.port
    except ValueError as exc:
        raise TranscriptionAPIError("语音转写 API 地址无效") from exc
    if parsed.scheme not in ("http", "https") or not parsed.netloc or not parsed.hostname:
        raise TranscriptionAPIError("语音转写 API 地址必须是有效的 http/https 地址")
    if parsed.username or parsed.password:
        raise TranscriptionAPIError("语音转写 API 地址中不能包含用户名或密码")
    if parsed.fragment or _contains_control_character(value):
        raise TranscriptionAPIError("语音转写 API 地址无效")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise TranscriptionAPIError("远程语音转写接口必须使用 HTTPS，HTTP 仅允许本机回环地址")


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.lower().strip().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_header_name(value: str) -> None:
    if not value or len(value) > 128 or not _HEADER_NAME_RE.fullmatch(value):
        raise TranscriptionAPIError("自定义 API Key 请求头名称无效")
    if value.lower() in {"host", "content-length", "content-type", "transfer-encoding"}:
        raise TranscriptionAPIError("自定义 API Key 请求头名称无效")


def _validate_header_value(value: str) -> None:
    if len(value) > 4096 or "\r" in value or "\n" in value:
        raise TranscriptionAPIError("自定义请求头值无效")


def _validate_form_field(value: str, *, required: bool) -> None:
    if not value and not required:
        return
    if not value or not _FORM_FIELD_RE.fullmatch(value):
        raise TranscriptionAPIError("自定义 multipart 字段名称无效")


def _validate_filename(value: str) -> None:
    if (
        not value
        or len(value) > 128
        or value != Path(value).name
        or _contains_control_character(value)
        or '"' in value
    ):
        raise TranscriptionAPIError("自定义音频文件名无效")


def _parse_extra_headers(
    raw: str, *, reserved_api_key_header: str
) -> Dict[str, str]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise TranscriptionAPIError("自定义附加请求头必须是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise TranscriptionAPIError("自定义附加请求头必须是 JSON 对象")
    if len(parsed) > 64:
        raise TranscriptionAPIError("自定义附加请求头数量过多")
    forbidden = {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        reserved_api_key_header.lower(),
    }
    result: Dict[str, str] = {}
    for name, value in parsed.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            raise TranscriptionAPIError("自定义附加请求头包含无效字段")
        if name.lower() in forbidden or not isinstance(value, str):
            raise TranscriptionAPIError("自定义附加请求头包含无效字段")
        _validate_header_value(value)
        result[name] = value
    return result


def _parse_extra_form_fields(raw: str) -> Dict[str, str]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise TranscriptionAPIError("自定义 multipart 附加字段必须是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise TranscriptionAPIError("自定义 multipart 附加字段必须是 JSON 对象")
    if len(parsed) > 64:
        raise TranscriptionAPIError("自定义 multipart 附加字段数量过多")
    result: Dict[str, str] = {}
    total_size = 0
    for name, value in parsed.items():
        if not isinstance(name, str):
            raise TranscriptionAPIError("自定义 multipart 附加字段无效")
        _validate_form_field(name, required=True)
        if value is None or not isinstance(value, (str, int, float, bool)):
            raise TranscriptionAPIError("自定义 multipart 附加字段值无效")
        text = str(value)
        total_size += len(name.encode("utf-8")) + len(text.encode("utf-8"))
        if total_size > 64 * 1024:
            raise TranscriptionAPIError("自定义 multipart 附加字段过大")
        result[name] = text
    return result


def _load_wav(wav: AudioInput, max_bytes: int) -> bytes:
    if isinstance(wav, (bytes, bytearray, memoryview)):
        data = bytes(wav)
    elif isinstance(wav, (str, os.PathLike)):
        try:
            path = Path(wav)
            if path.stat().st_size > max_bytes:
                raise TranscriptionAPIError(
                    f"语音文件超过 {max_bytes // (1024 * 1024)} MB，已停止处理"
                )
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except TranscriptionAPIError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            # Never include the local path in an error returned to the browser.
            raise TranscriptionAPIError("无法读取待转写语音文件") from exc
    else:
        raise TranscriptionAPIError("待转写语音必须是 WAV 字节数据或本地文件路径")

    if not data:
        raise TranscriptionAPIError("待转写语音数据为空")
    if len(data) > max_bytes:
        raise TranscriptionAPIError(
            f"语音文件超过 {max_bytes // (1024 * 1024)} MB，已停止处理"
        )
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise TranscriptionAPIError("待转写音频不是有效的 WAV 数据")
    return data


def _clean_transcript(value: Any) -> str:
    if isinstance(value, list):
        value = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item or "")
            for item in value
        )
    transcript = str(value or "").strip()
    if not transcript:
        raise TranscriptionAPIError("语音转写模型没有返回文字")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise TranscriptionAPIError("语音转写结果过长，已停止处理")
    return transcript


def _read_json_path(payload: Any, path: str) -> Any:
    if path == "$":
        return payload
    value = payload
    for segment in path.split("."):
        if isinstance(value, list):
            try:
                value = value[int(segment)]
            except (ValueError, IndexError) as exc:
                raise TranscriptionAPIError("自定义响应文本路径与实际响应不匹配") from exc
        elif isinstance(value, dict) and segment in value:
            value = value[segment]
        else:
            raise TranscriptionAPIError("自定义响应文本路径与实际响应不匹配")
    return value


def _replace_placeholders(value: str, replacements: Mapping[str, str]) -> str:
    result = value
    for name, replacement in replacements.items():
        result = result.replace("{{" + name + "}}", replacement)
    return result


def _encode_multipart(
    fields: Mapping[str, str],
    *,
    audio_field: str,
    filename: str,
    audio: bytes,
) -> Tuple[bytes, str]:
    boundary = "----WechatAssistant" + secrets.token_hex(16)
    boundary_bytes = boundary.encode("ascii")
    chunks = []
    for name, value in fields.items():
        _validate_form_field(name, required=True)
        chunks.extend(
            (
                b"--" + boundary_bytes + b"\r\n",
                ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            b"--" + boundary_bytes + b"\r\n",
            (
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (audio_field, filename)
            ).encode("ascii"),
            b"Content-Type: audio/wav\r\n\r\n",
            audio,
            b"\r\n--" + boundary_bytes + b"--\r\n",
        )
    )
    return b"".join(chunks), boundary


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward model credentials to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class _MultipartTranscriptionClient:
    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self.config.validate()
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _post_multipart(
        self,
        endpoint: str,
        fields: Mapping[str, str],
        headers: Mapping[str, str],
        *,
        audio_field: str,
        filename: str,
        audio: bytes,
    ) -> Any:
        body, boundary = _encode_multipart(
            fields,
            audio_field=audio_field,
            filename=filename,
            audio=audio,
        )
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            **headers,
        }
        last_error: Optional[TranscriptionAPIError] = None
        attempts = self.config.retry_count + 1
        for attempt in range(attempts):
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers=request_headers,
                method="POST",
            )
            retryable = True
            try:
                with self._opener.open(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TranscriptionAPIError("语音转写 API 响应过大")
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    last_error = TranscriptionAPIError("语音转写 API 返回的不是有效 JSON")
                    if attempt >= attempts - 1:
                        raise last_error from exc
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                retryable = status in (408, 409, 425, 429) or status >= 500
                last_error = TranscriptionAPIError(f"语音转写 API 返回 HTTP {status}")
                try:
                    exc.close()
                except Exception:
                    pass
            except urllib.error.URLError:
                last_error = TranscriptionAPIError("无法连接语音转写 API")
            except TimeoutError:
                last_error = TranscriptionAPIError("语音转写 API 请求超时")
            except TranscriptionAPIError:
                raise
            except OSError:
                last_error = TranscriptionAPIError("无法连接语音转写 API")

            if not retryable or attempt >= attempts - 1:
                break
            time.sleep(min(1.5 * (attempt + 1), 4.5))

        if last_error is not None:
            raise last_error
        raise TranscriptionAPIError("语音转写 API 请求失败")


class OpenAICompatibleTranscriptionClient(_MultipartTranscriptionClient):
    """OpenAI-compatible ``audio/transcriptions`` multipart adapter."""

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/audio/transcriptions"):
            return base
        return base + "/audio/transcriptions"

    def transcribe(self, wav: AudioInput) -> str:
        audio = _load_wav(wav, self.config.max_audio_bytes)
        fields = {"model": self.config.model, "response_format": "json"}
        if self.config.normalized_language:
            fields["language"] = self.config.normalized_language
        payload = self._post_multipart(
            self.endpoint,
            fields,
            self.config.api_key_headers(),
            audio_field="file",
            filename="audio.wav",
            audio=audio,
        )
        try:
            text = payload["text"]
        except (KeyError, TypeError) as exc:
            raise TranscriptionAPIError("语音转写 API 返回了无法识别的响应格式") from exc
        return _clean_transcript(text)


class CustomMultipartTranscriptionClient(_MultipartTranscriptionClient):
    """Configurable multipart adapter for non-standard transcription APIs."""

    @property
    def endpoint(self) -> str:
        return self.config.base_url

    def transcribe(self, wav: AudioInput) -> str:
        audio = _load_wav(wav, self.config.max_audio_bytes)
        language = self.config.normalized_language or ""
        replacements = {
            "model": self.config.model,
            "language": language,
            "filename": self.config.custom_filename,
        }
        fields = {
            name: _replace_placeholders(value, replacements)
            for name, value in _parse_extra_form_fields(
                self.config.custom_extra_form_fields
            ).items()
        }
        if self.config.custom_model_field:
            fields[self.config.custom_model_field] = self.config.model
        if self.config.custom_language_field and language:
            fields[self.config.custom_language_field] = language

        headers = _parse_extra_headers(
            self.config.custom_extra_headers,
            reserved_api_key_header=self.config.custom_api_key_header,
        )
        headers[self.config.custom_api_key_header] = (
            self.config.custom_api_key_prefix + self.config.api_key
        )
        payload = self._post_multipart(
            self.endpoint,
            fields,
            headers,
            audio_field=self.config.custom_audio_field,
            filename=self.config.custom_filename,
            audio=audio,
        )
        return _clean_transcript(
            _read_json_path(payload, self.config.custom_response_path)
        )


class DashScopeASRClient:
    """DashScope Chat Completions adapter for qwen3-asr-flash.

    This provider sends a JSON request to the Chat Completions endpoint with
    the WAV audio embedded as a base64 data URL inside an ``input_audio``
    content block.  The transcript is read from ``choices[0].message.content``.
    """

    def __init__(self, config: TranscriptionConfig):
        self.config = config
        self.config.validate()
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def transcribe(self, wav: AudioInput) -> str:
        audio = _load_wav(wav, self.config.max_audio_bytes)
        b64 = base64.b64encode(audio).decode("ascii")
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:audio/wav;base64,{b64}",
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": "Bearer " + self.config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        last_error: Optional[TranscriptionAPIError] = None
        attempts = self.config.retry_count + 1
        for attempt in range(attempts):
            retryable = True
            try:
                with self._opener.open(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TranscriptionAPIError("语音转写 API 响应过大")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    last_error = TranscriptionAPIError("语音转写 API 返回的不是有效 JSON")
                    if attempt >= attempts - 1:
                        raise last_error from exc
                try:
                    text = payload["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise TranscriptionAPIError(
                        "语音转写 API 返回了无法识别的响应格式"
                    ) from exc
                return _clean_transcript(text)
            except urllib.error.HTTPError as exc:
                status = int(getattr(exc, "code", 0) or 0)
                retryable = status in (408, 409, 425, 429) or status >= 500
                last_error = TranscriptionAPIError(
                    f"语音转写 API 返回 HTTP {status}"
                )
                try:
                    exc.close()
                except Exception:
                    pass
            except urllib.error.URLError:
                last_error = TranscriptionAPIError("无法连接语音转写 API")
            except TimeoutError:
                last_error = TranscriptionAPIError("语音转写 API 请求超时")
            except TranscriptionAPIError:
                raise
            except OSError:
                last_error = TranscriptionAPIError("无法连接语音转写 API")

            if not retryable or attempt >= attempts - 1:
                break
            time.sleep(min(1.5 * (attempt + 1), 4.5))

        if last_error is not None:
            raise last_error
        raise TranscriptionAPIError("语音转写 API 请求失败")


def create_transcription_client(config: TranscriptionConfig):
    """Create exactly the backend selected by the user (without fallback)."""
    clients = {
        "openai_compatible": OpenAICompatibleTranscriptionClient,
        "custom_multipart": CustomMultipartTranscriptionClient,
        "dashscope_asr": DashScopeASRClient,
    }
    try:
        client_class = clients[config.protocol]
    except KeyError as exc:
        raise TranscriptionAPIError("不支持的语音转写接口协议") from exc
    return client_class(config)


def transcribe(wav: AudioInput, config: TranscriptionConfig) -> str:
    """Convenience entry point for one explicit provider configuration."""
    return create_transcription_client(config).transcribe(wav)
