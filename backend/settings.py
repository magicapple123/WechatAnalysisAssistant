"""
微信解析助手 - 应用设置管理

存储用户偏好设置，如导出目录等。
设置文件: %LOCALAPPDATA%/WechatAnalysisAssistant/settings.json
"""
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Optional

from .vision import DEFAULT_CUSTOM_REQUEST_TEMPLATE, DEFAULT_VISION_PROMPT
from .ai_analysis import (
    DEFAULT_CUSTOM_REQUEST_TEMPLATE as DEFAULT_ANALYSIS_REQUEST_TEMPLATE,
)
from .model_interfaces import public_interface_metadata
from .app_paths import (
    app_data_path,
    atomic_write_text,
    migrate_legacy_json_file,
    source_root,
)
from .version import APP_VERSION

DEFAULT_SETTINGS_FILE = app_data_path("settings.json")
SETTINGS_FILE = DEFAULT_SETTINGS_FILE
LEGACY_SETTINGS_FILES = (source_root() / "backend" / "settings.json",)


def _ensure_settings_storage_migrated() -> None:
    """Copy checkout-era settings to the per-user data directory once."""

    # Tests and embedders patch SETTINGS_FILE deliberately.  Never populate a
    # caller-owned path with data from the developer checkout.
    if Path(SETTINGS_FILE) != DEFAULT_SETTINGS_FILE:
        return
    migrate_legacy_json_file(SETTINGS_FILE, LEGACY_SETTINGS_FILES)

# 默认设置
DEFAULTS = {
    "export": {
        "directory": str(Path(os.environ.get("USERPROFILE", "")) / "Downloads"),
    },
    "about": {
        "project_name": "微信解析助手",
        "version": APP_VERSION,
        "description": "本地优先的微信 4.x 聊天记录与朋友圈解析、预览、导出和 AI 分析工具。",
        "tutorial_url": "",
        "qq_group": "",
        "author": "",
        "contact": "",
    },
    "ui": {
        "show_chat_images": False,
        "image_quality": "smart",
        "hd_automation_timeout_seconds": 10,
        "hd_automation_min_dwell_seconds": 0.5,
    },
    "vision": {
        "provider": "openai_compatible",
        "custom_name": "",
        "custom_protocol": "openai_compatible",
        "custom_api_key_header": "Authorization",
        "custom_api_key_prefix": "Bearer ",
        "custom_extra_headers": "{}",
        "custom_request_template": DEFAULT_CUSTOM_REQUEST_TEMPLATE,
        "custom_response_path": "choices.0.message.content",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-5.4-mini",
        "image_detail": "low",
        "timeout_seconds": 60,
        "max_images_per_task": 50,
        "prompt": DEFAULT_VISION_PROMPT,
    },
    "transcription": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "whisper-1",
        "language": "auto",
        "timeout_seconds": 120,
        "retry_count": 2,
        "max_audio_bytes": 26214400,
        "max_voices_per_task": 50,
        "custom_name": "",
        "custom_api_key_header": "Authorization",
        "custom_api_key_prefix": "Bearer ",
        "custom_extra_headers": "{}",
        "custom_audio_field": "file",
        "custom_model_field": "model",
        "custom_language_field": "language",
        "custom_extra_form_fields": "{}",
        "custom_response_path": "text",
        "custom_filename": "audio.wav",
    },
    "analysis": {
        "provider": "openai_compatible",
        "custom_name": "",
        "custom_protocol": "openai_compatible",
        "custom_api_key_header": "Authorization",
        "custom_api_key_prefix": "Bearer ",
        "custom_extra_headers": "{}",
        "custom_request_template": DEFAULT_ANALYSIS_REQUEST_TEMPLATE,
        "custom_response_path": "choices.0.message.content",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-5.4-mini",
        "timeout_seconds": 90,
        "retry_count": 2,
        "max_output_tokens": 2400,
        "temperature": 0.2,
        "chunk_chars": 0,
        "presets": [
            {
                "id": "quick-summary",
                "name": "快速梳理",
                "strength": "quick",
                "detail": "brief",
                "requirements": "快速概括主要话题、关键结论和需要跟进的事项。",
            },
            {
                "id": "balanced-review",
                "name": "标准分析",
                "strength": "balanced",
                "detail": "standard",
                "requirements": "按时间和主题梳理对话，识别重要观点、情绪变化、共识与分歧。",
            },
            {
                "id": "deep-insight",
                "name": "深度洞察",
                "strength": "deep",
                "detail": "detailed",
                "requirements": "深入分析关系、语境、潜在动机和长期趋势，并用原始内容中的证据支撑结论。",
            },
        ],
    },
    # V2 image keys differ from the database key and are account-specific.
    "images": {
        "accounts": {},
    },
    "advanced": {},
}


def _get_default_export_dir() -> str:
    """获取默认导出目录（Windows 下载文件夹，适配中英文系统）"""
    raw_userprofile = os.environ.get("USERPROFILE", "")
    userprofile_path = Path(os.path.expandvars(raw_userprofile)).expanduser()
    if not raw_userprofile or not userprofile_path.is_absolute():
        userprofile_path = Path.home().resolve()

    # 1. 通过 Windows 注册表获取真实的 Downloads 路径（不依赖语言）
    if os.name == "nt":
        try:
            import winreg
            # KNOWNFOLDERID for Downloads: {374DE290-123F-4565-9164-39C4925E467B}
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            raw, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
            winreg.CloseKey(key)
            expanded = os.path.expandvars(raw)
            candidate = Path(expanded).expanduser()
            if candidate.is_absolute() and candidate.exists():
                return str(candidate.resolve())
        except Exception:
            pass

    # 2. 常见路径尝试（中/英文）
    for name in ["Downloads", "下载"]:
        downloads = userprofile_path / name
        if downloads.exists():
            return str(downloads.resolve())

    # 3. 回退：主动创建 Downloads 目录
    downloads = (userprofile_path / "Downloads").resolve()
    downloads.mkdir(parents=True, exist_ok=True)
    return str(downloads)


# 动态更新默认值
DEFAULTS["export"]["directory"] = _get_default_export_dir()


def load_settings() -> dict:
    """Load all settings after completing any legacy-path migration."""
    _ensure_settings_storage_migrated()
    settings = _deep_merge(DEFAULTS, {})
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            settings = _deep_merge(settings, data)
        except Exception:
            pass
    return settings


def save_settings(data: dict) -> dict:
    """保存设置（合并写入），返回完整的合并后设置"""
    current = load_settings()
    merged = _deep_merge(current, data)
    atomic_write_text(
        SETTINGS_FILE,
        json.dumps(merged, indent=2, ensure_ascii=False),
    )
    return merged


def get_account_image_settings(settings: dict, account_id: Optional[str]) -> dict:
    """Return the private V2 image-key settings for one WeChat account."""
    default = {"aes_key": "", "xor_key": "auto"}
    if not account_id:
        return default
    accounts = settings.get("images", {}).get("accounts", {})
    account = accounts.get(account_id, {}) if isinstance(accounts, dict) else {}
    return _deep_merge(default, account if isinstance(account, dict) else {})


def public_settings(settings: dict, account_id: Optional[str] = None) -> dict:
    """Remove stored secrets before settings are returned to the browser."""
    result = deepcopy(settings)
    vision = result.setdefault("vision", {})
    api_key = str(vision.pop("api_key", "") or "")
    vision["has_api_key"] = bool(api_key)
    vision["api_key_hint"] = f"••••{api_key[-4:]}" if api_key else ""
    vision.update(public_interface_metadata("vision", vision))

    transcription = result.setdefault("transcription", {})
    transcription_api_key = str(transcription.pop("api_key", "") or "")
    transcription["has_api_key"] = bool(transcription_api_key)
    transcription["api_key_hint"] = (
        f"••••{transcription_api_key[-4:]}" if transcription_api_key else ""
    )
    transcription.update(
        public_interface_metadata("transcription", transcription)
    )
    transcription["is_cloud"] = True

    analysis = result.setdefault("analysis", {})
    analysis_api_key = str(analysis.pop("api_key", "") or "")
    analysis["has_api_key"] = bool(analysis_api_key)
    analysis["api_key_hint"] = (
        f"••••{analysis_api_key[-4:]}" if analysis_api_key else ""
    )
    analysis.update(public_interface_metadata("analysis", analysis))

    private_image = get_account_image_settings(settings, account_id)
    result["images"] = {
        "current_account": account_id or "",
        "has_aes_key": bool(private_image.get("aes_key")),
        "xor_key": private_image.get("xor_key", "auto"),
    }
    return result


def get_export_dir() -> Path:
    """获取当前配置的导出目录，确保目录存在"""
    settings = load_settings()
    directory = settings.get("export", {}).get("directory", DEFAULTS["export"]["directory"])
    expanded = Path(os.path.expandvars(str(directory or ""))).expanduser()
    if not expanded.is_absolute():
        expanded = Path(DEFAULTS["export"]["directory"])
    path = expanded.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base"""
    result = {}
    for key in set(list(base.keys()) + list(override.keys())):
        if key in override:
            if isinstance(override[key], dict) and isinstance(base.get(key), dict):
                result[key] = _deep_merge(base[key], override[key])
            else:
                result[key] = override[key]
        else:
            result[key] = base[key]
    return result
