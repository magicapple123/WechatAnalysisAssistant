"""Shared model-interface preset metadata and authentication helpers.

The persisted ``provider`` field continues to describe the wire protocol used by
the backend adapters.  ``interface_preset`` identifies the vendor/service preset
selected in Settings.  Keeping the two concepts separate lets many vendors reuse
the well-tested OpenAI-compatible adapters while still applying vendor-specific
authentication rules where required (notably Azure OpenAI).
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Mapping


INTERFACE_PROTOCOLS: dict[str, dict[str, str]] = {
    "vision": {
        "openai": "openai_compatible",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "azure_openai": "openai_compatible",
        "dashscope": "openai_compatible",
        "kimi": "openai_compatible",
        "zhipu": "openai_compatible",
        "volcengine": "openai_compatible",
        "tencent_tokenhub": "openai_compatible",
        "siliconflow": "openai_compatible",
        "openrouter": "openai_compatible",
        "ollama": "openai_compatible",
        "lm_studio": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "custom": "custom",
    },
    "transcription": {
        "openai": "openai_compatible",
        "azure_openai": "openai_compatible",
        "dashscope_asr": "dashscope_asr",
        "groq": "openai_compatible",
        "mistral": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "custom_multipart": "custom_multipart",
    },
    "analysis": {
        "openai": "openai_compatible",
        "anthropic": "anthropic",
        "gemini": "gemini",
        "azure_openai": "openai_compatible",
        "deepseek": "openai_compatible",
        "dashscope": "openai_compatible",
        "kimi": "openai_compatible",
        "zhipu": "openai_compatible",
        "volcengine": "openai_compatible",
        "baidu_qianfan": "openai_compatible",
        "tencent_tokenhub": "openai_compatible",
        "siliconflow": "openai_compatible",
        "openrouter": "openai_compatible",
        "groq": "openai_compatible",
        "ollama": "openai_compatible",
        "lm_studio": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "custom": "custom",
    },
}


INTERFACE_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "azure_openai": "Azure OpenAI / Microsoft Foundry",
    "deepseek": "DeepSeek",
    "dashscope": "阿里云百炼 / 通义千问",
    "dashscope_asr": "阿里云百炼 Qwen-ASR",
    "kimi": "Moonshot / Kimi",
    "zhipu": "智谱 BigModel / GLM",
    "volcengine": "火山方舟 / 豆包",
    "baidu_qianfan": "百度智能云千帆",
    "tencent_tokenhub": "腾讯云 TokenHub",
    "siliconflow": "硅基流动 SiliconFlow",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "mistral": "Mistral Voxtral",
    "ollama": "Ollama（本机）",
    "lm_studio": "LM Studio（本机）",
    "openai_compatible": "其他 OpenAI 兼容接口",
    "custom": "完全自定义 JSON 接口",
    "custom_multipart": "完全自定义 multipart 接口",
}


_PROVIDER_ALIASES: dict[str, dict[str, str]] = {
    "transcription": {
        "local": "openai_compatible",
        "local_faster_whisper": "openai_compatible",
        "custom": "custom_multipart",
    },
    "analysis": {
        "custom_json": "custom",
    },
}


def supported_interface_presets(module: str) -> frozenset[str]:
    return frozenset(INTERFACE_PROTOCOLS.get(module, {}))


def normalize_provider(module: str, provider: object) -> str:
    """Normalize legacy persisted provider names to the current wire protocol."""

    normalized = str(provider or "openai_compatible").strip().lower()
    return _PROVIDER_ALIASES.get(module, {}).get(normalized, normalized)


def supported_providers(module: str) -> frozenset[str]:
    """Return the wire protocols accepted for one settings module."""

    return frozenset(INTERFACE_PROTOCOLS.get(module, {}).values())


def infer_compatible_interface_preset(module: str, base_url: str) -> str:
    """Infer a known preset from a legacy OpenAI-compatible Base URL."""

    try:
        parsed = urllib.parse.urlsplit(str(base_url or ""))
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError:
        return ""

    candidate = ""
    if host == "api.openai.com":
        candidate = "openai"
    elif host.endswith((
        ".openai.azure.com",
        ".services.ai.azure.com",
        ".cognitiveservices.azure.com",
    )):
        candidate = "azure_openai"
    elif host == "dashscope.aliyuncs.com":
        # The dedicated transcription preset uses a different wire protocol
        # from legacy OpenAI-compatible multipart settings, so URL inference
        # must not silently cross that protocol boundary.
        candidate = "" if module == "transcription" else "dashscope"
    elif host in {"api.moonshot.cn", "api.moonshot.ai"}:
        candidate = "kimi"
    elif host == "open.bigmodel.cn":
        candidate = "zhipu"
    elif host.endswith(".volces.com"):
        candidate = "volcengine"
    elif host == "qianfan.baidubce.com":
        candidate = "baidu_qianfan"
    elif host == "tokenhub.tencentmaas.com":
        candidate = "tencent_tokenhub"
    elif host in {"api.siliconflow.cn", "api.siliconflow.com"}:
        candidate = "siliconflow"
    elif host == "openrouter.ai":
        candidate = "openrouter"
    elif host == "api.groq.com":
        candidate = "groq"
    elif host == "api.mistral.ai":
        candidate = "mistral"
    elif host == "api.deepseek.com":
        candidate = "deepseek"
    elif is_loopback_url(base_url) and port == 11434:
        candidate = "ollama"
    elif is_loopback_url(base_url) and port == 1234:
        candidate = "lm_studio"

    return candidate if candidate in INTERFACE_PROTOCOLS.get(module, {}) else ""


def default_interface_preset(module: str, provider: str, base_url: str = "") -> str:
    """Derive a preset for settings written before presets were introduced."""

    normalized_provider = normalize_provider(module, provider)
    legacy = {
        "vision": {
            "anthropic": "anthropic",
            "gemini": "gemini",
            "custom": "custom",
        },
        "transcription": {
            "dashscope_asr": "dashscope_asr",
            "custom": "custom_multipart",
            "custom_multipart": "custom_multipart",
        },
        "analysis": {
            "anthropic": "anthropic",
            "gemini": "gemini",
            "custom": "custom",
            "custom_json": "custom",
        },
    }
    native = legacy.get(module, {}).get(normalized_provider)
    if native:
        return native
    if normalized_provider == "openai_compatible":
        return infer_compatible_interface_preset(module, base_url) or "openai_compatible"
    return "openai"


def normalize_interface_preset(
    module: str,
    value: object,
    provider: str,
    base_url: str = "",
) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in INTERFACE_PROTOCOLS.get(module, {}):
        return candidate
    return default_interface_preset(module, provider, base_url)


def protocol_for_interface(module: str, preset: str) -> str:
    try:
        return INTERFACE_PROTOCOLS[module][preset]
    except KeyError as exc:
        raise ValueError("unsupported model interface preset") from exc


def interface_label(preset: str, fallback: str = "") -> str:
    return INTERFACE_LABELS.get(preset, fallback or preset or "未知接口")


def is_loopback_url(base_url: str) -> bool:
    """Return whether an HTTP(S) URL resolves syntactically to loopback."""

    try:
        parsed = urllib.parse.urlsplit(str(base_url or ""))
        hostname = (parsed.hostname or "").strip().lower()
    except ValueError:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def api_key_required(module: str, preset: str, base_url: str) -> bool:
    """Allow local model servers to omit credentials only on loopback."""

    return not (
        preset in {"ollama", "lm_studio"}
        and module in {"vision", "analysis"}
        and is_loopback_url(base_url)
    )


def api_key_headers(
    *, module: str, preset: str, base_url: str, api_key: str
) -> dict[str, str]:
    """Return the API-key headers for built-in OpenAI-compatible presets."""

    key = str(api_key or "")
    if not key:
        return {}
    if preset == "azure_openai":
        return {"api-key": key}
    return {"Authorization": "Bearer " + key}


def prefers_max_completion_tokens(preset: str) -> bool:
    """Use OpenAI's newer limit field only where it is natively supported."""

    normalized = str(preset or "").strip().lower()
    # An empty preset represents a directly constructed legacy config. Keep
    # its historical OpenAI-oriented behavior for backwards compatibility.
    return not normalized or normalized in {"openai", "azure_openai"}


def public_interface_metadata(module: str, settings: Mapping[str, object]) -> dict:
    provider = normalize_provider(
        module, settings.get("provider") or "openai_compatible"
    )
    base_url = str(settings.get("base_url") or "")
    preset = normalize_interface_preset(
        module, settings.get("interface_preset"), provider, base_url
    )
    custom_name = str(settings.get("custom_name") or "").strip()
    label = (
        custom_name
        if preset in {"custom", "custom_multipart"} and custom_name
        else interface_label(preset, provider)
    )
    return {
        "provider": provider,
        "interface_preset": preset,
        "interface_label": label,
        "requires_api_key": api_key_required(module, preset, base_url),
    }
