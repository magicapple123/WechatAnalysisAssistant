"""
微信解析助手 - REST API

FastAPI 应用，提供:
- 状态查询
- 密钥管理
- 聊天列表
- 消息查询
- 导出功能
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import tempfile
import threading
import re
import secrets
import sys
from pathlib import Path
from typing import Callable, Optional
from datetime import datetime
from urllib.parse import quote as urlquote, urlencode, urlsplit

from fastapi import FastAPI, Header, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import config, is_smoke_test_environment
from .key_extractor import (
    delete_key as delete_saved_key,
    find_key_auto,
    load_key,
    save_key,
    validate_key,
)
from .decrypt import DatabaseDecryptor, test_key, get_sqlcipher_status
from .parser_v4 import MessageParserV4
from .exporter import ChatExporter, _safe_filename
from .settings import (
    get_account_image_settings,
    get_export_dir,
    load_settings,
    public_settings,
    save_settings,
)
from .image_recognition import ImageRecognitionManager
from .image_key_extractor import (
    ImageKeyExtractionError,
    extract_image_aes_key,
)
from .image_service import ImageResolutionError, WeChatImageService
from .image_hd_automation import ImageHDAutomationManager
from .image_hd_resource import (
    ImageHDResourceError,
    WeChatHDResourceMonitor,
)
from .image_store import ImageDescriptionStore
from .vision import VisionAPIError, VisionConfig
from .transcription import TranscriptionAPIError, TranscriptionConfig
from .ai_analysis import (
    AIAnalysisCancelled,
    AIAnalysisDeadlineExceeded,
    AIAnalysisError,
    AnalysisConfig,
    MAX_CUSTOM_TEMPLATE_BYTES,
    MAX_EXTRA_HEADERS_BYTES,
    analyze_records,
)
from .model_interfaces import (
    normalize_interface_preset,
    normalize_provider,
    protocol_for_interface,
    supported_interface_presets,
    supported_providers,
)
from .voice_service import VoiceServiceError, WeChatVoiceService
from .voice_store import VoiceTranscriptionStore
from .voice_transcription import VoiceTranscriptionManager
from .subprocess_env import sanitized_subprocess_env
from .moments import (
    DownloadedMedia,
    MomentsError,
    MomentsExporter,
    MomentsMediaDownloadError,
    MomentsSchemaError,
    MomentsSelectionError,
    MomentsService,
    SafeMomentsMediaDownloader,
    moments_media_download_candidates,
)
from .moments_media import (
    MomentsMediaError,
    MomentsMediaNotFound,
    MomentsMediaResolver,
    MomentsMediaValidationError,
)
from .avatar_service import (
    AvatarNotFound,
    AvatarService,
    AvatarValidationError,
)
from .sticker_service import StickerResolutionError, StickerService
from .app_paths import resource_path
from .version import APP_VERSION, DESKTOP_API_VERSION

# --- FastAPI App ---

app = FastAPI(
    title="微信解析助手",
    description="微信解析助手 - 本地微信聊天记录解密、浏览与导出工具",
    version=APP_VERSION,
)

# 允许跨域 (前端开发用)
TRUSTED_BROWSER_ORIGINS = frozenset({
    "http://127.0.0.1:8520",
    "http://localhost:8520",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "app://wechat-assistant",
    "app://wechat-analysis-assistant",
})
SMOKE_TEST_API_ALLOWLIST = frozenset({
    "/api/desktop/health",
    "/api/desktop/self-test",
    "/api/desktop/prepare-exit",
})

app.add_middleware(
    CORSMiddleware,
    # The API can decrypt local chat images and trigger an explicit upload.
    # Do not let arbitrary websites drive the localhost service through CORS.
    allow_origins=sorted(TRUSTED_BROWSER_ORIGINS),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DESKTOP_SESSION_TOKEN_ENV = "WECHAT_ASSISTANT_DESKTOP_TOKEN"
DESKTOP_SESSION_TOKEN_HEADER = b"x-desktop-session-token"
_desktop_shutdown_requested = threading.Event()
_desktop_shutdown_callback: Optional[Callable[[], None]] = None


def set_desktop_shutdown_callback(callback: Optional[Callable[[], None]]) -> None:
    """Connect the authenticated prepare-exit route to Uvicorn's server."""

    global _desktop_shutdown_callback
    _desktop_shutdown_callback = callback


def _configured_desktop_session_token() -> str:
    return str(os.environ.get(DESKTOP_SESSION_TOKEN_ENV, "") or "").strip()


def _request_desktop_session_token(request: Request) -> Optional[str]:
    values = [
        value
        for name, value in request.scope.get("headers", [])
        if bytes(name).lower() == DESKTOP_SESSION_TOKEN_HEADER
    ]
    if len(values) != 1:
        return None
    try:
        return bytes(values[0]).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None


def _is_loopback_client(host: str) -> bool:
    normalized = (host or "").strip().split("%", 1)[0]
    if normalized.lower() in ("localhost", "testclient"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _is_allowed_local_host_header(value: str, *, allow_testserver: bool = False) -> bool:
    """Validate an HTTP Host without relying on DNS or permissive URL parsing."""
    candidate = str(value or "")
    if not candidate or candidate != candidate.strip():
        return False
    if any(ord(char) <= 32 or ord(char) == 127 for char in candidate):
        return False
    if any(char in candidate for char in ("/", "\\", "?", "#", "@", ",")):
        return False

    host = ""
    port_text = ""
    bracketed = False
    if candidate.startswith("["):
        bracketed = True
        closing = candidate.find("]")
        if closing <= 1:
            return False
        host = candidate[1:closing]
        remainder = candidate[closing + 1:]
        if remainder:
            if not remainder.startswith(":"):
                return False
            port_text = remainder[1:]
            if not port_text:
                return False
    else:
        if candidate.count(":") > 1:
            # IPv6 Host values must use RFC-compliant square brackets.
            return False
        if ":" in candidate:
            host, port_text = candidate.rsplit(":", 1)
            if not port_text:
                return False
        else:
            host = candidate

    if port_text:
        if not port_text.isascii() or not port_text.isdigit():
            return False
        port = int(port_text)
        if port < 1 or port > 65535:
            return False

    normalized = host.lower()
    if "%" in normalized:
        return False
    if normalized == "localhost":
        return True
    if allow_testserver and normalized == "testserver":
        return True
    try:
        address = ipaddress.ip_address(normalized.split("%", 1)[0])
        if bracketed and not isinstance(address, ipaddress.IPv6Address):
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _request_has_allowed_local_host(request: Request) -> bool:
    host_values = [
        value
        for name, value in request.scope.get("headers", [])
        if bytes(name).lower() == b"host"
    ]
    if len(host_values) != 1:
        return False
    try:
        host_header = bytes(host_values[0]).decode("ascii", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return False
    client_host = request.client.host if request.client else ""
    return _is_allowed_local_host_header(
        host_header,
        allow_testserver=client_host == "testclient",
    )


def _request_has_trusted_browser_context(request: Request) -> bool:
    """Reject cross-site browser requests that CORS alone cannot prevent.

    CORS controls whether JavaScript may read a response, but a hostile page
    can still *send* simple requests to a localhost service.  Non-browser API
    clients normally omit both headers and remain supported.  Browser requests
    must either declare a trusted/same origin or a non-cross-site Fetch
    Metadata context.
    """

    origin_values = [
        value
        for name, value in request.scope.get("headers", [])
        if bytes(name).lower() == b"origin"
    ]
    if origin_values:
        if len(origin_values) != 1:
            return False
        try:
            origin = bytes(origin_values[0]).decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
        if origin in TRUSTED_BROWSER_ORIGINS:
            return True

        # The browser UI is also supported on a caller-selected backend port.
        # Accept that origin only when its authority exactly matches Host.
        host_values = [
            value
            for name, value in request.scope.get("headers", [])
            if bytes(name).lower() == b"host"
        ]
        if len(host_values) != 1:
            return False
        try:
            host = bytes(host_values[0]).decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError):
            return False
        same_origin = f"{request.url.scheme.lower()}://{host.lower()}"
        return origin.lower() == same_origin

    fetch_site_values = [
        value
        for name, value in request.scope.get("headers", [])
        if bytes(name).lower() == b"sec-fetch-site"
    ]
    if not fetch_site_values:
        return True
    if len(fetch_site_values) != 1:
        return False
    try:
        fetch_site = bytes(fetch_site_values[0]).decode(
            "ascii", errors="strict"
        ).strip().lower()
    except (UnicodeDecodeError, ValueError):
        return False
    return fetch_site in {"none", "same-origin", "same-site"}


def _secure_api_response(response: Response) -> Response:
    """Keep private local API data out of caches and MIME sniffers."""

    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.middleware("http")
async def protect_local_image_and_secret_routes(request: Request, call_next):
    """Keep the private localhost API local and reject browser CSRF."""
    path = request.url.path
    api_request = path == "/api" or path.startswith("/api/")
    desktop_token = _configured_desktop_session_token()
    if (
        desktop_token
        and api_request
        and request.method.upper() != "OPTIONS"
    ):
        supplied_token = _request_desktop_session_token(request)
        if supplied_token is None or not secrets.compare_digest(
            supplied_token,
            desktop_token,
        ):
            return _secure_api_response(JSONResponse(
                status_code=401,
                content={"detail": "桌面会话令牌缺失或无效"},
            ))
    if (
        desktop_token
        and _desktop_shutdown_requested.is_set()
        and api_request
        and path not in ("/api/desktop/health", "/api/desktop/prepare-exit")
    ):
        return _secure_api_response(JSONResponse(
            status_code=503,
            content={"detail": "桌面后端正在安全退出，暂不接受新任务"},
        ))
    if (
        is_smoke_test_environment()
        and api_request
        and request.method.upper() != "OPTIONS"
        and path not in SMOKE_TEST_API_ALLOWLIST
    ):
        return _secure_api_response(JSONResponse(
            status_code=503,
            content={"detail": "发布冒烟测试隔离模式不会访问本机微信数据"},
        ))
    if not _request_has_allowed_local_host(request):
        return _secure_api_response(JSONResponse(
            status_code=400,
            content={"detail": "请求 Host 不是受信任的本机地址"},
        ))
    if (
        api_request
        and not _request_has_trusted_browser_context(request)
    ):
        return _secure_api_response(JSONResponse(
            status_code=403,
            content={"detail": "拒绝来自非受信任网页来源的本机 API 请求"},
        ))
    client_host = request.client.host if request.client else ""
    if api_request and not _is_loopback_client(client_host):
        return _secure_api_response(JSONResponse(
            status_code=403,
            content={"detail": "微信数据 API 仅允许从本机访问"},
        ))
    response = await call_next(request)
    if api_request:
        return _secure_api_response(response)
    return response


@app.get("/api/desktop/health")
async def desktop_health():
    """Authenticated handshake used by the Electron main process."""

    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "api_version": DESKTOP_API_VERSION,
        "pid": os.getpid(),
        "desktop": bool(_configured_desktop_session_token()),
    }


def _desktop_runtime_self_test() -> list[str]:
    """Load native modules that are easy for a freezer to omit accidentally."""

    from Crypto.Cipher import AES
    import av
    import pillow_heif
    import pymem
    import pysilk
    import win32api
    import yara
    import zstandard
    from PIL import Image

    AES.new(bytes(16), AES.MODE_ECB).encrypt(bytes(16))
    av.CodecContext.create("hevc", "r")
    pillow_heif.register_heif_opener()
    Image.new("RGB", (1, 1)).getpixel((0, 0))
    zstandard.ZstdCompressor(level=1).compress(b"desktop-self-test")
    yara.compile(source='rule desktop_self_test { condition: true }')
    win32api.GetVersion()
    if not callable(getattr(pysilk, "decode", None)):
        raise RuntimeError("SILK decoder is unavailable")
    if not hasattr(pymem, "Pymem"):
        raise RuntimeError("PyMem runtime is unavailable")
    checks = [
        "pycryptodome",
        "pyav-hevc",
        "pillow-heif",
        "pillow",
        "zstandard",
        "yara",
        "pywin32",
        "silk-python",
        "pymem",
    ]
    # ``wx_key`` is an optional enhancement: its public upstream artifacts do
    # not currently provide a redistributable, verifiable build. Probe it in an
    # isolated child when present, but keep the supported Python/PyMem fallback
    # usable when it is absent or unsafe to import.
    checks.append(_optional_wx_key_self_test_label())
    return checks


def _optional_wx_key_self_test_label() -> str:
    """Describe the optional hook without making it a release requirement."""

    # Release smoke tests must be deterministic and side-effect free.  The
    # optional native hook is intentionally absent from public builds, and an
    # opt-in local copy could execute arbitrary import-time initialization in
    # the probe child.  Normal desktop sessions still probe it before use.
    if is_smoke_test_environment():
        return "wx-key-hook:optional-skipped"
    try:
        _probe_wx_key_runtime()
    except Exception:
        return "wx-key-hook:optional-skipped"
    return "wx-key-hook:optional"


@app.post("/api/desktop/self-test")
async def desktop_runtime_self_test():
    """Authenticated release smoke test for frozen native dependencies."""

    if not _configured_desktop_session_token():
        raise HTTPException(404, "桌面运行时自检仅在桌面模式可用")
    try:
        checks = await asyncio.to_thread(_desktop_runtime_self_test)
    except Exception as exc:
        raise HTTPException(500, f"桌面原生依赖自检失败: {type(exc).__name__}") from exc
    return {"status": "ok", "checks": checks}

# --- 全局状态 ---

# 缓存的解析器实例
_parser_cache: dict = {}
_decryptor_cache: dict[str, DatabaseDecryptor] = {}
_image_service_cache: dict[tuple, WeChatImageService] = {}
_image_description_store = ImageDescriptionStore()
_recognition_manager = ImageRecognitionManager(_image_description_store)
_image_hd_automation_manager = ImageHDAutomationManager()
_image_key_extraction_running = False
_voice_service_cache: dict[tuple, WeChatVoiceService] = {}
_voice_transcription_store = VoiceTranscriptionStore()
_voice_transcription_manager = VoiceTranscriptionManager(_voice_transcription_store)
_moments_service_cache: dict[str, MomentsService] = {}
_moments_service_fingerprints: dict[str, tuple] = {}
_moments_service_cache_lock = threading.RLock()
_moments_media_resolver_cache: dict[tuple, MomentsMediaResolver] = {}
_avatar_service_cache: dict[tuple, AvatarService] = {}
# 守卫其余运行时服务缓存（parser/decryptor/image/voice/avatar/moments_resolver）
# 的查建过程：这些构造是秒级重活且非原子，无锁时线程池并发会重复解密同一库。
# 锁序固定为 runtime → sticker/moments，避免与下方两个专用锁死锁。
_runtime_cache_lock = threading.RLock()
_sticker_service_cache: dict[tuple, StickerService] = {}
_sticker_service_cache_lock = threading.RLock()
_avatar_url_secret = secrets.token_bytes(32)
_sticker_url_secret = secrets.token_bytes(32)
MOMENTS_PREVIEW_MEDIA_MAX_POSTS = 100
MOMENTS_PREVIEW_MEDIA_MAX_ITEMS = 60
# “加载全部媒体”仍然是显式的本机操作，但必须给单个联系人设置
# 总量边界，避免异常数据库让一个 HTTP 请求无限占用后台线程。
MOMENTS_ALL_MEDIA_MAX_POSTS = 5000
MOMENTS_ALL_MEDIA_MAX_ITEMS = 2000
AI_ANALYSIS_MAX_CHAT_RECORDS = 5000
AI_ANALYSIS_MAX_MOMENTS_POSTS = 5000
AI_ANALYSIS_MAX_CONCURRENT_TASKS = 2
AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS = 720
_analysis_task_slots = threading.BoundedSemaphore(AI_ANALYSIS_MAX_CONCURRENT_TASKS)
_analysis_cancel_events: set[threading.Event] = set()
_analysis_cancel_events_lock = threading.Lock()

def _is_xwechat() -> bool:
    """动态检测是否为微信 4.x 格式"""
    return bool(config.wx_root and config.wx_root.name.lower() == "xwechat_files")


def _close_runtime_caches(cancel_recognition: bool = True) -> None:
    """Close decrypted DBs and temporary decoded-image caches."""
    if cancel_recognition:
        _recognition_manager.cancel_all()
        _image_hd_automation_manager.cancel_all()
        _voice_transcription_manager.cancel_all()
        with _analysis_cancel_events_lock:
            for cancel_event in tuple(_analysis_cancel_events):
                cancel_event.set()
    with _runtime_cache_lock, _sticker_service_cache_lock, _moments_service_cache_lock:
        services = list(
            {id(item): item for item in _image_service_cache.values()}.values()
        )
        decryptors = list(
            {id(item): item for item in _decryptor_cache.values()}.values()
        )
        voice_services = list(
            {id(item): item for item in _voice_service_cache.values()}.values()
        )
        _image_service_cache.clear()
        _parser_cache.clear()
        _decryptor_cache.clear()
        _voice_service_cache.clear()
        _moments_service_cache.clear()
        _moments_service_fingerprints.clear()
        _moments_media_resolver_cache.clear()
        _avatar_service_cache.clear()
        _sticker_service_cache.clear()

    def cleanup() -> None:
        for service in services:
            try:
                service.close()
            except Exception:
                pass
        for decryptor in decryptors:
            try:
                decryptor.close()
            except Exception:
                pass

    _recognition_manager.defer_cleanup(cleanup)

    def cleanup_voice() -> None:
        for service in voice_services:
            try:
                service.close()
            except Exception:
                pass

    _voice_transcription_manager.defer_cleanup(cleanup_voice)


@app.post("/api/desktop/prepare-exit")
async def desktop_prepare_exit():
    """Cancel background work and close runtime caches before app shutdown."""

    _desktop_shutdown_requested.set()
    await asyncio.to_thread(_close_runtime_caches, True)
    callback = _desktop_shutdown_callback
    if callback is not None:
        asyncio.get_running_loop().call_later(0.05, callback)
    return {"success": True, "status": "ready_to_exit"}


def _is_image_message(message: dict) -> bool:
    try:
        return (int(message.get("type") or 0) & 0xFFFFFFFF) in (2, 3)
    except (TypeError, ValueError):
        return False


def _is_voice_message(message: dict) -> bool:
    try:
        return (int(message.get("type") or 0) & 0xFFFFFFFF) in (4, 34)
    except (TypeError, ValueError):
        return False


def _is_sticker_message(message: dict) -> bool:
    try:
        return (int(message.get("type") or 0) & 0xFFFFFFFF) == 47
    except (TypeError, ValueError):
        return False


def _message_key(talker: str, message: dict) -> str:
    server_id = str(message.get("server_id") or "")
    local_id = int(message.get("id") or 0)
    create_time = int(message.get("create_time") or 0)
    identity = server_id if server_id and server_id != "0" else str(local_id)
    return f"{talker}:{identity}:{create_time}:{local_id}"


def _enrich_image_descriptions(talker: str, messages: list[dict]) -> None:
    """Attach cached image descriptions and voice transcriptions."""
    account_id = config.wxid or ""
    records = _image_description_store.get_many(account_id, talker, messages)
    voice_records = _voice_transcription_store.get_many(
        account_id, talker, messages
    )
    for message in messages:
        message["message_key"] = _message_key(talker, message)
        if not _is_image_message(message):
            continue
        key = (
            int(message.get("id") or 0),
            int(message.get("create_time") or 0),
            str(message.get("server_id") or ""),
        )
        record = records.get(key)
        if record:
            message["image_description"] = record.get("description") or ""
            message["image_recognition_status"] = record.get("status") or "unrecognized"
            message["image_recognition_error"] = record.get("error") or ""
        else:
            message["image_description"] = ""
            message["image_recognition_status"] = "unrecognized"
            message["image_recognition_error"] = ""
        continue

    for message in messages:
        if not _is_voice_message(message):
            continue
        key = (
            int(message.get("id") or 0),
            int(message.get("create_time") or 0),
            str(message.get("server_id") or ""),
        )
        record = voice_records.get(key)
        if record:
            message["voice_transcription"] = record.get("transcription") or ""
            message["voice_transcription_status"] = record.get("status") or "untranscribed"
            message["voice_transcription_error"] = record.get("error") or ""
            cached_duration = record.get("duration_seconds")
            if cached_duration:
                message["voice_duration_seconds"] = cached_duration
            else:
                message["voice_duration_seconds"] = (
                    message.get("voice_duration_seconds") or 0.0
                )
        else:
            message["voice_transcription"] = ""
            message["voice_transcription_status"] = "untranscribed"
            message["voice_transcription_error"] = ""
            message["voice_duration_seconds"] = (
                message.get("voice_duration_seconds") or 0.0
            )


def get_image_service() -> WeChatImageService:
    """Build an account-scoped 4.x image resolver on demand."""
    if not _is_xwechat():
        raise HTTPException(
            501,
            "当前账号不是受支持的微信 4.x 数据目录，无法关联聊天图片",
        )
    if not config.wxid or not config.msg_dir or not config.key:
        raise HTTPException(400, "请先选择微信账号并设置数据库解密密钥")
    encrypted_resource = config.msg_dir / "message_resource.db"
    if not encrypted_resource.exists():
        raise HTTPException(404, "未找到 message_resource.db，无法关联聊天图片")

    try:
        with open(encrypted_resource, "rb") as handle:
            is_plaintext = handle.read(16) == b"SQLite format 3\x00"
    except OSError as exc:
        raise HTTPException(500, f"无法读取图片资源数据库: {exc}") from exc

    if is_plaintext:
        resource_path = encrypted_resource
    else:
        with _runtime_cache_lock:
            decryptor_key = str(encrypted_resource)
            if decryptor_key not in _decryptor_cache:
                _decryptor_cache[decryptor_key] = DatabaseDecryptor(
                    encrypted_resource, config.key
                )
            try:
                resource_path = _decryptor_cache[decryptor_key].decrypt_to_temp()
            except Exception as exc:
                raise HTTPException(500, f"图片资源数据库解密失败: {exc}") from exc

    settings = load_settings()
    image_settings = get_account_image_settings(settings, config.wxid)
    cache_key = (
        config.wxid,
        str(resource_path),
        str(image_settings.get("aes_key") or ""),
        str(image_settings.get("xor_key") or "auto"),
    )
    with _runtime_cache_lock:
        if cache_key not in _image_service_cache:
            # config.msg_dir = <account>/db_storage/message
            account_root = config.msg_dir.parent.parent
            _image_service_cache[cache_key] = WeChatImageService(
                account_id=config.wxid,
                account_root=account_root,
                resource_db_path=resource_path,
                aes_key=image_settings.get("aes_key"),
                xor_key=image_settings.get("xor_key", "auto"),
            )
        return _image_service_cache[cache_key]


def get_voice_service() -> WeChatVoiceService:
    """Build an account-scoped WeChat 4.x voice resolver on demand."""
    if not _is_xwechat():
        raise HTTPException(501, "语音转文字目前仅支持微信 4.x")
    if not config.wxid or not config.msg_dir or not config.key:
        raise HTTPException(400, "请先选择微信账号并设置数据库解密密钥")

    cache_key = (config.wxid, str(config.msg_dir.resolve()), config.key)
    with _runtime_cache_lock:
        if cache_key not in _voice_service_cache:
            try:
                _voice_service_cache[cache_key] = WeChatVoiceService(
                    account_id=config.wxid,
                    msg_dir=config.msg_dir,
                    database_key=config.key,
                )
            except VoiceServiceError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
        return _voice_service_cache[cache_key]


def get_sticker_service() -> StickerService:
    """Build an account-scoped sticker resolver with optional emoticon DB lookup."""
    wx_root = config.wx_root
    account_id = str(config.wxid or "").strip()
    msg_dir = Path(config.msg_dir) if config.msg_dir else None
    database_key = str(config.key or "").strip()
    if not wx_root or wx_root.name.lower() != "xwechat_files":
        raise HTTPException(501, "聊天表情解析目前仅支持微信 4.x")
    if not account_id or msg_dir is None or not database_key:
        raise HTTPException(400, "请先选择微信账号并设置数据库解密密钥")

    account_root = msg_dir.parent.parent
    encrypted_emoticon = (
        account_root / "db_storage" / "emoticon" / "emoticon.db"
    )
    encrypted_identity = str(encrypted_emoticon.resolve())
    key_fingerprint = hashlib.sha256(database_key.encode("utf-8")).hexdigest()
    cache_key = (account_id, encrypted_identity, key_fingerprint)
    with _runtime_cache_lock, _sticker_service_cache_lock:
        cached = _sticker_service_cache.get(cache_key)
        if cached is not None:
            return cached

        emoticon_path = None
        if encrypted_emoticon.is_file():
            try:
                with open(encrypted_emoticon, "rb") as handle:
                    is_plaintext = handle.read(16) == b"SQLite format 3\x00"
                if is_plaintext:
                    emoticon_path = encrypted_emoticon
                else:
                    decryptor_key = (
                        f"sticker:{encrypted_identity}:{key_fingerprint}"
                    )
                    decryptor = _decryptor_cache.get(decryptor_key)
                    if decryptor is None:
                        decryptor = DatabaseDecryptor(
                            encrypted_emoticon, database_key
                        )
                        _decryptor_cache[decryptor_key] = decryptor
                    try:
                        emoticon_path = decryptor.decrypt_to_temp()
                    except Exception:
                        failed = _decryptor_cache.pop(decryptor_key, None)
                        if failed is not None:
                            try:
                                failed.close()
                            except Exception:
                                pass
                        raise
            except Exception:
                # Message XML may still contain a direct resource URL. Failure
                # to open the optional DB must not break that safe path.
                emoticon_path = None

        service = StickerService(
            account_id,
            emoticon_db_path=emoticon_path,
        )
        _sticker_service_cache[cache_key] = service
        return service


def _find_image_message(
    parser,
    talker: str,
    message_id: int,
    create_time: int,
    expected_key: Optional[str] = None,
) -> Optional[dict]:
    # A narrow time window disambiguates local_id reuse without requiring the
    # browser to know which message shard owns the row.
    result = parser.get_messages(
        talker,
        page=1,
        page_size=50,
        start_time=max(0, int(create_time) - 2),
        end_time=int(create_time) + 2,
        message_ids=[int(message_id)],
    )
    candidates = [
        message for message in result.get("messages", []) if _is_image_message(message)
    ]
    if expected_key:
        exact = [
            message
            for message in candidates
            if _message_key(talker, message) == expected_key
        ]
        if exact:
            candidates = exact
        else:
            return None
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda message: abs(int(message.get("create_time") or 0) - int(create_time)),
    )


def _collect_image_messages(
    parser,
    talker: str,
    *,
    message_ids: Optional[list[int]],
    start_time: Optional[int],
    end_time: Optional[int],
    hard_limit: int,
) -> list[dict]:
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    normalized_ids = None
    if message_ids:
        normalized_ids = list(dict.fromkeys(int(item) for item in message_ids))
        if len(normalized_ids) > 2000:
            raise HTTPException(400, "一次选择的消息数量过多")

    page = 1
    page_size = 500
    images: list[dict] = []
    seen: set[str] = set()
    while True:
        result = parser.get_messages(
            talker,
            page=page,
            page_size=page_size,
            start_time=start_time,
            end_time=end_time,
            message_ids=normalized_ids,
        )
        for message in result.get("messages", []):
            if not _is_image_message(message):
                continue
            key = _message_key(talker, message)
            if key in seen:
                continue
            seen.add(key)
            images.append(message)
            if len(images) > hard_limit:
                return images
        if page >= int(result.get("total_pages") or 1):
            break
        page += 1
    return images


def _collect_image_viewer_sequence(
    parser,
    talker: str,
    *,
    start_time: Optional[int],
    end_time: Optional[int],
    max_images: int,
) -> tuple[list[dict], list[dict]]:
    """Collect images plus intervening videos in the viewer's time order."""
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    page = 1
    media: list[dict] = []
    images: list[dict] = []
    seen: set[str] = set()
    while True:
        result = parser.get_messages(
            talker,
            page=page,
            page_size=500,
            start_time=start_time,
            end_time=end_time,
        )
        for message in result.get("messages", []):
            try:
                message_type = int(message.get("type") or 0) & 0xFFFFFFFF
            except (TypeError, ValueError):
                continue
            if message_type not in (2, 3, 5, 43):
                continue
            key = _message_key(talker, message)
            if key in seen:
                continue
            seen.add(key)
            message["message_key"] = key
            media.append(message)
            if message_type in (2, 3):
                images.append(message)
                if len(images) > max_images:
                    return media, images
        if page >= int(result.get("total_pages") or 1):
            break
        page += 1
    return media, images


def _find_voice_message(
    parser,
    talker: str,
    message_id: int,
    create_time: int,
    expected_key: Optional[str] = None,
) -> Optional[dict]:
    """Find one exact voice message without trusting a reusable local id alone."""
    result = parser.get_messages(
        talker,
        page=1,
        page_size=50,
        start_time=max(0, int(create_time) - 2),
        end_time=int(create_time) + 2,
        message_ids=[int(message_id)],
    )
    candidates = [
        message for message in result.get("messages", []) if _is_voice_message(message)
    ]
    if expected_key:
        candidates = [
            message
            for message in candidates
            if _message_key(talker, message) == expected_key
        ]
        if not candidates:
            return None
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda message: abs(
            int(message.get("create_time") or 0) - int(create_time)
        ),
    )


def _find_sticker_message(
    parser,
    talker: str,
    message_id: int,
    create_time: int,
    expected_key: Optional[str] = None,
) -> Optional[dict]:
    """Find one exact custom-sticker message across reusable local ids."""
    result = parser.get_messages(
        talker,
        page=1,
        page_size=50,
        start_time=max(0, int(create_time) - 2),
        end_time=int(create_time) + 2,
        message_ids=[int(message_id)],
    )
    candidates = [
        message for message in result.get("messages", []) if _is_sticker_message(message)
    ]
    if expected_key:
        candidates = [
            message
            for message in candidates
            if _message_key(talker, message) == expected_key
        ]
        if not candidates:
            return None
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda message: abs(
            int(message.get("create_time") or 0) - int(create_time)
        ),
    )


def _sticker_token(
    account_id: str,
    talker: str,
    message_id: int,
    create_time: int,
    message_key: str,
    *,
    download: bool,
) -> str:
    payload = json.dumps(
        [
            "sticker-v1",
            str(account_id or ""),
            str(talker or ""),
            int(message_id),
            int(create_time),
            str(message_key or ""),
            bool(download),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_sticker_url_secret, payload, hashlib.sha256).hexdigest()


def _valid_sticker_token(
    token: str,
    account_id: str,
    talker: str,
    message_id: int,
    create_time: int,
    message_key: str,
    *,
    download: bool,
) -> bool:
    supplied = str(token or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return False
    expected = _sticker_token(
        account_id,
        talker,
        message_id,
        create_time,
        message_key,
        download=download,
    )
    return hmac.compare_digest(supplied, expected)


def _sticker_public_url(talker: str, message: dict, *, download: bool = False) -> str:
    message_id = int(message.get("id") or 0)
    create_time = int(message.get("create_time") or 0)
    message_key = str(message.get("message_key") or _message_key(talker, message))
    params = {
        "create_time": str(create_time),
        "message_key": message_key,
        "token": _sticker_token(
            config.wxid or "",
            talker,
            message_id,
            create_time,
            message_key,
            download=download,
        ),
    }
    if download:
        params["download"] = "1"
    return (
        f"/api/chat/{urlquote(str(talker), safe='')}/sticker/{message_id}"
        f"?{urlencode(params)}"
    )


def _enrich_sticker_messages(talker: str, messages: list[dict]) -> None:
    """Attach local proxy URLs and recursively remove private resolver fields."""
    for message in messages:
        message["message_key"] = str(
            message.get("message_key") or _message_key(talker, message)
        )
        source = message.pop("_sticker_source", None)
        if _is_sticker_message(message):
            sticker = message.get("sticker")
            if not isinstance(sticker, dict):
                sticker = {}
            sticker = {
                "md5": str(sticker.get("md5") or ""),
                "description": str(sticker.get("description") or ""),
                "available": bool(sticker.get("available") or source),
            }
            if sticker["available"]:
                sticker["display_url"] = _sticker_public_url(talker, message)
                sticker["export_url"] = _sticker_public_url(
                    talker, message, download=True
                )
            message["sticker"] = sticker

        reply = message.get("reply")
        if not isinstance(reply, dict):
            continue
        # Inline quoted resolver material is private. Prefer the fully resolved
        # target message, whose own signed local proxy can be served exactly.
        reply.pop("_sticker_source", None)
        target = reply.get("target_message")
        if isinstance(target, dict):
            _enrich_sticker_messages(talker, [target])


def _collect_voice_messages(
    parser,
    talker: str,
    *,
    message_ids: Optional[list[int]],
    start_time: Optional[int],
    end_time: Optional[int],
    hard_limit: int,
) -> list[dict]:
    """Collect a bounded, de-duplicated voice-message range."""
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    normalized_ids = None
    if message_ids:
        normalized_ids = list(dict.fromkeys(int(item) for item in message_ids))
        if len(normalized_ids) > 2000:
            raise HTTPException(400, "一次选择的消息数量过多")

    page = 1
    page_size = 500
    voices: list[dict] = []
    seen: set[str] = set()
    while True:
        result = parser.get_messages(
            talker,
            page=page,
            page_size=page_size,
            start_time=start_time,
            end_time=end_time,
            message_ids=normalized_ids,
        )
        for message in result.get("messages", []):
            if not _is_voice_message(message):
                continue
            key = _message_key(talker, message)
            if key in seen:
                continue
            seen.add(key)
            message["message_key"] = key
            voices.append(message)
            if len(voices) > hard_limit:
                return voices
        if page >= int(result.get("total_pages") or 1):
            break
        page += 1
    return voices


def _collect_chat_analysis_messages(
    parser,
    talker: str,
    *,
    message_refs: Optional[list] = None,
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    hard_limit: int = AI_ANALYSIS_MAX_CHAT_RECORDS,
) -> list[dict]:
    """Collect one explicit chat scope without trusting a reusable local id.

    Exact browser references are resolved in small time-ordered batches so a
    local id reused by another WeChat 4.x shard cannot silently enter a report.
    Date/all scopes are bounded before any model request is made.
    """
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(400, "开始时间不能晚于结束时间")

    if message_refs is not None:
        if not message_refs:
            raise HTTPException(400, "所选聊天记录范围为空")
        if len(message_refs) > hard_limit:
            raise HTTPException(
                400, f"一次最多分析 {hard_limit} 条聊天记录，请缩小范围"
            )

        normalized: dict[tuple[int, int, str], AnalysisMessageReference] = {}
        for reference in message_refs:
            message_key = str(reference.message_key or "")
            if not message_key:
                raise HTTPException(400, "所选消息缺少精确标识，请刷新聊天记录后重新选择")
            key = (
                int(reference.id),
                int(reference.create_time),
                message_key,
            )
            normalized.setdefault(key, reference)
        ordered_refs = sorted(
            normalized.values(),
            key=lambda item: (int(item.create_time), int(item.id)),
        )
        found: dict[tuple[int, int, str], dict] = {}
        for offset in range(0, len(ordered_refs), 200):
            batch = ordered_refs[offset:offset + 200]
            ids = list(dict.fromkeys(int(item.id) for item in batch))
            times = [int(item.create_time) for item in batch]
            allowed: dict[tuple[int, int], set[str]] = {}
            for item in batch:
                allowed.setdefault(
                    (int(item.id), int(item.create_time)), set()
                ).add(str(item.message_key))

            page = 1
            while True:
                result = parser.get_messages(
                    talker,
                    page=page,
                    page_size=500,
                    start_time=max(0, min(times) - 2),
                    end_time=max(times) + 2,
                    message_ids=ids,
                )
                for message in result.get("messages", []):
                    identity = (
                        int(message.get("id") or 0),
                        int(message.get("create_time") or 0),
                    )
                    expected = allowed.get(identity)
                    if not expected:
                        continue
                    actual_key = _message_key(talker, message)
                    if actual_key not in expected:
                        continue
                    found[(identity[0], identity[1], actual_key)] = message
                if page >= int(result.get("total_pages") or 1):
                    break
                page += 1

        selected: list[dict] = []
        missing = 0
        for item in ordered_refs:
            expected_key = str(item.message_key)
            match = found.get((int(item.id), int(item.create_time), expected_key))
            if match is None:
                missing += 1
                continue
            selected.append(match)
        if missing:
            raise HTTPException(
                409,
                f"有 {missing} 条所选消息已无法精确定位，请刷新聊天记录后重新选择",
            )
        messages = selected
    else:
        page = 1
        messages = []
        while True:
            result = parser.get_messages(
                talker,
                page=page,
                page_size=500,
                start_time=start_time,
                end_time=end_time,
            )
            total = int(result.get("total") or 0)
            if total > hard_limit:
                raise HTTPException(
                    400,
                    f"所选范围包含 {total} 条聊天记录，单次最多分析 {hard_limit} 条，请缩小范围",
                )
            messages.extend(result.get("messages", []))
            if len(messages) > hard_limit:
                raise HTTPException(
                    400, f"一次最多分析 {hard_limit} 条聊天记录，请缩小范围"
                )
            if page >= int(result.get("total_pages") or 1):
                break
            page += 1

    unique: list[dict] = []
    seen: set[str] = set()
    for message in sorted(
        messages,
        key=lambda item: (
            int(item.get("create_time") or 0), int(item.get("id") or 0)
        ),
    ):
        key = _message_key(talker, message)
        if key in seen:
            continue
        seen.add(key)
        message["message_key"] = key
        unique.append(message)
    _enrich_image_descriptions(talker, unique)
    return unique


def _chat_records_for_analysis(
    messages: list[dict], *, peer_name: str
) -> list[dict]:
    """Project parsed messages to bounded text-only records for a model."""
    records: list[dict] = []
    self_name = str(config.display_name or "我").strip() or "我"
    peer = str(peer_name or "对方").strip() or "对方"
    for index, message in enumerate(messages, start=1):
        timestamp = int(message.get("create_time") or 0)
        is_sender = message.get("is_sender")
        if is_sender is None:
            sender = "系统"
        elif is_sender:
            sender = self_name
        else:
            sender = str(message.get("sender_name") or peer).strip() or peer
        message_type = str(message.get("type_name") or "消息").strip() or "消息"
        content = str(message.get("content") or "").strip()
        image_description = str(message.get("image_description") or "").strip()
        transcription = str(message.get("voice_transcription") or "").strip()
        if _is_image_message(message) and image_description:
            content = f"[图片内容] {image_description}"
        elif _is_voice_message(message) and transcription:
            content = f"[语音转写] {transcription}"
        if not content:
            content = f"[{message_type}]"
        records.append({
            "sequence": index,
            "create_time_str": (
                datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                if timestamp > 0 else "时间未知"
            ),
            "sender_name": sender[:256],
            "type_name": message_type[:100],
            "content": content[:20000],
        })
    return records


def _moments_records_for_analysis(
    posts: list[dict], contacts: list[dict]
) -> list[dict]:
    """Project Moments posts, likes and comments to text-only records."""
    names = {
        str(item.get("username") or ""): str(
            item.get("display_name")
            or item.get("nickname")
            or item.get("username")
            or "未知联系人"
        )
        for item in contacts
    }
    records: list[dict] = []
    ordered = sorted(
        posts,
        key=lambda item: (int(item.get("create_time") or 0), str(item.get("tid") or "")),
    )
    for index, post in enumerate(ordered, start=1):
        username = str(post.get("username") or "")
        author = names.get(username) or str(
            post.get("nickname") or username or "未知联系人"
        )
        timestamp = int(post.get("create_time") or 0)
        text_parts = [
            str(value).strip()
            for value in (
                post.get("content"), post.get("title"), post.get("description")
            )
            if str(value or "").strip()
        ]
        content = "\n".join(text_parts) or "[无文字内容]"
        likes: list[dict] = []
        comments: list[dict] = []
        for interaction in post.get("comments") or []:
            try:
                interaction_type = int(interaction.get("type") or 0)
            except (TypeError, ValueError):
                continue
            from_name = str(
                interaction.get("from_display_name")
                or interaction.get("from_nickname")
                or interaction.get("from_username")
                or "未知用户"
            ).strip()
            if interaction_type == 1:
                likes.append({"display_name": from_name[:256]})
            elif interaction_type == 2:
                to_name = str(
                    interaction.get("to_display_name")
                    or interaction.get("to_nickname")
                    or ""
                ).strip()
                comment = str(interaction.get("content") or "").strip()
                comments.append({
                    "display_name": from_name[:256],
                    "reply_to_name": to_name[:256],
                    "content": comment[:5000],
                })
        location = post.get("location") or {}
        media_count = max(
            0,
            int(post.get("media_count") or len(post.get("media") or [])),
        )
        context = [content]
        if post.get("is_top"):
            context.append("[置顶朋友圈]")
        if location.get("poi_name"):
            context.append(f"[位置] {str(location.get('poi_name'))[:500]}")
        if media_count:
            context.append(f"[媒体] 共 {media_count} 项；本次仅分析文字，不发送媒体文件")
        records.append({
            "sequence": index,
            "create_time_str": (
                datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
                if timestamp > 0 else "时间未知"
            ),
            "author_name": author[:256],
            "analysis_content": "\n".join(context)[:30000],
            "likes": likes[:1000],
            "comments": comments[:1000],
        })
    return records


def _reserve_analysis_report_path(report_title: str) -> Path:
    """Preflight the export folder and atomically reserve a unique filename."""
    export_dir = get_export_dir()
    safe_title = _safe_filename(report_title or "AI分析报告")[:120]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    counter = 0
    while True:
        suffix = "" if counter == 0 else f"({counter})"
        destination = export_dir / f"{safe_title}_{stamp}{suffix}.md"
        try:
            descriptor = os.open(
                str(destination),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            counter += 1
            continue
        try:
            os.close(descriptor)
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return destination


def _discard_analysis_report_reservation(destination: Optional[Path]) -> None:
    """Best-effort cleanup for an unused report filename reservation."""
    if destination is None:
        return
    try:
        Path(destination).unlink()
    except OSError:
        pass


def _save_analysis_report(
    markdown: str,
    report_title: str,
    *,
    destination: Optional[Path] = None,
) -> Path:
    """Atomically replace a reserved report path with generated Markdown."""
    destination = (
        Path(destination)
        if destination is not None
        else _reserve_analysis_report_path(report_title)
    )
    descriptor = None
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="analysis_", suffix=".tmp", dir=str(destination.parent)
        )
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline="\n"
        ) as handle:
            descriptor = None
            handle.write(str(markdown or ""))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
        return destination
    except BaseException:
        _discard_analysis_report_reservation(destination)
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


_ANALYSIS_REPORT_SAVE_WARNING = (
    "AI 分析报告已生成，但无法自动保存到导出目录。"
    "报告内容仍可预览和下载，请检查设置中的导出路径和写入权限。"
)


def _preflight_analysis_report(report_title: str) -> Path:
    """Fail before the cloud request when the configured folder is unwritable."""
    try:
        return _reserve_analysis_report_path(report_title)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            500,
            "无法写入 AI 分析报告导出目录，请检查设置中的导出路径和写入权限",
        ) from exc


def _finish_analysis_report(
    markdown: str,
    report_title: str,
    destination: Path,
) -> tuple[Optional[Path], str]:
    """Save a generated report without discarding it when local I/O fails."""
    try:
        saved_path = _save_analysis_report(
            markdown,
            report_title,
            destination=destination,
        )
        return saved_path, ""
    except Exception as exc:
        _discard_analysis_report_reservation(destination)
        # Keep diagnostic output ASCII-safe: a failed log write must never
        # discard the already generated report on a legacy Windows console.
        print(f"[AI analysis] report auto-save failed: {type(exc).__name__}")
        return None, _ANALYSIS_REPORT_SAVE_WARNING


def _resolve_analysis_options(req, settings: dict) -> tuple:
    """Resolve a saved preset plus bounded per-run overrides."""
    config_value = AnalysisConfig.from_settings(settings)
    try:
        config_value.validate()
    except AIAnalysisError as exc:
        raise HTTPException(400, str(exc)) from exc

    requested_preset = str(req.preset_id or "").strip()
    preset = None
    if requested_preset and requested_preset != "custom":
        for candidate in settings.get("analysis", {}).get("presets", []) or []:
            if (
                isinstance(candidate, dict)
                and str(candidate.get("id") or "").strip() == requested_preset
            ):
                preset = candidate
                break
        if preset is None:
            raise HTTPException(409, "所选 AI 分析预设已发生变化，请刷新设置后重试")

    strength = str(
        req.strength
        or (preset or {}).get("strength")
        or config_value.strength
        or "balanced"
    ).strip().lower()
    detail = str(
        req.detail
        or (preset or {}).get("detail")
        or config_value.detail_level
        or "standard"
    ).strip().lower()
    if strength not in ("quick", "balanced", "deep"):
        raise HTTPException(400, "分析强度必须是 quick、balanced 或 deep")
    if detail not in ("brief", "standard", "detailed"):
        raise HTTPException(400, "报告详细程度必须是 brief、standard 或 detailed")

    preset_requirements = str((preset or {}).get("requirements") or "").strip()
    run_requirements = str(req.requirements or "").strip()
    requirements = "\n\n".join(
        value for value in (preset_requirements, run_requirements) if value
    )
    if requested_preset == "custom" and not requirements:
        raise HTTPException(400, "使用自定义分析时请填写具体分析要求")
    if len(requirements) > 8000 or any(
        ord(char) < 32 and char not in "\r\n\t" for char in requirements
    ):
        raise HTTPException(400, "本次分析要求过长或包含无效字符")
    return (
        config_value,
        strength,
        detail,
        requirements,
        str((preset or {}).get("name") or "自定义分析"),
        requested_preset or "custom",
    )


def _analysis_error_status(message: str) -> int:
    """Separate local selection/size errors from remote model failures."""
    local_markers = (
        "结构化分析记录",
        "分析资料类型",
        "分析内容为空",
        "待分析内容",
        "分析报告标题",
        "超过安全",
        "自定义分析要求",
        "分析强度",
        "详细程度",
    )
    return 400 if any(marker in str(message) for marker in local_markers) else 502


def _release_analysis_slot_for_task(task: asyncio.Task) -> None:
    """Release a detached worker slot and consume its terminal exception."""

    try:
        task.exception()
    except BaseException:
        pass
    _analysis_task_slots.release()


async def _run_analysis_with_limits(
    request: Request,
    records: list[dict],
    analysis_config: AnalysisConfig,
    **analysis_kwargs,
):
    """Run one cancellable analysis with a global concurrency and time bound."""

    if not _analysis_task_slots.acquire(blocking=False):
        raise HTTPException(
            429,
            f"当前已有 {AI_ANALYSIS_MAX_CONCURRENT_TASKS} 个 AI 分析任务在运行，请稍后重试",
        )

    cancel_event = threading.Event()
    with _analysis_cancel_events_lock:
        _analysis_cancel_events.add(cancel_event)
    worker: Optional[asyncio.Task] = None
    release_in_finally = True
    try:
        worker = asyncio.create_task(asyncio.to_thread(
            analyze_records,
            records,
            analysis_config,
            total_timeout_seconds=AI_ANALYSIS_TOTAL_TIMEOUT_SECONDS,
            cancel_event=cancel_event,
            **analysis_kwargs,
        ))
        disconnected = False
        while True:
            done, _pending = await asyncio.wait({worker}, timeout=0.25)
            if worker in done:
                return worker.result()
            if not disconnected and await request.is_disconnected():
                disconnected = True
                cancel_event.set()
    except asyncio.CancelledError:
        cancel_event.set()
        if worker is not None and not worker.done():
            worker.add_done_callback(_release_analysis_slot_for_task)
            release_in_finally = False
        raise
    finally:
        with _analysis_cancel_events_lock:
            _analysis_cancel_events.discard(cancel_event)
        if release_in_finally:
            _analysis_task_slots.release()


def get_parser():
    """获取微信 4.x 解析器实例（带缓存）。"""
    db_key = str(config.msg_dir)

    with _runtime_cache_lock:
        if db_key not in _parser_cache:
            if not config.key or not config.msg_dir:
                raise HTTPException(400, "请先设置解密密钥")
            if not _is_xwechat():
                raise HTTPException(400, "当前仅支持微信 4.x 数据目录")

            msg_dbs = config.get_all_msg_dbs()
            if not msg_dbs:
                raise HTTPException(404, "未找到消息数据库")
            connections = []
            for db_path in msg_dbs:
                path_key = str(db_path)
                if path_key not in _decryptor_cache:
                    _decryptor_cache[path_key] = DatabaseDecryptor(db_path, config.key)
                connections.append(_decryptor_cache[path_key].open_decrypted())

            contact_conn = None
            contact_path = config.micromsg_db
            if contact_path and contact_path.exists():
                ck = str(contact_path)
                if ck not in _decryptor_cache:
                    _decryptor_cache[ck] = DatabaseDecryptor(contact_path, config.key)
                contact_conn = _decryptor_cache[ck].open_decrypted()

            _parser_cache[db_key] = MessageParserV4(
                connections, config.wx_root, config.wxid, contact_conn
            )

        return _parser_cache[db_key]


def _connection_database_path(conn) -> Optional[Path]:
    """Return the stable decrypted main-database path for a SQLite connection."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            if str(row[1] or "") == "main" and str(row[2] or ""):
                path = Path(str(row[2])).resolve()
                if path.is_file():
                    return path
    except Exception:
        pass
    return None


def get_avatar_service() -> AvatarService:
    """Open an account-isolated real-avatar resolver."""
    if not config.wxid or not config.key:
        raise HTTPException(400, "请先选择微信账号并设置数据库解密密钥")

    contact_source = config.micromsg_db
    head_source = config.head_image_db
    key_fingerprint = hashlib.sha256(config.key.encode("ascii")).hexdigest()[:16]
    cache_key = (
        config.wxid,
        str(contact_source.resolve()) if contact_source else "",
        str(head_source.resolve()) if head_source else "",
        key_fingerprint,
    )
    with _runtime_cache_lock:
        if cache_key in _avatar_service_cache:
            return _avatar_service_cache[cache_key]

        def open_snapshot(source: Optional[Path]) -> Optional[Path]:
            if source is None or not source.is_file():
                return None
            source_key = str(source.resolve())
            try:
                if source_key not in _decryptor_cache:
                    _decryptor_cache[source_key] = DatabaseDecryptor(source, config.key)
                conn = _decryptor_cache[source_key].open_decrypted()
                return _connection_database_path(conn)
            except Exception:
                return None

        contact_snapshot = open_snapshot(contact_source)
        head_snapshot = open_snapshot(head_source)
        if contact_snapshot is None and head_snapshot is None:
            raise HTTPException(404, "未找到可读取的微信头像数据库")

        service = AvatarService(
            account_id=config.wxid,
            contact_db_path=contact_snapshot,
            head_image_db_path=head_snapshot,
        )
        _avatar_service_cache[cache_key] = service
        return service


def _avatar_token(username: str) -> str:
    account_id = str(config.wxid or "")
    payload = f"{account_id}\0{str(username or '')}".encode("utf-8")
    return hmac.new(_avatar_url_secret, payload, hashlib.sha256).hexdigest()


def _avatar_public_url(username: str) -> str:
    normalized = str(username or "").strip()
    if not normalized or not config.wxid or not config.key:
        return ""
    return (
        f"/api/avatar/{urlquote(normalized, safe='')}"
        f"?token={_avatar_token(normalized)}"
    )


def _valid_avatar_token(username: str, token: str) -> bool:
    normalized = str(username or "").strip()
    supplied = str(token or "").strip()
    return bool(
        normalized
        and supplied
        and hmac.compare_digest(supplied, _avatar_token(normalized))
    )


def _enrich_chat_avatars(contacts: list[dict]) -> None:
    for contact in contacts:
        username = str(contact.get("talker") or "").strip()
        contact["avatar_url"] = _avatar_public_url(username)


def _enrich_message_avatars(
    messages: list[dict], *, default_talker: str = ""
) -> None:
    for message in messages:
        if message.get("is_sender") is None:
            message["sender_username"] = ""
            message["sender_avatar_url"] = ""
            continue
        talker = str(message.get("talker") or default_talker or "").strip()
        is_group = talker.endswith("@chatroom")
        if bool(message.get("is_sender")):
            sender_username = "__self__"
            if not message.get("sender_name"):
                message["sender_name"] = config.display_name or "我"
        else:
            sender_username = str(message.get("sender_username") or "").strip()
            if not sender_username and not is_group:
                sender_username = talker
            # Never substitute the room's composite avatar for an unresolved
            # group member. The frontend will use a deterministic placeholder.
            if is_group and sender_username == talker:
                sender_username = ""
        message["sender_username"] = sender_username
        message["sender_avatar_url"] = _avatar_public_url(sender_username)


def _moments_file_fingerprint(path: Path) -> tuple[bool, int, int]:
    """Return an inexpensive identity for one live Moments DB file."""

    try:
        stat = path.stat()
    except FileNotFoundError:
        return False, 0, 0
    return True, int(stat.st_size), int(stat.st_mtime_ns)


def _moments_source_fingerprint(sns_path: Path) -> tuple:
    """Track both the main SNS database and its live WAL."""

    wal_path = Path(f"{sns_path}-wal")
    return (
        _moments_file_fingerprint(sns_path),
        _moments_file_fingerprint(wal_path),
    )


def get_moments_service() -> MomentsService:
    """Open the current account's local WeChat 4.x Moments snapshot."""
    if not _is_xwechat():
        raise HTTPException(501, "朋友圈导出目前仅支持微信 4.x")
    if not config.key or not config.wxid:
        raise HTTPException(400, "请先选择微信账号并设置数据库解密密钥")
    sns_path = config.sns_db
    if sns_path is None:
        raise HTTPException(404, "未找到当前账号的朋友圈数据库 sns.db")

    sns_path = Path(sns_path)
    service_key = f"{config.wxid}:{sns_path.resolve()}"

    try:
        # The lock prevents two simultaneous preview/export requests from
        # rebuilding the same snapshot.  Superseded SNS decryptors deliberately
        # remain in _decryptor_cache until the normal runtime cleanup: a running
        # export may still hold the old MomentsService and SQLite connection.
        with _runtime_cache_lock, _moments_service_cache_lock:
            source_fingerprint = _moments_source_fingerprint(sns_path)
            cached_service = _moments_service_cache.get(service_key)
            if (
                cached_service is not None
                and _moments_service_fingerprints.get(service_key)
                == source_fingerprint
            ):
                return cached_service

            contact_conn = None
            contact_path = config.micromsg_db
            if contact_path and contact_path.exists():
                contact_key = str(contact_path)
                if contact_key not in _decryptor_cache:
                    _decryptor_cache[contact_key] = DatabaseDecryptor(
                        contact_path, config.key
                    )
                contact_conn = _decryptor_cache[contact_key].open_decrypted()

            sns_decryptor = None
            snapshot_fingerprint = source_fingerprint
            # A live WAL can rotate while it is being copied.  Retry once when
            # its fingerprint changes during decryption.  If it keeps moving,
            # cache the last readable snapshot against its *starting* identity,
            # so the following refresh will automatically try again.
            for attempt in range(2):
                snapshot_fingerprint = _moments_source_fingerprint(sns_path)
                candidate = DatabaseDecryptor(sns_path, config.key)
                try:
                    sns_conn = candidate.open_decrypted()
                except Exception:
                    candidate.close()
                    raise
                fingerprint_after_open = _moments_source_fingerprint(sns_path)
                if (
                    snapshot_fingerprint == fingerprint_after_open
                    or attempt == 1
                ):
                    sns_decryptor = candidate
                    break
                candidate.close()

            if sns_decryptor is None:  # Defensive; the bounded loop always sets it.
                raise RuntimeError("朋友圈数据库快照创建失败")

            try:
                service = MomentsService(
                    sns_conn,
                    contact_conn=contact_conn,
                    account_id=config.wxid,
                    display_name=config.display_name or "",
                )
            except Exception:
                sns_decryptor.close()
                raise

            decryptor_key = f"moments:{service_key}:{id(sns_decryptor)}"
            _decryptor_cache[decryptor_key] = sns_decryptor
            _moments_service_cache[service_key] = service
            _moments_service_fingerprints[service_key] = snapshot_fingerprint
            return service
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"朋友圈数据库读取失败: {exc}") from exc


def get_moments_media_resolver() -> MomentsMediaResolver:
    """Return an account-isolated resolver for validated Moments images."""

    if not _is_xwechat():
        raise HTTPException(501, "朋友圈媒体预览目前仅支持微信 4.x")
    if not config.wxid or not config.msg_dir:
        raise HTTPException(400, "请先选择微信账号")

    account_root = config.msg_dir.parent.parent.resolve()
    image_settings = get_account_image_settings(load_settings(), config.wxid)
    aes_key = str(image_settings.get("aes_key") or "")
    xor_key = image_settings.get("xor_key", "auto")
    cache_key = (
        config.wxid,
        str(account_root),
        aes_key,
        str(xor_key),
    )
    with _runtime_cache_lock:
        if cache_key not in _moments_media_resolver_cache:
            _moments_media_resolver_cache[cache_key] = MomentsMediaResolver(
                account_id=config.wxid,
                aes_key=aes_key or None,
                xor_key=xor_key,
                sns_cache_root=account_root / "cache",
                # A user-triggered refresh must remain bounded even when years of
                # WeChat cache are present. Existing bindings persist in the
                # account-isolated manifest, so later runs can continue safely.
                max_scan_files=3000,
            )
        return _moments_media_resolver_cache[cache_key]


class _MomentsExportSnapshot:
    """Thread-safe, already-materialized input for the file/network exporter."""

    def __init__(self, contacts: list[dict], posts: list[dict]):
        self._contacts = contacts
        self._posts = posts

    @staticmethod
    def _normalize_selection(usernames):
        return MomentsService._normalize_selection(usernames)

    def get_contacts(self):
        return [dict(contact) for contact in self._contacts]

    def get_posts(self, _usernames, start_time=None, end_time=None, tids=None):
        # Filtering and SQLite access were completed before entering the worker.
        return list(self._posts)


# --- Pydantic Models ---

class KeyInput(BaseModel):
    key: str = Field(..., min_length=64, max_length=256)


class SwitchAccountRequest(BaseModel):
    index: int = Field(..., ge=0)


class MessageReference(BaseModel):
    """An exact browser-visible message identity across database shards."""

    id: int
    create_time: int = Field(..., ge=0)
    message_key: Optional[str] = Field(None, max_length=1024)


class AnalysisMessageReference(BaseModel):
    """A mandatory exact identity used only by chat AI analysis requests."""

    id: int
    create_time: int = Field(..., ge=0)
    message_key: str = Field(..., min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_exact_message_key(self):
        value = self.message_key
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("消息精确标识格式无效")
        parts = value.rsplit(":", 3)
        if len(parts) != 4:
            raise ValueError("消息精确标识格式无效")
        talker, identity, create_time, local_id = parts
        if (
            not talker
            or len(talker) > 512
            or not re.fullmatch(r"-?\d{1,40}", identity)
            or not re.fullmatch(r"\d{1,20}", create_time)
            or not re.fullmatch(r"-?\d{1,20}", local_id)
        ):
            raise ValueError("消息精确标识格式无效")
        if int(create_time) != self.create_time or int(local_id) != self.id:
            raise ValueError("消息精确标识与消息 ID 或时间不一致")
        return self


class ExportRequest(BaseModel):
    talker: str = Field(..., min_length=1, max_length=512)
    display_name: str = Field(..., min_length=1, max_length=256)
    format: str = Field("html", min_length=1, max_length=16)
    start_time: Optional[int] = Field(None, ge=0)  # Unix timestamp
    end_time: Optional[int] = Field(None, ge=0)  # Unix timestamp
    message_ids: Optional[list[int]] = Field(None, max_length=5000)
    message_refs: Optional[list[MessageReference]] = Field(None, max_length=5000)
    filename: Optional[str] = Field(None, max_length=180)  # 不含扩展名
    replace_images_with_descriptions: bool = False
    replace_voices_with_transcriptions: bool = False
    embed_images: bool = True
    html_image_quality: str = "best"

    @model_validator(mode="after")
    def validate_time_range(self):
        if (
            self.start_time is not None
            and self.end_time is not None
            and self.start_time > self.end_time
        ):
            raise ValueError("开始时间不能晚于结束时间")
        return self


class MomentsExportRequest(BaseModel):
    usernames: list[str]
    format: str = "html"
    start_time: Optional[int] = Field(None, ge=0)
    end_time: Optional[int] = Field(None, ge=0)
    tids: Optional[list[str]] = None
    filename: Optional[str] = Field(None, max_length=180)
    download_media: bool = False


class MomentsPreviewRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=256)
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=50)
    start_time: Optional[int] = Field(None, ge=0)
    end_time: Optional[int] = Field(None, ge=0)
    keyword: Optional[str] = Field(None, max_length=200)


class MomentsMediaLoadRequest(BaseModel):
    """Explicit preview posts, or one contact's complete feed, to load."""

    username: str = Field(..., min_length=1, max_length=256)
    tids: list[str] = Field(
        default_factory=list,
        # Keep request parsing bounded before route-level canonicalization.
        max_length=MOMENTS_PREVIEW_MEDIA_MAX_POSTS,
    )
    all_posts: bool = False


class AnalysisPresetSettingsUpdate(BaseModel):
    """One bounded, user-editable analysis preset in a settings request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=60)
    strength: str = Field("balanced", min_length=1, max_length=20)
    detail: str = Field("standard", min_length=1, max_length=20)
    requirements: str = Field("", max_length=4000)


class AnalysisSettingsUpdate(BaseModel):
    """Bounded partial update for the third-party analysis provider."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Optional[str] = Field(None, min_length=1, max_length=32)
    interface_preset: Optional[str] = Field(None, min_length=1, max_length=64)
    custom_name: Optional[str] = Field(None, max_length=256)
    custom_protocol: Optional[str] = Field(None, min_length=1, max_length=32)
    custom_api_key_header: Optional[str] = Field(None, max_length=128)
    custom_api_key_prefix: Optional[str] = Field(None, max_length=4096)
    custom_extra_headers: Optional[str] = Field(
        None, max_length=MAX_EXTRA_HEADERS_BYTES
    )
    custom_request_template: Optional[str] = Field(
        None, max_length=MAX_CUSTOM_TEMPLATE_BYTES
    )
    custom_response_path: Optional[str] = Field(None, max_length=512)
    base_url: Optional[str] = Field(None, max_length=4096)
    model: Optional[str] = Field(None, max_length=256)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=600)
    retry_count: Optional[int] = Field(None, ge=0, le=3)
    max_output_tokens: Optional[int] = Field(None, ge=256, le=8192)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    chunk_chars: Optional[int] = Field(None, ge=0, le=60000)
    presets: Optional[list[AnalysisPresetSettingsUpdate]] = Field(
        None, min_length=1, max_length=50
    )
    api_key: Optional[str] = Field(None, max_length=16384)
    clear_api_key: bool = False

    @field_validator("custom_extra_headers")
    @classmethod
    def validate_extra_headers_bytes(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.encode("utf-8")) > MAX_EXTRA_HEADERS_BYTES:
            raise ValueError("自定义附加请求头过大")
        return value

    @field_validator("custom_request_template")
    @classmethod
    def validate_request_template_bytes(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.encode("utf-8")) > MAX_CUSTOM_TEMPLATE_BYTES:
            raise ValueError("自定义请求模板过大")
        return value

    @field_validator("chunk_chars")
    @classmethod
    def validate_chunk_chars(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value != 0 and value < 1000:
            raise ValueError("AI 分析分块大小必须为 0 或 1000–60000")
        return value


class SettingsUpdate(BaseModel):
    """可选的扁平化设置更新 — 只需传要改的字段"""
    export: Optional[dict] = None
    ui: Optional[dict] = None
    vision: Optional[dict] = None
    transcription: Optional[dict] = None
    analysis: Optional[AnalysisSettingsUpdate] = None
    images: Optional[dict] = None
    about: Optional[dict] = None
    advanced: Optional[dict] = None


class ImageRecognitionRequest(BaseModel):
    """Scope for a user-triggered image-recognition task."""

    message_refs: Optional[list[MessageReference]] = None
    message_ids: Optional[list[int]] = None
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    all_images: bool = False
    force: bool = False


class ImageHDAutomationRequest(BaseModel):
    """A bounded media range explicitly prepared for foreground automation."""

    message_refs: Optional[list[MessageReference]] = None
    start_time: Optional[int] = Field(None, ge=0)
    end_time: Optional[int] = Field(None, ge=0)
    all_images: bool = False
    direction: str = "next"
    per_image_timeout: int = Field(10, ge=5, le=120)
    min_dwell_seconds: float = Field(0.5, ge=0, le=5)


class ImageHDNavigationModeRequest(BaseModel):
    mode: str


class VoiceTranscriptionRequest(BaseModel):
    """Scope for a user-triggered voice-transcription task."""

    message_refs: Optional[list[MessageReference]] = None
    message_ids: Optional[list[int]] = None
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    all_voices: bool = False
    force: bool = False
    cloud_upload_confirmed: bool = False


class AnalysisRequestOptions(BaseModel):
    """User-selected behavior for one explicit AI analysis request."""

    preset_id: Optional[str] = Field(None, max_length=100)
    strength: Optional[str] = Field(None, max_length=20)
    detail: Optional[str] = Field(None, max_length=20)
    requirements: Optional[str] = Field(None, max_length=4000)
    report_title: Optional[str] = Field(None, max_length=180)
    cloud_upload_confirmed: bool = False


class ChatAnalysisRequest(AnalysisRequestOptions):
    display_name: str = Field(..., min_length=1, max_length=256)
    message_refs: Optional[list[AnalysisMessageReference]] = Field(
        None, max_length=AI_ANALYSIS_MAX_CHAT_RECORDS
    )
    start_time: Optional[int] = Field(None, ge=0)
    end_time: Optional[int] = Field(None, ge=0)
    all_messages: bool = False


class MomentsAnalysisRequest(AnalysisRequestOptions):
    usernames: list[str] = Field(..., min_length=1, max_length=500)
    start_time: Optional[int] = Field(None, ge=0)
    end_time: Optional[int] = Field(None, ge=0)
    tids: Optional[list[str]] = Field(
        None, max_length=AI_ANALYSIS_MAX_MOMENTS_POSTS
    )


def _cleanup_temp_files():
    """Delete stale private temp artifacts without touching another server."""
    try:
        temp_dir = Path(tempfile.gettempdir())
        count = 0

        # Current decryptors include their owner PID. A second development
        # server must never unlink a live server's SQLite snapshot.
        for candidate in temp_dir.glob("wechat_db_*_*.db"):
            try:
                pid_match = re.fullmatch(r"wechat_db_(\d+)_.+\.db", candidate.name)
                if not pid_match or not candidate.is_file():
                    continue
                import psutil
                owner_pid = int(pid_match.group(1))
                if psutil.pid_exists(owner_pid):
                    continue
                candidate.unlink()
                count += 1
            except (OSError, ValueError):
                pass

        # Pre-PID releases used ``wechat_<random>.db``. Their owner cannot be
        # identified, so only remove files old enough not to be a live peer.
        now = datetime.now().timestamp()
        for candidate in temp_dir.glob("wechat_*.db"):
            if candidate.name.startswith("wechat_db_"):
                continue
            try:
                if not candidate.is_file() or now - candidate.stat().st_mtime < 86400:
                    continue
                candidate.unlink()
                count += 1
            except OSError:
                pass

        temp_root = temp_dir.resolve()
        for directory in temp_dir.glob("wechat_analysis_images_*"):
            try:
                resolved = directory.resolve()
                pid_match = re.match(
                    r"^wechat_analysis_images_(\d+)_", resolved.name
                )
                if pid_match:
                    try:
                        import psutil
                        owner_pid = int(pid_match.group(1))
                        if owner_pid != os.getpid() and psutil.pid_exists(owner_pid):
                            continue
                    except Exception:
                        continue
                if (
                    resolved.parent == temp_root
                    and resolved.name.startswith("wechat_analysis_images_")
                    and resolved.is_dir()
                ):
                    shutil.rmtree(resolved, ignore_errors=True)
                    count += 1
            except Exception:
                pass
        export_prefixes = (
            "wechat_moments_export_",
            "wechat_export_all_",
            "wechat_export_",
        )
        for prefix in export_prefixes:
            for directory in temp_dir.glob(f"{prefix}*"):
                try:
                    resolved = directory.resolve()
                    if resolved.parent != temp_root or not resolved.is_dir():
                        continue
                    pid_match = re.match(rf"^{re.escape(prefix)}(\d+)_", resolved.name)
                    if pid_match:
                        owner_pid = int(pid_match.group(1))
                        if owner_pid != os.getpid():
                            try:
                                import psutil
                                if psutil.pid_exists(owner_pid):
                                    continue
                            except Exception:
                                continue
                    elif (datetime.now().timestamp() - resolved.stat().st_mtime) < 86400:
                        # Legacy directories had no owner PID. Avoid touching a
                        # directory that another development server may use.
                        continue
                    shutil.rmtree(resolved, ignore_errors=True)
                    count += 1
                except Exception:
                    pass
        if count:
            print(f"[INFO] Cleaned up {count} old temp files")
    except Exception:
        pass


@app.on_event("startup")
async def on_startup():
    """启动时清理上次强制关闭遗留的临时文件，并更新根目录密钥文件"""
    _desktop_shutdown_requested.clear()
    _cleanup_temp_files()
    try:
        from .key_extractor import _write_readable_keyfile
        _write_readable_keyfile()
    except Exception:
        pass


@app.on_event("shutdown")
async def on_shutdown():
    """关闭时清理所有解密临时文件"""
    _close_runtime_caches(cancel_recognition=True)
    # Active worker threads own their image directory until they return.  Any
    # force-quit leftovers are removed at the next startup.
    print("[INFO] Server shutdown cleanup requested")


# --- API Routes ---

@app.get("/api/status")
async def get_status():
    """获取工具运行状态"""
    if not config.accounts:
        config.detect_and_set_accounts()
    else:
        config._load_cached_names()
    if config.wxid:
        config.detect_databases()

    # 获取用户自己的微信名
    if config.wxid and config.key and not config.display_name:
        config.detect_self_name()

    config_data = config.to_dict()
    if config_data.get("has_key"):
        config_data["avatar_url"] = _avatar_public_url("__self__")

    return {
        "success": True,
        "data": {
            "config": config_data,
            "sqlcipher": get_sqlcipher_status(),
            "has_saved_key": bool(load_key(config.wxid)),
        },
    }


@app.post("/api/switch-account")
async def switch_account(data: SwitchAccountRequest):
    """切换当前微信账号"""
    index = data.index
    if config.set_active_account(index):
        # 清除缓存的解析器，因为数据库变了
        _close_runtime_caches(cancel_recognition=True)
        config.detect_databases()
        config.display_name = None
        config.alias = None
        if config.key:
            config.detect_self_name()
        return {"success": True, "message": f"已切换到 {config.wxid}"}
    return {"success": False, "message": "无效的账号索引"}


@app.get("/api/auto-detect")
def auto_detect():
    """自动检测微信数据和密钥（扫描磁盘目录，属同步重活，走线程池）"""
    if not config.accounts:
        config.detect_and_set_accounts()
    else:
        config._load_cached_names()
    if config.wxid:
        config.detect_databases()

    # auto-detect 的结果必须只代表当前账号。先清除内存中的旧值，避免
    # 验证失败时 status 仍把上一账号的密钥报告为可用。
    config.key = None

    result = {
        "wxid_found": bool(config.wxid),
        "wxid": config.wxid,
        "accounts": [a.to_dict() for a in config.accounts],
        "databases_found": bool(config.msg_dir and config.get_all_msg_dbs()),
        "msg_dbs": [str(f.name) for f in config.get_all_msg_dbs()],
    }

    # 尝试自动提取密钥 - 并验证
    saved_key = load_key(config.wxid)
    if saved_key:
        # 验证已保存的密钥
        msg_dbs = config.get_all_msg_dbs()
        if msg_dbs and test_key(msg_dbs[0], saved_key):
            result["key_found"] = True
            result["key_source"] = "saved"
            config.key = saved_key
        else:
            # 保存的密钥无效，删除
            result["key_found"] = False
            result["key_source"] = None
            delete_saved_key(config.wxid)
    else:
        # A heuristic memory candidate is not persisted until it has passed
        # validation against the current account's encrypted database.
        auto_key = find_key_auto(persist=False)
        if auto_key:
            # 验证提取的密钥
            msg_dbs = config.get_all_msg_dbs()
            if msg_dbs and test_key(msg_dbs[0], auto_key):
                result["key_found"] = True
                result["key_source"] = "memory"
                config.key = auto_key
                save_key(auto_key, config.wxid)
            else:
                result["key_found"] = False
                result["key_source"] = None
        else:
            result["key_found"] = False
            result["key_source"] = None

    return {"success": True, "data": result}


@app.post("/api/set-key")
def set_key(data: KeyInput):
    """手动设置解密密钥"""
    key = data.key.strip().lower()

    if not validate_key(key):
        raise HTTPException(400, "密钥格式无效，需要64位十六进制字符串")

    # 测试密钥
    config.detect_wxid()
    config.detect_databases()

    msg_dbs = config.get_all_msg_dbs()
    # contact.db uses the same account key and is a fast validation target.
    contact_path = config.micromsg_db
    if contact_path and contact_path.exists() and contact_path not in msg_dbs:
        msg_dbs.insert(0, contact_path)

    if msg_dbs:
        # Try each database until one verifies
        ok = False
        for db in msg_dbs:
            if test_key(db, key):
                ok = True
                break
        if ok:
            config.key = key
            save_key(key, config.wxid)
            _close_runtime_caches(cancel_recognition=True)
            return {"success": True, "message": "密钥验证成功"}
        else:
            raise HTTPException(400, "密钥验证失败，请检查密钥是否正确")
    else:
        config.key = key
        save_key(key, config.wxid)
        return {"success": True, "message": "密钥已保存 (未找到数据库进行验证)"}


def _native_module_probe_command(module_name: str) -> list[str]:
    """Return a source/frozen command that imports one native dependency."""

    if getattr(sys, "frozen", False):
        return [sys.executable, "--probe-native-module", module_name]
    return [
        sys.executable,
        "-m",
        "backend.main",
        "--probe-native-module",
        module_name,
    ]


def _probe_wx_key_runtime() -> None:
    """Keep a broken native hook runtime from crashing the API process."""

    import subprocess

    try:
        completed = subprocess.run(
            _native_module_probe_command("wx_key"),
            capture_output=True,
            text=True,
            timeout=15,
            env=sanitized_subprocess_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            500,
            f"自动提取组件自检失败，请重新安装最新版软件（{type(exc).__name__}）",
        ) from exc
    if completed.returncode != 0:
        code = int(completed.returncode)
        raise HTTPException(
            500,
            f"自动提取组件无法安全加载，请重新安装最新版软件（错误码 {code}）",
        )


def _load_optional_wx_key_runtime():
    """Return the isolated-probed optional hook module, or ``None``."""

    try:
        _probe_wx_key_runtime()
    except Exception:
        return None
    try:
        import importlib

        module = importlib.import_module("wx_key")
    except Exception:
        return None
    required = (
        "initialize_hook",
        "get_last_error_msg",
        "poll_key_data",
        "cleanup_hook",
    )
    if any(not callable(getattr(module, name, None)) for name in required):
        return None
    return module


def _database_key_validation_context() -> tuple[str, str, list[Path]]:
    """Capture the selected account and its bounded key-validation targets."""

    config.detect_wxid()
    config.detect_databases()
    account_id = str(config.wxid or "").strip()
    msg_dir = config.msg_dir
    if not account_id or msg_dir is None:
        raise HTTPException(409, "请先在微信中登录并选择一个本地账号")

    targets = list(config.get_all_msg_dbs())
    contact_path = config.micromsg_db
    if contact_path and contact_path.is_file() and contact_path not in targets:
        targets.insert(0, contact_path)
    if not targets:
        raise HTTPException(409, "当前账号没有可用于验证密钥的微信数据库")
    return account_id, str(Path(msg_dir).resolve()), targets


def _extract_key_with_python_fallback() -> dict:
    """Use the redistributable PyMem scanner without controlling WeChat."""

    account_id, msg_dir_identity, validation_targets = (
        _database_key_validation_context()
    )
    candidate = find_key_auto(persist=False)
    if not candidate:
        raise HTTPException(
            404,
            "未从已运行的微信进程中找到数据库密钥；请保持微信 4.x 已登录后重试，"
            "或改为手动输入 64 位密钥",
        )
    if not validate_key(candidate):
        raise HTTPException(422, "内存扫描返回了格式无效的候选密钥")

    # Account selection is global UI state. Never commit a result obtained for
    # one account after another request has switched the active account.
    current_msg_dir = config.msg_dir
    if (
        config.wxid != account_id
        or current_msg_dir is None
        or str(Path(current_msg_dir).resolve()) != msg_dir_identity
    ):
        raise HTTPException(409, "扫描期间账号已切换，结果未保存，请重新尝试")
    if not any(test_key(path, candidate) for path in validation_targets):
        raise HTTPException(
            422,
            "内存扫描找到了候选密钥，但未能通过当前账号数据库验证；结果未保存",
        )
    current_msg_dir = config.msg_dir
    if (
        config.wxid != account_id
        or current_msg_dir is None
        or str(Path(current_msg_dir).resolve()) != msg_dir_identity
    ):
        raise HTTPException(409, "验证期间账号已切换，结果未保存，请重新尝试")

    config.key = candidate
    save_key(candidate, account_id)
    _close_runtime_caches(cancel_recognition=True)
    # 密钥已保存到本机账号设置；不通过 HTTP 响应回传明文，
    # 避免本机其他进程调用该接口即可拿到数据库密钥。
    return {
        "success": True,
        "verified": True,
        "source": "python_memory_fallback",
        "hook_available": False,
        "message": "已通过 Python 内存扫描提取并验证密钥（未使用可选 Hook）",
    }


@app.post("/api/extract-key")
async def extract_key():
    """Extract a key in a worker so the local API event loop stays responsive."""

    return await asyncio.to_thread(_extract_key_sync)


def _extract_key_sync():
    """
    自动从微信进程提取数据库密钥 (Hook 模式)

    流程:
    1. 关闭所有微信进程
    2. 重新启动微信并注入 Hook
    3. 等待用户在微信中登录
    4. 捕获密钥并验证
    5. 返回密钥

    注意: 此操作会强制关闭微信，请确保已保存聊天记录。
    """
    import subprocess
    import time as _time
    import psutil as _psutil

    _wx_key = _load_optional_wx_key_runtime()
    if _wx_key is None:
        # This path only reads the already-running WeChat process. In
        # particular, it must return before the Hook flow terminates or starts
        # any WeChat process.
        return _extract_key_with_python_fallback()

    # 1. 定位微信 4.x。优先复用正在运行的可执行文件路径，这能覆盖
    # 用户自定义安装目录；注册表和常见目录仅作为后备。
    wechat_exe = None

    def valid_weixin_executable(value) -> Optional[str]:
        if not value:
            return None
        candidate = Path(str(value).strip().strip('"'))
        try:
            if candidate.is_dir():
                candidate = candidate / "Weixin.exe"
            if candidate.is_file() and candidate.name.lower() == "weixin.exe":
                return str(candidate)
        except OSError:
            pass
        return None

    for proc in _psutil.process_iter(["name", "exe"]):
        try:
            if str(proc.info.get("name") or "").lower() == "weixin.exe":
                wechat_exe = valid_weixin_executable(proc.info.get("exe"))
                if wechat_exe:
                    break
        except Exception:
            continue

    if not wechat_exe:
        # 搜索微信 4.x 和 Windows App Paths 注册表项。
        try:
            import winreg
            registry_entries = (
                (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin", "InstallPath"),
                (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat", "InstallPath"),
                (
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\App Paths\Weixin.exe",
                    "",
                ),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\Weixin", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\WeChat", "InstallPath"),
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"Software\Microsoft\Windows\CurrentVersion\App Paths\Weixin.exe",
                    "",
                ),
            )
            registry_views = (
                0,
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
            )
            for hive, key_path, value_name in registry_entries:
                for view in dict.fromkeys(registry_views):
                    try:
                        with winreg.OpenKey(
                            hive,
                            key_path,
                            0,
                            winreg.KEY_READ | view,
                        ) as key:
                            install_path, _ = winreg.QueryValueEx(key, value_name)
                        wechat_exe = valid_weixin_executable(install_path)
                        if wechat_exe:
                            break
                    except OSError:
                        continue
                if wechat_exe:
                    break
        except Exception:
            pass

    if not wechat_exe:
        for candidate in [
            r"D:\Weixin\Weixin.exe",
            r"C:\Program Files\Tencent\WeChat\Weixin.exe",
            r"C:\Program Files (x86)\Tencent\WeChat\Weixin.exe",
            r"C:\Program Files\Tencent\Weixin\Weixin.exe",
            r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
        ]:
            wechat_exe = valid_weixin_executable(candidate)
            if wechat_exe:
                break

    if not wechat_exe:
        raise HTTPException(500, "未找到微信安装路径，请手动输入密钥")

    # 2. 关闭微信
    killed = False
    for proc in _psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == 'weixin.exe':
                proc.terminate()
                killed = True
        except Exception:
            pass
    if killed:
        _time.sleep(1.5)

    # 3. 启动微信
    try:
        subprocess.Popen(
            wechat_exe,
            env=sanitized_subprocess_env(),
        )
        _time.sleep(2)
    except Exception as e:
        raise HTTPException(500, f"启动微信失败: {e}")

    # 4. 找到微信 PID
    pid = None
    for proc in _psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == 'weixin.exe':
                pid = proc.info['pid']
                break
        except Exception:
            pass

    if not pid:
        raise HTTPException(500, "微信启动后未找到进程，请重试")

    # 5. 初始化 Hook；native 资源只清理一次，避免错误分支重复释放。
    hook_initialized = False
    try:
        if not _wx_key.initialize_hook(pid):
            err = _wx_key.get_last_error_msg()
            raise HTTPException(500, f"Hook 初始化失败: {err}")
        hook_initialized = True

        # 6. 轮询密钥 (最多等待 120 秒)
        timeout = 120
        start = _time.time()
        while _time.time() - start < timeout:
            key_data = _wx_key.poll_key_data()
            if key_data and 'key' in key_data:
                raw_key = str(key_data['key'] or '').strip().lower()
                if validate_key(raw_key):
                    config.detect_wxid()
                    config.detect_databases()

                    # 有数据库时必须先验证，绝不保存 native 组件返回的未验证候选。
                    msg_dbs = config.get_all_msg_dbs()
                    verified = any(test_key(db, raw_key) for db in msg_dbs)
                    if msg_dbs and not verified:
                        raise HTTPException(422, "已提取到候选密钥，但未能通过当前账号数据库验证")

                    config.key = raw_key
                    save_key(raw_key, config.wxid)
                    _close_runtime_caches(cancel_recognition=True)
                    # 同上：密钥已在服务端验证并保存，不回传明文。
                    return {
                        "success": True,
                        "verified": verified,
                        "source": "wx_key_hook",
                        "hook_available": True,
                        "message": "密钥已验证" if verified else "密钥已提取并保存（当前未找到可验证数据库）",
                    }

            _time.sleep(0.2)

        raise HTTPException(408, "获取密钥超时 (120s)，请确保在弹出的微信中完成登录")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"提取过程出错: {e}")
    finally:
        if hook_initialized:
            try:
                _wx_key.cleanup_hook()
            except Exception:
                pass


@app.delete("/api/key")
async def delete_key():
    """只删除当前账号已保存的密钥，退出到主界面。"""
    from .key_extractor import KEY_FILE
    current_wxid = config.wxid
    removed = delete_saved_key(current_wxid)
    config.key = None
    _close_runtime_caches(cancel_recognition=True)
    try:
        KEY_FILE.unlink()
    except Exception:
        pass
    return {
        "success": True,
        "message": "当前账号密钥已清除",
        "wxid": current_wxid,
        "removed": removed,
    }


@app.post("/api/clear-cache")
async def clear_cache():
    """清除解析器缓存"""
    _close_runtime_caches(cancel_recognition=True)
    return {"success": True, "message": "缓存已清除"}


@app.get("/api/avatar/{username}")
async def get_avatar(
    username: str,
    request: Request,
    token: str = Query(..., min_length=64, max_length=64),
):
    """Return one validated real avatar from the current WeChat account."""
    normalized = str(username or "").strip()
    if not _valid_avatar_token(normalized, token):
        raise HTTPException(403, "头像链接已失效，请刷新页面")
    service = get_avatar_service()
    try:
        avatar = await asyncio.to_thread(service.get_avatar, normalized)
    except AvatarNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except AvatarValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    etag = f'"{avatar.etag}"'
    if str(request.headers.get("if-none-match") or "").strip() == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=300, must-revalidate",
            },
        )
    return Response(
        content=avatar.data,
        media_type=avatar.mime_type,
        headers={
            "Content-Length": str(len(avatar.data)),
            "Cache-Control": "private, max-age=300, must-revalidate",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
            "X-WeChat-Avatar-Source": avatar.source,
        },
    )


# 同步重型路由有意使用 def 而非 async def：FastAPI 会自动将同步路由放入线程池执行，
# 避免 SQLite 查询、解密与消息解析等 CPU 密集操作阻塞事件循环（拖慢图片/语音等并发请求）。
@app.get("/api/chats")
def get_chats():
    """获取聊天列表"""
    parser = get_parser()
    contacts = parser.get_contacts()
    _enrich_chat_avatars(contacts)
    return {
        "success": True,
        "data": contacts,
        "total": len(contacts),
    }


@app.get("/api/contacts")
def get_contacts_directory():
    """Return the bounded WeChat 4.x address book, not only chat sessions."""
    parser = get_parser()
    contacts = parser.get_address_book()
    _enrich_chat_avatars(contacts)
    chat_usernames = {
        str(item.get("talker") or item.get("username") or "").strip()
        for item in parser.get_contacts()
    }
    for contact in contacts:
        username = str(
            contact.get("username") or contact.get("talker") or ""
        ).strip()
        contact["has_chat"] = username in chat_usernames
    return {
        "success": True,
        "data": contacts,
        "total": len(contacts),
    }


@app.get("/api/chat/{talker}")
def get_messages(
    talker: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    msg_type: int = Query(None),
    keyword: str = Query(None),
    sender_name: str = Query(None),
    start_time: int = Query(None),
    end_time: int = Query(None),
):
    """获取聊天消息 (分页)"""
    parser = get_parser()
    query = {
        "page": page,
        "page_size": page_size,
        "msg_type": msg_type,
        "keyword": keyword,
        "start_time": start_time,
        "end_time": end_time,
    }
    query["sender_name"] = sender_name
    result = parser.get_messages(talker, **query)
    _enrich_image_descriptions(talker, result.get("messages", []))
    _enrich_sticker_messages(talker, result.get("messages", []))
    _enrich_message_avatars(result.get("messages", []), default_talker=talker)
    return {"success": True, **result}


@app.get("/api/chat/{talker}/timerange")
def get_chat_timerange(talker: str):
    """获取聊天的消息时间范围 (起止时间戳)"""
    parser = get_parser()
    result = parser.get_messages(talker, page=1, page_size=1)
    if result["total"] == 0:
        return {"success": True, "start_time": None, "end_time": None}
    # 第一条 (最旧)
    first = result["messages"][0]["create_time"] if result["messages"] else None
    # 最后一条 (最新)：反向取第 1 页，避免深页把全表候选加载进内存
    last_result = parser.get_messages(talker, page=1, page_size=1, reverse=True)
    last = last_result["messages"][0]["create_time"] if last_result["messages"] else None
    return {"success": True, "start_time": first, "end_time": last}


@app.get("/api/message-position/{talker}/{msg_id}")
def get_message_position(
    talker: str,
    msg_id: int,
    create_time: Optional[int] = Query(None, ge=0),
    message_key: Optional[str] = Query(None, max_length=1000),
):
    """获取消息在聊天中的页码位置"""
    parser = get_parser()
    pos = parser.get_message_position(
        talker,
        msg_id,
        create_time=create_time,
        message_key=message_key,
    )
    if pos is None:
        raise HTTPException(404, "消息未找到")
    return {"success": True, **pos}


@app.get("/api/search")
def search_messages(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=500),
):
    """全局搜索消息"""
    parser = get_parser()
    results = parser.search_messages(keyword, limit=limit)
    by_talker: dict[str, list[dict]] = {}
    for message in results:
        by_talker.setdefault(str(message.get("talker") or ""), []).append(message)
    for result_talker, result_messages in by_talker.items():
        _enrich_sticker_messages(result_talker, result_messages)
    _enrich_message_avatars(results)
    return {"success": True, "data": results, "total": len(results)}


@app.get("/api/statistics")
def get_statistics():
    """获取聊天统计"""
    parser = get_parser()
    stats = parser.get_statistics()
    return {"success": True, "data": stats}


# --- Chat images (local display + explicit vision recognition) ---

@app.get("/api/chat/{talker}/image/{message_id}")
async def get_chat_image(
    talker: str,
    message_id: int,
    create_time: int = Query(..., ge=0),
    message_key: Optional[str] = Query(None, max_length=1000),
    quality: str = Query("thumbnail"),
    download: bool = Query(False),
):
    """Return one locally decrypted chat image; this route never calls a model."""
    parser = get_parser()
    message = _find_image_message(
        parser,
        talker,
        message_id,
        create_time,
        expected_key=message_key,
    )
    if not message:
        raise HTTPException(404, "未找到对应的图片消息")
    if quality not in ("thumbnail", "best"):
        raise HTTPException(400, "图片质量参数必须是 thumbnail 或 best")
    service = get_image_service()
    try:
        image, image_bytes = await asyncio.to_thread(
            service.get_image_bytes,
            talker=talker,
            message_id=message_id,
            create_time=int(message.get("create_time") or create_time),
            server_id=message.get("server_id"),
            purpose="display",
            quality=quality,
        )
    except ImageResolutionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    source_path = getattr(image, "source_path", None)
    source_stem = source_path.stem.lower() if source_path is not None else ""
    extension = str(getattr(image, "extension", "") or "")
    if not extension:
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(str(image.mime_type), ".img")
    disposition = "attachment" if download else "inline"
    filename = f"image_{message_id}_{create_time}{extension}"
    return Response(
        content=image_bytes,
        media_type=image.mime_type,
        headers={
            # Account identity is intentionally absent from the URL.  Never let
            # a browser reuse decrypted bytes after an account switch.
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Image-Quality": (
                "thumbnail"
                if source_stem.endswith(("_t", "_t_w"))
                else "enhanced"
            ),
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


@app.get("/api/chat/{talker}/sticker/{message_id}")
async def get_chat_sticker(
    talker: str,
    message_id: int,
    create_time: int = Query(..., ge=0),
    message_key: str = Query(..., min_length=1, max_length=1000),
    token: str = Query(..., min_length=64, max_length=64),
    download: bool = Query(False),
):
    """Resolve one custom sticker through an account-isolated local proxy."""
    if not _valid_sticker_token(
        token,
        config.wxid or "",
        talker,
        message_id,
        create_time,
        message_key,
        download=download,
    ):
        raise HTTPException(403, "表情访问令牌无效或已过期")
    parser = get_parser()
    message = _find_sticker_message(
        parser,
        talker,
        message_id,
        create_time,
        expected_key=message_key,
    )
    if not message:
        raise HTTPException(404, "未找到对应的表情消息")
    source = message.get("_sticker_source")
    if not isinstance(source, dict):
        raise HTTPException(404, "该表情没有可解析的资源")
    try:
        sticker = await asyncio.to_thread(
            lambda: get_sticker_service().resolve(source)
        )
    except StickerResolutionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    disposition = "attachment" if download else "inline"
    filename = f"sticker_{message_id}_{create_time}{sticker.extension}"
    return Response(
        content=sticker.data,
        media_type=sticker.mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Content-Length": str(len(sticker.data)),
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/image-key/reveal")
async def reveal_chat_image_key(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Reveal one saved account key only after an explicit local UI action."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "图片密钥读取请求缺少本机客户端标识")
    if not config.wxid:
        raise HTTPException(400, "请先选择微信账号")
    account_settings = get_account_image_settings(load_settings(), config.wxid)
    aes_key = str(account_settings.get("aes_key") or "")
    if not aes_key:
        raise HTTPException(404, "当前账号尚未保存图片 AES 密钥")
    return JSONResponse(
        {"success": True, "aes_key": aes_key},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/image-key/extract")
async def extract_chat_image_key(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Explicitly derive or scan, verify, and save one local image key."""
    global _image_key_extraction_running
    if x_wechat_assistant != "1":
        raise HTTPException(403, "图片密钥提取请求缺少本机客户端标识")
    # 注意：下面的占用检查与函数后部 `_image_key_extraction_running = True` 之间
    # 不得插入任何 await —— 事件循环单线程下两者之间没有挂起点即天然原子，
    # 并发请求无法重复通过检查。若未来需要在两者之间加 await，请改用 asyncio.Lock。
    if _image_key_extraction_running:
        raise HTTPException(409, "图片密钥正在获取中，请勿重复点击")
    if _recognition_manager.has_active_runs:
        raise HTTPException(409, "图片识别任务正在运行，请等待完成后再获取密钥")
    if _image_hd_automation_manager.has_active_runs:
        raise HTTPException(409, "批量获取高清图片任务正在运行，请先停止该任务")
    if _voice_transcription_manager.has_active_runs:
        raise HTTPException(409, "语音转文字任务正在运行，请等待完成后再获取密钥")
    if not _is_xwechat():
        raise HTTPException(501, "自动获取图片密钥仅支持微信 4.x")
    if not config.wxid or not config.msg_dir:
        raise HTTPException(400, "请先选择微信账号")

    account_id = config.wxid
    attach_dir = config.msg_dir.parent.parent / "msg" / "attach"
    if not attach_dir.is_dir():
        raise HTTPException(404, "未找到当前账号的聊天图片缓存目录")

    _image_key_extraction_running = True
    try:
        worker = asyncio.create_task(
            asyncio.to_thread(
                extract_image_aes_key,
                attach_dir,
                account_id=account_id,
                max_seconds=90,
            )
        )
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await worker
            except Exception:
                pass
            raise

        if config.wxid != account_id:
            raise HTTPException(
                409,
                "获取期间微信账号已切换，结果未保存，请在当前账号下重试",
            )

        settings = load_settings()
        all_accounts = dict(
            settings.get("images", {}).get("accounts", {}) or {}
        )
        account_settings = dict(all_accounts.get(account_id, {}) or {})
        account_settings["aes_key"] = result.aes_key
        if result.xor_key is not None:
            account_settings["xor_key"] = f"0x{result.xor_key:02x}"
        else:
            account_settings.setdefault("xor_key", "auto")
        all_accounts[account_id] = account_settings
        merged = save_settings({"images": {"accounts": all_accounts}})
        _close_runtime_caches(cancel_recognition=False)
        return {
            "success": True,
            "message": "图片 AES 密钥已获取、验证并保存",
            "data": public_settings(merged, account_id),
            "meta": {
                "verified": True,
                "verified_format": result.verified_format,
                "samples_found": result.samples_found,
                "processes_scanned": result.processes_scanned,
                "method": result.extraction_method,
            },
        }
    except ImageKeyExtractionError as exc:
        status_codes = {
            "UNSUPPORTED_PLATFORM": 501,
            "BITNESS_MISMATCH": 501,
            "NO_V2_IMAGE_CACHE": 409,
            "WECHAT_NOT_RUNNING": 409,
            "PROCESS_ACCESS_DENIED": 403,
            "SCAN_TIMEOUT": 504,
            "KEY_NOT_FOUND": 422,
        }
        raise HTTPException(status_codes.get(exc.code, 422), str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            500, f"自动获取图片密钥失败：{type(exc).__name__}"
        ) from exc
    finally:
        _image_key_extraction_running = False


@app.post("/api/chat/{talker}/images/recognize")
def recognize_chat_images(talker: str, req: ImageRecognitionRequest):
    """Create a background task after the user explicitly requests recognition."""
    if _image_key_extraction_running:
        raise HTTPException(409, "图片密钥正在获取中，请等待完成后再识别图片")
    if _recognition_manager.has_active_runs:
        raise HTTPException(409, "已有图片识别任务正在运行，请等待其完成后再试")
    if _image_hd_automation_manager.has_active_runs:
        raise HTTPException(409, "批量获取高清图片任务正在运行，请先停止该任务")
    if _voice_transcription_manager.has_active_runs:
        raise HTTPException(409, "语音转文字任务正在运行，请等待完成后再识别图片")
    if req.message_refs is not None and not req.message_refs:
        raise HTTPException(400, "所选消息范围为空")
    if req.message_ids is not None and not req.message_ids:
        raise HTTPException(400, "所选消息范围为空")
    has_bounded_scope = (
        req.message_refs is not None
        or req.message_ids is not None
        or req.start_time is not None
        or req.end_time is not None
    )
    if not has_bounded_scope and not req.all_images:
        raise HTTPException(400, "请明确选择消息、时间范围或整个聊天")
    settings = load_settings()
    vision_config = VisionConfig.from_settings(settings)
    try:
        vision_config.validate()
    except VisionAPIError as exc:
        raise HTTPException(400, str(exc)) from exc

    max_images = max(
        1,
        min(500, int(settings.get("vision", {}).get("max_images_per_task") or 50)),
    )
    parser = get_parser()
    if req.message_refs is not None:
        if len(req.message_refs) > 2000:
            raise HTTPException(400, "一次选择的消息数量过大")
        messages = []
        seen: set[str] = set()
        for reference in req.message_refs:
            message = _find_image_message(
                parser,
                talker,
                reference.id,
                reference.create_time,
                expected_key=reference.message_key,
            )
            if not message:
                continue
            key = _message_key(talker, message)
            if key in seen:
                continue
            seen.add(key)
            messages.append(message)
            if len(messages) > max_images:
                break
    else:
        messages = _collect_image_messages(
            parser,
            talker,
            message_ids=req.message_ids,
            start_time=req.start_time,
            end_time=req.end_time,
            hard_limit=max_images,
        )
    if len(messages) > max_images:
        raise HTTPException(
            400,
            f"所选范围包含超过 {max_images} 张图片。请缩小范围，或在设置中提高单次上限",
        )
    service = get_image_service()
    job = _recognition_manager.start(
        account_id=config.wxid or "",
        talker=talker,
        messages=messages,
        image_service=service,
        vision_config=vision_config,
        force=req.force,
    )
    return {"success": True, **job}


@app.get("/api/image-recognition/tasks/{task_id}")
async def get_image_recognition_task(task_id: str):
    job = _recognition_manager.get(task_id)
    if not job:
        raise HTTPException(404, "图片识别任务不存在或已过期")
    return {"success": True, **job}


@app.post("/api/chat/{talker}/images/hd-automation")
async def start_image_hd_automation(
    talker: str,
    req: ImageHDAutomationRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Prepare a bounded sequence; F9 starts only from an open WeChat viewer."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "批量获取高清图片请求缺少本机客户端标识")
    if os.name != "nt":
        raise HTTPException(501, "批量获取高清图片仅支持 Windows")
    if _image_key_extraction_running:
        raise HTTPException(409, "图片密钥正在获取中，请等待完成")
    if _recognition_manager.has_active_runs:
        raise HTTPException(409, "图片识别任务正在运行，请等待完成")
    if _voice_transcription_manager.has_active_runs:
        raise HTTPException(409, "语音转文字任务正在运行，请等待完成")
    if _image_hd_automation_manager.has_active_runs:
        raise HTTPException(409, "已有批量获取高清图片任务正在运行")
    if req.direction not in ("next", "previous"):
        raise HTTPException(400, "翻页方向必须是 next 或 previous")
    if req.message_refs is not None and not req.message_refs:
        raise HTTPException(400, "所选消息范围为空")

    start_time = req.start_time
    end_time = req.end_time
    if req.message_refs is not None:
        if len(req.message_refs) > 2000:
            raise HTTPException(400, "一次选择的消息数量过多")
        ordered_refs = sorted(
            req.message_refs,
            key=lambda reference: (int(reference.create_time), int(reference.id)),
        )
        boundary_messages = []
        for reference in (ordered_refs[0], ordered_refs[-1]):
            if not reference.message_key:
                raise HTTPException(400, "当前范围缺少精确消息标识，请刷新聊天后重试")
            boundary = _find_image_message(
                get_parser(),
                talker,
                reference.id,
                reference.create_time,
                expected_key=reference.message_key,
            )
            if not boundary:
                raise HTTPException(400, "当前范围边界与聊天记录不匹配")
            boundary_messages.append(boundary)
        start_time = min(int(item.get("create_time") or 0) for item in boundary_messages)
        end_time = max(int(item.get("create_time") or 0) for item in boundary_messages)
    if start_time is not None and end_time is not None and start_time > end_time:
        raise HTTPException(400, "开始时间不能晚于结束时间")
    if (
        req.message_refs is None
        and start_time is None
        and end_time is None
        and not req.all_images
    ):
        raise HTTPException(400, "请选择当前范围、时间范围或整个聊天")

    if not _is_xwechat():
        raise HTTPException(501, "批量获取高清图片仅支持微信 4.x")
    parser = get_parser()
    max_images = 500
    media, image_messages = await asyncio.to_thread(
        _collect_image_viewer_sequence,
        parser,
        talker,
        start_time=start_time,
        end_time=end_time,
        max_images=max_images,
    )
    if not image_messages:
        raise HTTPException(400, "所选范围内没有图片")
    if len(image_messages) > max_images:
        raise HTTPException(
            400,
            f"单次最多处理 {max_images} 张图片，请缩小日期范围后分批执行",
        )

    for message in image_messages:
        message["message_key"] = _message_key(talker, message)
    settings = load_settings()
    image_settings = get_account_image_settings(settings, config.wxid or "")
    image_aes_key = str(image_settings.get("aes_key") or "")
    if len(image_aes_key) != 16:
        raise HTTPException(
            409,
            "请先在设置中获取并保存当前账号的16位图片 AES 密钥",
        )
    if not config.wxid or not config.msg_dir or not config.key:
        raise HTTPException(400, "请先选择微信账号并设置数据库密钥")
    account_id = config.wxid
    account_root = config.msg_dir.parent.parent
    monitor = WeChatHDResourceMonitor(
        account_id=account_id,
        account_root=account_root,
        encrypted_resource_db=config.msg_dir / "message_resource.db",
        database_key=config.key,
        image_aes_key=image_aes_key,
        image_xor_key=image_settings.get("xor_key", "auto"),
    )
    try:
        targets = await asyncio.to_thread(
            monitor.prepare_targets, talker, image_messages
        )
    except ImageHDResourceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if config.wxid != account_id:
        raise HTTPException(409, "准备期间微信账号已切换，请重试")
    if len(targets) != len(image_messages):
        raise HTTPException(
            422,
            "部分图片无法与资源数据库精确关联，任务未启动",
        )
    target_by_key = {target.message_key: target for target in targets}

    ordered_media = sorted(
        media,
        key=lambda message: (
            int(message.get("create_time") or 0),
            int(message.get("id") or 0),
        ),
        reverse=req.direction == "previous",
    )
    image_positions = [
        index
        for index, message in enumerate(ordered_media)
        if _is_image_message(message)
    ]
    ordered_media = ordered_media[
        image_positions[0]:image_positions[-1] + 1
    ]
    pre_skipped = 0
    pending_start = None
    for index, message in enumerate(ordered_media):
        if not _is_image_message(message):
            continue
        key = str(message.get("message_key") or _message_key(talker, message))
        target = target_by_key.get(key)
        if target is not None and not target.initially_verified:
            pending_start = index
            break
        pre_skipped += 1
    if pending_start is not None:
        ordered_media = ordered_media[pending_start:]
    else:
        pre_skipped = 0
    sequence = []
    for message in ordered_media:
        if _is_image_message(message):
            key = str(message.get("message_key") or _message_key(talker, message))
            target = target_by_key.get(key)
            if target is None:
                raise HTTPException(422, "图片资源顺序校验失败，任务未启动")
            sequence.append({"kind": "image", "target": target})
        else:
            sequence.append({"kind": "video"})
    try:
        job = _image_hd_automation_manager.start(
            account_id=account_id,
            talker=talker,
            sequence=sequence,
            monitor=monitor,
            direction=req.direction,
            per_image_timeout=req.per_image_timeout,
            min_dwell_seconds=req.min_dwell_seconds,
            pre_skipped=pre_skipped,
            total_images=len(image_messages),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"success": True, **job}


@app.get("/api/image-hd-automation/tasks/{task_id}")
async def get_image_hd_automation_task(task_id: str):
    job = _image_hd_automation_manager.get(task_id)
    if not job:
        raise HTTPException(404, "批量高清图片任务不存在或已过期")
    return {"success": True, **job}


@app.get("/api/image-hd-automation/active")
async def get_active_image_hd_automation_task():
    job = _image_hd_automation_manager.get_active()
    return {"success": True, **(job or {"task_id": None})}


@app.post("/api/image-hd-automation/tasks/{task_id}/pause")
async def pause_image_hd_automation_task(
    task_id: str,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    if x_wechat_assistant != "1":
        raise HTTPException(403, "暂停任务请求缺少本机客户端标识")
    if not _image_hd_automation_manager.pause(task_id):
        raise HTTPException(409, "该任务当前无法暂停")
    return {"success": True, **(_image_hd_automation_manager.get(task_id) or {})}


@app.post("/api/image-hd-automation/tasks/{task_id}/navigation-mode")
async def set_image_hd_automation_navigation_mode(
    task_id: str,
    req: ImageHDNavigationModeRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    if x_wechat_assistant != "1":
        raise HTTPException(403, "切换翻页模式请求缺少本机客户端标识")
    if not _image_hd_automation_manager.get(task_id):
        raise HTTPException(404, "批量高清图片任务不存在或已过期")
    try:
        changed = _image_hd_automation_manager.set_navigation_mode(
            task_id, str(req.mode or "").strip().lower()
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not changed:
        raise HTTPException(409, "该任务当前无法切换翻页模式")
    return {"success": True, **(_image_hd_automation_manager.get(task_id) or {})}


@app.post("/api/image-hd-automation/tasks/{task_id}/cancel")
async def cancel_image_hd_automation_task(
    task_id: str,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    if x_wechat_assistant != "1":
        raise HTTPException(403, "停止任务请求缺少本机客户端标识")
    if not _image_hd_automation_manager.cancel(task_id):
        raise HTTPException(409, "该任务当前无法停止")
    return {"success": True, **(_image_hd_automation_manager.get(task_id) or {})}


@app.post("/api/chat/{talker}/voices/transcribe")
def transcribe_chat_voices(
    talker: str,
    req: VoiceTranscriptionRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Create one explicitly requested, bounded voice-transcription task."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "语音转文字请求缺少本机客户端标识")
    if _image_key_extraction_running:
        raise HTTPException(409, "图片密钥正在获取中，请等待完成后再转写语音")
    if _recognition_manager.has_active_runs:
        raise HTTPException(409, "图片识别任务正在运行，请等待完成后再转写语音")
    if _image_hd_automation_manager.has_active_runs:
        raise HTTPException(409, "批量获取高清图片任务正在运行，请先停止该任务")
    if _voice_transcription_manager.has_active_runs:
        raise HTTPException(409, "已有语音转文字任务正在运行，请等待其完成后再试")
    if req.message_refs is not None and not req.message_refs:
        raise HTTPException(400, "所选消息范围为空")
    if req.message_ids is not None and not req.message_ids:
        raise HTTPException(400, "所选消息范围为空")
    has_bounded_scope = (
        req.message_refs is not None
        or req.message_ids is not None
        or req.start_time is not None
        or req.end_time is not None
    )
    if not has_bounded_scope and not req.all_voices:
        raise HTTPException(400, "请明确选择消息、时间范围或整个聊天")

    settings = load_settings()
    transcription_config = TranscriptionConfig.from_settings(settings)
    try:
        transcription_config.validate()
    except TranscriptionAPIError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not req.cloud_upload_confirmed:
        raise HTTPException(
            400,
            "当前语音转写接口会上传所选语音；请先确认云端上传提示",
        )

    try:
        max_voices = max(
            1,
            min(
                500,
                int(
                    settings.get("transcription", {}).get(
                        "max_voices_per_task", 50
                    )
                ),
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "单次语音转写数量设置无效") from exc

    parser = get_parser()
    if req.message_refs is not None:
        if len(req.message_refs) > 2000:
            raise HTTPException(400, "一次选择的消息数量过大")
        messages = []
        seen: set[str] = set()
        for reference in req.message_refs:
            message = _find_voice_message(
                parser,
                talker,
                reference.id,
                reference.create_time,
                expected_key=reference.message_key,
            )
            if not message:
                continue
            key = _message_key(talker, message)
            if key in seen:
                continue
            seen.add(key)
            message["message_key"] = key
            messages.append(message)
            if len(messages) > max_voices:
                break
    else:
        messages = _collect_voice_messages(
            parser,
            talker,
            message_ids=req.message_ids,
            start_time=req.start_time,
            end_time=req.end_time,
            hard_limit=max_voices,
        )
    if len(messages) > max_voices:
        raise HTTPException(
            400,
            f"所选范围包含超过 {max_voices} 条语音。请缩小范围，或在设置中提高单次上限",
        )
    if not messages:
        raise HTTPException(404, "所选范围内没有可转写的语音消息")

    service = get_voice_service()
    job = _voice_transcription_manager.start(
        account_id=config.wxid or "",
        talker=talker,
        messages=messages,
        voice_service=service,
        transcription_config=transcription_config,
        force=req.force,
    )
    return {"success": True, **job}


@app.get("/api/voice-transcription/tasks/{task_id}")
async def get_voice_transcription_task(task_id: str):
    job = _voice_transcription_manager.get(task_id)
    if not job:
        raise HTTPException(404, "语音转文字任务不存在或已过期")
    return {"success": True, **job}


@app.post("/api/voice-transcription/tasks/{task_id}/cancel")
async def cancel_voice_transcription_task(
    task_id: str,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    if x_wechat_assistant != "1":
        raise HTTPException(403, "取消语音转文字请求缺少本机客户端标识")
    job = _voice_transcription_manager.get(task_id)
    if not job:
        raise HTTPException(404, "语音转文字任务不存在或已过期")
    if not _voice_transcription_manager.cancel(task_id):
        raise HTTPException(409, "语音转文字任务已结束，无法取消")
    updated = _voice_transcription_manager.get(task_id) or job
    return {"success": True, **updated}


@app.get("/api/chat/{talker}/voice/{message_id}/audio")
async def get_voice_audio(
    talker: str,
    message_id: int,
    create_time: int = Query(...),
    server_id: str = Query(""),
):
    """Stream decoded voice WAV audio for inline playback."""
    try:
        service = get_voice_service()
        # Try with server_id first, fall back without — the media database
        # may store a different server_id representation than the main DB.
        try:
            decoded = await asyncio.to_thread(
                service.decode_voice,
                talker, message_id, create_time, server_id,
            )
        except VoiceServiceError:
            decoded = await asyncio.to_thread(
                service.decode_voice,
                talker, message_id, create_time, "",
            )
    except VoiceServiceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return Response(
        content=decoded.wav_data,
        media_type="audio/wav",
        headers={
            "Content-Disposition": "inline",
            "Content-Length": str(len(decoded.wav_data)),
            "Cache-Control": "private, max-age=86400",
            "Accept-Ranges": "bytes",
        },
    )


@app.get("/api/chat/{talker}/voice/{message_id}/export")
async def export_voice_audio(
    talker: str,
    message_id: int,
    create_time: int = Query(...),
    server_id: str = Query(""),
):
    """Download decoded voice WAV as a file attachment."""
    try:
        service = get_voice_service()
        # Try with server_id first, fall back without.
        try:
            decoded = await asyncio.to_thread(
                service.decode_voice,
                talker, message_id, create_time, server_id,
            )
        except VoiceServiceError:
            decoded = await asyncio.to_thread(
                service.decode_voice,
                talker, message_id, create_time, "",
            )
    except VoiceServiceError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    filename = f"voice_{message_id}_{create_time}.wav"
    return Response(
        content=decoded.wav_data,
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(decoded.wav_data)),
        },
    )


# --- 应用设置 ---

@app.get("/api/settings")
async def get_settings():
    """获取应用设置"""
    settings = load_settings()
    return {"success": True, "data": public_settings(settings, config.wxid)}


def _reveal_saved_api_key(section: str, client_marker: Optional[str]) -> JSONResponse:
    """Return one stored API key only for an explicit request from the local UI."""
    if client_marker != "1":
        raise HTTPException(403, "API Key 读取请求缺少本机客户端标识")

    api_key = str((load_settings().get(section) or {}).get("api_key") or "")
    if not api_key:
        raise HTTPException(404, "当前尚未保存 API Key")

    return JSONResponse(
        {"success": True, "api_key": api_key},
        headers={
            "Cache-Control": (
                "no-store, no-cache, private, max-age=0, must-revalidate"
            ),
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/settings/vision/api-key/reveal")
async def reveal_vision_api_key(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Reveal the saved vision API key after the user clicks Show."""
    return _reveal_saved_api_key("vision", x_wechat_assistant)


@app.get("/api/settings/transcription/api-key/reveal")
async def reveal_transcription_api_key(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Reveal the saved transcription API key after the user clicks Show."""
    return _reveal_saved_api_key("transcription", x_wechat_assistant)


@app.get("/api/settings/analysis/api-key/reveal")
async def reveal_analysis_api_key(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Reveal the saved analysis key only after an explicit local UI click."""
    return _reveal_saved_api_key("analysis", x_wechat_assistant)


def _resolve_interface_update(
    module: str,
    incoming: dict,
    current: dict,
) -> tuple[str, str]:
    """Resolve a settings update without letting stale fields override it.

    Older clients only sent ``provider`` while newer clients send
    ``interface_preset`` as well.  When just one of those fields is supplied,
    that field is authoritative.  When both are supplied they must describe
    the same wire protocol.
    """

    provider_supplied = "provider" in incoming
    preset_supplied = "interface_preset" in incoming
    current_provider = normalize_provider(
        module, current.get("provider", "openai_compatible")
    )
    current_base_url = str(current.get("base_url") or "").strip()
    current_preset = normalize_interface_preset(
        module,
        current.get("interface_preset"),
        current_provider,
        current_base_url,
    )
    provider = normalize_provider(
        module,
        incoming.get(
            "provider", current.get("provider", "openai_compatible")
        ),
    )
    if provider_supplied and provider not in supported_providers(module):
        raise ValueError("unsupported provider")

    if preset_supplied:
        raw_preset = str(incoming.get("interface_preset") or "").strip().lower()
        if raw_preset not in supported_interface_presets(module):
            raise ValueError("unsupported preset")
        protocol = protocol_for_interface(module, raw_preset)
        if provider_supplied and provider != protocol:
            raise ValueError("preset/provider mismatch")
        return raw_preset, protocol

    if provider_supplied:
        incoming_base_url = str(
            incoming.get("base_url", current_base_url) or ""
        ).strip()
        same_protocol = (
            current_preset in supported_interface_presets(module)
            and protocol_for_interface(module, current_preset) == provider
        )
        base_unchanged = (
            incoming_base_url.rstrip("/") == current_base_url.rstrip("/")
        )
        if same_protocol and base_unchanged:
            return current_preset, provider
        preset = normalize_interface_preset(
            module, None, provider, incoming_base_url
        )
        protocol = protocol_for_interface(module, preset)
        if protocol != provider:
            raise ValueError("unsupported provider")
        return preset, protocol

    if current_preset in supported_interface_presets(module):
        return current_preset, protocol_for_interface(module, current_preset)

    if provider not in supported_providers(module):
        provider = "openai_compatible"
    preset = normalize_interface_preset(
        module, None, provider, current_base_url
    )
    return preset, protocol_for_interface(module, preset)


def _canonical_model_base_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = f":{parsed.port}" if parsed.port is not None else ""
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme.lower()}://{host}{port}{path}{query}"
    except ValueError:
        return raw.rstrip("/")


def _resolve_model_api_key(
    *,
    module: str,
    label: str,
    current: dict,
    interface_preset: str,
    base_url: object,
    supplied_api_key: str,
    clear_api_key: bool,
) -> str:
    """Never carry a credential across a service or endpoint change."""

    if supplied_api_key:
        return supplied_api_key
    if clear_api_key:
        return ""

    current_key = str(current.get("api_key") or "")
    if not current_key:
        return ""
    current_base_url = str(current.get("base_url") or "")
    current_provider = normalize_provider(
        module, current.get("provider") or "openai_compatible"
    )
    current_preset = normalize_interface_preset(
        module,
        current.get("interface_preset"),
        current_provider,
        current_base_url,
    )
    scope_changed = (
        current_preset != interface_preset
        or _canonical_model_base_url(current_base_url)
        != _canonical_model_base_url(base_url)
    )
    if scope_changed:
        raise HTTPException(
            400,
            f"更换{label}接口服务或地址时，请输入该接口的新 API Key，或明确清除旧密钥",
        )
    return current_key


@app.post("/api/settings")
async def update_settings(req: SettingsUpdate):
    """更新应用设置（只需传要修改的字段）"""
    previous_settings = load_settings()
    data = {}
    if req.export is not None:
        data["export"] = req.export
    if req.ui is not None:
        current_ui = previous_settings.get("ui", {})
        image_quality = str(
            req.ui.get("image_quality", current_ui.get("image_quality", "smart"))
        ).strip().lower()
        if image_quality not in ("smart", "high", "thumbnail"):
            raise HTTPException(400, "聊天图片质量必须是 smart、high 或 thumbnail")
        try:
            hd_timeout_value = float(
                req.ui.get(
                    "hd_automation_timeout_seconds",
                    current_ui.get("hd_automation_timeout_seconds", 10),
                )
            )
            if not hd_timeout_value.is_integer():
                raise ValueError("not an integer")
            hd_timeout_seconds = int(hd_timeout_value)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, "高清任务单张最大等待时间必须是整数秒")
        if not 5 <= hd_timeout_seconds <= 120:
            raise HTTPException(400, "高清任务单张最大等待时间必须在 5–120 秒之间")
        try:
            hd_min_dwell_seconds = float(
                req.ui.get(
                    "hd_automation_min_dwell_seconds",
                    current_ui.get("hd_automation_min_dwell_seconds", 0.5),
                )
            )
        except (TypeError, ValueError):
            raise HTTPException(400, "高清任务验证成功后的最短停留时间必须是数字")
        if not 0 <= hd_min_dwell_seconds <= 5:
            raise HTTPException(400, "高清任务验证成功后的最短停留时间必须在 0–5 秒之间")
        data["ui"] = {
            "show_chat_images": bool(
                req.ui.get(
                    "show_chat_images",
                    current_ui.get("show_chat_images", False),
                )
            ),
            "image_quality": image_quality,
            "hd_automation_timeout_seconds": hd_timeout_seconds,
            "hd_automation_min_dwell_seconds": hd_min_dwell_seconds,
        }
    if req.vision is not None:
        current = previous_settings.get("vision", {})
        incoming = dict(req.vision)
        clear_api_key = bool(incoming.pop("clear_api_key", False))
        api_key = str(incoming.pop("api_key", "") or "").strip()
        allowed = {
            "provider",
            "interface_preset",
            "custom_name",
            "custom_protocol",
            "custom_api_key_header",
            "custom_api_key_prefix",
            "custom_extra_headers",
            "custom_request_template",
            "custom_response_path",
            "base_url",
            "model",
            "image_detail",
            "timeout_seconds",
            "max_images_per_task",
            "prompt",
        }
        vision = {key: value for key, value in incoming.items() if key in allowed}
        try:
            interface_preset, provider = _resolve_interface_update(
                "vision", vision, current
            )
        except ValueError as exc:
            message = (
                "图片识别接口预设与请求协议不匹配"
                if "mismatch" in str(exc)
                else "不支持的图片识别接口预设或协议"
            )
            raise HTTPException(400, message) from exc
        vision["interface_preset"] = interface_preset
        vision["provider"] = provider
        vision["api_key"] = _resolve_model_api_key(
            module="vision",
            label="图片识别",
            current=current,
            interface_preset=interface_preset,
            base_url=vision.get("base_url", current.get("base_url", "")),
            supplied_api_key=api_key,
            clear_api_key=clear_api_key,
        )

        detail = str(vision.get("image_detail", current.get("image_detail", "low"))).lower()
        if detail not in ("low", "auto", "high"):
            raise HTTPException(400, "图片识别精度必须是 low、auto 或 high")
        vision["image_detail"] = detail
        try:
            vision["timeout_seconds"] = max(
                5,
                min(300, int(vision.get("timeout_seconds", current.get("timeout_seconds", 60)))),
            )
            vision["max_images_per_task"] = max(
                1,
                min(500, int(vision.get("max_images_per_task", current.get("max_images_per_task", 50)))),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "视觉模型超时或单次图片数设置无效") from exc
        candidate = {"vision": {**current, **vision}}
        # Validate once all required fields are present. Loopback-only local
        # model presets may intentionally omit an API key.
        candidate_config = VisionConfig.from_settings(candidate)
        if (
            candidate_config.base_url
            and candidate_config.model
            and (candidate_config.api_key or not candidate_config.requires_api_key)
        ):
            try:
                candidate_config.validate()
            except VisionAPIError as exc:
                raise HTTPException(400, str(exc)) from exc
        data["vision"] = vision

    if req.transcription is not None:
        current = previous_settings.get("transcription", {})
        incoming = dict(req.transcription)
        clear_api_key = bool(incoming.pop("clear_api_key", False))
        api_key = str(incoming.pop("api_key", "") or "").strip()
        allowed = {
            "provider",
            "interface_preset",
            "base_url",
            "model",
            "language",
            "timeout_seconds",
            "retry_count",
            "max_audio_bytes",
            "max_voices_per_task",
            "custom_name",
            "custom_api_key_header",
            "custom_api_key_prefix",
            "custom_extra_headers",
            "custom_audio_field",
            "custom_model_field",
            "custom_language_field",
            "custom_extra_form_fields",
            "custom_response_path",
            "custom_filename",
        }
        transcription = {
            key: value for key, value in incoming.items() if key in allowed
        }
        try:
            interface_preset, provider = _resolve_interface_update(
                "transcription", transcription, current
            )
        except ValueError as exc:
            message = (
                "语音转写接口预设与请求协议不匹配"
                if "mismatch" in str(exc)
                else "不支持的语音转写接口预设或协议"
            )
            raise HTTPException(400, message) from exc
        transcription["interface_preset"] = interface_preset
        transcription["provider"] = provider
        transcription["api_key"] = _resolve_model_api_key(
            module="transcription",
            label="语音转写",
            current=current,
            interface_preset=interface_preset,
            base_url=transcription.get(
                "base_url", current.get("base_url", "")
            ),
            supplied_api_key=api_key,
            clear_api_key=clear_api_key,
        )

        language = str(
            transcription.get("language", current.get("language", "auto"))
            or "auto"
        ).strip().lower()
        transcription["language"] = language
        try:
            transcription["timeout_seconds"] = max(
                5,
                min(
                    600,
                    int(
                        transcription.get(
                            "timeout_seconds", current.get("timeout_seconds", 120)
                        )
                    ),
                ),
            )
            transcription["retry_count"] = max(
                0,
                min(
                    3,
                    int(
                        transcription.get(
                            "retry_count", current.get("retry_count", 2)
                        )
                    ),
                ),
            )
            transcription["max_audio_bytes"] = max(
                1024,
                min(
                    100 * 1024 * 1024,
                    int(
                        transcription.get(
                            "max_audio_bytes",
                            current.get("max_audio_bytes", 25 * 1024 * 1024),
                        )
                    ),
                ),
            )
            transcription["max_voices_per_task"] = max(
                1,
                min(
                    500,
                    int(
                        transcription.get(
                            "max_voices_per_task",
                            current.get("max_voices_per_task", 50),
                        )
                    ),
                ),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "语音转写超时、重试、大小或数量设置无效") from exc

        candidate_transcription = {**current, **transcription}
        candidate = {"transcription": candidate_transcription}
        candidate_config = TranscriptionConfig.from_settings(candidate)
        cloud_fields_ready = bool(
            candidate_config.base_url
            and candidate_config.model
            and (candidate_config.api_key or not candidate_config.requires_api_key)
        )
        # Cloud settings are validated once all required fields are filled.
        if cloud_fields_ready:
            try:
                candidate_config.validate()
            except TranscriptionAPIError as exc:
                raise HTTPException(400, str(exc)) from exc
        data["transcription"] = transcription

    if req.analysis is not None:
        current = previous_settings.get("analysis", {})
        incoming = req.analysis.model_dump(exclude_none=True)
        clear_api_key = bool(incoming.pop("clear_api_key", False))
        api_key = str(incoming.pop("api_key", "") or "").strip()
        allowed = {
            "provider",
            "interface_preset",
            "custom_name",
            "custom_protocol",
            "custom_api_key_header",
            "custom_api_key_prefix",
            "custom_extra_headers",
            "custom_request_template",
            "custom_response_path",
            "base_url",
            "model",
            "timeout_seconds",
            "retry_count",
            "max_output_tokens",
            "temperature",
            "chunk_chars",
            "presets",
        }
        analysis = {key: value for key, value in incoming.items() if key in allowed}
        try:
            interface_preset, provider = _resolve_interface_update(
                "analysis", analysis, current
            )
        except ValueError as exc:
            message = (
                "AI 分析接口预设与请求协议不匹配"
                if "mismatch" in str(exc)
                else "不支持的 AI 分析接口预设或协议"
            )
            raise HTTPException(400, message) from exc
        analysis["interface_preset"] = interface_preset
        analysis["provider"] = provider
        analysis["api_key"] = _resolve_model_api_key(
            module="analysis",
            label="内容分析",
            current=current,
            interface_preset=interface_preset,
            base_url=analysis.get("base_url", current.get("base_url", "")),
            supplied_api_key=api_key,
            clear_api_key=clear_api_key,
        )

        try:
            analysis["timeout_seconds"] = max(
                5,
                min(
                    600,
                    int(analysis.get("timeout_seconds", current.get("timeout_seconds", 90))),
                ),
            )
            for key, default, minimum, maximum in (
                ("retry_count", 2, 0, 3),
                ("max_output_tokens", 2400, 256, 8192),
                ("chunk_chars", 0, 0, 60000),
            ):
                if key in analysis:
                    analysis[key] = max(
                        minimum,
                        min(maximum, int(analysis.get(key, current.get(key, default)))),
                    )
            if "temperature" in analysis:
                analysis["temperature"] = max(
                    0.0,
                    min(2.0, float(analysis["temperature"])),
                )
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(400, "AI 分析超时或高级参数设置无效") from exc

        raw_presets = analysis.get("presets", current.get("presets", []))
        if not isinstance(raw_presets, list) or not 1 <= len(raw_presets) <= 50:
            raise HTTPException(400, "AI 分析预设数量必须在 1–50 个之间")
        normalized_presets = []
        preset_ids: set[str] = set()
        for index, raw_preset in enumerate(raw_presets, start=1):
            if not isinstance(raw_preset, dict):
                raise HTTPException(400, f"第 {index} 个 AI 分析预设格式无效")
            preset_id = str(raw_preset.get("id") or "").strip()
            name = str(raw_preset.get("name") or "").strip()
            strength = str(raw_preset.get("strength") or "balanced").strip().lower()
            detail = str(raw_preset.get("detail") or "standard").strip().lower()
            requirements = str(raw_preset.get("requirements") or "").strip()
            if not re.fullmatch(r"[0-9A-Za-z._-]{1,100}", preset_id):
                raise HTTPException(400, f"第 {index} 个 AI 分析预设标识无效")
            if preset_id in preset_ids:
                raise HTTPException(400, "AI 分析预设标识不能重复")
            if not name or len(name) > 60 or any(ord(char) < 32 for char in name):
                raise HTTPException(400, f"第 {index} 个 AI 分析预设名称无效")
            if strength not in ("quick", "balanced", "deep"):
                raise HTTPException(400, f"第 {index} 个预设的分析强度无效")
            if detail not in ("brief", "standard", "detailed"):
                raise HTTPException(400, f"第 {index} 个预设的详细程度无效")
            if len(requirements) > 4000 or any(
                ord(char) < 32 and char not in "\r\n\t" for char in requirements
            ):
                raise HTTPException(400, f"第 {index} 个预设的分析要求无效")
            preset_ids.add(preset_id)
            normalized_presets.append({
                "id": preset_id,
                "name": name,
                "strength": strength,
                "detail": detail,
                "requirements": requirements,
            })
        analysis["presets"] = normalized_presets

        candidate_analysis = {**current, **analysis}
        candidate_config = AnalysisConfig.from_settings(
            {"analysis": candidate_analysis}
        )
        if (
            candidate_config.base_url
            and candidate_config.model
            and (candidate_config.api_key or not candidate_config.requires_api_key)
        ):
            try:
                candidate_config.validate()
            except AIAnalysisError as exc:
                raise HTTPException(400, str(exc)) from exc
        data["analysis"] = analysis

    if req.images is not None:
        if not config.wxid:
            raise HTTPException(400, "请先选择微信账号，再保存图片密钥")
        settings = previous_settings
        all_accounts = dict(settings.get("images", {}).get("accounts", {}) or {})
        account_settings = dict(all_accounts.get(config.wxid, {}) or {})
        incoming = dict(req.images)
        clear_aes_key = bool(incoming.get("clear_aes_key", False))
        aes_key = str(incoming.get("aes_key") or "").strip()
        if aes_key:
            try:
                aes_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise HTTPException(400, "图片 AES key 必须是 ASCII 字符") from exc
            if len(aes_key) != 16:
                raise HTTPException(400, "图片 AES key 必须是 16 个 ASCII 字符")
            account_settings["aes_key"] = aes_key
        elif clear_aes_key:
            account_settings["aes_key"] = ""

        xor_value = str(incoming.get("xor_key", account_settings.get("xor_key", "auto"))).strip()
        if xor_value.lower() != "auto":
            try:
                parsed_xor = int(xor_value, 0)
            except ValueError as exc:
                raise HTTPException(400, "图片 XOR key 应为 auto、十进制或 0x 十六进制") from exc
            if not 0 <= parsed_xor <= 0xFF:
                raise HTTPException(400, "图片 XOR key 必须在 0x00 到 0xFF 之间")
            xor_value = f"0x{parsed_xor:02x}"
        else:
            xor_value = "auto"
        account_settings["xor_key"] = xor_value
        all_accounts[config.wxid] = account_settings
        data["images"] = {"accounts": all_accounts}
    if req.about is not None:
        incoming_about = dict(req.about)
        limits = {
            "project_name": 80,
            "version": 32,
            "description": 2000,
            "tutorial_url": 2048,
            "qq_group": 80,
            "author": 100,
            "contact": 500,
        }
        unknown = set(incoming_about) - set(limits)
        if unknown:
            raise HTTPException(400, "关于页面包含不支持的配置项")
        about = {}
        for field, limit in limits.items():
            if field not in incoming_about:
                continue
            value = str(incoming_about.get(field) or "").strip()
            if len(value) > limit or any(
                ord(char) < 32 and char not in "\r\n\t" for char in value
            ):
                raise HTTPException(400, f"关于页面的 {field} 内容无效或过长")
            about[field] = value
        tutorial_url = about.get("tutorial_url")
        if tutorial_url:
            parsed_tutorial = urlsplit(tutorial_url)
            if (
                parsed_tutorial.scheme.lower() not in ("http", "https")
                or not parsed_tutorial.hostname
                or parsed_tutorial.username
                or parsed_tutorial.password
            ):
                raise HTTPException(400, "教程地址必须是有效的 HTTP 或 HTTPS 链接")
        data["about"] = about
    if req.advanced is not None:
        data["advanced"] = req.advanced
    merged = save_settings(data)
    runtime_settings_changed = (
        merged.get("images") != previous_settings.get("images")
        or merged.get("vision") != previous_settings.get("vision")
        or merged.get("transcription") != previous_settings.get("transcription")
    )
    if runtime_settings_changed:
        _close_runtime_caches(cancel_recognition=True)
    return {"success": True, "data": public_settings(merged, config.wxid)}


@app.post("/api/select-folder")
async def select_folder():
    """打开原生文件夹选择对话框，返回选中的路径"""

    def choose_folder() -> Optional[str]:
        # Running ``sys.executable -c`` starts another copy of a frozen
        # sidecar instead of Python.  Create the fallback dialog in-process;
        # Electron can replace this endpoint with its native dialog bridge.
        root = None
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择导出文件保存位置")
            return str(selected).replace("\\", "/") if selected else None
        except Exception:
            return None
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass

    selected = await asyncio.to_thread(choose_folder)
    return {"success": True, "path": selected}


@app.get("/api/moments/contacts")
def get_moments_contacts(
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """List only authors who have posts in the local Moments snapshot."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "朋友圈读取请求缺少本机客户端标识")
    service = get_moments_service()
    try:
        contacts = [dict(item) for item in service.get_contacts()]
        for contact in contacts:
            contact["avatar_url"] = _avatar_public_url(
                str(contact.get("username") or "")
            )
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(
        {
            "success": True,
            "data": {
                "contacts": contacts,
                "total_contacts": len(contacts),
                "total_posts": sum(
                    int(contact.get("post_count") or 0) for contact in contacts
                ),
                "parse_failures": int(getattr(service, "parse_failures", 0)),
                "scope": "local_cache",
            },
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _moments_preview_post(post: dict) -> dict:
    """Project a post into the local preview contract without CDN secrets."""

    def text(value, limit: int) -> tuple[str, bool]:
        content = "" if value is None else str(value)
        return content[:limit], len(content) > limit

    content, content_truncated = text(post.get("content"), 4000)
    title, _ = text(post.get("title"), 1000)
    description, description_truncated = text(post.get("description"), 2000)
    comments = list(post.get("comments") or [])
    preview_comments = []
    for comment in comments:
        interaction_type = int(comment.get("type") or 0)
        if interaction_type not in (1, 2):
            # The preview has two deliberately distinct regions. Unknown
            # notification types must not be presented as ordinary comments.
            continue
        comment_text, comment_truncated = text(comment.get("content"), 500)
        preview_comments.append({
            "type": interaction_type,
            "type_name": str(comment.get("type_name") or ""),
            "create_time": int(comment.get("create_time") or 0),
            "create_time_str": str(comment.get("create_time_str") or ""),
            "from_name": str(
                comment.get("from_display_name")
                or comment.get("from_nickname")
                or comment.get("from_username")
                or ""
            ),
            "to_name": str(
                comment.get("to_display_name")
                or comment.get("to_nickname")
                or comment.get("to_username")
                or ""
            ),
            "content": comment_text,
            "content_truncated": comment_truncated,
        })

    media = []
    media_versions = []
    for index, item in enumerate(list(post.get("media") or [])[:20]):
        media_type = str(item.get("type") or "")
        local_media = item.get("local_media") or {}
        status = str(local_media.get("status") or "").strip().lower()
        if status not in ("ready", "partial", "unbound", "unsupported"):
            status = "unbound" if media_type in ("", "2") else "unsupported"
        version = str(local_media.get("version") or "")
        if version:
            media_versions.append(f"{index}:{version}")
        media.append({
            "index": index,
            "type": media_type,
            "sub_type": str(item.get("sub_type") or ""),
            "width": str(item.get("width") or ""),
            "height": str(item.get("height") or ""),
            "video_duration": str(item.get("video_duration") or ""),
            "status": status,
            "quality": str(local_media.get("quality") or "unknown"),
            "mime_type": str(local_media.get("mime_type") or ""),
            "loaded_width": int(local_media.get("width") or 0),
            "loaded_height": int(local_media.get("height") or 0),
            "size": int(local_media.get("size") or 0),
            "version": version,
            "reason": str(local_media.get("reason") or ""),
        })
    location = post.get("location") or {}
    likes_count = sum(1 for item in comments if int(item.get("type") or 0) == 1)
    replies_count = sum(1 for item in comments if int(item.get("type") or 0) == 2)
    return {
        "tid": str(post.get("tid") or ""),
        "username": str(post.get("username") or ""),
        "create_time": int(post.get("create_time") or 0),
        "create_time_str": str(post.get("create_time_str") or ""),
        "content_type": int(post.get("content_type") or 0),
        "content_type_name": str(post.get("content_type_name") or ""),
        "content": content,
        "content_truncated": content_truncated,
        "title": title,
        "description": description,
        "description_truncated": description_truncated,
        "is_private": bool(post.get("is_private")),
        "is_top": bool(post.get("is_top")),
        "location": {
            "poi_name": str(location.get("poi_name") or ""),
            "city": str(location.get("city") or ""),
            "country": str(location.get("country") or ""),
        },
        "media": media,
        "media_count": len(post.get("media") or []),
        "media_ready_count": sum(
            1 for item in media if item.get("status") == "ready"
        ),
        "media_refresh_token": "|".join(media_versions),
        "has_finder_feed": bool(post.get("finder_feed")),
        "likes_count": likes_count,
        "comments_count": replies_count,
        "interactions": preview_comments,
        "interactions_total": len(preview_comments),
        "interactions_truncated": False,
    }


def _load_moments_preview_media_sync(
    resolver: MomentsMediaResolver,
    posts: list[dict],
    *,
    process_all: bool = False,
) -> dict:
    """Load exact media references without guessing cache associations.

    The legacy preview-page mode keeps the original one-batch limit.  Complete
    contact mode walks every unresolved target in fixed batches, giving each
    batch a fresh downloader budget so an early group of expired URLs cannot
    prevent later media from being attempted.
    """

    if process_all and len(posts) > MOMENTS_ALL_MEDIA_MAX_POSTS:
        raise MomentsMediaError(
            f"该联系人朋友圈超过 {MOMENTS_ALL_MEDIA_MAX_POSTS} 条，无法一次加载"
        )

    scan_payload = {
        "files_seen": 0,
        "files_decoded": 0,
        "partial_files": 0,
        "unmatched_files": 0,
        "exact_content_matches": 0,
        "media_bound": 0,
    }
    scan_warning = ""
    try:
        cache_files = resolver.discover_cache_files()
        scan_payload = resolver.scan_exact_cache_files(
            posts, cache_files
        ).to_dict()
    except (MomentsMediaError, OSError):
        # Remote URLs are tied to a specific TimelineObject/media index and
        # remain safe to try even when an old local cache file is unreadable.
        scan_warning = "local_cache_scan_failed"

    hydrated_before = resolver.hydrate_posts(posts)
    ready_before = sum(
        1
        for post in hydrated_before
        for media in post.get("media") or []
        if (media.get("local_media") or {}).get("status") == "ready"
    )
    targets: list[tuple[str, int, dict]] = []
    unsupported = 0
    no_source = 0
    for post in hydrated_before:
        tid = str(post.get("tid") or "")
        for index, media in enumerate(post.get("media") or []):
            media_type = str(media.get("type") or "")
            if media_type not in ("", "2"):
                unsupported += 1
                continue
            if (media.get("local_media") or {}).get("status") == "ready":
                continue
            if not moments_media_download_candidates(media):
                no_source += 1
                continue
            targets.append((tid, index, media))

    if process_all and len(targets) > MOMENTS_ALL_MEDIA_MAX_ITEMS:
        raise MomentsMediaError(
            f"该联系人有超过 {MOMENTS_ALL_MEDIA_MAX_ITEMS} 项未加载媒体，"
            "请先在微信中加载一部分后再重试"
        )
    selected_targets = (
        targets if process_all else targets[:MOMENTS_PREVIEW_MEDIA_MAX_ITEMS]
    )
    truncated = max(0, len(targets) - len(selected_targets))
    loaded = 0
    failed = 0
    expired = 0
    attempted_urls = 0
    for batch_start in range(0, len(selected_targets), MOMENTS_PREVIEW_MEDIA_MAX_ITEMS):
        batch = selected_targets[
            batch_start:batch_start + MOMENTS_PREVIEW_MEDIA_MAX_ITEMS
        ]
        downloader = SafeMomentsMediaDownloader()
        downloader.reset_budget()
        url_cache: dict[
            tuple[str, str, str, str], tuple[Optional[DownloadedMedia], bool]
        ] = {}
        for tid, index, media in batch:
            resolved = False
            saw_expired = False
            references = moments_media_download_candidates(media)
            for reference in references:
                if reference in url_cache:
                    asset, reference_expired = url_cache[reference]
                    saw_expired = saw_expired or reference_expired
                else:
                    attempted_urls += 1
                    reference_expired = False
                    try:
                        url, token, key, enc_idx = reference
                        reference_download = getattr(
                            downloader, "download_reference", None
                        )
                        if callable(reference_download):
                            downloaded = reference_download(
                                url,
                                token=token,
                                key=key,
                                enc_idx=enc_idx,
                            )
                        else:
                            downloaded = downloader.download(url)
                        if not isinstance(downloaded, DownloadedMedia):
                            raise MomentsMediaDownloadError(
                                "媒体下载器返回了无效结果"
                            )
                        asset = downloaded
                    except Exception as exc:
                        asset = None
                        message = str(exc).casefold()
                        reference_expired = bool(
                            re.search(r"http\s+(?:403|404|410)\b", message)
                            or "过期" in message
                            or "expired" in message
                        )
                    url_cache[reference] = (asset, reference_expired)
                    saw_expired = saw_expired or reference_expired
                if asset is None:
                    continue
                try:
                    resolver.store.save_bytes(
                        tid,
                        index,
                        asset.data,
                        expected_width=media.get("width"),
                        expected_height=media.get("height"),
                    )
                except (MomentsMediaError, OSError, ValueError):
                    continue
                loaded += 1
                resolved = True
                break
            if not resolved:
                if saw_expired:
                    expired += 1
                else:
                    failed += 1

    hydrated_after = resolver.hydrate_posts(posts)
    ready_after = sum(
        1
        for post in hydrated_after
        for media in post.get("media") or []
        if (media.get("local_media") or {}).get("status") == "ready"
    )
    remaining = sum(
        1
        for post in hydrated_after
        for media in post.get("media") or []
        if str(media.get("type") or "") in ("", "2")
        and (media.get("local_media") or {}).get("status") != "ready"
    )
    return {
        "posts": len(posts),
        "ready_before": ready_before,
        "ready_after": ready_after,
        "loaded": loaded,
        "failed": failed,
        "expired": expired,
        "remaining": remaining,
        "no_source": no_source,
        "unsupported": unsupported,
        "truncated": truncated,
        "attempted_urls": attempted_urls,
        "local_scan": scan_payload,
        "scan_warning": scan_warning,
    }


@app.post("/api/moments/preview")
def preview_moments(
    req: MomentsPreviewRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Preview one contact's local Moments snapshot without network access."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "朋友圈预览请求缺少本机客户端标识")
    username = str(req.username or "").strip()
    keyword = str(req.keyword or "").strip()
    if not username:
        raise HTTPException(400, "请选择要预览的朋友圈联系人")
    if req.start_time is not None and req.end_time is not None:
        if req.start_time > req.end_time:
            raise HTTPException(400, "开始时间不能晚于结束时间")

    service = get_moments_service()
    try:
        contacts = service.get_contacts()
        contact = next(
            (
                item for item in contacts
                if str(item.get("username") or "") == username
            ),
            None,
        )
        if contact is None:
            raise HTTPException(404, "未找到该联系人的本地朋友圈动态")
        contact = dict(contact)
        contact["avatar_url"] = _avatar_public_url(username)
        page_data = service.get_posts_page(
            username,
            page=req.page,
            page_size=req.page_size,
            start_time=req.start_time,
            end_time=req.end_time,
            keyword=keyword,
        )
    except HTTPException:
        raise
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc

    raw_pinned_posts = list(page_data.get("pinned_posts", []))
    raw_posts = list(page_data["posts"])
    media_warning = ""
    try:
        resolver = get_moments_media_resolver()
        hydrated = resolver.hydrate_posts(raw_pinned_posts + raw_posts)
        raw_pinned_posts = hydrated[:len(raw_pinned_posts)]
        raw_posts = hydrated[len(raw_pinned_posts):]
    except (HTTPException, MomentsMediaError, OSError):
        # Text/comments must remain previewable even if the app-specific media
        # manifest cannot be opened. The projection will mark these as unbound.
        media_warning = "朋友圈媒体缓存状态暂时无法读取"

    posts = [_moments_preview_post(post) for post in raw_posts]
    pinned_posts = [_moments_preview_post(post) for post in raw_pinned_posts]
    for post in pinned_posts + posts:
        post["avatar_url"] = _avatar_public_url(
            str(post.get("username") or username)
        )
    return JSONResponse(
        {
            "success": True,
            "data": {
                "contact": contact,
                "pinned_posts": pinned_posts,
                "pinned_total": int(page_data.get("pinned_total") or 0),
                "unavailable_pinned_total": int(
                    page_data.get("unavailable_pinned_total") or 0
                ),
                "posts": posts,
                "pagination": {
                    key: page_data[key]
                    for key in (
                        "page",
                        "page_size",
                        "total",
                        "total_pages",
                        "has_previous",
                        "has_next",
                    )
                },
                "scope": "local_cache",
                "keyword": keyword,
                "media_warning": media_warning,
                "parse_failures": int(getattr(service, "parse_failures", 0)),
            },
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/moments/media/{tid}/{media_index}")
async def get_moments_media(tid: str, media_index: int):
    """Return one validated, account-scoped Moments image without networking."""

    service = get_moments_service()
    try:
        reference = service.get_media_item(tid, media_index)
    except MomentsSelectionError as exc:
        raise HTTPException(404, str(exc)) from exc
    media_type = str((reference.get("media") or {}).get("type") or "")
    if media_type not in ("", "2"):
        raise HTTPException(415, "该朋友圈媒体不是可预览的图片")

    resolver = get_moments_media_resolver()
    try:
        status, payload = await asyncio.to_thread(
            resolver.get_bytes, tid, media_index
        )
    except MomentsMediaNotFound as exc:
        raise HTTPException(404, "该朋友圈图片尚未加载") from exc
    except MomentsMediaValidationError as exc:
        raise HTTPException(422, "该朋友圈图片缓存校验失败") from exc
    except MomentsMediaError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(
        content=payload,
        media_type=status.mime_type or "application/octet-stream",
        headers={
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store, no-cache, private, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Moments-Media-Quality": status.quality,
        },
    )


@app.post("/api/moments/media/load")
async def load_moments_media(
    req: MomentsMediaLoadRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Explicitly load selected preview media or one contact's complete feed."""

    if x_wechat_assistant != "1":
        raise HTTPException(403, "朋友圈媒体加载请求缺少本机客户端标识")
    username = str(req.username or "").strip()
    if req.all_posts and req.tids:
        raise HTTPException(400, "加载全部媒体时不能同时指定朋友圈动态")
    tids: Optional[list[str]] = None
    if not req.all_posts:
        try:
            tids = MomentsService._normalize_tids(req.tids)
        except MomentsSelectionError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not tids:
            raise HTTPException(400, "当前页没有可加载媒体的朋友圈动态")
        tids = list(dict.fromkeys(tids))
        if len(tids) > MOMENTS_PREVIEW_MEDIA_MAX_POSTS:
            raise HTTPException(
                400,
                f"一次最多加载 {MOMENTS_PREVIEW_MEDIA_MAX_POSTS} 条朋友圈动态",
            )

    service = get_moments_service()
    try:
        posts = service.get_posts([username], tids=tids)
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc

    resolver = get_moments_media_resolver()
    try:
        if req.all_posts:
            result = await asyncio.to_thread(
                _load_moments_preview_media_sync,
                resolver,
                posts,
                process_all=True,
            )
        else:
            result = await asyncio.to_thread(
                _load_moments_preview_media_sync,
                resolver,
                posts,
            )
    except MomentsMediaError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(
        {"success": True, "data": result},
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/analysis/chat/{talker}")
async def analyze_chat_records(
    talker: str,
    req: ChatAnalysisRequest,
    request: Request,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Analyze one explicit chat scope and save a downloadable Markdown report."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "聊天 AI 分析请求缺少本机客户端标识")
    if not req.cloud_upload_confirmed:
        raise HTTPException(400, "请先确认允许将所选聊天文本发送到已配置的模型接口")
    if req.start_time is not None and req.end_time is not None:
        if req.start_time > req.end_time:
            raise HTTPException(400, "开始时间不能晚于结束时间")
    has_references = req.message_refs is not None
    has_dates = req.start_time is not None or req.end_time is not None
    scope_count = int(has_references) + int(has_dates) + int(bool(req.all_messages))
    if scope_count != 1:
        raise HTTPException(400, "请且仅请选择已选消息、日期范围或整个聊天中的一种范围")

    settings = load_settings()
    (
        analysis_config,
        strength,
        detail,
        requirements,
        preset_name,
        preset_id,
    ) = _resolve_analysis_options(req, settings)
    parser = get_parser()
    messages = await asyncio.to_thread(
        _collect_chat_analysis_messages,
        parser,
        talker,
        message_refs=req.message_refs if has_references else None,
        start_time=req.start_time,
        end_time=req.end_time,
    )
    if not messages:
        raise HTTPException(404, "所选范围内没有可分析的聊天记录")
    records = await asyncio.to_thread(
        _chat_records_for_analysis, messages, peer_name=req.display_name
    )
    report_title = str(req.report_title or "").strip() or (
        f"{req.display_name}聊天记录 AI 分析报告"
    )
    reserved_destination = _preflight_analysis_report(report_title)
    source_metadata = {
        "record_count": len(records),
        "scope": "selected" if has_references else ("date" if has_dates else "all"),
        "start_time": req.start_time,
        "end_time": req.end_time,
    }
    try:
        result = await _run_analysis_with_limits(
            request,
            records,
            analysis_config,
            source_type="chat",
            title=report_title,
            strength=strength,
            detail_level=detail,
            custom_requirements=requirements,
            source_metadata=source_metadata,
        )
    except AIAnalysisCancelled as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(409, str(exc)) from exc
    except AIAnalysisDeadlineExceeded as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(504, str(exc)) from exc
    except AIAnalysisError as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(_analysis_error_status(str(exc)), str(exc)) from exc
    except BaseException:
        _discard_analysis_report_reservation(reserved_destination)
        raise
    destination, save_warning = _finish_analysis_report(
        result.markdown,
        report_title,
        reserved_destination,
    )
    metadata = dict(result.metadata)
    metadata.update({
        "preset_id": preset_id,
        "preset_name": preset_name,
        "selected_records": len(records),
    })
    return {
        "success": True,
        "report_markdown": result.markdown,
        "report_title": report_title,
        "path": str(destination) if destination is not None else "",
        "filename": destination.name if destination is not None else "",
        "warning": save_warning,
        "metadata": metadata,
    }


@app.post("/api/analysis/moments")
async def analyze_moments_records(
    req: MomentsAnalysisRequest,
    request: Request,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Analyze an explicit Moments selection without uploading media files."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "朋友圈 AI 分析请求缺少本机客户端标识")
    if not req.cloud_upload_confirmed:
        raise HTTPException(400, "请先确认允许将所选朋友圈文本发送到已配置的模型接口")
    if req.start_time is not None and req.end_time is not None:
        if req.start_time > req.end_time:
            raise HTTPException(400, "开始时间不能晚于结束时间")
    try:
        tids = MomentsService._normalize_tids(req.tids)
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if tids is not None and (req.start_time is not None or req.end_time is not None):
        raise HTTPException(400, "日期范围和逐条朋友圈范围不能同时使用")

    usernames = list(dict.fromkeys(
        str(username or "").strip() for username in (req.usernames or [])
    ))
    if not usernames or any(not username for username in usernames):
        raise HTTPException(400, "请至少选择一位朋友圈联系人")
    if len(usernames) > 500 or any(len(username) > 256 for username in usernames):
        raise HTTPException(400, "一次最多分析 500 位有效的朋友圈联系人")

    settings = load_settings()
    (
        analysis_config,
        strength,
        detail,
        requirements,
        preset_name,
        preset_id,
    ) = _resolve_analysis_options(req, settings)
    service = get_moments_service()
    try:
        contacts = await asyncio.to_thread(service.get_contacts)
        posts = await asyncio.to_thread(
            service.get_posts,
            usernames,
            start_time=req.start_time,
            end_time=req.end_time,
            tids=tids,
        )
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not posts:
        raise HTTPException(404, "所选范围内没有可分析的朋友圈内容")
    if len(posts) > AI_ANALYSIS_MAX_MOMENTS_POSTS:
        raise HTTPException(
            400,
            f"所选范围包含 {len(posts)} 条朋友圈，单次最多分析 "
            f"{AI_ANALYSIS_MAX_MOMENTS_POSTS} 条，请缩小范围",
        )
    selected_contacts = [
        item for item in contacts
        if str(item.get("username") or "") in set(usernames)
    ]
    records = _moments_records_for_analysis(posts, contacts)
    default_title = (
        f"{selected_contacts[0].get('display_name') or selected_contacts[0].get('nickname') or '联系人'}"
        "朋友圈 AI 分析报告"
        if len(selected_contacts) == 1
        else f"{len(usernames)}位联系人朋友圈 AI 分析报告"
    )
    report_title = str(req.report_title or "").strip() or default_title
    reserved_destination = _preflight_analysis_report(report_title)
    source_metadata = {
        "record_count": len(records),
        "contact_count": len(usernames),
        "scope": "selected" if tids is not None else (
            "date" if req.start_time is not None or req.end_time is not None else "all"
        ),
        "start_time": req.start_time,
        "end_time": req.end_time,
    }
    try:
        result = await _run_analysis_with_limits(
            request,
            records,
            analysis_config,
            source_type="moments",
            title=report_title,
            strength=strength,
            detail_level=detail,
            custom_requirements=requirements,
            source_metadata=source_metadata,
        )
    except AIAnalysisCancelled as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(409, str(exc)) from exc
    except AIAnalysisDeadlineExceeded as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(504, str(exc)) from exc
    except AIAnalysisError as exc:
        _discard_analysis_report_reservation(reserved_destination)
        raise HTTPException(_analysis_error_status(str(exc)), str(exc)) from exc
    except BaseException:
        _discard_analysis_report_reservation(reserved_destination)
        raise
    destination, save_warning = _finish_analysis_report(
        result.markdown,
        report_title,
        reserved_destination,
    )
    metadata = dict(result.metadata)
    metadata.update({
        "preset_id": preset_id,
        "preset_name": preset_name,
        "selected_posts": len(records),
        "selected_contacts": len(usernames),
    })
    return {
        "success": True,
        "report_markdown": result.markdown,
        "report_title": report_title,
        "path": str(destination) if destination is not None else "",
        "filename": destination.name if destination is not None else "",
        "warning": save_warning,
        "metadata": metadata,
    }


@app.post("/api/moments/export")
async def export_moments(
    req: MomentsExportRequest,
    x_wechat_assistant: Optional[str] = Header(
        None, alias="X-Wechat-Assistant"
    ),
):
    """Export an explicit local-cache author selection to the configured folder."""
    if x_wechat_assistant != "1":
        raise HTTPException(403, "朋友圈导出请求缺少本机客户端标识")

    fmt = str(req.format or "").strip().lower()
    if fmt not in ("html", "json", "csv", "txt"):
        raise HTTPException(400, "朋友圈导出格式必须是 HTML、JSON、CSV 或 TXT")
    if req.start_time is not None and req.end_time is not None:
        if req.start_time > req.end_time:
            raise HTTPException(400, "开始时间不能晚于结束时间")

    try:
        tids = MomentsService._normalize_tids(req.tids)
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    if tids is not None and (
        req.start_time is not None or req.end_time is not None
    ):
        raise HTTPException(400, "日期范围和逐条动态范围不能同时使用")

    usernames = list(dict.fromkeys(
        str(username or "").strip() for username in (req.usernames or [])
    ))
    if not usernames or any(not username for username in usernames):
        raise HTTPException(400, "请至少选择一位朋友圈联系人")
    if len(usernames) > 500:
        raise HTTPException(400, "一次最多导出 500 位朋友圈联系人")
    if any(len(username) > 256 for username in usernames):
        raise HTTPException(400, "朋友圈联系人标识长度无效")
    if req.download_media and fmt != "html":
        raise HTTPException(400, "联网下载朋友圈图片仅支持 HTML 导出")

    service = get_moments_service()
    try:
        contacts_snapshot = await asyncio.to_thread(service.get_contacts)
        posts_snapshot = await asyncio.to_thread(
            service.get_posts,
            usernames,
            start_time=req.start_time,
            end_time=req.end_time,
            tids=tids,
        )
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc
    export_snapshot = _MomentsExportSnapshot(
        contacts_snapshot, posts_snapshot
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"wechat_moments_export_{os.getpid()}_"))
    try:
        local_media_resolver = None
        if fmt == "html":
            try:
                local_media_resolver = get_moments_media_resolver()
            except (HTTPException, MomentsMediaError, OSError):
                # Exporting text remains useful even when the optional local
                # media store is unavailable or contains a stale binding.
                local_media_resolver = None
        exporter = MomentsExporter(
            export_snapshot,
            tmp_dir,
            local_media_resolver=local_media_resolver,
        )
        result = await asyncio.to_thread(
            exporter.export,
            usernames,
            fmt=fmt,
            tids=tids,
            filename=req.filename,
            download_media=bool(req.download_media),
        )
        source_path = Path(result.path).resolve()
        tmp_root = tmp_dir.resolve()
        try:
            source_path.relative_to(tmp_root)
        except ValueError as exc:
            raise HTTPException(500, "朋友圈导出器返回了无效文件路径") from exc
        if not source_path.is_file():
            raise HTTPException(500, "朋友圈导出文件生成失败")

        user_export_dir = get_export_dir()
        dest_path = user_export_dir / source_path.name
        if dest_path.exists():
            base_stem = dest_path.stem
            suffix = dest_path.suffix
            counter = 1
            while dest_path.exists():
                dest_path = user_export_dir / f"{base_stem}({counter}){suffix}"
                counter += 1
        shutil.move(str(source_path), str(dest_path))

        payload = result.to_dict()
        payload.update({
            "success": True,
            "path": str(dest_path),
            "filename": dest_path.name,
            "posts_count": int(result.post_count),
            "contacts_count": int(result.contact_count),
            "parse_failures": int(getattr(service, "parse_failures", 0)),
        })
        print(
            f"[Moments export] saved {result.contact_count} contact(s) and "
            f"{result.post_count} post(s)"
        )
        return payload
    except MomentsSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except MomentsSchemaError as exc:
        raise HTTPException(422, str(exc)) from exc
    except MomentsError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _move_export_file(source: Path, filename: str) -> Path:
    """Move one generated file into the configured absolute export folder."""

    safe_filename = Path(str(filename or "")).name
    if not safe_filename or safe_filename != str(filename or ""):
        raise HTTPException(500, "导出器返回了无效文件名")
    source_path = Path(source)
    if not source_path.is_file():
        raise HTTPException(500, "导出文件生成失败")
    user_export_dir = get_export_dir()
    destination = user_export_dir / safe_filename
    stem = destination.stem
    suffix = destination.suffix
    counter = 1
    while destination.exists():
        destination = user_export_dir / f"{stem}({counter}){suffix}"
        counter += 1
    shutil.move(str(source_path), str(destination))
    return destination


# 同步重型路由：导出可能耗时数分钟，必须在线程池执行而非事件循环上。
@app.post("/api/export")
def export_chat(req: ExportRequest):
    """导出单个聊天 — 直接保存到用户配置的导出目录"""
    fmt = str(req.format or "").strip().lower()
    if fmt not in ("html", "json", "csv", "txt"):
        raise HTTPException(400, "聊天导出格式必须是 HTML、JSON、CSV 或 TXT")
    parser = get_parser()

    # 生成文件名: 优先自定义，否则 时间范围_联系人名
    if req.filename:
        download_name = _safe_filename(req.filename) + f".{fmt}"
    else:
        safe_name = _safe_filename(req.display_name)
        start_ts = req.start_time
        end_ts = req.end_time

        if start_ts is None and end_ts is None and req.message_refs:
            msg_times = [ref.create_time for ref in req.message_refs if ref.create_time]
            if msg_times:
                start_ts = min(msg_times)
                end_ts = max(msg_times)
        elif (
            start_ts is None
            and end_ts is None
            and req.message_ids
        ):
            try:
                msg_result = parser.get_messages(
                    req.talker, page=1, page_size=len(req.message_ids),
                    message_ids=req.message_ids,
                )
                msg_times = [m["create_time"] for m in msg_result["messages"] if m.get("create_time")]
                if msg_times:
                    start_ts = min(msg_times)
                    end_ts = max(msg_times)
            except Exception:
                pass

        time_part = ""
        if start_ts is not None and end_ts is not None:
            try:
                s = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
                e = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError) as exc:
                raise HTTPException(400, "聊天导出时间范围无效") from exc
            time_part = f"{s}至{e}_"
        elif start_ts is not None:
            try:
                s = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError) as exc:
                raise HTTPException(400, "聊天导出开始时间无效") from exc
            time_part = f"{s}起_"
        elif end_ts is not None:
            try:
                e = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError) as exc:
                raise HTTPException(400, "聊天导出结束时间无效") from exc
            time_part = f"至{e}_"
        else:
            try:
                msg_result = parser.get_messages(req.talker, page=1, page_size=1)
                if msg_result["total"] > 0:
                    first_messages = msg_result["messages"]
                    first_time = first_messages[0]["create_time"] if first_messages else None
                    last_page_num = msg_result["total_pages"]
                    last_page = parser.get_messages(req.talker, page=last_page_num, page_size=1)
                    last_time = last_page["messages"][0]["create_time"] if last_page["messages"] else None
                    if first_time and last_time:
                        start_ts = first_time
                        end_ts = last_time
            except Exception:
                pass
            if start_ts is not None and end_ts is not None:
                s = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
                e = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")
                time_part = f"{s}至{e}_"
            else:
                time_part = datetime.now().strftime("%Y-%m-%d_")
        download_name = f"{time_part}{safe_name}.{fmt}"

    if req.html_image_quality not in ("thumbnail", "best"):
        raise HTTPException(400, "HTML 图片清晰度必须是 thumbnail 或 best")
    # 先在临时目录生成文件，再移动到用户配置的导出目录
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"wechat_export_{os.getpid()}_"))
    export_image_service = None
    if fmt == "html" and req.embed_images and not req.replace_images_with_descriptions:
        try:
            export_image_service = get_image_service()
        except Exception:
            export_image_service = None
    exporter = ChatExporter(
        parser,
        tmp_dir,
        description_store=_image_description_store,
        voice_store=_voice_transcription_store,
        account_id=config.wxid or "",
        image_service=export_image_service,
    )
    try:
        tmp_filepath = exporter.export_chat(
            req.talker, req.display_name, fmt=fmt,
            start_time=req.start_time, end_time=req.end_time,
            message_ids=req.message_ids,
            message_refs=[
                reference.model_dump() if hasattr(reference, "model_dump") else reference.dict()
                for reference in (req.message_refs or [])
            ] or None,
            replace_images_with_descriptions=req.replace_images_with_descriptions,
            replace_voices_with_transcriptions=req.replace_voices_with_transcriptions,
            embed_images=req.embed_images,
            html_image_quality=req.html_image_quality,
        )
        dest_path = _move_export_file(tmp_filepath, download_name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    final_path = str(dest_path)
    print(f"[导出] 已保存到: {final_path}")
    return {"success": True, "path": final_path, "filename": dest_path.name}


@app.post("/api/export-all")
def export_all(
    fmt: str = "html",
    replace_images_with_descriptions: bool = False,
    replace_voices_with_transcriptions: bool = False,
    embed_images: bool = True,
    html_image_quality: str = "best",
):
    """导出所有聊天 — 直接保存到用户配置的导出目录"""
    fmt = str(fmt or "").strip().lower()
    if fmt not in ("html", "json", "csv", "txt"):
        raise HTTPException(400, "聊天导出格式必须是 HTML、JSON、CSV 或 TXT")
    parser = get_parser()
    if html_image_quality not in ("thumbnail", "best"):
        raise HTTPException(400, "HTML 图片清晰度必须是 thumbnail 或 best")
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"wechat_export_all_{os.getpid()}_"))
    export_image_service = None
    if fmt == "html" and embed_images and not replace_images_with_descriptions:
        try:
            export_image_service = get_image_service()
        except Exception:
            export_image_service = None
    exporter = ChatExporter(
        parser,
        tmp_dir,
        description_store=_image_description_store,
        voice_store=_voice_transcription_store,
        account_id=config.wxid or "",
        image_service=export_image_service,
    )

    try:
        zip_path, failed_chats = exporter.export_all_chats(
            fmt=fmt,
            replace_images_with_descriptions=replace_images_with_descriptions,
            replace_voices_with_transcriptions=replace_voices_with_transcriptions,
            embed_images=embed_images,
            html_image_quality=html_image_quality,
        )
        dest_path = _move_export_file(zip_path, zip_path.name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    final_path = str(dest_path)
    print(f"[导出] 已保存到: {final_path}")
    return {
        "success": True,
        "path": final_path,
        "filename": dest_path.name,
        "failed_chats": failed_chats,
    }


# --- 前端静态文件 ---

frontend_dist = resource_path("frontend", "dist")

if frontend_dist.exists():
    from fastapi.staticfiles import StaticFiles

    @app.get("/")
    async def serve_frontend():
        index_path = frontend_dist / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse(FALLBACK_HTML)

    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"))
else:
    @app.get("/")
    async def serve_fallback():
        """没有构建前端时，提供内置的简易界面"""
        return HTMLResponse(FALLBACK_HTML)


# 内置的简易 HTML 界面 (无需构建前端即可使用)
FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>微信解析助手</title>
    <style>
        :root {
            --green: #07c160;
            --green-dark: #06ad56;
            --bg: #f0f0f0;
            --bubble-self: #95ec69;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
            background: var(--bg); min-height: 100vh;
        }
        .header {
            background: var(--green); color: white; padding: 14px 20px;
            display: flex; align-items: center; justify-content: space-between;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        }
        .header h1 { font-size: 18px; }
        .header .status { font-size: 12px; opacity: 0.9; }
        .container { max-width: 1000px; margin: 0 auto; padding: 16px; }
        .card {
            background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px;
            box-shadow: 0 1px 4px rgba(0,0,0,0.08);
        }
        .card h2 { font-size: 16px; margin-bottom: 12px; color: #333; }
        .key-area textarea {
            width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 8px;
            font-family: monospace; font-size: 13px; resize: vertical; min-height: 60px;
        }
        .key-area textarea:focus { outline: none; border-color: var(--green); box-shadow: 0 0 0 2px rgba(7,193,96,0.15); }
        .btn {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 18px; border: none; border-radius: 8px; cursor: pointer;
            font-size: 14px; font-weight: 500; transition: all 0.2s;
        }
        .btn-primary { background: var(--green); color: white; }
        .btn-primary:hover { background: var(--green-dark); }
        .btn-outline { background: white; color: var(--green); border: 1px solid var(--green); }
        .btn-outline:hover { background: #f0fff5; }
        .btn-sm { padding: 5px 12px; font-size: 12px; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-row { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
        .chat-list { list-style: none; }
        .chat-item {
            display: flex; align-items: center; gap: 12px; padding: 12px;
            border-bottom: 1px solid #f0f0f0; cursor: pointer; transition: background 0.15s;
        }
        .chat-item:hover { background: #f9f9f9; }
        .chat-item.active { background: #e8f8ef; }
        .chat-avatar {
            width: 44px; height: 44px; border-radius: 8px; background: var(--green);
            color: white; display: flex; align-items: center; justify-content: center;
            font-weight: bold; font-size: 16px; flex-shrink: 0; overflow: hidden;
            position: relative;
        }
        .chat-avatar img, .msg-avatar img {
            position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
        }
        .chat-info { flex: 1; min-width: 0; }
        .chat-name { font-weight: 600; font-size: 14px; color: #333; }
        .chat-preview { font-size: 12px; color: #999; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .chat-meta { text-align: right; flex-shrink: 0; }
        .chat-time { font-size: 11px; color: #bbb; }
        .chat-count { font-size: 11px; color: #999; margin-top: 2px; }
        .two-col { display: flex; gap: 16px; }
        .col-left { width: 320px; flex-shrink: 0; }
        .col-right { flex: 1; }
        .msg-container { max-height: 65vh; overflow-y: auto; padding: 16px; }
        .msg { margin-bottom: 12px; display: flex; align-items: flex-start; gap: 8px; }
        .msg.self { justify-content: flex-start; flex-direction: row-reverse; }
        .msg-avatar {
            width: 36px; height: 36px; border-radius: 6px; background: var(--green);
            color: white; display: flex; align-items: center; justify-content: center;
            font-size: 12px; font-weight: 700; flex: 0 0 36px; overflow: hidden;
            position: relative;
        }
        .msg-bubble {
            max-width: 70%; padding: 10px 14px; border-radius: 10px;
            font-size: 14px; line-height: 1.5; word-break: break-word;
        }
        .msg.self .msg-bubble { background: var(--bubble-self); border-top-right-radius: 3px; }
        .msg.other .msg-bubble { background: white; border: 1px solid #e8e8e8; border-top-left-radius: 3px; }
        .msg-sender { color: #576b95; font-size: 11px; font-weight: 600; margin-bottom: 2px; }
        .msg-time { font-size: 10px; color: #bbb; text-align: right; margin-top: 3px; }
        .date-divider { text-align: center; margin: 16px 0; }
        .date-divider span { background: #e0e0e0; color: #888; font-size: 11px; padding: 2px 10px; border-radius: 10px; }
        .empty { text-align: center; color: #ccc; padding: 40px; }
        .search-box {
            width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 8px;
            font-size: 13px; margin-bottom: 8px;
        }
        .search-box:focus { outline: none; border-color: var(--green); }
        .filter-row { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
        .filter-row select { padding: 5px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 12px; }
        .badge { display: inline-block; font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #e8e8e8; color: #666; margin-right: 4px; }
        .toast { position: fixed; top: 16px; right: 16px; background: #333; color: white; padding: 10px 18px; border-radius: 8px; font-size: 13px; z-index: 999; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }
        .loading { text-align: center; padding: 20px; color: #999; }
        .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #ddd; border-top-color: var(--green); border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .error { background: #fff0f0; border: 1px solid #ffd0d0; color: #c00; padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 12px; }
        .success { background: #f0fff5; border: 1px solid #c0f0d0; color: #060; padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 12px; }
        .pagination { display: flex; justify-content: center; gap: 8px; align-items: center; padding: 12px; }
        .pagination span { font-size: 13px; color: #666; }
        @media (max-width: 768px) {
            .two-col { flex-direction: column; }
            .col-left { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>💬 微信解析助手</h1>
        <span class="status" id="statusText">检测中...</span>
    </div>
    <div class="container" id="app"></div>

    <script>
        // --- 简易内嵌前端 ---
        const $ = (s) => document.querySelector(s);
        const $$ = (s) => document.querySelectorAll(s);
        const API = '/api';

        async function fetchJSON(url, opts = {}) {
            const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }
            return res.json();
        }

        function escapeHTML(str) {
            if (!str) return '';
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        function timeStr(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            const pad = (n) => String(n).padStart(2, '0');
            return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
        }

        function dateStr(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            const now = new Date();
            if (d.toDateString() === now.toDateString()) return '今天';
            const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
            if (d.toDateString() === yesterday.toDateString()) return '昨天';
            return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`;
        }

        // --- 全局状态 ---
        let state = { status: null, chats: [], activeChat: null, page: 1, totalPages: 1 };

        // --- 初始化 ---
        async function init() {
            try {
                const statusRes = await fetchJSON(API + '/status');
                state.status = statusRes.data;
                $('#statusText').textContent = state.status.wxid
                    ? `已检测: ${state.status.wxid}`
                    : '未检测到微信数据';

                const detectRes = await fetchJSON(API + '/auto-detect');
                const data = detectRes.data;

                if (data.key_found) {
                    state.keyReady = true;
                    await loadChats();
                } else {
                    renderKeyInput(data);
                }
            } catch (e) {
                renderError('初始化失败: ' + e.message);
            }
        }

        // --- 渲染函数 ---
        function renderKeyInput(data) {
            let statusHTML = '';
            if (data) {
                statusHTML = `
                <div class="${data.wxid_found ? 'success' : 'error'}">
                    <strong>检测结果:</strong><br>
                    微信账号: ${data.wxid || '未检测到'}<br>
                    数据库: ${data.databases_found ? (data.msg_dbs||[]).join(', ') : '未找到'}<br>
                    ${data.key_found ? '✅ 已自动提取密钥' : '⚠ 需要手动输入密钥'}
                </div>`;
            }

            $('#app').innerHTML = `
                <div class="card">
                    <h2>🔐 设置解密密钥</h2>
                    ${statusHTML}
                    <div class="key-area">
                        <textarea id="keyInput" placeholder="请输入64位十六进制解密密钥&#10;例如: a1b2c3d4e5f67890abcdef1234567890abcdef1234567890abcdef1234567890"></textarea>
                        <div class="btn-row">
                            <button class="btn btn-primary" onclick="submitKey()">确认密钥</button>
                            <button class="btn btn-outline" onclick="autoDetect()">自动检测</button>
                        </div>
                    </div>
                    <div id="keyError"></div>
                </div>
                <div class="card" style="background:#fffbe6;border:1px solid #ffe58f;">
                    <h3 style="font-size:14px;color:#ad6800;">💡 如何获取密钥?</h3>
                    <p style="font-size:13px;color:#8c6800;margin-top:8px;">
                        1. 确保微信正在运行且已登录<br>
                        2. 点击"自动检测"从微信内存中提取密钥<br>
                        3. 或使用其他工具提取后手动粘贴
                    </p>
                </div>`;
        }

        window.submitKey = async function() {
            const key = $('#keyInput').value.trim().replace(/\\s/g, '');
            if (!/^[a-fA-F0-9]{64}$/.test(key)) {
                $('#keyError').innerHTML = '<div class="error">密钥格式不正确，需要64位十六进制字符</div>';
                return;
            }
            try {
                await fetchJSON(API + '/set-key', { method: 'POST', body: JSON.stringify({ key: key.toLowerCase() }) });
                state.keyReady = true;
                await loadChats();
            } catch (e) {
                $('#keyError').innerHTML = '<div class="error">' + escapeHTML(e.message) + '</div>';
            }
        };

        window.autoDetect = async function() {
            try {
                const res = await fetchJSON(API + '/auto-detect');
                if (res.data.key_found) {
                    state.keyReady = true;
                    await loadChats();
                } else {
                    $('#keyError').innerHTML = '<div class="error">未能自动提取密钥，请手动输入</div>';
                }
            } catch (e) {
                $('#keyError').innerHTML = '<div class="error">' + escapeHTML(e.message) + '</div>';
            }
        };

        function renderError(msg) {
            $('#app').innerHTML = `
                <div class="card" style="text-align:center;">
                    <div style="font-size:48px;margin-bottom:12px;">😞</div>
                    <p style="color:#666;">${escapeHTML(msg)}</p>
                    <button class="btn btn-primary" onclick="init()" style="margin-top:12px;">重试</button>
                </div>`;
        }

        // --- 聊天列表 ---
        async function loadChats() {
            try {
                $('#app').innerHTML = '<div class="loading"><span class="spinner"></span> 加载聊天列表...</div>';
                const res = await fetchJSON(API + '/chats');
                state.chats = res.data || [];
                renderMainLayout();
            } catch (e) {
                renderError('加载聊天失败: ' + e.message);
            }
        }

        function renderMainLayout() {
            let chatListHTML = '';
            if (state.chats.length === 0) {
                chatListHTML = '<div class="empty">暂无聊天记录</div>';
            } else {
                chatListHTML = state.chats.map(c => `
                    <li class="chat-item${state.activeChat && state.activeChat.talker === c.talker ? ' active' : ''}"
                        onclick="selectChat('${escapeHTML(c.talker)}')">
                        <div class="chat-avatar">
                            <span>${c.is_group ? '群' : escapeHTML((c.display_name||'?')[0])}</span>
                            ${c.avatar_url ? `<img src="${escapeHTML(c.avatar_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ''}
                        </div>
                        <div class="chat-info">
                            <div class="chat-name">${escapeHTML(c.display_name)}${c.is_group ? ' <span class="badge">群聊</span>' : ''}</div>
                            <div class="chat-preview">${escapeHTML(c.last_message || '')}</div>
                        </div>
                        <div class="chat-meta">
                            <div class="chat-time">${c.last_time_str||''}</div>
                            <div class="chat-count">${c.message_count||0} 条</div>
                        </div>
                    </li>
                `).join('');
            }

            let rightPanel = '';
            if (state.activeChat) {
                rightPanel = `
                <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #eee;background:white;">
                    <div style="display:flex;align-items:center;gap:9px;min-width:0;">
                        <div class="chat-avatar" style="width:36px;height:36px;">
                            <span>${state.activeChat.is_group ? '群' : escapeHTML((state.activeChat.display_name||'?')[0])}</span>
                            ${state.activeChat.avatar_url ? `<img src="${escapeHTML(state.activeChat.avatar_url)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : ''}
                        </div>
                        <strong>${escapeHTML(state.activeChat.display_name)}</strong>
                    </div>
                    <div style="display:flex;gap:8px;align-items:center;">
                        <select id="msgTypeFilter" onchange="loadMessages(1)" style="padding:5px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;">
                            <option value="">全部类型</option>
                            <option value="1">文本</option>
                            <option value="3">图片</option>
                            <option value="34">语音</option>
                            <option value="43">视频</option>
                            <option value="47">表情</option>
                            <option value="49">链接/卡片（含红包、转账）</option>
                        </select>
                        <button class="btn btn-outline btn-sm" onclick="switchToExport()">📥 导出</button>
                    </div>
                </div>
                <div style="padding:8px 16px;background:#f5f5f5;border-bottom:1px solid #eee;">
                    <input class="search-box" type="text" id="searchKeyword" placeholder="搜索聊天内容..."
                           onkeydown="if(event.key==='Enter')loadMessages(1)" style="margin:0;">
                </div>
                <div class="msg-container" id="messageList">
                    <div class="loading"><span class="spinner"></span> 加载中...</div>
                </div>
                <div class="pagination" id="pagination"></div>`;
            } else {
                rightPanel = '<div class="empty" style="padding:80px 20px;"><div style="font-size:64px;">💬</div><p style="margin-top:12px;">选择左侧聊天查看消息</p></div>';
            }

            $('#app').innerHTML = `
                <div class="two-col">
                    <div class="col-left">
                        <div class="card" style="padding:12px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                                <h2 style="margin:0;">聊天列表</h2>
                                <button class="btn btn-outline btn-sm" onclick="loadChats()">🔄 刷新</button>
                            </div>
                            <input class="search-box" type="text" id="chatSearch" placeholder="搜索联系人..." oninput="filterChats()">
                            <div style="display:flex;gap:6px;margin-bottom:8px;">
                                <button class="btn btn-outline btn-sm" onclick="exportAllChats()">📦 导出全部</button>
                                <button class="btn btn-outline btn-sm" onclick="showStats()">📊 统计</button>
                            </div>
                            <ul class="chat-list" id="chatList">${chatListHTML}</ul>
                        </div>
                    </div>
                    <div class="col-right">
                        <div class="card" style="padding:0;overflow:hidden;">${rightPanel}</div>
                    </div>
                </div>`;

            if (state.activeChat) {
                loadMessages(1);
            }
        }

        window.filterChats = function() {
            const q = ($('#chatSearch')?.value || '').toLowerCase();
            $$('.chat-item').forEach(el => {
                const name = (el.querySelector('.chat-name')?.textContent || '').toLowerCase();
                el.style.display = !q || name.includes(q) ? '' : 'none';
            });
        };

        window.selectChat = function(talker) {
            state.activeChat = state.chats.find(c => c.talker === talker) || null;
            state.page = 1;
            renderMainLayout();
        };

        window.showStats = async function() {
            try {
                const res = await fetchJSON(API + '/statistics');
                const stats = res.data;
                let typeHTML = '';
                for (const [k, v] of Object.entries(stats.msg_by_type || {})) {
                    typeHTML += `<tr><td>${k}</td><td>${v.toLocaleString()}</td></tr>`;
                }
                const html = `
                <div class="card" style="margin-bottom:16px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <h2>📊 聊天统计</h2>
                        <button class="btn btn-outline btn-sm" onclick="renderMainLayout()">返回</button>
                    </div>
                    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0;">
                        <div style="text-align:center;padding:12px;background:#f5f5f5;border-radius:8px;">
                            <div style="font-size:24px;font-weight:bold;color:var(--green);">${(stats.total_messages||0).toLocaleString()}</div>
                            <div style="font-size:12px;color:#999;">总消息数</div>
                        </div>
                        <div style="text-align:center;padding:12px;background:#f5f5f5;border-radius:8px;">
                            <div style="font-size:24px;font-weight:bold;color:var(--green);">${(stats.total_talkers||0).toLocaleString()}</div>
                            <div style="font-size:12px;color:#999;">联系人</div>
                        </div>
                        <div style="text-align:center;padding:12px;background:#f5f5f5;border-radius:8px;">
                            <div style="font-size:14px;font-weight:bold;color:#666;">${stats.date_range?.start||'-'} ~ ${stats.date_range?.end||'-'}</div>
                            <div style="font-size:12px;color:#999;">时间范围</div>
                        </div>
                    </div>
                    <h3 style="font-size:14px;margin-bottom:8px;">消息类型分布</h3>
                    <table style="width:100%;font-size:13px;border-collapse:collapse;">
                        <tr style="background:#f5f5f5;"><th style="text-align:left;padding:6px 10px;">类型</th><th style="text-align:right;padding:6px 10px;">数量</th></tr>
                        ${typeHTML}
                    </table>
                </div>`;
                $('#app').innerHTML = html;
            } catch (e) {
                alert('获取统计失败: ' + e.message);
            }
        };

        // --- 消息加载 ---
        async function loadMessages(pageNum) {
            if (!state.activeChat) return;
            state.page = pageNum;

            const msgType = $('#msgTypeFilter')?.value || '';
            const keyword = $('#searchKeyword')?.value || '';

            try {
                $('#messageList').innerHTML = '<div class="loading"><span class="spinner"></span> 加载中...</div>';
                const params = new URLSearchParams({ page: pageNum, page_size: 50 });
                if (msgType) params.set('msg_type', msgType);
                if (keyword) params.set('keyword', keyword);

                const res = await fetchJSON(`${API}/chat/${encodeURIComponent(state.activeChat.talker)}?${params}`);
                state.totalPages = res.total_pages;

                renderMessages(res.messages || [], res.total, res.page, res.total_pages);
            } catch (e) {
                $('#messageList').innerHTML = '<div class="error">加载失败: ' + escapeHTML(e.message) + '</div>';
            }
        }

        function renderMessages(messages, total, page, totalPages) {
            if (messages.length === 0) {
                $('#messageList').innerHTML = '<div class="empty">暂无消息</div>';
                $('#pagination').innerHTML = '';
                return;
            }

            let html = '';
            let lastDate = '';

            for (const msg of messages) {
                const d = dateStr(msg.create_time);
                if (d !== lastDate) {
                    lastDate = d;
                    html += `<div class="date-divider"><span>${d}</span></div>`;
                }

                if (msg.is_sender === null) {
                    html += `<div class="date-divider"><span>${escapeHTML(msg.content || '')}</span></div>`;
                    continue;
                }
                const cls = msg.is_sender ? 'self' : 'other';
                const t = (msg.time_str || '').slice(-8);
                const typeBadge = msg.type !== 1 ? `<span class="badge">${escapeHTML(msg.type_name)}</span>` : '';
                const content = escapeHTML(msg.content || '').replace(/\\n/g, '<br>');
                const senderLabel = msg.is_sender
                    ? '我'
                    : (msg.sender_name || msg.sender_username || (state.activeChat.is_group ? '未知成员' : state.activeChat.display_name) || '未知用户');
                const avatarFallback = escapeHTML((senderLabel || '?')[0]);
                const avatarImage = msg.sender_avatar_url
                    ? `<img src="${escapeHTML(msg.sender_avatar_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`
                    : '';
                const senderName = (!msg.is_sender && msg.sender_name)
                    ? `<div class="msg-sender">${escapeHTML(msg.sender_name)}</div>`
                    : '';

                html += `
                <div class="msg ${cls}">
                    <div class="msg-avatar"><span>${avatarFallback}</span>${avatarImage}</div>
                    <div class="msg-bubble">
                        ${senderName}
                        ${typeBadge}
                        <div>${content || '[空]'}</div>
                        <div class="msg-time">${t}</div>
                    </div>
                </div>`;
            }

            $('#messageList').innerHTML = html;
            // 滚动到底部 (初次加载)
            if (page === totalPages || page === 1) {
                $('#messageList').scrollTop = $('#messageList').scrollHeight;
            }

            // 分页
            let pagHTML = '';
            if (totalPages > 1) {
                pagHTML = `
                    <button class="btn btn-outline btn-sm" onclick="loadMessages(1)" ${page===1?'disabled':''}>首页</button>
                    <button class="btn btn-outline btn-sm" onclick="loadMessages(${page-1})" ${page===1?'disabled':''}>上一页</button>
                    <span>${page} / ${totalPages} (共${total}条)</span>
                    <button class="btn btn-outline btn-sm" onclick="loadMessages(${page+1})" ${page===totalPages?'disabled':''}>下一页</button>
                    <button class="btn btn-outline btn-sm" onclick="loadMessages(${totalPages})" ${page===totalPages?'disabled':''}>末页</button>`;
            }
            $('#pagination').innerHTML = pagHTML;
        }

        window.loadMessages = loadMessages;

        // --- 导出 ---
        window.switchToExport = function() {
            if (!state.activeChat) return;
            const html = `
            <div class="card">
                <h2>📥 导出聊天记录</h2>
                <p style="color:#666;font-size:13px;margin-bottom:12px;">导出: ${escapeHTML(state.activeChat.display_name)}</p>
                <div style="display:flex;flex-direction:column;gap:8px;">
                    ${['html','json','csv','txt'].map(fmt => `
                    <label style="display:flex;align-items:center;gap:8px;padding:10px;border:1px solid #ddd;border-radius:8px;cursor:pointer;">
                        <input type="radio" name="exportFmt" value="${fmt}" ${fmt==='html'?'checked':''}>
                        <span style="font-weight:500;">${fmt.toUpperCase()}</span>
                        <span style="font-size:12px;color:#999;">— ${{html:'美观的网页格式',json:'结构化数据',csv:'Excel可打开的表格',txt:'纯文本格式'}[fmt]}</span>
                    </label>`).join('')}
                </div>
                <div class="btn-row">
                    <button class="btn btn-primary" onclick="doExport()">开始导出</button>
                    <button class="btn btn-outline" onclick="renderMainLayout()">返回</button>
                </div>
            </div>`;
            $('#app').innerHTML = html;
        };

        window.doExport = async function() {
            const fmt = [...$$('input[name="exportFmt"]')].find(r => r.checked)?.value || 'html';
            try {
                const res = await fetch(API + '/export', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ talker: state.activeChat.talker, display_name: state.activeChat.display_name, format: fmt })
                });
                if (!res.ok) throw new Error('导出失败');
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${state.activeChat.display_name}.${fmt}`;
                a.click();
                URL.revokeObjectURL(url);
                showToast('导出成功!');
            } catch (e) {
                alert('导出失败: ' + e.message);
            }
        };

        window.exportAllChats = async function() {
            if (!confirm('确定要导出所有聊天记录吗？这可能需要一些时间。')) return;
            try {
                showToast('正在导出所有聊天记录...');
                const res = await fetch(API + '/export-all?fmt=html', { method: 'POST' });
                if (!res.ok) throw new Error('导出失败');
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'wechat_all_chats.zip';
                a.click();
                URL.revokeObjectURL(url);
                showToast('全部导出成功!');
            } catch (e) {
                alert('导出失败: ' + e.message);
            }
        };

        function showToast(msg) {
            const el = document.createElement('div');
            el.className = 'toast';
            el.textContent = msg;
            document.body.appendChild(el);
            setTimeout(() => el.remove(), 3000);
        }

        // --- 启动 ---
        init();
    </script>
</body>
</html>"""


@app.get("/api")
async def api_root():
    """API 根路径"""
    return {
        "name": "微信解析助手 API",
        "version": APP_VERSION,
        "endpoints": [
            "GET  /api/status",
            "GET  /api/auto-detect",
            "POST /api/set-key",
            "GET  /api/chats",
            "GET  /api/chat/{talker}",
            "GET  /api/search?keyword=",
            "GET  /api/statistics",
            "GET  /api/moments/contacts",
            "POST /api/moments/preview",
            "POST /api/moments/export",
            "POST /api/export",
            "POST /api/export-all",
        ],
    }
