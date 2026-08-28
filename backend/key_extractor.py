"""
微信数据库解密密钥提取模块

支持多种方式获取密钥:
1. 从微信进程内存中自动提取 (需要微信正在运行)
   - 支持微信 4.x 的 Weixin.exe
2. 从已保存的密钥文件读取
3. 手动输入
"""
import re
import sys
from pathlib import Path
from typing import Optional
import json

from .app_paths import (
    app_data_path,
    atomic_write_text,
    migrate_legacy_file,
    migrate_legacy_json_file,
    source_root,
)

DEFAULT_KEY_FILE = app_data_path("wechat_key.txt")
DEFAULT_KEYS_FILE = app_data_path("wechat_keys.json")
DEFAULT_READABLE_KEY_FILE = app_data_path("wechat_keys.txt")
KEY_FILE = DEFAULT_KEY_FILE
KEYS_FILE = DEFAULT_KEYS_FILE  # 多账号密钥存储
ROOT_KEY_FILE = DEFAULT_READABLE_KEY_FILE
LEGACY_KEY_FILE = source_root() / "backend" / "wechat_key.txt"
LEGACY_KEYS_FILE = source_root() / "backend" / "wechat_keys.json"
LEGACY_READABLE_KEY_FILE = source_root() / "wechat_keys.txt"


def _valid_legacy_single_key(payload: bytes) -> bool:
    try:
        value = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return False
    return bool(re.fullmatch(r"[a-fA-F0-9]{64}", value))


def _ensure_key_storage_migrated() -> None:
    """Copy checkout-era key files into per-user storage once."""

    # Unit tests patch these public paths.  Do not leak real user keys into a
    # caller-provided storage location.
    if Path(KEYS_FILE) != DEFAULT_KEYS_FILE or Path(KEY_FILE) != DEFAULT_KEY_FILE:
        return
    migrate_legacy_json_file(KEYS_FILE, (LEGACY_KEYS_FILE,))
    migrate_legacy_file(
        KEY_FILE,
        (LEGACY_KEY_FILE,),
        validator=_valid_legacy_single_key,
    )
    if Path(ROOT_KEY_FILE) == DEFAULT_READABLE_KEY_FILE:
        migrate_legacy_file(ROOT_KEY_FILE, (LEGACY_READABLE_KEY_FILE,))


def _load_keys_dict() -> dict:
    """加载所有账号的存储 {wxid: {key, display_name, alias}}"""
    _ensure_key_storage_migrated()
    if KEYS_FILE.exists():
        try:
            try:
                raw = KEYS_FILE.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # 兼容早期按 Windows 本地编码写出的文件；下次保存会迁移为 UTF-8。
                raw = KEYS_FILE.read_text()
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            # 迁移旧格式: {wxid: "key_string"} → {wxid: {key: "..."}}
            migrated = {}
            for k, v in data.items():
                if isinstance(v, str):
                    migrated[k] = {"key": v}
                else:
                    migrated[k] = v
            if migrated != data:
                _save_keys_dict(migrated)
            return migrated
        except Exception:
            return {}
    # 迁移旧版单密钥文件：仅在 KEYS_FILE 不存在时才使用
    if KEY_FILE.exists() and not KEYS_FILE.exists():
        old_key = KEY_FILE.read_text().strip()
        if old_key and re.match(r"^[a-fA-F0-9]{64}$", old_key):
            return {"_default": {"key": old_key}}
        # 格式不对，删除旧文件
        try:
            KEY_FILE.unlink()
        except Exception:
            pass
    return {}


def _save_keys_dict(data: dict):
    """保存存储映射"""
    atomic_write_text(
        KEYS_FILE,
        json.dumps(data, indent=2, ensure_ascii=False),
    )


def _write_readable_keyfile():
    """将密钥和账号信息写入每用户应用数据目录的易读文件。"""
    from datetime import datetime

    keys = _load_keys_dict()
    # 收集账号信息：从 config 获取数据路径
    try:
        from .config import config
        accounts = {a.wxid: a for a in config.accounts}
    except Exception:
        accounts = {}

    lines = []
    lines.append("=" * 60)
    lines.append("微信聊天数据库解密密钥")
    lines.append("=" * 60)
    lines.append(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    idx = 0
    for wxid_base, entry in keys.items():
        if wxid_base == "_default":
            continue  # 跳过无主残留条目
        key = entry.get("key", "") if isinstance(entry, dict) else ""
        if not key:
            continue
        idx += 1
        display = entry.get("display_name", "") if isinstance(entry, dict) else ""
        alias = entry.get("alias", "") if isinstance(entry, dict) else ""

        # 尝试匹配完整 wxid
        full_wxid = wxid_base
        data_path = ""
        for aid, acc in accounts.items():
            base = aid.rsplit("_", 1)[0] if "_" in aid else aid
            if base == wxid_base:
                full_wxid = aid
                if acc.msg_dir:
                    data_path = str(acc.msg_dir)
                if not display:
                    display = acc.display_name or ""
                if not alias:
                    alias = acc.alias or ""
                break

        lines.append(f"账号 #{idx}")
        lines.append(f"  wxid:        {full_wxid}")
        if display:
            lines.append(f"  显示名:       {display}")
        if alias:
            lines.append(f"  微信号:       {alias}")
        if data_path:
            lines.append(f"  数据路径:     {data_path}")
        lines.append(f"  密钥:         {key}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("此文件包含微信聊天数据库的解密密钥")
    lines.append("请妥善保管，不要分享给他人")
    lines.append("=" * 60)
    lines.append("")

    try:
        atomic_write_text(ROOT_KEY_FILE, "\n".join(lines))
    except Exception:
        pass


def save_key(key: str, wxid: str = None) -> None:
    """保存密钥，关联到指定账号"""
    normalized_key = str(key or "").strip().lower()
    if not validate_key(normalized_key):
        raise ValueError("数据库密钥必须是 64 位十六进制字符串")
    key = normalized_key
    keys = _load_keys_dict()
    # 如果没传 wxid，尝试从 config 获取当前账号
    if not wxid:
        try:
            from .config import config
            wxid = config.wxid
        except Exception:
            pass
    if wxid:
        base = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
        if base not in keys:
            keys[base] = {}
        keys[base]["key"] = key
        # 如果之前有无主的 _default 且 key 相同，清理掉
        if "_default" in keys and keys["_default"].get("key") == key:
            del keys["_default"]
    else:
        if "_default" not in keys:
            keys["_default"] = {}
        keys["_default"]["key"] = key
    _save_keys_dict(keys)
    _write_readable_keyfile()


def save_account_info(wxid: str, display_name: str = None, alias: str = None):
    """保存账号的显示信息"""
    if not wxid:
        return
    base = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
    keys = _load_keys_dict()
    if base not in keys:
        keys[base] = {}
    if display_name:
        keys[base]["display_name"] = display_name
    if alias:
        keys[base]["alias"] = alias
    _save_keys_dict(keys)
    _write_readable_keyfile()


def load_key(wxid: str = None) -> Optional[str]:
    """加载指定账号的密钥"""
    keys = _load_keys_dict()
    if wxid:
        base = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
        # An explicit account record without a key is a tombstone created by
        # delete_key; it must not silently fall back to a legacy global key.
        entry = keys.get(base) if base in keys else keys.get("_default")
    else:
        entry = keys.get("_default")
    if isinstance(entry, dict):
        candidate = entry.get("key")
        if validate_key(candidate):
            return str(candidate).lower()
    return None


def delete_key(wxid: str = None) -> bool:
    """仅删除指定账号保存的密钥，保留其他账号及缓存的账号资料。"""
    keys = _load_keys_dict()
    account_id = None
    if wxid:
        account_id = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
        # A sole _default entry is the migrated legacy single-account key. It
        # can be removed safely when no account-scoped entry exists.
        if account_id not in keys and set(keys) == {"_default"}:
            account_id = "_default"
        elif account_id not in keys and "_default" in keys:
            # Other accounts may still rely on the legacy fallback. Add an
            # account-specific tombstone so only this wxid stops inheriting it.
            keys[account_id] = {}
            _save_keys_dict(keys)
            _write_readable_keyfile()
            return True
    elif "_default" in keys:
        account_id = "_default"

    entry = keys.get(account_id) if account_id else None
    if not isinstance(entry, dict) or not entry.get("key"):
        return False

    updated_entry = dict(entry)
    updated_entry.pop("key", None)
    if updated_entry or account_id != "_default":
        keys[account_id] = updated_entry
    else:
        keys.pop(account_id, None)
    _save_keys_dict(keys)
    _write_readable_keyfile()
    return True


def load_account_info(wxid: str = None) -> Optional[dict]:
    """加载指定账号的缓存信息 {display_name, alias}"""
    keys = _load_keys_dict()
    if wxid:
        base = wxid.rsplit("_", 1)[0] if "_" in wxid else wxid
        return keys.get(base)
    return keys.get("_default")

# 微信 4.x 进程名
WECHAT_PROCESS_NAMES = [
    "Weixin.exe",
]

# 可能包含密钥的微信 4.x 核心模块名
WECHAT_MODULE_NAMES = [
    "Weixin.exe",
    "wxpublic.dll",
]


def find_key_from_memory() -> Optional[str]:
    """从微信 4.x 进程内存中提取数据库密钥。"""
    if sys.platform != "win32":
        return None

    try:
        import pymem
        import pymem.process
    except ImportError:
        return None

    # 1. 找到微信进程
    pm = None
    process_name = None
    for name in WECHAT_PROCESS_NAMES:
        try:
            pm = pymem.Pymem(name)
            process_name = name
            print(f"[INFO] 找到微信进程: {name}")
            break
        except pymem.exception.ProcessNotFound:
            continue

    if pm is None:
        print("[INFO] 未找到运行中的微信进程")
        return None

    # 2. 尝试从不同模块中搜索密钥
    for module_name in WECHAT_MODULE_NAMES:
        key = _search_module(pm, process_name, module_name)
        if key:
            try:
                pm.close_process()
            except Exception:
                pass
            return key

    # 3. 如果所有已知模块都找不到，搜索整个 Weixin.exe 模块的内存
    try:
        main_module = pymem.process.module_from_name(
            pm.process_handle, process_name
        )
        if main_module:
            key = _search_memory_region(pm, main_module.lpBaseOfDll, main_module.SizeOfImage)
            if key:
                try:
                    pm.close_process()
                except Exception:
                    pass
                return key
    except Exception:
        pass

    # 4. 搜索所有已加载的模块
    try:
        for mod in pm.list_modules():
            if mod.szModule and any(
                name.lower() in mod.szModule.lower()
                for name in ["weixin", "xwechat", "wxpublic", "mmmojo"]
            ):
                try:
                    key = _search_memory_region(pm, mod.lpBaseOfDll, min(mod.SizeOfImage, 50 * 1024 * 1024))
                    if key:
                        print(f"[INFO] 在模块 {mod.szModule} 中找到密钥")
                        try:
                            pm.close_process()
                        except Exception:
                            pass
                        return key
                except Exception:
                    pass
    except Exception:
        pass

    try:
        pm.close_process()
    except Exception:
        pass
    return None


def _search_module(pm, process_name: str, module_name: str) -> Optional[str]:
    """在指定模块中搜索密钥"""
    try:
        import pymem.process
        mod = pymem.process.module_from_name(pm.process_handle, module_name)
        if mod:
            base = mod.lpBaseOfDll
            size = mod.SizeOfImage
            print(f"[INFO] 搜索模块 {module_name} (base=0x{base:X}, size={size})")
            return _search_memory_region(pm, base, size)
    except Exception:
        pass
    return None


def _search_memory_region(pm, base: int, size: int) -> Optional[str]:
    """在内存区域中搜索密钥 - 使用多种策略"""
    # 限制搜索大小以提高效率
    max_size = min(size, 100 * 1024 * 1024)  # 最多搜索 100MB
    try:
        data = pm.read_bytes(base, max_size)
    except Exception:
        return None

    # 策略1: 搜索微信 4.x 数据库名称，密钥通常在其附近
    targets = [
        b"contact.db",
        b"message_0.db",
        b"message_resource.db",
    ]
    for target in targets:
        idx = data.find(target)
        if idx != -1:
            # 在周围 16KB 范围内搜索
            start = max(0, idx - 8192)
            end = min(len(data), idx + 8192 + len(target))
            key = _find_hex_key_pattern(data[start:end])
            if key:
                return key
            # 也尝试搜索二进制密钥
            key = _find_binary_key(data[start:end])
            if key:
                return key

    # 策略2: 搜索 wxid 字符串
    wxid = _find_wxid_in_memory(data)
    if wxid:
        wxid_bytes = wxid.encode()
        idx = data.find(wxid_bytes)
        if idx != -1:
            start = max(0, idx - 16384)
            end = min(len(data), idx + 16384)
            key = _find_hex_key_pattern(data[start:end])
            if key:
                return key
            key = _find_binary_key(data[start:end])
            if key:
                return key

    # 策略3: 全局搜索64位hex密钥
    key = _find_hex_key_pattern(data)
    if key:
        return key

    # 策略4: 搜索二进制密钥模式 (32 bytes, preceded by common markers)
    key = _find_binary_key(data)
    if key:
        return key

    return None


def _find_hex_key_pattern(data: bytes) -> Optional[str]:
    """
    在数据中搜索64位十六进制密钥字符串

    微信数据库密钥常见特征:
    - 64位十六进制 ASCII 字符串 [a-f0-9]
    - 包含数字和字母的混合，非全0/全f
    - 有时在 key= 或 password= 附近
    """
    # 搜索所有64位十六进制字符串
    pattern = re.compile(rb"[a-fA-F0-9]{64}")
    matches = pattern.findall(data)

    candidates = []
    for match in matches:
        key_str = match.decode("ascii").lower()
        if not _is_boring_key(key_str):
            candidates.append(key_str)

    # 如果候选太多，搜索带上下文的
    if len(candidates) > 100:
        # 搜索 key / password 关键词附近的
        for marker in [b"key", b"Key", b"KEY", b"pass", b"sqlite", b"cipher"]:
            idx = data.find(marker)
            if idx != -1:
                nearby = data[max(0, idx-256):idx+256]
                match = re.search(rb"[a-fA-F0-9]{64}", nearby)
                if match:
                    key_str = match.group(0).decode("ascii").lower()
                    if not _is_boring_key(key_str):
                        return key_str

    return candidates[0] if candidates else None


def _find_binary_key(data: bytes) -> Optional[str]:
    """
    搜索二进制密钥 (32字节原始二进制)

    新版微信可能将密钥存储为原始32字节二进制而非hex字符串。
    特征: 看起来像随机字节的32字节块，前面可能有长度前缀。
    """
    # 搜索0x20 (32) 作为长度前缀的32字节二进制密钥
    # 模式: 00 00 00 20 (big-endian 32) 后跟32个字节
    marker = b"\x00\x00\x00\x20"
    idx = 0
    while idx < len(data) - 40:
        idx = data.find(marker, idx)
        if idx == -1:
            break

        # 获取后面的32字节
        start = idx + 4
        key_bytes = data[start:start + 32]

        # 检查是否是随机二进制 (非全0, 非重复)
        if len(key_bytes) == 32:
            key_hex = key_bytes.hex()
            if not _is_boring_key(key_hex):
                # 验证是否看起来像加密密钥
                # 好的密钥应该有良好的随机性
                unique_bytes = len(set(key_bytes))
                if unique_bytes >= 10:  # 至少10个不同的字节值
                    return key_hex

        idx += 1

    return None


def _is_boring_key(key: str) -> bool:
    """检查密钥是否是无效值"""
    if len(key) != 64:
        return True
    if key == "0" * 64:
        return True
    if key == "f" * 64:
        return True
    if key == "F" * 64:
        return True
    # 重复短模式
    for pattern_len in [2, 4, 8, 16, 32]:
        pattern = key[:pattern_len]
        if key == pattern * (64 // pattern_len):
            return True
    return False


def _find_wxid_in_memory(data: bytes) -> Optional[str]:
    """从内存中查找已登录的 wxid"""
    pattern = re.compile(rb"wxid_[a-zA-Z0-9_-]{6,36}")
    matches = pattern.findall(data)
    if matches:
        return matches[0].decode("ascii")
    return None


def find_key_auto(*, persist: bool = True) -> Optional[str]:
    """
    自动查找密钥 - 综合多种方式

    优先级:
    1. 已保存的密钥文件
    2. 微信进程内存提取
    """
    # 1. 尝试从保存的文件读取
    if KEY_FILE.exists():
        try:
            saved_key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            saved_key = ""
        if validate_key(saved_key):
            print("[INFO] 使用已保存的密钥")
            return saved_key.lower()
        else:
            # 格式不对，删除
            try:
                KEY_FILE.unlink()
            except OSError:
                pass

    # 2. 尝试从内存提取
    print("[INFO] 尝试从微信进程内存提取密钥...")
    key = find_key_from_memory()
    if key:
        print("[INFO] 成功从内存提取到候选密钥")
        if persist:
            save_key(key)
        return key

    print("[WARN] 无法自动提取密钥，请手动输入")
    return None




def validate_key(key: object) -> bool:
    """验证密钥格式"""
    return isinstance(key, str) and bool(re.fullmatch(r"[a-fA-F0-9]{64}", key))


if __name__ == "__main__":
    key = find_key_auto()
    if key:
        print(f"找到密钥: {key}")
    else:
        print("未找到密钥")
