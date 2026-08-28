"""Multi-provider vision clients used only after an explicit user action."""

from __future__ import annotations

import base64
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .model_interfaces import (
    api_key_headers as built_in_api_key_headers,
    api_key_required,
    is_loopback_url,
    normalize_interface_preset,
    normalize_provider,
    prefers_max_completion_tokens,
    protocol_for_interface,
)


DEFAULT_VISION_PROMPT = (
    "请用一到两句简洁、客观的中文描述这张聊天图片的主要内容。"
    "如果图片包含对理解聊天有帮助的清晰文字，请概括关键文字；"
    "不要猜测看不清的人名、地点或隐私信息，不要添加前后缀。"
)

DEFAULT_CUSTOM_REQUEST_TEMPLATE = json.dumps(
    {
        "model": "{{model}}",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "{{prompt}}"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "{{image_data_url}}", "detail": "{{detail}}"},
                    },
                ],
            }
        ],
    },
    ensure_ascii=False,
    indent=2,
)


class VisionAPIError(RuntimeError):
    """A sanitized error that is safe to show in the local UI."""


@dataclass(frozen=True)
class VisionConfig:
    base_url: str
    api_key: str
    model: str
    provider: str = "openai_compatible"
    interface_preset: str = ""
    custom_name: str = ""
    custom_protocol: str = "openai_compatible"
    custom_api_key_header: str = "Authorization"
    custom_api_key_prefix: str = "Bearer "
    custom_extra_headers: str = "{}"
    custom_request_template: str = DEFAULT_CUSTOM_REQUEST_TEMPLATE
    custom_response_path: str = "choices.0.message.content"
    detail: str = "low"
    timeout_seconds: int = 60
    prompt: str = DEFAULT_VISION_PROMPT

    @classmethod
    def from_settings(cls, settings: dict) -> "VisionConfig":
        raw = settings.get("vision", {}) if isinstance(settings, dict) else {}
        provider = normalize_provider(
            "vision", raw.get("provider") or "openai_compatible"
        )
        base_url = str(raw.get("base_url") or "").strip()
        return cls(
            base_url=base_url,
            api_key=str(raw.get("api_key") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            provider=provider,
            interface_preset=normalize_interface_preset(
                "vision", raw.get("interface_preset"), provider, base_url
            ),
            custom_name=str(raw.get("custom_name") or "").strip(),
            custom_protocol=str(
                raw.get("custom_protocol") or "openai_compatible"
            ).strip().lower(),
            custom_api_key_header=str(
                raw.get("custom_api_key_header") or "Authorization"
            ).strip(),
            custom_api_key_prefix=str(
                raw.get("custom_api_key_prefix")
                if raw.get("custom_api_key_prefix") is not None
                else "Bearer "
            ),
            custom_extra_headers=str(raw.get("custom_extra_headers") or "{}"),
            custom_request_template=str(
                raw.get("custom_request_template") or DEFAULT_CUSTOM_REQUEST_TEMPLATE
            ),
            custom_response_path=str(
                raw.get("custom_response_path") or "choices.0.message.content"
            ).strip(),
            detail=str(raw.get("image_detail") or "low").strip().lower(),
            timeout_seconds=max(5, min(300, int(raw.get("timeout_seconds") or 60))),
            prompt=str(raw.get("prompt") or DEFAULT_VISION_PROMPT).strip(),
        )

    def validate(self) -> None:
        if self.provider not in (
            "openai_compatible", "anthropic", "gemini", "custom"
        ):
            raise VisionAPIError("不支持的图片识别接口协议")
        if self.interface_preset:
            try:
                expected_provider = protocol_for_interface(
                    "vision", self.interface_preset
                )
            except ValueError as exc:
                raise VisionAPIError("不支持的图片识别接口预设") from exc
            if expected_provider != self.provider:
                raise VisionAPIError("图片识别接口预设与请求协议不匹配")
        if self.provider == "custom":
            if not self.custom_name:
                raise VisionAPIError("请填写自定义接口名称")
            if self.custom_protocol not in (
                "openai_compatible", "anthropic", "gemini", "custom_json"
            ):
                raise VisionAPIError("不支持的自定义接口请求协议")
            if self.custom_protocol == "custom_json":
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.custom_api_key_header):
                    raise VisionAPIError("自定义 API Key 请求头名称无效")
                try:
                    extra_headers = json.loads(self.custom_extra_headers)
                except json.JSONDecodeError as exc:
                    raise VisionAPIError("自定义附加请求头必须是有效 JSON") from exc
                if not isinstance(extra_headers, dict):
                    raise VisionAPIError("自定义附加请求头必须是 JSON 对象")
                forbidden_headers = {"host", "content-length", "transfer-encoding"}
                for header_name, header_value in extra_headers.items():
                    if (
                        not isinstance(header_name, str)
                        or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header_name)
                        or header_name.lower() in forbidden_headers
                        or not isinstance(header_value, str)
                    ):
                        raise VisionAPIError("自定义附加请求头包含无效字段")
                try:
                    template = json.loads(self.custom_request_template)
                except json.JSONDecodeError as exc:
                    raise VisionAPIError("自定义请求模板必须是有效 JSON") from exc
                if not isinstance(template, (dict, list)):
                    raise VisionAPIError("自定义请求模板必须是 JSON 对象或数组")
                if not self.custom_response_path:
                    raise VisionAPIError("请填写自定义响应文本路径")
        if not self.base_url:
            raise VisionAPIError("请先在设置中填写视觉模型 API Base URL")
        try:
            parsed = urllib.parse.urlparse(self.base_url)
            # Accessing ``port`` also rejects malformed/non-numeric ports.
            _ = parsed.port
        except ValueError as exc:
            raise VisionAPIError("视觉模型 API Base URL 无效") from exc
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise VisionAPIError("视觉模型 API Base URL 必须是有效的 http/https 地址")
        if parsed.username or parsed.password:
            raise VisionAPIError("API Base URL 中不能包含用户名或密码")
        if parsed.scheme == "http" and not is_loopback_url(self.base_url):
            raise VisionAPIError(
                "远程视觉模型接口必须使用 HTTPS；HTTP 仅允许本机回环地址"
            )
        if not self.model:
            raise VisionAPIError("请先在设置中填写支持图片输入的模型名称")
        if self.requires_api_key and not self.api_key:
            raise VisionAPIError("请先在设置中填写视觉模型 API Key")
        if self.detail not in ("low", "auto", "high"):
            raise VisionAPIError("图片识别精度必须是 low、auto 或 high")

    @property
    def protocol(self) -> str:
        return self.custom_protocol if self.provider == "custom" else self.provider

    @property
    def requires_api_key(self) -> bool:
        return api_key_required(
            "vision", self.interface_preset, self.base_url
        )

    def api_key_headers(self) -> dict[str, str]:
        return built_in_api_key_headers(
            module="vision",
            preset=self.interface_preset,
            base_url=self.base_url,
            api_key=self.api_key,
        )

    @property
    def sends_openai_detail_hint(self) -> bool:
        return self.provider in {"openai_compatible", "custom"} and (
            not self.interface_preset or self.interface_preset in {
                "openai",
                "azure_openai",
                "openrouter",
                "openai_compatible",
            }
        )

    @property
    def uses_modern_openai_token_limit(self) -> bool:
        return prefers_max_completion_tokens(self.interface_preset)


def _extract_error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
        error = data.get("error", {})
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    except Exception:
        pass
    cleaned = " ".join(raw.split())
    return cleaned[:300] or "请求失败"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward model credentials to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class _JSONVisionClient:
    """Shared bounded JSON POST transport for non-OpenAI protocols."""

    def __init__(self, config: VisionConfig):
        self.config = config
        self.config.validate()
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def _post_json(self, endpoint: str, payload: dict, headers: dict) -> dict:
        last_error: Optional[Exception] = None
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        }
        for attempt in range(3):
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=request_headers,
                method="POST",
            )
            try:
                with self._opener.open(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read(2 * 1024 * 1024)
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw_error = exc.read(64 * 1024).decode("utf-8", errors="replace")
                message = _extract_error_message(raw_error)
                last_error = VisionAPIError(
                    f"视觉模型 API 返回 {exc.code}: {message}"
                )
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    break
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                last_error = VisionAPIError(
                    f"无法连接视觉模型 API: {reason or '网络错误'}"
                )
            except TimeoutError:
                last_error = VisionAPIError("视觉模型 API 请求超时")
            except (json.JSONDecodeError, UnicodeDecodeError):
                last_error = VisionAPIError("视觉模型 API 返回的不是有效 JSON")
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

        if isinstance(last_error, VisionAPIError):
            raise last_error
        raise VisionAPIError("视觉模型 API 请求失败")


def normalize_image_for_vision(
    data: bytes,
    *,
    max_edge: int = 2048,
    max_bytes: int = 12 * 1024 * 1024,
) -> tuple[bytes, str]:
    """Normalize a local image to a bounded JPEG/PNG payload.

    EXIF metadata is intentionally discarded before the image leaves the
    machine. Animated images are reduced to the first frame.
    """
    if not data:
        raise VisionAPIError("图片数据为空")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise VisionAPIError("缺少 Pillow，无法安全处理待识别图片") from exc

    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_edge, max_edge))

            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            output = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime = "image/png"
            else:
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=85,
                    optimize=True,
                )
                mime = "image/jpeg"
            normalized = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise VisionAPIError("图片格式无法转换为视觉模型支持的格式") from exc

    if len(normalized) > max_bytes:
        raise VisionAPIError(
            f"图片处理后仍超过 {max_bytes // (1024 * 1024)} MB，已跳过上传"
        )
    return normalized, mime


class OpenAICompatibleVisionClient:
    """Minimal Chat Completions client with no provider SDK dependency."""

    def __init__(self, config: VisionConfig):
        self.config = config
        self.config.validate()
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def describe_image(self, data: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        image_url = {"url": f"data:{mime_type};base64,{encoded}"}
        if self.config.sends_openai_detail_hint:
            image_url["detail"] = self.config.detail
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是聊天记录图片描述器。只描述图片中可见的内容，"
                        "输出简短中文，不推断敏感身份信息。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.config.prompt},
                        {
                            "type": "image_url",
                            "image_url": image_url,
                        },
                    ],
                },
            ],
        }
        response = self._post_with_retries(payload, token_limit=220)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionAPIError("视觉模型返回了无法识别的响应格式") from exc

        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        description = str(content or "").strip()
        if not description:
            raise VisionAPIError("视觉模型没有返回图片描述")
        return description[:2000]

    def _post_with_retries(self, payload: dict, *, token_limit: int) -> dict:
        last_error: Optional[Exception] = None
        use_modern_limit = self.config.uses_modern_openai_token_limit
        changed_limit_field = False
        for attempt in range(3):
            body_payload = dict(payload)
            limit_field = (
                "max_completion_tokens" if use_modern_limit else "max_tokens"
            )
            body_payload[limit_field] = token_limit
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(body_payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    **self.config.api_key_headers(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with self._opener.open(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    raw = response.read(2 * 1024 * 1024)
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw_error = exc.read(64 * 1024).decode("utf-8", errors="replace")
                if (
                    exc.code in (400, 422)
                    and not changed_limit_field
                    and limit_field in raw_error
                ):
                    use_modern_limit = not use_modern_limit
                    changed_limit_field = True
                    continue
                message = _extract_error_message(raw_error)
                last_error = VisionAPIError(f"视觉模型 API 返回 {exc.code}: {message}")
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    break
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                last_error = VisionAPIError(f"无法连接视觉模型 API: {reason or '网络错误'}")
            except TimeoutError:
                last_error = VisionAPIError("视觉模型 API 请求超时")
            except (json.JSONDecodeError, UnicodeDecodeError):
                last_error = VisionAPIError("视觉模型 API 返回的不是有效 JSON")

            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

        if isinstance(last_error, VisionAPIError):
            raise last_error
        raise VisionAPIError("视觉模型 API 请求失败")



class AnthropicVisionClient(_JSONVisionClient):
    """Anthropic Messages API adapter with base64 image content blocks."""

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/messages") else f"{base}/messages"

    def describe_image(self, data: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.config.model,
            "max_tokens": 220,
            "system": (
                "你是聊天记录图片描述器。只描述图片中可见的内容，"
                "输出简短中文，不推断敏感身份信息。"
            ),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": self.config.prompt},
                    ],
                }
            ],
        }
        response = self._post_json(
            self.endpoint,
            payload,
            {
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            blocks = response["content"]
            description = "".join(
                str(block.get("text") or "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        except (KeyError, TypeError) as exc:
            raise VisionAPIError("Anthropic 返回了无法识别的响应格式") from exc
        if not description:
            raise VisionAPIError("视觉模型没有返回图片描述")
        return description[:2000]


class GeminiVisionClient(_JSONVisionClient):
    """Google Gemini generateContent API adapter."""

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith(":generateContent"):
            return base
        model = self.config.model
        if model.startswith("models/"):
            model = model[len("models/"):]
        encoded_model = urllib.parse.quote(model, safe="-._~")
        if base.endswith("/models"):
            return f"{base}/{encoded_model}:generateContent"
        return f"{base}/models/{encoded_model}:generateContent"

    def describe_image(self, data: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": encoded,
                            }
                        },
                        {"text": self.config.prompt},
                    ],
                }
            ],
            "generationConfig": {"maxOutputTokens": 220},
        }
        response = self._post_json(
            self.endpoint,
            payload,
            {"x-goog-api-key": self.config.api_key},
        )
        try:
            parts = response["candidates"][0]["content"]["parts"]
            description = "".join(
                str(part.get("text") or "")
                for part in parts
                if isinstance(part, dict)
            ).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionAPIError("Gemini 返回了无法识别的响应格式") from exc
        if not description:
            raise VisionAPIError("视觉模型没有返回图片描述")
        return description[:2000]


def _replace_template_placeholders(value, replacements: dict[str, str]):
    if isinstance(value, dict):
        return {
            key: _replace_template_placeholders(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_template_placeholders(item, replacements) for item in value
        ]
    if isinstance(value, str):
        result = value
        for placeholder, replacement in replacements.items():
            result = result.replace("{{" + placeholder + "}}", replacement)
        return result
    return value


def _read_json_path(payload, path: str):
    value = payload
    for segment in path.split("."):
        if isinstance(value, list):
            try:
                value = value[int(segment)]
            except (ValueError, IndexError) as exc:
                raise VisionAPIError("自定义响应文本路径与实际响应不匹配") from exc
        elif isinstance(value, dict) and segment in value:
            value = value[segment]
        else:
            raise VisionAPIError("自定义响应文本路径与实际响应不匹配")
    return value


class CustomJSONVisionClient(_JSONVisionClient):
    """Template-driven adapter for APIs that use a different JSON schema."""

    @property
    def endpoint(self) -> str:
        return self.config.base_url

    def describe_image(self, data: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        try:
            template = json.loads(self.config.custom_request_template)
        except json.JSONDecodeError as exc:
            raise VisionAPIError("自定义请求模板必须是有效 JSON") from exc
        payload = _replace_template_placeholders(
            template,
            {
                "model": self.config.model,
                "prompt": self.config.prompt,
                "image_base64": encoded,
                "image_data_url": f"data:{mime_type};base64,{encoded}",
                "mime_type": mime_type,
                "detail": self.config.detail,
            },
        )
        headers = json.loads(self.config.custom_extra_headers)
        headers[self.config.custom_api_key_header] = (
            f"{self.config.custom_api_key_prefix}{self.config.api_key}"
        )
        response = self._post_json(self.endpoint, payload, headers)
        content = _read_json_path(response, self.config.custom_response_path)
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict)
            )
        description = str(content or "").strip()
        if not description:
            raise VisionAPIError("视觉模型没有返回图片描述")
        return description[:2000]


def create_vision_client(config: VisionConfig):
    """Create the adapter selected by the user's saved protocol setting."""
    clients = {
        "openai_compatible": OpenAICompatibleVisionClient,
        "anthropic": AnthropicVisionClient,
        "gemini": GeminiVisionClient,
        "custom_json": CustomJSONVisionClient,
    }
    try:
        client_class = clients[config.protocol]
    except KeyError as exc:
        raise VisionAPIError("不支持的图片识别接口协议") from exc
    return client_class(config)
