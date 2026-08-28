"""
WCDB 内存密钥扫描器

原理: WCDB 为每个数据库在内存中缓存 raw key，格式为:
    x'<64hex_enc_key><32hex_salt>'
salt 直接嵌入在 hex 字符串中，可以匹配 DB 文件的 salt 来定位正确的 key。
"""
import ctypes
import ctypes.wintypes as wt
import re
import sys
import time
import struct
import hashlib
import hmac
from pathlib import Path
from typing import Optional

try:
    from .subprocess_env import sanitized_subprocess_env
except ImportError:  # Support direct execution of this diagnostic module.
    from subprocess_env import sanitized_subprocess_env

kernel32 = ctypes.windll.kernel32
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

# SQLCipher 4 参数
PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE  # 80
PBKDF2_ITER = 256000


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD),
        ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_pad2", wt.DWORD),
    ]


def get_weixin_pids() -> list[tuple[int, int]]:
    """返回所有 Weixin.exe 进程的 (pid, mem_kb) 列表"""
    import subprocess
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        env=sanitized_subprocess_env(),
    )
    pids = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(",", "").replace(" K", "").strip() or "0")
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def read_mem(handle, addr: int, size: int) -> Optional[bytes]:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(handle, ctypes.c_uint64(addr), buf, size, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def enum_regions(handle) -> list[tuple[int, int]]:
    """枚举进程中可读的已提交内存区域"""
    regions = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(
            handle, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
        ) == 0:
            break
        if (mbi.State == MEM_COMMIT
            and mbi.Protect in READABLE
            and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regions


def collect_db_files(msg_dir: Path) -> tuple[dict[str, Path], dict[str, list[str]]]:
    """
    收集所有加密数据库文件及其 salt

    Returns:
        db_files: {db_name: db_path}
        salt_to_dbs: {salt_hex: [db_name, ...]}
    """
    db_files = {}
    salt_to_dbs = {}

    for db_path in sorted(msg_dir.glob("*.db")):
        if db_path.stat().st_size < PAGE_SIZE:
            continue
        try:
            with open(db_path, "rb") as f:
                header = f.read(SALT_SIZE)
            salt_hex = header.hex()
            db_name = db_path.name
            db_files[db_name] = db_path
            salt_to_dbs.setdefault(salt_hex, []).append(db_name)
        except Exception:
            pass

    return db_files, salt_to_dbs


def verify_wcdb_key(key_hex: str, db_path: Path) -> bool:
    """验证提取的 64-char hex key 是否能解密数据库"""
    if len(key_hex) != 64:
        return False
    try:
        pass_bytes = bytes.fromhex(key_hex)
    except Exception:
        return False

    try:
        with open(db_path, "rb") as f:
            page = f.read(PAGE_SIZE)
        if len(page) < PAGE_SIZE:
            return False

        salt = page[:SALT_SIZE]
        mac_salt = bytes(b ^ 0x3A for b in salt)

        enc_key = hashlib.pbkdf2_hmac("sha512", pass_bytes, salt, PBKDF2_ITER, KEY_SIZE)
        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, KEY_SIZE)

        hmac_data = page[SALT_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
        hmac_data += struct.pack("<I", 1)
        computed = hmac.new(mac_key, hmac_data, "sha512").digest()
        stored = page[PAGE_SIZE - RESERVE_SIZE + IV_SIZE:
                      PAGE_SIZE - RESERVE_SIZE + IV_SIZE + HMAC_SIZE]
        return computed == stored
    except Exception:
        return False


def scan_memory_for_wcdb_keys(
    data: bytes,
    hex_re: re.Pattern,
    salt_to_dbs: dict[str, list[str]],
    key_map: dict[str, str],
    remaining_salts: set[str],
    base_addr: int,
    pid: int,
    verbose: bool = True,
) -> int:
    """
    扫描一块内存，搜索 x'<hex_key><hex_salt>' 模式

    Returns: 找到的 hex 模式数量
    """
    matches_found = 0
    for match in hex_re.finditer(data):
        matches_found += 1
        full_hex = match.group(1).decode("ascii").lower()

        # WCDB 缓存格式: 64 hex key + 32 hex salt = 96 hex chars
        # 也可能是 128 hex (key+salt+extra) 或 64 hex (just key)
        if len(full_hex) >= 96:
            # 尝试不同偏移提取 salt
            for salt_start in [64, 128, 192]:  # 尝试多个可能位置
                if salt_start + 32 <= len(full_hex):
                    salt = full_hex[salt_start:salt_start + 32]
                    if salt in remaining_salts:
                        key = full_hex[salt_start - 64:salt_start]
                        if len(key) == 64:
                            key_map[salt] = key
                            remaining_salts.discard(salt)
                            if verbose:
                                offset = match.start()
                                print(
                                    f"  [+] salt={salt[:16]}... matched! "
                                    f"key={key[:16]}... "
                                    f"(PID={pid}, addr=0x{base_addr + offset:X})"
                                )
                            break
        elif len(full_hex) >= 64:
            # 只有 key 没有 salt，记录下来待验证
            pass

    return matches_found


def extract_wcdb_keys(db_dir: str, verbose: bool = True) -> dict[str, str]:
    """
    主函数: 从微信进程内存提取 WCDB 数据库密钥

    Args:
        db_dir: 微信 4.x 数据库目录（如 db_storage/message）

    Returns:
        {salt_hex: key_hex, ...}  密钥字典
    """
    msg_dir = Path(db_dir)

    # 1. 收集所有 DB 文件和 salt
    db_files, salt_to_dbs = collect_db_files(msg_dir)
    if not db_files:
        raise RuntimeError(f"未找到数据库文件: {msg_dir}")

    if verbose:
        print(f"找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同 salt")
        for salt_hex, dbs in sorted(salt_to_dbs.items(),
                                     key=lambda x: len(x[1]), reverse=True):
            print(f"  salt {salt_hex}: {', '.join(dbs)}")

    # 2. 获取微信进程
    pids = get_weixin_pids()
    if verbose:
        for pid, mem_kb in pids:
            print(f"[+] Weixin.exe PID={pid} ({mem_kb // 1024}MB)")

    # 3. 扫描内存
    hex_re = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
    key_map: dict[str, str] = {}
    remaining_salts = set(salt_to_dbs.keys())
    all_matches = 0
    t0 = time.time()

    for pid, mem_kb in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            if verbose:
                print(f"[WARN] 无法打开进程 PID={pid}")
            continue

        try:
            regions = enum_regions(h)
            total_mb = sum(s for _, s in regions) / 1024 / 1024
            if verbose:
                print(f"\n[*] 扫描 PID={pid} ({total_mb:.0f}MB, {len(regions)} 区域)")

            scanned = 0
            for reg_idx, (base, size) in enumerate(regions):
                data = read_mem(h, base, size)
                scanned += size
                if not data:
                    continue

                all_matches += scan_memory_for_wcdb_keys(
                    data, hex_re, salt_to_dbs, key_map,
                    remaining_salts, base, pid, verbose,
                )

                if (reg_idx + 1) % 500 == 0:
                    elapsed = time.time() - t0
                    progress = scanned / sum(s for _, s in regions) * 100
                    if verbose:
                        print(
                            f"  [{progress:.0f}%] {len(key_map)}/{len(salt_to_dbs)} salts, "
                            f"{all_matches} patterns, {elapsed:.1f}s"
                        )

        finally:
            kernel32.CloseHandle(h)

        if not remaining_salts:
            if verbose:
                print("\n[+] 所有密钥已找到!")
            break

    elapsed = time.time() - t0
    if verbose:
        print(f"\n扫描完成: {elapsed:.1f}s, {all_matches} 个 hex 模式")

    # 4. 交叉验证
    verified_keys = {}
    for salt_hex, key_hex in key_map.items():
        db_names = salt_to_dbs.get(salt_hex, [])
        if db_names:
            db_path = db_files.get(db_names[0])
            if db_path and verify_wcdb_key(key_hex, db_path):
                verified_keys[salt_hex] = key_hex
                if verbose:
                    print(f"[VERIFIED] {db_names[0]}: key={key_hex}")
            elif verbose:
                print(f"[FAILED] {db_names[0]}: key={key_hex} HMAC mismatch")
        elif verbose:
            print(f"[UNKNOWN] salt={salt_hex} key={key_hex}")

    return verified_keys


def get_db_key(db_dir: str) -> Optional[str]:
    """
    便捷方法: 提取数据库目录对应的加密密钥

    Returns:
        64 位 hex 密钥字符串, 或 None
    """
    try:
        key_map = extract_wcdb_keys(db_dir, verbose=True)
        if key_map:
            # 返回第一个找到的 key（同一账号的消息数据库通常共享密钥）
            return list(key_map.values())[0]
    except Exception as e:
        print(f"[ERROR] 密钥提取失败: {e}")
    return None


if __name__ == "__main__":
    # 测试
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.config import config
    config.detect_wxid()
    config.detect_databases()

    if config.msg_dir:
        key = get_db_key(str(config.msg_dir))
        if key:
            print(f"\n>>> 数据库密钥: {key}")
            # 保存到统一的用户数据目录，避免在源码或安装目录生成密钥。
            from backend.key_extractor import KEYS_FILE, save_key
            save_key(key, config.wxid)
            print(f"已保存到: {KEYS_FILE}")
        else:
            print("\n未能提取密钥")
    else:
        print("未找到微信数据库目录")
