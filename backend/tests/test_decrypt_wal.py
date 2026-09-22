import hashlib
import hmac
import os
import sqlite3
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from Crypto.Cipher import AES

from backend.decrypt import (
    DatabaseDecryptor,
    KEY_SIZE,
    HMAC_SIZE,
    IV_SIZE,
    PAGE_RESERVE,
    PAGE_SIZE,
    PBKDF2_ITER,
    SQLITE_HEADER,
    test_key,
)


class EncryptedWalTests(unittest.TestCase):
    def test_wechat_4_page_validates_and_decrypts_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "message_0.db"
            raw_pass = bytes(range(32))
            wrong_pass = bytes(reversed(range(32)))
            salt = bytes(range(16, 32))
            enc_key = hashlib.pbkdf2_hmac(
                "sha512", raw_pass, salt, PBKDF2_ITER, KEY_SIZE
            )
            mac_salt = bytes(value ^ 0x3A for value in salt)
            mac_key = hashlib.pbkdf2_hmac(
                "sha512", enc_key, mac_salt, 2, KEY_SIZE
            )
            plain = b"wechat-4-page" + bytes(
                PAGE_SIZE - PAGE_RESERVE - len(salt) - len(b"wechat-4-page")
            )
            iv = os.urandom(IV_SIZE)
            encrypted = AES.new(enc_key, AES.MODE_CBC, iv=iv).encrypt(plain)
            page_mac = hmac.new(
                mac_key,
                encrypted + iv + struct.pack("<I", 1),
                "sha512",
            ).digest()
            self.assertEqual(len(page_mac), HMAC_SIZE)
            database.write_bytes(salt + encrypted + iv + page_mac)

            self.assertTrue(test_key(database, raw_pass.hex()))
            self.assertFalse(test_key(database, wrong_pass.hex()))

            decryptor = DatabaseDecryptor(database, raw_pass.hex())
            self.addCleanup(decryptor.close)
            decrypted_path = decryptor.decrypt_to_temp()
            self.assertRegex(
                decrypted_path.name,
                rf"^wechat_db_{os.getpid()}_.+\.db$",
            )
            decrypted = decrypted_path.read_bytes()
            self.assertEqual(decrypted[: len(SQLITE_HEADER)], SQLITE_HEADER)
            self.assertEqual(
                decrypted[len(SQLITE_HEADER):PAGE_SIZE - PAGE_RESERVE],
                plain,
            )

    def test_open_decrypted_connection_allows_cross_thread_use(self):
        """同步路由在线程池中执行，缓存的解密连接必须允许跨线程使用。

        回归测试：``open_decrypted`` 若缺少 ``check_same_thread=False``，
        解析器连接在事件循环/某个工作线程创建后，其他线程使用即抛
        ``sqlite3.ProgrammingError``。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain_db = root / "plain.db"
            setup = sqlite3.connect(plain_db)
            try:
                setup.execute("CREATE TABLE sample (value INTEGER)")
                setup.execute("INSERT INTO sample VALUES (42)")
                setup.commit()
            finally:
                setup.close()

            placeholder = root / "message_0.db"
            placeholder.write_bytes(b"\x00" * 16)
            decryptor = DatabaseDecryptor(placeholder, "ab" * 32)
            try:
                with mock.patch.object(
                    decryptor, "decrypt_to_temp", return_value=plain_db
                ):
                    conn = decryptor.open_decrypted()

                failures = []

                def worker():
                    try:
                        row = conn.execute("SELECT value FROM sample").fetchone()
                        if not row or row[0] != 42:
                            failures.append(f"unexpected row: {row!r}")
                    except Exception as exc:  # noqa: BLE001 - 报告任意失败
                        failures.append(repr(exc))

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join()
                self.assertEqual(failures, [])
            finally:
                # 必须在 TemporaryDirectory 清理前释放 plain.db
                decryptor.close()

    def test_applies_only_committed_encrypted_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "message_resource.db"
            output = root / "plain.db"
            raw_pass = bytes(range(32))
            salt = bytes(range(16, 32))
            database.write_bytes(salt + bytes(PAGE_SIZE - len(salt)))
            output.write_bytes(bytes(PAGE_SIZE * 2))

            enc_key = hashlib.pbkdf2_hmac(
                "sha512", raw_pass, salt, PBKDF2_ITER, KEY_SIZE
            )
            visible = b"wal-only-image-resource"
            plain_data = visible + bytes(PAGE_SIZE - PAGE_RESERVE - len(visible))
            iv = os.urandom(16)
            encrypted_data = AES.new(enc_key, AES.MODE_CBC, iv=iv).encrypt(plain_data)
            encrypted_page = encrypted_data + iv + bytes(PAGE_RESERVE - len(iv))

            wal_salt_1 = 0x01020304
            wal_salt_2 = 0x05060708
            wal_header = bytearray(32)
            wal_header[:4] = struct.pack(">I", 0x377F0682)
            wal_header[8:12] = struct.pack(">I", PAGE_SIZE)
            wal_header[16:20] = struct.pack(">I", wal_salt_1)
            wal_header[20:24] = struct.pack(">I", wal_salt_2)
            frame_header = (
                struct.pack(">I", 2)
                + struct.pack(">I", 2)
                + struct.pack(">I", wal_salt_1)
                + struct.pack(">I", wal_salt_2)
                + bytes(8)
            )
            Path(str(database) + "-wal").write_bytes(
                bytes(wal_header) + frame_header + encrypted_page
            )

            decryptor = DatabaseDecryptor(database, raw_pass.hex())
            decryptor._decrypted_path = output
            self.assertEqual(decryptor._apply_encrypted_wal(), 1)
            self.assertEqual(output.read_bytes()[PAGE_SIZE:PAGE_SIZE + len(visible)], visible)

    def test_failed_decryption_removes_partial_temporary_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "invalid.db"
            database.write_bytes(bytes(PAGE_SIZE))
            decryptor = DatabaseDecryptor(database, bytes(range(32)).hex())

            with self.assertRaises(RuntimeError):
                decryptor.decrypt_to_temp()

            self.assertIsNone(decryptor._decrypted_path)


if __name__ == "__main__":
    unittest.main()
