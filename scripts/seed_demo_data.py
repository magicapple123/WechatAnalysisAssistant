"""生成完全虚构的演示数据集，用于项目宣传截图。

- 在 runtime/demo/profile/xwechat_files/wxid_demo2026 下生成微信 4.x 目录结构，
  含 WCDB 加密的 message_0.db（多会话虚构聊天）与 contact.db（虚构联系人）。
- 在 runtime/demo/appdata 下生成隔离的 wechat_keys.json（演示密钥），
  配合 WECHAT_ASSISTANT_DATA_DIR 环境变量实现与真实数据的完全隔离。
- 所有人名/群名/文本内容均带「示例」标记；手机号 13800000000；域名 example.com。

用法:
    python scripts/seed_demo_data.py            # 生成
    python scripts/seed_demo_data.py --check    # 生成后用项目解密器自校验

本脚本只写 runtime/demo/ 目录，不触碰任何真实微信数据。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from Crypto.Cipher import AES  # noqa: E402

from backend.decrypt import (  # noqa: E402
    KEY_SIZE,
    PAGE_RESERVE,
    PAGE_SIZE,
    PBKDF2_ITER,
    SALT_SIZE,
)

DEMO_WXID = "wxid_demo2026"
DEMO_KEY = hashlib.sha256("WechatAnalysisAssistant-demo-key".encode()).hexdigest()
DEMO_DISPLAY_NAME = "示例用户"
STICKER_CACHE_MAX_BYTES = 0  # 占位，避免误用

EN = "https://example.com"


def encrypt_wcdb(plain: bytes, key_hex: str) -> bytes:
    """把整库明文按 WCDB 4.x 页面格式加密（decrypt.py 的逆过程）。"""
    assert len(plain) % PAGE_SIZE == 0, "数据库必须严格按 4096 字节分页"
    key_bytes = bytes.fromhex(key_hex)
    salt = os.urandom(SALT_SIZE)
    enc_key = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, PBKDF2_ITER, KEY_SIZE)
    mac_salt = bytes(v ^ 0x3A for v in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, KEY_SIZE)

    out = bytearray()
    total_pages = len(plain) // PAGE_SIZE
    for number in range(1, total_pages + 1):
        chunk = plain[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]
        offset = SALT_SIZE if number == 1 else 0
        iv = os.urandom(16)
        cipher = AES.new(enc_key, AES.MODE_CBC, iv).encrypt(chunk[offset : PAGE_SIZE - PAGE_RESERVE])
        mac = hashlib.sha512(mac_key).digest()  # 占位，下一行才是真 HMAC
        mac = hashlib.sha512(mac_key).digest()[:0] + __hmac(mac_key, cipher, iv, number)
        out += (salt if number == 1 else b"") + cipher + iv + mac
    return bytes(out)


def __hmac(mac_key: bytes, cipher: bytes, iv: bytes, number: int) -> bytes:
    import hmac as hmac_lib

    return hmac_lib.new(mac_key, cipher + iv + struct.pack("<I", number), "sha512").digest()


def create_reserved_db(path: Path) -> sqlite3.Connection:
    """创建每页预留 80 字节的 SQLite 库（WCDB 解密还原后的页面布局）。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    # user_version 只写页头、不产生任何 cell，保证页尾 80 字节空闲可预留
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    with open(path, "r+b") as handle:  # 页头第 20 字节 = 每页预留字节数
        handle.seek(20)
        handle.write(bytes([80]))
        # page1 btree 头的「cell 内容区起始」(偏移 100+5) 仍为 4096，
        # 需同步改为 4096-80=4016（0x0FB0），否则 free space 校验报 malformed
        handle.seek(100 + 5)
        handle.write(struct.pack(">H", PAGE_SIZE - PAGE_RESERVE))
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def now_ts(days_ago: float, hour: int = 10, minute: int = 30) -> int:
    base = time.time() - days_ago * 86400
    struct_time = time.localtime(base)
    return int(time.mktime((struct_time.tm_year, struct_time.tm_mon, struct_time.tm_mday, hour, minute, 0, 0, 0, -1)))


def build_message_db(path: Path) -> None:
    conn = create_reserved_db(path)
    cur = conn.cursor()

    name2id = [
        # 注意：Name2Id 不含自己（真实微信如此），聊天列表才不会出现「示例用户」。
        # self 判定独立于此表：real_sender_id=1 出现在所有 1v1 消息表中。
        (2, "wxid_demo_zhangwei"),
        (3, "wxid_demo_liting"),
        (4, "wxid_demo_wangfang"),
        (5, "wxid_demo_liuyang"),
        (6, "99988877766@chatroom"),
    ]
    cur.execute("CREATE TABLE Name2Id (user_name TEXT)")
    cur.executemany("INSERT INTO Name2Id (rowid, user_name) VALUES (?, ?)", name2id)

    chats: dict[str, list[tuple]] = {}

    group = "99988877766@chatroom"

    def msg(local_id, sender_rowid, ctype, ts, content, ct_flag=0):
        server_id = 770000000 + local_id * 7
        return (local_id, server_id, ctype, sender_rowid, ts, content, b"", b"", ct_flag)

    # --- 群聊：示例-产品讨论群 ---
    rows = []
    ts0 = now_ts(2, 9, 15)
    rows.append(msg(1, 4, 1, ts0, "各位早上好，今天 10 点对齐一下示例项目排期"))
    rows.append(msg(2, 2, 1, ts0 + 180, "收到，我这边把上周的示例数据整理好了"))
    rows.append(msg(3, 3, 1, ts0 + 240, "示例模块的联调环境我已经部署到测试服了"))
    rows.append(msg(4, 1, 1, ts0 + 400, "辛苦大家，会议链接稍后发到群里"))
    rows.append(msg(5, 5, 49, ts0 + 420, (
        "<msg><appmsg><title>示例项目排期表（9 月）</title><des>包含里程碑与分工的示例文档</des>"
        f"<type>5</type><url>{EN}/docs/demo-plan</url></appmsg></msg>"
    )))
    rows.append(msg(6, 1, 10000, ts0 + 500, "示例-李婷 邀请 示例-王芳 加入了群聊"))
    rows.append(msg(7, 4, 1, ts0 + 700, "欢迎新同事～示例团队欢迎你"))
    rows.append(msg(8, 2, 49, ts0 + 900, (
        "<msg><appmsg><title>示例接口文档 v2</title><des>更新了示例接口的返回结构</des>"
        f"<type>57</type><url>{EN}/docs/demo-api</url>"
        "<refermsg><type>1</type><svrid>7700000014</svrid><fromusr>" + group +
        "</fromusr><chatusr>wxid_demo_liuyang</chatusr><displayname>示例-刘洋</displayname>"
        "<content>示例接口的返回结构谁能同步一下？</content></refermsg></appmsg></msg>"
    )))
    rows.append(msg(9, 3, 47, ts0 + 960, "[示例表情]"))
    rows.append(msg(10, 1, 1, now_ts(0, 9, 41), "上午的示例评审结论我整理好了，下午发出来"))
    rows.append(msg(11, 5, 1, now_ts(0, 9, 45), "收到，示例版本的发布时间定在周五"))
    chats[group] = rows

    # --- 张伟：日常闲聊 ---
    rows = []
    ts1 = now_ts(1, 20, 10)
    rows.append(msg(1, 2, 1, ts1, "周末的示例聚会你来吗？"))
    rows.append(msg(2, 1, 1, ts1 + 120, "来！我把示例场地订好了"))
    rows.append(msg(3, 2, 1, ts1 + 200, "👍 那我把名单统计一下"))
    rows.append(msg(4, 2, 49, ts1 + 300, (
        "<msg><appmsg><title>示例餐厅订座确认</title><des>周六 18:00，8 人桌</des>"
        f"<type>5</type><url>{EN}/booking/demo123</url></appmsg></msg>"
    )))
    rows.append(msg(5, 1, 1, now_ts(0, 8, 30), "早，今天通勤路上看了你推荐的示例播客，不错"))
    rows.append(msg(6, 2, 1, now_ts(0, 8, 42), "哈哈示例播客是我最近的下饭神器"))
    chats["wxid_demo_zhangwei"] = rows

    # --- 李婷：出行计划 ---
    rows = []
    ts2 = now_ts(3, 15, 20)
    rows.append(msg(1, 3, 1, ts2, "示例城市的天气这周末不错，适合去拍外景"))
    rows.append(msg(2, 1, 1, ts2 + 200, "好，示例相机的电池我充上"))
    rows.append(msg(3, 3, 49, ts2 + 600, (
        "<msg><appmsg><title>示例民宿预订</title><des>两晚套房，含示例早餐</des>"
        f"<type>5</type><url>{EN}/stay/demo-room</url></appmsg></msg>"
    )))
    rows.append(msg(4, 1, 49, ts2 + 800, (
        "<msg><appmsg><title>微信转账</title><type>2000</type>"
        "<wcpayinfo><feedesc>¥288.00</feedesc><paysubtype>1</paysubtype></wcpayinfo></appmsg></msg>"
    )))
    rows.append(msg(5, 3, 1, ts2 + 900, "示例民宿的定金我收到了，周五出发🚗"))
    chats["wxid_demo_liting"] = rows

    # --- 王芳：工作对接 ---
    rows = []
    ts3 = now_ts(0, 9, 2)
    rows.append(msg(1, 4, 1, ts3, "示例报告初稿在附件里，麻烦帮忙看看第二部分"))
    rows.append(msg(2, 1, 1, ts3 + 300, "收到，我 11 点前把批注发你"))
    rows.append(msg(3, 4, 34, ts3 + 600, ""))
    rows.append(msg(4, 4, 1, ts3 + 700, "语音里说的示例口径以文档为准哈"))
    chats["wxid_demo_wangfang"] = rows

    # --- 刘洋：发布检查 ---
    rows = []
    ts4 = now_ts(1, 16, 40)
    rows.append(msg(1, 5, 1, ts4, "示例环境的冒烟测试我跑完了，全部通过"))
    rows.append(msg(2, 1, 1, ts4 + 400, "效率很高，示例包我来打"))
    rows.append(msg(3, 5, 1, ts4 + 800, "好嘞，打完包我在群里同步"))
    chats["wxid_demo_liuyang"] = rows

    for talker, chat_rows in chats.items():
        table = f"Msg_{hashlib.md5(talker.encode()).hexdigest()}"
        cur.execute(
            f"""CREATE TABLE [{table}] (
                local_id INTEGER, server_id INTEGER, local_type INTEGER,
                real_sender_id INTEGER, create_time INTEGER,
                message_content BLOB, source BLOB, packed_info_data BLOB,
                WCDB_CT_message_content INTEGER)"""
        )
        cur.executemany(f"INSERT INTO [{table}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", chat_rows)

    conn.commit()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def build_contact_db(path: Path) -> None:
    conn = create_reserved_db(path)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE Contact (
            user_name TEXT, nick_name TEXT, remark TEXT, alias TEXT,
            description TEXT, local_type INTEGER)"""
    )
    rows = [
        (DEMO_WXID, DEMO_DISPLAY_NAME, "", "DemoUser", "示例账号自我介绍", 1),
        ("wxid_demo_zhangwei", "张伟Demo", "示例-张伟", "demo_zw", "示例好友：产品同事", 1),
        ("wxid_demo_liting", "李婷Demo", "示例-李婷", "demo_lt", "示例好友：摄影搭子", 1),
        ("wxid_demo_wangfang", "王芳Demo", "示例-王芳", "demo_wf", "示例好友：报告对接", 1),
        ("wxid_demo_liuyang", "刘洋Demo", "示例-刘洋", "demo_ly", "示例好友：测试同事", 1),
        ("99988877766@chatroom", "示例-产品讨论群", "", "", "示例群聊", 2),
    ]
    cur.executemany("INSERT INTO Contact VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def write_encrypted(plain_db: Path, target: Path) -> None:
    plain = plain_db.read_bytes()
    assert len(plain) % PAGE_SIZE == 0, f"{plain_db} 不是 4096 的整数倍"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encrypt_wcdb(plain, DEMO_KEY))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成虚构演示数据集")
    parser.add_argument("--check", action="store_true", help="生成后用项目解密器自校验")
    args = parser.parse_args()

    demo_root = REPO_ROOT / "runtime" / "demo"
    profile = demo_root / "profile"
    appdata = demo_root / "appdata"
    account_root = profile / "xwechat_files" / DEMO_WXID
    msg_dir = account_root / "db_storage" / "message"
    contact_dir = account_root / "db_storage" / "contact"

    work = demo_root / "_plain"
    work.mkdir(parents=True, exist_ok=True)
    plain_msg = work / "message_0.plain.db"
    plain_contact = work / "contact.plain.db"

    build_message_db(plain_msg)
    build_contact_db(plain_contact)
    write_encrypted(plain_msg, msg_dir / "message_0.db")
    write_encrypted(plain_contact, contact_dir / "contact.db")

    appdata.mkdir(parents=True, exist_ok=True)
    import json

    keys = {
        DEMO_WXID: {
            "key": DEMO_KEY,
            "display_name": DEMO_DISPLAY_NAME,
            "alias": "DemoUser",
        }
    }
    (appdata / "wechat_keys.json").write_text(
        json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"演示账号: {DEMO_WXID} (display={DEMO_DISPLAY_NAME})")
    print(f"微信目录: {account_root}")
    print(f"隔离数据目录: {appdata}")
    print(f"演示密钥: {DEMO_KEY}")

    if not args.check:
        return 0

    from backend.decrypt import DatabaseDecryptor
    from backend.parser_v4 import MessageParserV4

    msg_conn = DatabaseDecryptor(msg_dir / "message_0.db", DEMO_KEY).open_decrypted()
    contact_conn = DatabaseDecryptor(contact_dir / "contact.db", DEMO_KEY).open_decrypted()
    for conn in (msg_conn, contact_conn):
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "解密后 integrity_check 失败"

    wx_parser = MessageParserV4([msg_conn], account_root.parent, DEMO_WXID, contact_conn)
    contacts = wx_parser.get_contacts()
    assert len(contacts) >= 4, f"联系人数量异常: {len(contacts)}"
    for contact in contacts:
        assert "示例" in (contact.get("display_name") or ""), f"联系人缺少虚构标记: {contact}"
    chat_count = 0
    for contact in contacts:
        talker = contact["talker"]
        result = wx_parser.get_messages(talker, page=1, page_size=100)
        if result["total"] > 0:
            chat_count += 1
    assert chat_count >= 4, f"有消息的会话数量异常: {chat_count}"
    names = sorted(c["display_name"] for c in contacts if c.get("last_message"))
    print("自校验通过，会话:", "、".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
