import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import api
from backend.config import WeChatAccount
from backend.parser_v4 import MessageParserV4


def make_message_connection(username: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
    connection.execute("INSERT INTO Name2Id VALUES (?)", (username,))
    return connection


class WeChat4OnlyParserApiTests(unittest.TestCase):
    def test_get_parser_wires_all_v4_shards_and_contact_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            account_root = root / "wxid_self"
            message_dir = account_root / "db_storage" / "message"
            contact_db = account_root / "db_storage" / "contact" / "contact.db"
            message_dir.mkdir(parents=True)
            contact_db.parent.mkdir(parents=True)
            shard_paths = [message_dir / "message_0.db", message_dir / "message_1.db"]
            for path in (*shard_paths, contact_db):
                path.write_bytes(b"encrypted")

            shard_0 = make_message_connection("wxid_friend_0")
            shard_1 = make_message_connection("wxid_friend_1")
            contact_connection = sqlite3.connect(":memory:")
            contact_connection.row_factory = sqlite3.Row
            contact_connection.execute(
                "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT)"
            )
            contact_connection.execute(
                "INSERT INTO contact VALUES (?, ?, ?)",
                ("wxid_friend_0", "好友备注", "好友昵称"),
            )
            self.addCleanup(shard_0.close)
            self.addCleanup(shard_1.close)
            self.addCleanup(contact_connection.close)

            connections = {
                "message_0.db": shard_0,
                "message_1.db": shard_1,
                "contact.db": contact_connection,
            }

            class FakeDecryptor:
                def __init__(self, path, _key):
                    self.path = Path(path)

                def open_decrypted(self):
                    return connections[self.path.name]

            account = WeChatAccount(
                "wxid_self",
                root,
                msg_dir=message_dir,
                micromsg_db=contact_db,
            )
            with patch.object(api.config, "accounts", [account]), patch.object(
                api.config, "active_index", 0
            ), patch.object(api.config, "key", "ab" * 32), patch.object(
                api, "DatabaseDecryptor", FakeDecryptor
            ), patch.dict(api._parser_cache, {}, clear=True), patch.dict(
                api._decryptor_cache, {}, clear=True
            ):
                parser = api.get_parser()

            self.assertIsInstance(parser, MessageParserV4)
            self.assertEqual(parser.conns, [shard_0, shard_1])
            self.assertIs(parser.contact_conn, contact_connection)
            self.assertEqual(parser._name_cache["wxid_friend_0"], "好友备注")


if __name__ == "__main__":
    unittest.main()
