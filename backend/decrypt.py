"""
微信数据库解密模块

使用 WCDB 逐页解密，直接实现微信 4.x 的数据库加密算法。

微信数据库加密参数:
- 页面大小: 4096 字节
- 每页结构: 数据(4000/4016) + IV(16) + HMAC-SHA512(64)
- 加密: AES-256-CBC
- 密钥派生: PBKDF2-HMAC-SHA512 (iter=256000)
- HMAC 密钥派生: PBKDF2-HMAC-SHA512 (iter=2, salt XOR 0x3a)
"""
import os
import struct
import hashlib
import hmac as hmac_lib
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Optional

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

SQLITE_HEADER = b"SQLite format 3\x00"
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64       # HMAC-SHA512 = 64 bytes
KEY_SIZE = 32
AES_BLOCK_SIZE = 16
PAGE_SIZE = 4096
PAGE_RESERVE = 80    # IV(16) + HMAC(64) = 80 (AES aligned)
PBKDF2_ITER = 256000  # SQLCipher 4 default


class DatabaseDecryptor:
    """微信 4.x 数据库解密器（WCDB 逐页解密）。"""

    def __init__(self, db_path: Path, key: str):
        """
        Args:
            db_path: 加密数据库路径
            key: 密钥字符串 - 支持两种格式:
                 - 64位十六进制 (从内存提取的hex字符串)
                 - 32字节原始密钥的十六进制表示
        """
        self.db_path = Path(db_path)
        self.key = key.strip().lower()
        self._decrypted_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        # 解密与开连接都是重活且非原子：两个线程同时触发会对同一临时文件
        # 双写（产生损坏的明文库）或双开连接。RLock 允许 open_decrypted
        # 持锁调用 decrypt_to_temp。
        self._lock = threading.RLock()

    @property
    def raw_pass(self) -> bytes:
        """将 key 转换为原始字节"""
        if len(self.key) == 64:
            # 64位十六进制字符串
            return bytes.fromhex(self.key)
        elif len(self.key) == 128:
            # 也可能是 128 位 hex
            return bytes.fromhex(self.key)
        else:
            raise ValueError(f"密钥格式不支持: 长度 {len(self.key)}")

    def decrypt_to_temp(self) -> Path:
        """解密数据库到临时文件"""
        with self._lock:
            if self._decrypted_path and self._decrypted_path.exists():
                return self._decrypted_path

            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".db",
                prefix=f"wechat_db_{os.getpid()}_",
            )
            os.close(tmp_fd)
            self._decrypted_path = Path(tmp_path)

            try:
                # 微信 4.x WCDB 逐页解密
                if HAS_CRYPTO and len(self.key) == 64:
                    if self._decrypt_wcdb():
                        try:
                            self._apply_encrypted_wal()
                        except (OSError, ValueError, struct.error):
                            # The live WAL may rotate while being copied.  The main
                            # DB remains valid and is preferable to failing the
                            # feature.
                            pass
                        return self._decrypted_path

                raise RuntimeError(
                    "数据库解密失败。请检查:\n"
                    "1. 密钥是否正确 (64位十六进制)\n"
                    "2. 数据库是否来自受支持的微信 4.x 版本\n"
                    "3. 依赖是否安装: pip install pycryptodome"
                )
            except BaseException:
                # Failed attempts must not accumulate empty or partially decrypted
                # databases containing private chat data in the system temp dir.
                self._discard_decrypted_file()
                raise

    def _derive_wcdb_encryption_key(self) -> bytes:
        with open(self.db_path, "rb") as handle:
            salt = handle.read(SALT_SIZE)
        if len(salt) != SALT_SIZE:
            raise ValueError("数据库 salt 不完整")
        return hashlib.pbkdf2_hmac(
            "sha512", self.raw_pass, salt, PBKDF2_ITER, KEY_SIZE
        )

    @staticmethod
    def _decrypt_wcdb_page(page: bytes, page_number: int, enc_key: bytes) -> bytes:
        if len(page) != PAGE_SIZE or page_number <= 0:
            raise ValueError("WCDB 页面长度或页码无效")
        offset = SALT_SIZE if page_number == 1 else 0
        iv = page[PAGE_SIZE - PAGE_RESERVE:PAGE_SIZE - PAGE_RESERVE + IV_SIZE]
        encrypted = page[offset:PAGE_SIZE - PAGE_RESERVE]
        plain = AES.new(enc_key, AES.MODE_CBC, iv=iv).decrypt(encrypted)
        output = bytearray(PAGE_SIZE)
        if page_number == 1:
            output[:SALT_SIZE] = SQLITE_HEADER
            output[SALT_SIZE:PAGE_SIZE - PAGE_RESERVE] = plain
        else:
            output[:PAGE_SIZE - PAGE_RESERVE] = plain
        return bytes(output)

    def _apply_encrypted_wal(self) -> int:
        """Decrypt committed frames from the encrypted sibling WAL into the copy.

        This follows the reference implementation in
        ``wechat-decrypt-main/mcp_server.py`` while ignoring frames after the
        final commit marker.
        """
        if not self._decrypted_path or not self._decrypted_path.exists():
            return 0
        wal_path = Path(str(self.db_path) + "-wal")
        if not wal_path.exists() or wal_path.stat().st_size <= 32:
            return 0

        enc_key = self._derive_wcdb_encryption_key()
        frames = []
        with open(wal_path, "rb") as wal:
            header = wal.read(32)
            if len(header) != 32:
                return 0
            magic = struct.unpack(">I", header[:4])[0]
            wal_page_size = struct.unpack(">I", header[8:12])[0]
            if magic not in (0x377F0682, 0x377F0683):
                return 0
            if wal_page_size == 1:
                wal_page_size = 65536
            if wal_page_size != PAGE_SIZE:
                return 0
            salt_1 = struct.unpack(">I", header[16:20])[0]
            salt_2 = struct.unpack(">I", header[20:24])[0]
            while True:
                frame_header = wal.read(24)
                if len(frame_header) != 24:
                    break
                encrypted_page = wal.read(PAGE_SIZE)
                if len(encrypted_page) != PAGE_SIZE:
                    break
                page_number = struct.unpack(">I", frame_header[:4])[0]
                database_size = struct.unpack(">I", frame_header[4:8])[0]
                frame_salt_1 = struct.unpack(">I", frame_header[8:12])[0]
                frame_salt_2 = struct.unpack(">I", frame_header[12:16])[0]
                if (
                    0 < page_number <= 1_000_000
                    and frame_salt_1 == salt_1
                    and frame_salt_2 == salt_2
                ):
                    frames.append((page_number, database_size, encrypted_page))

        committed_through = -1
        committed_size = 0
        for index, (_page_number, database_size, _page) in enumerate(frames):
            if database_size:
                committed_through = index
                committed_size = database_size
        if committed_through < 0:
            return 0

        patched = 0
        with open(self._decrypted_path, "r+b") as output:
            for page_number, _database_size, encrypted_page in frames[:committed_through + 1]:
                plain_page = self._decrypt_wcdb_page(
                    encrypted_page, page_number, enc_key
                )
                output.seek((page_number - 1) * PAGE_SIZE)
                output.write(plain_page)
                patched += 1
            if committed_size:
                output.truncate(committed_size * PAGE_SIZE)
            output.flush()
            os.fsync(output.fileno())
        return patched

    def _decrypt_wcdb(self) -> bool:
        """
        WCDB 逐页解密 - 直接实现微信数据库的加密算法

        页面结构 (4096 字节):
        [数据区域 (4000/4016)] [IV (16)] [HMAC-SHA512 (64)]
        """
        try:
            pass_bytes = bytes.fromhex(self.key)
            if len(pass_bytes) != 32:
                return False
        except Exception:
            return False

        try:
            file_size = self.db_path.stat().st_size
        except OSError:
            return False
        if file_size < PAGE_SIZE or file_size % PAGE_SIZE != 0:
            return False
        num_pages = file_size // PAGE_SIZE

        # 读取 salt (数据库前 16 字节)
        try:
            with open(self.db_path, "rb") as source:
                salt = source.read(SALT_SIZE)
        except OSError:
            return False
        if len(salt) != SALT_SIZE:
            return False

        # 派生 MAC salt: mac_salt[i] = salt[i] XOR 0x3a
        mac_salt = bytes(b ^ 0x3a for b in salt)

        # 派生加密密钥: key = PBKDF2-HMAC-SHA512(pass, salt, 256000, 32)
        enc_key = hashlib.pbkdf2_hmac("sha512", pass_bytes, salt, PBKDF2_ITER, KEY_SIZE)

        # 派生 MAC 密钥: mac_key = PBKDF2-HMAC-SHA512(key, mac_salt, 2, 32)
        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, KEY_SIZE)

        # Stream one page at a time. Message shards can be several gigabytes;
        # reading the encrypted input plus a same-sized output bytearray used
        # to require roughly twice the database size in RAM.
        try:
            with open(self.db_path, "rb") as source, open(
                self._decrypted_path, "wb"
            ) as output:
                for page_idx in range(num_pages):
                    page = source.read(PAGE_SIZE)
                    if len(page) != PAGE_SIZE:
                        return False

                    # 第一页有 16 字节 SQLite header 偏移
                    offset = SALT_SIZE if page_idx == 0 else 0

                    # IV 在页面末尾 reserve 区域的前 16 字节
                    iv = page[
                        PAGE_SIZE - PAGE_RESERVE:
                        PAGE_SIZE - PAGE_RESERVE + IV_SIZE
                    ]

                    # 验证 HMAC (仅第一页验证以确认密钥正确)
                    if page_idx == 0:
                        hmac_data = page[
                            offset:PAGE_SIZE - PAGE_RESERVE + IV_SIZE
                        ]
                        hmac_data += struct.pack("<I", 1)  # 页码 (1-indexed)
                        computed_hmac = hmac_lib.new(
                            mac_key, hmac_data, "sha512"
                        ).digest()
                        stored_hmac = page[
                            PAGE_SIZE - PAGE_RESERVE + IV_SIZE:
                            PAGE_SIZE - PAGE_RESERVE + IV_SIZE + HMAC_SIZE
                        ]
                        if not hmac_lib.compare_digest(computed_hmac, stored_hmac):
                            return False  # 密钥错误

                    encrypted = page[offset:PAGE_SIZE - PAGE_RESERVE]
                    plain = AES.new(enc_key, AES.MODE_CBC, iv=iv).decrypt(
                        encrypted
                    )
                    decrypted_page = bytearray(page)
                    if page_idx == 0:
                        decrypted_page[:SALT_SIZE] = SQLITE_HEADER
                        decrypted_page[
                            SALT_SIZE:PAGE_SIZE - PAGE_RESERVE
                        ] = plain
                    else:
                        decrypted_page[:PAGE_SIZE - PAGE_RESERVE] = plain
                    output.write(decrypted_page)
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            return False

        return True

    def open_decrypted(self) -> sqlite3.Connection:
        """打开解密后的数据库连接"""
        with self._lock:
            if self._conn:
                return self._conn

            decrypted_path = self.decrypt_to_temp()
            # 连接会被缓存并跨线程复用（FastAPI 将同步路由放入线程池执行，图片/表情
            # 服务也通过 asyncio.to_thread 调用）；CPython 的 sqlite3 模块在序列化
            # 模式下会保护语句级执行，跨线程使用是安全的。
            self._conn = sqlite3.connect(
                str(decrypted_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            return self._conn

    def _discard_decrypted_file(self) -> None:
        path = self._decrypted_path
        self._decrypted_path = None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
            self._discard_decrypted_file()

    def __enter__(self):
        return self.open_decrypted()

    def __exit__(self, *args):
        self.close()


def _verify_single(key_bytes: bytes, page: bytes, hash_name: str,
                   iters: int, reserve: int, hmac_size: int) -> bool:
    """单次 HMAC 验证"""
    try:
        salt = page[:SALT_SIZE]
        mac_salt = bytes(b ^ 0x3a for b in salt)
        enc_key = hashlib.pbkdf2_hmac(hash_name, key_bytes, salt, iters, KEY_SIZE)
        mac_key = hashlib.pbkdf2_hmac(hash_name, enc_key, mac_salt, 2, KEY_SIZE)

        hmac_data = page[SALT_SIZE:PAGE_SIZE - reserve + IV_SIZE]
        hmac_data += struct.pack("<I", 1)
        computed = hmac_lib.new(mac_key, hmac_data, hash_name).digest()
        stored = page[PAGE_SIZE - reserve + IV_SIZE:
                      PAGE_SIZE - reserve + IV_SIZE + hmac_size]
        return computed == stored
    except Exception:
        return False


def _verify_direct_key(key_bytes: bytes, page: bytes, reserve: int,
                       hmac_size: int, hash_name: str) -> bool:
    """将 key 直接作为 AES 加密密钥使用（跳过 PBKDF2）"""
    try:
        salt = page[:SALT_SIZE]
        mac_salt = bytes(b ^ 0x3a for b in salt)
        # key_bytes IS the derived encryption key, derive only mac_key
        mac_key = hashlib.pbkdf2_hmac(hash_name, key_bytes, mac_salt, 2, KEY_SIZE)

        hmac_data = page[SALT_SIZE:PAGE_SIZE - reserve + IV_SIZE]
        hmac_data += struct.pack("<I", 1)
        computed = hmac_lib.new(mac_key, hmac_data, hash_name).digest()
        stored = page[PAGE_SIZE - reserve + IV_SIZE:
                      PAGE_SIZE - reserve + IV_SIZE + hmac_size]
        return computed == stored
    except Exception:
        return False


def test_key(db_path: Path, key: str) -> bool:
    """使用微信 4.x WCDB 参数测试密钥是否有效。"""
    if len(key) != 64:
        return False

    try:
        key_bytes = bytes.fromhex(key)
        if len(key_bytes) != KEY_SIZE:
            return False
    except Exception:
        return False

    try:
        with open(db_path, "rb") as f:
            page = f.read(PAGE_SIZE)
        if len(page) < PAGE_SIZE:
            return False
    except Exception:
        return False

    # 微信 4.x 同时可能提供原始口令或已派生的直接密钥。
    strategies = [
        ("sha512", PBKDF2_ITER, PAGE_RESERVE, HMAC_SIZE, True),
        ("sha512", PBKDF2_ITER, PAGE_RESERVE, HMAC_SIZE, False),
    ]

    for hash_name, iters, reserve, hmac_size, use_pbkdf2 in strategies:
        try:
            if use_pbkdf2:
                ok = _verify_single(key_bytes, page, hash_name, iters, reserve, hmac_size)
            else:
                ok = _verify_direct_key(key_bytes, page, reserve, hmac_size, hash_name)
            if ok:
                return True
        except Exception:
            continue

    return False


def get_sqlcipher_status() -> dict:
    """检查解密工具状态"""
    return {
        "wcdb_pycrypto": HAS_CRYPTO,
        # 保留既有状态响应字段，旧客户端仍可安全判断这些备用项不可用。
        "sqlcipher3": False,
        "sqlcipher_cli": False,
    }
