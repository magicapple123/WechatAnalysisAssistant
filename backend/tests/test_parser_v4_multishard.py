import base64
import hashlib
import html
import json
import sqlite3
import unittest
from unittest.mock import patch

from backend.parser_v4 import MessageParserV4


def make_shard(
    talker: str,
    rows: list[tuple],
    *,
    name2id_users: list[str] | None = None,
) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    table = f"Msg_{hashlib.md5(talker.encode()).hexdigest()}"
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.executemany(
        "INSERT INTO Name2Id VALUES (?)",
        [(username,) for username in (name2id_users or [talker])],
    )
    conn.execute(
        f"""
        CREATE TABLE [{table}] (
            local_id INTEGER,
            server_id INTEGER,
            local_type INTEGER,
            real_sender_id INTEGER,
            create_time INTEGER,
            message_content BLOB,
            source BLOB,
            packed_info_data BLOB,
            WCDB_CT_message_content INTEGER
        )
        """
    )
    conn.executemany(
        f"INSERT INTO [{table}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    return conn


class ParserV4MultiShardTests(unittest.TestCase):
    def test_merges_shards_and_normalizes_composite_image_type(self):
        talker = "wxid_friend"
        shard_a = make_shard(
            talker,
            [
                (1, 101, 1, 10, 100, "first", b"", b"", 0),
                (3, 103, 2, 10, 150, b"legacy-image", b"", b"", 0),
            ],
        )
        shard_b = make_shard(
            talker,
            [(2, 102, (9 << 32) | 3, 10, 200, b"binary", b"", b"", 0)],
        )
        self.addCleanup(shard_a.close)
        self.addCleanup(shard_b.close)

        parser = MessageParserV4([shard_a, shard_b], None, "wxid_self", None)
        result = parser.get_messages(talker, page=1, page_size=10)
        self.assertEqual(result["total"], 3)
        self.assertEqual([message["id"] for message in result["messages"]], [1, 3, 2])
        image = result["messages"][2]
        self.assertEqual(image["type"], 3)
        self.assertEqual(image["raw_type"], (9 << 32) | 3)
        self.assertEqual(image["content"], "[图片]")

        filtered = parser.get_messages(talker, page=1, page_size=10, msg_type=3)
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(
            [message["id"] for message in filtered["messages"]],
            [3, 2],
        )

    def test_group_sender_username_uses_name2id_rowid_per_shard(self):
        talker = "test-room@chatroom"
        shard_a = make_shard(
            talker,
            [(1, 101, 3, 2, 100, b"binary-image-a", b"", b"", 0)],
            name2id_users=[talker, "wxid_alice"],
        )
        shard_b = make_shard(
            talker,
            [(2, 102, 3, 2, 200, b"binary-image-b", b"", b"", 0)],
            name2id_users=[talker, "wxid_bob"],
        )
        self.addCleanup(shard_a.close)
        self.addCleanup(shard_b.close)

        parser = MessageParserV4(
            [shard_a, shard_b], None, "wxid_self_abcd", None
        )
        result = parser.get_messages(talker, page=1, page_size=10)

        self.assertEqual(
            [message["sender_username"] for message in result["messages"]],
            ["wxid_alice", "wxid_bob"],
        )
        self.assertEqual(
            [message["sender_name"] for message in result["messages"]],
            ["wxid_alice", "wxid_bob"],
        )
        self.assertTrue(
            all(message["is_sender"] is False for message in result["messages"])
        )

    def test_name2id_mapping_beats_spoofed_body_prefix_and_identifies_self(self):
        talker = "test-room@chatroom"
        shard = make_shard(
            talker,
            [
                (
                    1,
                    101,
                    1,
                    2,
                    100,
                    "wxid_spoof:\n<msg>hello</msg>",
                    b"",
                    b"",
                    0,
                ),
                (2, 102, 1, 3, 200, "sent by self", b"", b"", 0),
            ],
            name2id_users=[talker, "wxid_real_member", "wxid_self"],
        )
        self.addCleanup(shard.close)

        parser = MessageParserV4([shard], None, "wxid_self_abcd", None)
        result = parser.get_messages(talker, page=1, page_size=10)

        incoming, outgoing = result["messages"]
        self.assertEqual(incoming["sender_username"], "wxid_real_member")
        self.assertEqual(incoming["sender_name"], "wxid_real_member")
        self.assertIs(incoming["is_sender"], False)
        self.assertEqual(outgoing["sender_username"], "wxid_self")
        self.assertIs(outgoing["is_sender"], True)

    def test_voice_duration_and_sticker_public_private_contracts(self):
        talker = "wxid_friend"
        description = "开心呀".encode("utf-8")
        encoded_description = base64.b64encode(
            b"default\x12" + bytes([len(description)]) + description
        ).decode("ascii")
        sticker_md5 = "a" * 32
        extern_md5 = "b" * 32
        sticker_xml = (
            '<msg><emoji md5="' + sticker_md5 + '" '
            'externmd5="' + extern_md5 + '" '
            'desc="' + encoded_description + '" '
            'thumburl="https://emoji.qpic.cn/private-thumb" '
            'encrypturl="https://emoji.qpic.cn/private-encrypted" '
            'aeskey="0123456789abcdef0123456789abcdef" /></msg>'
        )
        shard = make_shard(
            talker,
            [
                (
                    1, 101, 34, 1, 100,
                    '<msg><voicemsg voicelength="3300" /></msg>',
                    b"", b"", 0,
                ),
                (2, 102, 47, 1, 200, sticker_xml, b"", b"", 0),
                (3, 103, 47, 1, 300, '<msg><emoji /></msg>', b"", b"", 0),
                (
                    4, 104, 47, 1, 400,
                    '<!DOCTYPE x [<!ENTITY y SYSTEM "file:///secret">]>'
                    '<msg><emoji md5="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" /></msg>',
                    b"", b"", 0,
                ),
            ],
        )
        self.addCleanup(shard.close)

        parser = MessageParserV4([shard], None, "wxid_self", None)
        messages = parser.get_messages(talker, page=1, page_size=10)["messages"]

        self.assertEqual(messages[0]["voice_duration_seconds"], 3.3)
        sticker = messages[1]
        self.assertEqual(
            sticker["sticker"],
            {
                "md5": sticker_md5,
                "description": "开心呀",
                "available": True,
            },
        )
        self.assertEqual(sticker["content"], "[表情] 开心呀")
        self.assertNotIn("url", json.dumps(sticker["sticker"]))
        self.assertEqual(
            sticker["_sticker_source"]["thumb_url"],
            "https://emoji.qpic.cn/private-thumb",
        )
        self.assertEqual(sticker["_sticker_source"]["extern_md5"], extern_md5)
        self.assertNotIn("extern_md5", sticker["sticker"])
        self.assertIn("aes_key", sticker["_sticker_source"])
        self.assertEqual(
            messages[2]["sticker"],
            {"md5": "", "description": "", "available": False},
        )
        self.assertEqual(
            messages[3]["sticker"],
            {"md5": "", "description": "", "available": False},
        )

    def test_reply_is_structured_and_type_name_is_reply(self):
        talker = "wxid_friend"
        image_md5 = "b" * 32
        embedded_image = html.escape(
            '<msg><img md5="' + image_md5 + '" '
            'cdnurl="https://private.example/image" aeskey="secret" '
            'cdnthumbwidth="320" cdnthumbheight="240" /></msg>'
        )
        reply_xml = (
            '<msg><appmsg><title>这张不错</title><type>57</type><refermsg>'
            '<type>3</type><svrid>90001</svrid>'
            '<fromusr>wxid_original</fromusr><chatusr>wxid_original</chatusr>'
            '<displayname>原昵称</displayname><createtime>1700000000</createtime>'
            f'<content>{embedded_image}</content>'
            '</refermsg></appmsg></msg>'
        )
        shard = make_shard(
            talker,
            [(1, 70001, (57 << 32) | 49, 1, 1700000100, reply_xml, b"", b"", 0)],
        )
        self.addCleanup(shard.close)
        contact = sqlite3.connect(":memory:")
        contact.execute(
            "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)"
        )
        contact.execute(
            "INSERT INTO contact VALUES (?, ?, ?)",
            ("wxid_original", "原昵称", "好友备注"),
        )
        contact.commit()
        self.addCleanup(contact.close)

        parser = MessageParserV4(
            [shard], None, "wxid_self", contact
        )
        message = parser.get_messages(talker, page=1, page_size=10)["messages"][0]

        self.assertEqual(message["type_name"], "回复")
        self.assertIn("这张不错", message["content"])
        self.assertIn("回复 好友备注", message["content"])
        reply = message["reply"]
        self.assertEqual(reply["reply_text"], "这张不错")
        self.assertEqual(reply["refer_type"], 3)
        self.assertEqual(reply["refer_type_name"], "图片")
        self.assertEqual(reply["sender_username"], "wxid_original")
        self.assertEqual(reply["sender_name"], "好友备注")
        self.assertEqual(reply["server_id"], "90001")
        self.assertEqual(reply["create_time"], 1700000000)
        self.assertEqual(
            reply["media"],
            {
                "kind": "image",
                "md5": image_md5,
                "available": True,
                "width": 320,
                "height": 240,
            },
        )
        public_reply = json.dumps(reply, ensure_ascii=False)
        self.assertNotIn("cdnurl", public_reply)
        self.assertNotIn("private.example", public_reply)
        self.assertNotIn("aeskey", public_reply)

    def test_reply_sticker_keeps_download_material_private(self):
        talker = "wxid_friend"
        sticker_md5 = "c" * 32
        embedded = html.escape(
            '<msg><emoji md5="' + sticker_md5 + '" '
            'externurl="https://emoji.qpic.cn/private" aeskey="hidden" /></msg>'
        )
        reply_xml = (
            '<msg><appmsg><title>哈哈</title><type>57</type><refermsg>'
            '<type>47</type><svrid>90002</svrid><displayname>朋友</displayname>'
            f'<content>{embedded}</content>'
            '</refermsg></appmsg></msg>'
        )
        shard = make_shard(
            talker,
            [(1, 70002, 49, 1, 1700000200, reply_xml, b"", b"", 0)],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        reply = parser.get_messages(talker, page=1, page_size=10)["messages"][0]["reply"]
        self.assertEqual(
            reply["media"],
            {
                "kind": "sticker",
                "md5": sticker_md5,
                "description": "",
                "available": True,
            },
        )
        self.assertNotIn("url", json.dumps(reply["media"]))
        self.assertEqual(
            reply["_sticker_source"]["extern_url"],
            "https://emoji.qpic.cn/private",
        )

    def test_compressed_type_one_appmsg_is_still_labeled_reply(self):
        talker = "wxid_friend"
        reply_xml = (
            '<msg><appmsg><title>收到</title><type>57</type><refermsg>'
            '<type>1</type><content>原文</content><displayname>朋友</displayname>'
            '</refermsg></appmsg></msg>'
        )
        shard = make_shard(
            talker,
            [(1, 10, 1, 1, 100, b"\x28\xb5\x2f\xfdcompressed", b"", b"", 4)],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        with patch("backend.parser_v4._decompress_zstd", return_value=reply_xml):
            message = parser.get_messages(talker, page=1, page_size=10)["messages"][0]
        self.assertEqual(message["type_name"], "回复")
        self.assertEqual(message["reply"]["summary"], "原文")

    def test_transfer_and_red_packet_messages_have_semantic_labels(self):
        talker = "wxid_friend"
        transfer_xml = (
            "<msg><appmsg><title>微信转账</title><type>2000</type>"
            "<wcpayinfo><paysubtype>3</paysubtype><feeDesc>￥88.00</feeDesc>"
            "<paymemo>晚餐</paymemo><transferid>private-transfer-id</transferid>"
            "<transcationid>private-transaction-id</transcationid>"
            "</wcpayinfo></appmsg></msg>"
        )
        red_packet_xml = (
            "<msg><appmsg><title>微信红包</title><type>2001</type>"
            "<wcpayinfo><scenetext>微信红包</scenetext>"
            "<sendertitle>恭喜发财</sendertitle>"
            "<nativeurl>wxpay://c2cbizmessagehandler/hongbao/receivehongbao?"
            "sendusername=private</nativeurl></wcpayinfo></appmsg></msg>"
        )
        normal_link_xml = (
            "<msg><appmsg><title>红包使用攻略</title><type>5</type>"
            "<url>https://example.test/article</url></appmsg></msg>"
        )
        shard = make_shard(
            talker,
            [
                (1, 101, 49, 1, 100, transfer_xml, b"", b"", 0),
                (2, 102, 49, 1, 200, red_packet_xml, b"", b"", 0),
                (3, 103, (2000 << 32) | 49, 1, 300, "<broken", b"", b"", 0),
                (4, 104, 49, 1, 400, normal_link_xml, b"", b"", 0),
            ],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        messages = parser.get_messages(talker, page=1, page_size=10)["messages"]
        transfer, red_packet, damaged, normal_link = messages

        self.assertEqual(transfer["type"], 49)
        self.assertEqual(transfer["type_name"], "转账")
        self.assertEqual(
            transfer["payment"],
            {
                "kind": "transfer",
                "label": "转账",
                "amount": "¥88.00",
                "status": "已收款",
                "memo": "晚餐",
                "title": "微信转账",
            },
        )
        self.assertIn("¥88.00", transfer["content"])
        self.assertIn("晚餐", transfer["content"])
        serialized_transfer = json.dumps(transfer, ensure_ascii=False)
        self.assertNotIn("private-transfer-id", serialized_transfer)
        self.assertNotIn("private-transaction-id", serialized_transfer)

        self.assertEqual(red_packet["type"], 49)
        self.assertEqual(red_packet["type_name"], "红包")
        self.assertEqual(red_packet["payment"]["kind"], "red_packet")
        self.assertEqual(red_packet["payment"]["amount"], "")
        self.assertIn("恭喜发财", red_packet["content"])
        self.assertNotIn("sendusername", json.dumps(red_packet, ensure_ascii=False))

        self.assertEqual(damaged["type_name"], "转账")
        self.assertEqual(damaged["content"], "[转账]")
        self.assertEqual(damaged["payment"]["kind"], "transfer")

        self.assertEqual(normal_link["type_name"], "链接")
        self.assertNotIn("payment", normal_link)

    def test_payment_fallbacks_keep_labels_amounts_and_private_ids_safe(self):
        talker = "wxid_friend"
        missing_inner_type = (
            "<msg><appmsg><title>微信转账</title></appmsg></msg>"
        )
        nested_amount = (
            "<msg><appmsg><title>微信转账</title><type>2000</type>"
            "<wcpayinfo><paysubtype>3</paysubtype>"
            "<feedesc>(null)</feedesc>"
            "<feedescxml><span>￥ 12.30 元</span></feedescxml>"
            "<pay_memo>(null)</pay_memo></wcpayinfo></appmsg></msg>"
        )
        padded_amount = (
            "<msg><appmsg><title>微信转账</title><type>2000</type>"
            "<wcpayinfo><feedesc>¥9.90</feedesc></wcpayinfo>"
            "</appmsg></msg>\x00\x00"
        )
        damaged_private_xml = (
            "<msg><appmsg><type>2001</type>"
            "<nativeurl>wxpay://c2cbizmessagehandler/hongbao/receivehongbao"
            "</nativeurl><paymsgid>SYNTH_PRIVATE_PAYMENT_ID</paymsgid>"
        )
        double_encoded_private_xml = (
            "wxpay://c2cbizmessagehandler/hongbao/receivehongbao "
            "&amp;lt;paymsgid&amp;gt;DOUBLE_ENCODED_PRIVATE_ID"
            "&amp;lt;/paymsgid&amp;gt;"
        )
        valid_reply_with_conflicting_subtype = (
            "<msg><appmsg><title>收到</title><type>57</type><refermsg>"
            "<type>1</type><content>原消息</content><displayname>朋友</displayname>"
            "</refermsg></appmsg></msg>"
        )
        shard = make_shard(
            talker,
            [
                (
                    1, 101, (2000 << 32) | 49, 1, 100,
                    missing_inner_type, b"", b"", 0,
                ),
                (2, 102, 49, 1, 200, nested_amount, b"", b"", 0),
                (3, 103, 49, 1, 300, damaged_private_xml, b"", b"", 0),
                (4, 104, 49, 1, 400, double_encoded_private_xml, b"", b"", 0),
                (
                    5, 105, (2000 << 32) | 49, 1, 500,
                    valid_reply_with_conflicting_subtype, b"", b"", 0,
                ),
                (6, 106, 49, 1, 600, padded_amount, b"", b"", 0),
            ],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        (
            inferred, nested, damaged, double_encoded, valid_reply, padded
        ) = parser.get_messages(talker, page=1, page_size=10)["messages"]

        self.assertEqual(inferred["type_name"], "转账")
        self.assertEqual(inferred["content"], "[转账]")
        self.assertEqual(nested["payment"]["amount"], "¥12.30")
        self.assertEqual(nested["payment"]["memo"], "")
        self.assertNotIn("(null)", nested["content"])
        self.assertEqual(damaged["type_name"], "红包")
        self.assertEqual(damaged["content"], "[红包]")
        self.assertNotIn(
            "SYNTH_PRIVATE_PAYMENT_ID",
            json.dumps(damaged, ensure_ascii=False),
        )
        self.assertEqual(double_encoded["content"], "[红包]")
        self.assertNotIn(
            "DOUBLE_ENCODED_PRIVATE_ID",
            json.dumps(double_encoded, ensure_ascii=False),
        )
        self.assertEqual(valid_reply["type_name"], "回复")
        self.assertIn("reply", valid_reply)
        self.assertNotIn("payment", valid_reply)
        self.assertEqual(padded["type_name"], "转账")
        self.assertEqual(padded["payment"]["amount"], "¥9.90")

    def test_rich_red_packet_notices_are_cleaned_and_never_leak_links(self):
        talker = "wxid_friend"
        rich_notice = (
            '<img src="SystemMessages_HongbaoIcon.png"/> '
            '你领取了朋友的<*wc_custom_link* color="#FD9931" '
            'href="weixin://weixinhongbao/opendetail?sendid=private-sendid'
            '&amp;sign=private-sign">红包</*wc_custom_link*>'
        )
        wrapped_notice = (
            "<msg><appmsg><title>"
            + html.escape(rich_notice)
            + "</title><type>5</type></appmsg></msg>"
        )
        shard = make_shard(
            talker,
            [
                (1, 101, 10000, 0, 100, rich_notice, b"", b"", 0),
                (2, 102, 49, 1, 200, wrapped_notice, b"", b"", 0),
            ],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        system_notice, wrapped = parser.get_messages(
            talker, page=1, page_size=10
        )["messages"]
        for message in (system_notice, wrapped):
            with self.subTest(message_id=message["id"]):
                self.assertEqual(message["type_name"], "红包")
                self.assertEqual(message["payment"]["kind"], "red_packet")
                self.assertIn("你领取了朋友的红包", message["content"])
                serialized = json.dumps(message, ensure_ascii=False)
                self.assertNotIn("<img", serialized)
                self.assertNotIn("wc_custom_link", serialized)
                self.assertNotIn("weixinhongbao", serialized)
                self.assertNotIn("private-sendid", serialized)
                self.assertNotIn("private-sign", serialized)

        self.assertIsNone(system_notice["is_sender"])
        self.assertEqual(wrapped["type"], 49)

    def test_system_payment_notice_rejects_repeatedly_encoded_private_nodes(self):
        talker = "wxid_friend"
        encoded_private_notice = (
            "wxpay://c2cbizmessagehandler/hongbao/receivehongbao "
            "&amp;lt;paymsgid&amp;gt;SYSTEM_PRIVATE_PAYMENT_ID"
            "&amp;lt;/paymsgid&amp;gt;"
        )
        shard = make_shard(
            talker,
            [(1, 101, 10000, 0, 100, encoded_private_notice, b"", b"", 0)],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        message = parser.get_messages(talker, page=1, page_size=10)["messages"][0]

        self.assertEqual(message["type_name"], "红包")
        self.assertEqual(message["content"], "红包")
        self.assertEqual(message["payment"]["title"], "红包")
        self.assertNotIn(
            "SYSTEM_PRIVATE_PAYMENT_ID",
            json.dumps(message, ensure_ascii=False),
        )

    def test_contact_preview_uses_transfer_amount_instead_of_link(self):
        talker = "wxid_friend"
        transfer_xml = (
            "<msg><appmsg><title>微信转账</title><type>2000</type>"
            "<wcpayinfo><feedesc>¥6.66</feedesc></wcpayinfo>"
            "</appmsg></msg>"
        )
        shard = make_shard(
            talker,
            [(1, 101, 49, 1, 100, transfer_xml, b"", b"", 0)],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        contact = next(
            item for item in parser.get_contacts() if item["talker"] == talker
        )
        self.assertIn("[转账]", contact["last_message"])
        self.assertIn("¥6.66", contact["last_message"])

    def test_server_id_lookup_crosses_shards_and_uses_time_tiebreaker(self):
        talker = "wxid_friend"
        shard_a = make_shard(
            talker,
            [(1, 888, 1, 1, 100, "older", b"", b"", 0)],
        )
        shard_b = make_shard(
            talker,
            [(2, 888, 1, 1, 200, "target", b"", b"", 0)],
        )
        self.addCleanup(shard_a.close)
        self.addCleanup(shard_b.close)
        parser = MessageParserV4(
            [shard_a, shard_b], None, "wxid_self", None
        )

        message = parser.get_message_by_server_id(
            talker, "888", create_time=200
        )
        self.assertIsNotNone(message)
        self.assertEqual(message["id"], 2)
        self.assertEqual(message["content"], "target")
        self.assertEqual(
            message["message_key"], "wxid_friend:888:200:2"
        )
        self.assertIsNone(parser.get_message_by_server_id(talker, "0"))

    def test_server_id_lookup_accepts_uint64_text_for_signed_sqlite_value(self):
        talker = "wxid_friend"
        unsigned_id = (2**63) + 123
        signed_id = unsigned_id - (2**64)
        shard = make_shard(
            talker,
            [(4, signed_id, 1, 1, 300, "uint64 target", b"", b"", 0)],
        )
        self.addCleanup(shard.close)
        parser = MessageParserV4([shard], None, "wxid_self", None)

        message = parser.get_message_by_server_id(
            talker, str(unsigned_id), create_time=300
        )

        self.assertIsNotNone(message)
        self.assertEqual(message["id"], 4)
        self.assertEqual(message["content"], "uint64 target")
        self.assertEqual(message["server_id"], signed_id)

    def test_get_messages_enriches_reply_target_across_shards(self):
        talker = "wxid_friend"
        sticker_md5 = "d" * 32
        original_sticker = (
            '<msg><emoji md5="' + sticker_md5 + '" '
            'cdnurl="https://emoji.qpic.cn/original" /></msg>'
        )
        reply_xml = (
            '<msg><appmsg><title>确实</title><type>57</type><refermsg>'
            '<type>47</type><svrid>777</svrid><createtime>100</createtime>'
            '<displayname>朋友</displayname><content></content>'
            '</refermsg></appmsg></msg>'
        )
        shard_a = make_shard(
            talker,
            [(5, 777, 47, 1, 100, original_sticker, b"", b"", 0)],
        )
        shard_b = make_shard(
            talker,
            [(8, 999, (57 << 32) | 49, 1, 200, reply_xml, b"", b"", 0)],
        )
        self.addCleanup(shard_a.close)
        self.addCleanup(shard_b.close)
        parser = MessageParserV4(
            [shard_a, shard_b], None, "wxid_self", None
        )

        messages = parser.get_messages(talker, page=1, page_size=10)["messages"]
        reply_message = next(item for item in messages if item["type_name"] == "回复")
        target = reply_message["reply"]["target_message"]
        self.assertEqual(
            {key: target[key] for key in (
                "id", "create_time", "server_id", "message_key", "type", "type_name"
            )},
            {
                "id": 5,
                "create_time": 100,
                "server_id": 777,
                "message_key": "wxid_friend:777:100:5",
                "type": 47,
                "type_name": "表情",
            },
        )
        self.assertEqual(target["sticker"]["md5"], sticker_md5)
        self.assertNotIn("url", json.dumps(target["sticker"]))
        self.assertEqual(
            target["_sticker_source"]["cdn_url"],
            "https://emoji.qpic.cn/original",
        )

    def test_address_book_reads_contact_db_and_excludes_room_members(self):
        talker = "wxid_chat"
        shard = make_shard(talker, [])
        self.addCleanup(shard.close)
        contact = sqlite3.connect(":memory:")
        contact.execute(
            """
            CREATE TABLE Contact (
                username TEXT, nick_name TEXT, remark TEXT, alias TEXT,
                description TEXT, phone_number TEXT, local_type INTEGER
            )
            """
        )
        contact.executemany(
            "INSERT INTO Contact VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("wxid_friend", "昵称", "好友备注", "friend_alias", "简介", "123", 1),
                ("wxid_no_chat", "通讯录好友", "", "", "x" * 2500, "", 1),
                ("room@chatroom", "群聊", "", "", "", "", 2),
                ("wxid_room_member", "群成员", "", "", "", "", 3),
                ("wxid_self", "我", "", "self_alias", "", "", 1),
            ],
        )
        contact.commit()
        self.addCleanup(contact.close)
        parser = MessageParserV4(
            [shard], None, "wxid_self_abcd", contact
        )

        address_book = parser.get_address_book()
        by_username = {item["username"]: item for item in address_book}
        self.assertNotIn("wxid_room_member", by_username)
        self.assertIn("wxid_no_chat", by_username)
        self.assertEqual(
            by_username["wxid_friend"]["display_name"], "好友备注"
        )
        self.assertTrue(by_username["room@chatroom"]["is_group"])
        self.assertTrue(by_username["wxid_self"]["is_self"])
        self.assertEqual(
            len(by_username["wxid_no_chat"]["description"]), 2000
        )


if __name__ == "__main__":
    unittest.main()
