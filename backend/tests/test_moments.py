import base64
import csv
import html
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import zstandard as zstd
from PIL import Image

from backend.moments import (
    DownloadedMedia,
    MEDIA_MAX_ATTEMPTS,
    MomentsExporter,
    MomentsMediaDownloadError,
    MomentsSelectionError,
    MomentsService,
    SafeMomentsMediaDownloader,
    SNS_XML_MAX_LENGTH,
    parse_sns_timeline_content,
)


SELF_DB_USERNAME = "wxid_self"
SELF_ACCOUNT_ID = "wxid_self_abcd"
FRIEND_USERNAME = "wxid_friend"
SECOND_FRIEND_USERNAME = "wxid_friend_two"
BASE_TID = 8_100_000_000_000_000_000


def _xml_text(value):
    return html.escape(str(value), quote=False)


def _timeline_xml(
    *,
    tid,
    username,
    create_time,
    content,
    nickname="",
    title="",
    description="",
    content_url="",
    media_url="",
    content_type=2,
    live_photo=False,
    raw_content=False,
    local_extra="",
    is_top=False,
    guide_top=False,
):
    body = str(content) if raw_content else _xml_text(content)
    media = ""
    if media_url:
        live_photo_node = "<LivePhoto/>" if live_photo else ""
        media = (
            "<mediaList><media><type>2</type><sub_type>0</sub_type>"
            f"{live_photo_node}"
            f"<url md5=\"synthetic-md5\">{_xml_text(media_url)}</url>"
            f"<thumb>{_xml_text(media_url)}</thumb>"
            "<size width=\"640\" height=\"480\" totalSize=\"1234\"/>"
            "</media></mediaList>"
        )
    return (
        "<TimelineObjects><TimelineObject>"
        f"<id>{tid}</id><username>{_xml_text(username)}</username>"
        f"<createTime>{create_time}</createTime>"
        f"<contentDesc>{body}</contentDesc><private>0</private>"
        f"<isTop>{int(bool(is_top))}</isTop>"
        f"<guideTop>{int(bool(guide_top))}</guideTop>"
        f"<ContentObject><type>{int(content_type)}</type>"
        f"<title>{_xml_text(title)}</title>"
        f"<description>{_xml_text(description)}</description>"
        f"<contentUrl>{_xml_text(content_url)}</contentUrl>"
        f"{media}</ContentObject>"
        "</TimelineObject>"
        f"<LocalExtraInfo><nickname>{_xml_text(nickname)}</nickname>"
        f"{local_extra}</LocalExtraInfo>"
        "</TimelineObjects>"
    )


def _make_sns_connection(posts, comments=None, *, with_del_status=True):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE SnsTimeLine("
        "tid INTEGER, user_name TEXT, content TEXT, pack_info_buf TEXT)"
    )
    conn.executemany(
        "INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (?, ?, ?)",
        posts,
    )
    if comments is not None:
        columns = (
            "local_id INTEGER, create_time INTEGER, type INTEGER, "
            "feed_id INTEGER, from_username TEXT, from_nickname TEXT, "
            "to_username TEXT, to_nickname TEXT, content TEXT, "
            "comment_id INTEGER, comment64_id INTEGER"
        )
        if with_del_status:
            columns += ", del_status INTEGER"
        conn.execute(f"CREATE TABLE SnsMessage_tmp3({columns})")
        insert_columns = (
            "local_id, create_time, type, feed_id, from_username, "
            "from_nickname, to_username, to_nickname, content, "
            "comment_id, comment64_id"
        )
        placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        if with_del_status:
            insert_columns += ", del_status"
            placeholders += ", ?"
        conn.executemany(
            f"INSERT INTO SnsMessage_tmp3({insert_columns}) "
            f"VALUES ({placeholders})",
            comments,
        )
    conn.commit()
    return conn


def _make_contact_connection(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT)"
    )
    conn.executemany(
        "INSERT INTO contact(username, remark, nick_name) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


class MomentsParsingTests(unittest.TestCase):
    def test_plain_bytes_zstd_hex_and_base64_content(self):
        variants = []
        expected_contents = []
        for index, encoding in enumerate(
            ("plain", "bytes", "zstd", "hex", "base64"), start=1
        ):
            expected = f"encoding-{encoding}"
            xml = _timeline_xml(
                tid=BASE_TID + index,
                username=FRIEND_USERNAME,
                create_time=1_700_000_000 + index,
                content=expected,
            )
            raw = xml.encode("utf-8")
            if encoding == "plain":
                encoded = xml
            elif encoding == "bytes":
                encoded = raw
            elif encoding == "zstd":
                encoded = zstd.ZstdCompressor().compress(raw)
            elif encoding == "hex":
                encoded = raw.hex()
            else:
                encoded = base64.b64encode(raw).decode("ascii")
            variants.append((BASE_TID + index, FRIEND_USERNAME, encoded))
            expected_contents.append(expected)

        conn = _make_sns_connection(variants)
        self.addCleanup(conn.close)
        service = MomentsService(conn)

        posts = service.get_posts([FRIEND_USERNAME])

        self.assertEqual(len(posts), 5)
        self.assertEqual(
            {post["content"] for post in posts}, set(expected_contents)
        )
        self.assertEqual(service.parse_failures, 0)

    def test_pseudo_xml_is_sanitized(self):
        xml = _timeline_xml(
            tid=BASE_TID,
            username=FRIEND_USERNAME,
            create_time=1_700_000_000,
            content="old A&B <3 control\x01removed",
            raw_content=True,
        )

        parsed = parse_sns_timeline_content(xml)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["content"], "old A&B <3 controlremoved")

    def test_type_54_image_album_is_not_mislabelled_as_livestream(self):
        image_post = parse_sns_timeline_content(
            _timeline_xml(
                tid=BASE_TID + 100,
                username=FRIEND_USERNAME,
                create_time=1_700_000_100,
                content="live photo album",
                content_type=54,
                media_url="https://shmmsns.qpic.cn/example/0",
                live_photo=True,
            )
        )
        non_image_post = parse_sns_timeline_content(
            _timeline_xml(
                tid=BASE_TID + 101,
                username=FRIEND_USERNAME,
                create_time=1_700_000_101,
                content="actual live fallback",
                content_type=54,
            )
        )
        finder_xml = _timeline_xml(
            tid=BASE_TID + 102,
            username=FRIEND_USERNAME,
            create_time=1_700_000_102,
            content="finder live",
            content_type=54,
            media_url="https://shmmsns.qpic.cn/example/0",
        ).replace(
            "</ContentObject>",
            "<finderFeed><mediaType>4</mediaType></finderFeed></ContentObject>",
        )
        finder_post = parse_sns_timeline_content(finder_xml)

        self.assertIsNotNone(image_post)
        self.assertEqual(image_post["content_type_name"], "图文")
        self.assertTrue(image_post["media"][0]["is_live_photo"])
        self.assertIsNotNone(non_image_post)
        self.assertEqual(non_image_post["content_type_name"], "直播")
        self.assertIsNotNone(finder_post)
        self.assertEqual(finder_post["content_type_name"], "直播")

    def test_xxe_and_oversized_xml_are_rejected(self):
        xxe = (
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<TimelineObjects><TimelineObject><id>1</id>'
            '<username>wxid_x</username><createTime>1</createTime>'
            '<contentDesc>&xxe;</contentDesc></TimelineObject></TimelineObjects>'
        )
        oversized = _timeline_xml(
            tid=BASE_TID,
            username=FRIEND_USERNAME,
            create_time=1,
            content="x" * (SNS_XML_MAX_LENGTH + 1),
        )

        self.assertIsNone(parse_sns_timeline_content(xxe))
        self.assertIsNone(parse_sns_timeline_content(oversized))


class MomentsServiceTests(unittest.TestCase):
    def test_contacts_include_self_and_public_ids_are_strings(self):
        self_tid = BASE_TID + 1
        friend_tid = BASE_TID + 2
        conn = _make_sns_connection(
            [
                (
                    self_tid,
                    SELF_DB_USERNAME,
                    _timeline_xml(
                        tid=self_tid,
                        username=SELF_DB_USERNAME,
                        create_time=200,
                        content="self post",
                        nickname="XML self name",
                    ),
                ),
                (
                    friend_tid,
                    FRIEND_USERNAME,
                    _timeline_xml(
                        tid=friend_tid,
                        username=FRIEND_USERNAME,
                        create_time=100,
                        content="friend post",
                        nickname="XML friend name",
                    ),
                ),
            ]
        )
        contacts = _make_contact_connection(
            [(FRIEND_USERNAME, "Friend remark", "Friend nickname")]
        )
        self.addCleanup(conn.close)
        self.addCleanup(contacts.close)
        service = MomentsService(
            conn,
            contacts,
            account_id=SELF_ACCOUNT_ID,
            display_name="My account",
        )

        authors = service.get_contacts()
        posts = service.get_posts([SELF_DB_USERNAME, FRIEND_USERNAME])

        self.assertEqual(len(authors), 2)
        self.assertEqual(authors[0]["username"], SELF_DB_USERNAME)
        self.assertTrue(authors[0]["is_self"])
        self.assertEqual(authors[0]["display_name"], "My account")
        friend = next(
            item for item in authors if item["username"] == FRIEND_USERNAME
        )
        self.assertEqual(friend["display_name"], "Friend remark")
        self.assertTrue(all(isinstance(post["tid"], str) for post in posts))
        self.assertEqual({post["tid"] for post in posts}, {str(self_tid), str(friend_tid)})

    def test_recalled_comments_are_filtered_and_ids_are_strings(self):
        tid = BASE_TID + 10
        post = (
            tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=tid,
                username=FRIEND_USERNAME,
                create_time=200,
                content="post",
            ),
        )
        comments = [
            (1, 201, 1, tid, "wxid_a", "A", "", "", "", 11, 111, 0),
            (2, 202, 2, tid, "wxid_b", "B", "", "", "recalled", 22, 222, 1),
            (3, 203, 2, tid, "wxid_c", "C", "", "", "kept", 33, 333, None),
        ]
        conn = _make_sns_connection([post], comments)
        self.addCleanup(conn.close)

        result = MomentsService(conn).get_posts([FRIEND_USERNAME])[0]["comments"]

        self.assertEqual([item["from_nickname"] for item in result], ["A", "C"])
        self.assertEqual([item["id"] for item in result], ["111", "333"])
        self.assertTrue(all(item["feed_id"] == str(tid) for item in result))

    def test_xml_interactions_include_self_and_use_remarks_for_reply_names(self):
        tid = BASE_TID + 11
        local_extra = (
            "<like_user_list>"
            "<user_comment><username>wxid_liker</username><nickname>XML liker</nickname>"
            "<create_time>201</create_time><comment_64id>0</comment_64id>"
            "<comment_id>0</comment_id>"
            "<b_deleted>0</b_deleted></user_comment>"
            "<user_comment><username>wxid_deleted</username><nickname>Deleted</nickname>"
            "<comment_64id>599</comment_64id><b_deleted>1</b_deleted></user_comment>"
            "</like_user_list>"
            "<comment_user_list>"
            "<user_comment><username>wxid_self</username><nickname>XML self</nickname>"
            "<content>XML authoritative content</content><create_time>202</create_time>"
            "<comment_64id>0</comment_64id><comment_id>502</comment_id>"
            "<ref_username>wxid_target</ref_username>"
            "<b_deleted>0</b_deleted></user_comment>"
            "</comment_user_list>"
        )
        post = (
            tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=tid,
                username=FRIEND_USERNAME,
                create_time=200,
                content="post",
                local_extra=local_extra,
            ),
        )
        comments = [
            # A differently encoded ID with the same semantic comment must
            # still be suppressed; the XML snapshot is authoritative.
            (1, 202, 2, tid, SELF_DB_USERNAME, "DB self", SELF_DB_USERNAME,
             "Notification receiver", "XML authoritative content", 0, 999, 0),
            # A notification-only supplement. Its to_username is not a reply target.
            (2, 203, 2, tid, "wxid_extra", "XML extra", SELF_DB_USERNAME,
             "Notification receiver", "supplement", 0, 503, 0),
        ]
        conn = _make_sns_connection([post], comments)
        contacts = _make_contact_connection([
            ("wxid_liker", "Liker remark", "Liker nickname"),
            ("wxid_target", "Target remark", "Target nickname"),
            ("wxid_extra", "Extra remark", "Extra nickname"),
        ])
        self.addCleanup(conn.close)
        self.addCleanup(contacts.close)
        service = MomentsService(
            conn,
            contacts,
            account_id=SELF_ACCOUNT_ID,
            display_name="My account",
        )

        result = service.get_posts([FRIEND_USERNAME])[0]["comments"]

        self.assertEqual([item["id"] for item in result], ["", "502", "503"])
        self.assertEqual(result[0]["from_display_name"], "Liker remark")
        self.assertEqual(result[1]["from_display_name"], "My account")
        self.assertEqual(result[1]["to_display_name"], "Target remark")
        self.assertEqual(result[1]["content"], "XML authoritative content")
        self.assertEqual(result[2]["from_display_name"], "Extra remark")
        self.assertEqual(result[2]["to_username"], "")
        self.assertEqual(result[2]["to_display_name"], "")
        self.assertNotIn("wxid_deleted", {item["from_username"] for item in result})

    def test_signed_sqlite_ids_are_exported_as_canonical_uint64_strings(self):
        canonical_tid = 18_000_000_000_000_000_000
        signed_tid = canonical_tid - (1 << 64)
        canonical_comment_id = 17_000_000_000_000_000_000
        signed_comment_id = canonical_comment_id - (1 << 64)
        post = (
            signed_tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=canonical_tid,
                username=FRIEND_USERNAME,
                create_time=200,
                content="unsigned ids",
            ),
        )
        comments = [
            (
                1,
                201,
                2,
                signed_tid,
                "wxid_a",
                "A",
                "",
                "",
                "kept",
                0,
                signed_comment_id,
                0,
            ),
        ]
        conn = _make_sns_connection([post], comments)
        self.addCleanup(conn.close)

        result = MomentsService(conn).get_posts(
            [FRIEND_USERNAME], tids=[str(canonical_tid)]
        )[0]

        self.assertEqual(result["tid"], str(canonical_tid))
        self.assertEqual(result["comments"][0]["feed_id"], str(canonical_tid))
        self.assertEqual(
            result["comments"][0]["id"], str(canonical_comment_id)
        )

    def test_old_comment_schema_without_del_status_is_supported(self):
        tid = BASE_TID + 20
        post = (
            tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=tid,
                username=FRIEND_USERNAME,
                create_time=200,
                content="post",
            ),
        )
        comments = [
            (1, 201, 2, tid, "wxid_a", "A", "", "", "old schema", 7, 77),
        ]
        conn = _make_sns_connection(
            [post], comments, with_del_status=False
        )
        self.addCleanup(conn.close)

        result = MomentsService(conn).get_posts([FRIEND_USERNAME])[0]["comments"]

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["content"], "old schema")

    def test_sparse_old_comment_schema_fills_optional_fields(self):
        tid = BASE_TID + 21
        post = (
            tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=tid,
                username=FRIEND_USERNAME,
                create_time=200,
                content="post",
            ),
        )
        conn = _make_sns_connection([post])
        conn.execute(
            "CREATE TABLE SnsMessage_tmp3("
            "feed_id INTEGER, type INTEGER, from_username TEXT)"
        )
        conn.execute(
            "INSERT INTO SnsMessage_tmp3(feed_id, type, from_username) "
            "VALUES (?, ?, ?)",
            (tid, 1, "wxid_sparse"),
        )
        conn.commit()
        self.addCleanup(conn.close)

        result = MomentsService(conn).get_posts([FRIEND_USERNAME])[0]["comments"]

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["from_username"], "wxid_sparse")
        self.assertEqual(result[0]["from_nickname"], "")
        self.assertEqual(result[0]["create_time"], 0)

    def test_selected_contacts_and_inclusive_date_range_are_enforced(self):
        rows = []
        for offset, username, timestamp, content in (
            (1, FRIEND_USERNAME, 100, "friend old"),
            (2, FRIEND_USERNAME, 200, "friend selected"),
            (3, SECOND_FRIEND_USERNAME, 200, "other contact"),
            (4, FRIEND_USERNAME, 300, "friend too new"),
        ):
            tid = BASE_TID + offset
            rows.append(
                (
                    tid,
                    username,
                    _timeline_xml(
                        tid=tid,
                        username=username,
                        create_time=timestamp,
                        content=content,
                    ),
                )
            )
        conn = _make_sns_connection(rows)
        self.addCleanup(conn.close)

        posts = MomentsService(conn).get_posts(
            [FRIEND_USERNAME], start_time=200, end_time=200
        )

        self.assertEqual([post["content"] for post in posts], ["friend selected"])

    def test_exact_tids_are_enforced_and_stale_selection_is_rejected(self):
        rows = []
        tids = []
        for offset, username, content in (
            (31, FRIEND_USERNAME, "first selected"),
            (32, FRIEND_USERNAME, "not selected"),
            (33, SECOND_FRIEND_USERNAME, "wrong contact"),
        ):
            tid = BASE_TID + offset
            tids.append(str(tid))
            rows.append(
                (
                    tid,
                    username,
                    _timeline_xml(
                        tid=tid,
                        username=username,
                        create_time=200 + offset,
                        content=content,
                    ),
                )
            )
        conn = _make_sns_connection(rows)
        self.addCleanup(conn.close)
        service = MomentsService(conn)

        posts = service.get_posts([FRIEND_USERNAME], tids=[tids[0], tids[0]])

        self.assertEqual([post["tid"] for post in posts], [tids[0]])
        with self.assertRaisesRegex(MomentsSelectionError, "不属于当前联系人"):
            service.get_posts([FRIEND_USERNAME], tids=[tids[2]])
        with self.assertRaisesRegex(MomentsSelectionError, "标识格式无效"):
            service.get_posts([FRIEND_USERNAME], tids=["-1"])

    def test_get_media_item_validates_selectors_and_returns_public_data(self):
        canonical_tid = 18_000_000_000_000_000_000
        signed_tid = canonical_tid - (1 << 64)
        conn = _make_sns_connection([
            (
                signed_tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=canonical_tid,
                    username=FRIEND_USERNAME,
                    create_time=200,
                    content="media post",
                    media_url="https://shmmsns.qpic.cn/synthetic.jpg",
                ),
            ),
        ])
        self.addCleanup(conn.close)
        service = MomentsService(conn)

        result = service.get_media_item(str(canonical_tid), 0)

        self.assertEqual(result["post"]["tid"], str(canonical_tid))
        self.assertEqual(result["post"]["content"], "media post")
        self.assertFalse(any(key.startswith("_") for key in result["post"]))
        self.assertEqual(result["media_index"], 0)
        self.assertEqual(
            result["media"]["url"],
            "https://shmmsns.qpic.cn/synthetic.jpg",
        )

        with self.assertRaisesRegex(MomentsSelectionError, "动态不存在"):
            service.get_media_item(str(canonical_tid - 1), 0)
        with self.assertRaisesRegex(MomentsSelectionError, "超出有效范围"):
            service.get_media_item(str(canonical_tid), 1)
        with self.assertRaisesRegex(MomentsSelectionError, "不能为负数"):
            service.get_media_item(str(canonical_tid), -1)
        for invalid_index in (True, "1.5", None):
            with self.subTest(media_index=invalid_index), self.assertRaisesRegex(
                MomentsSelectionError, "序号格式无效"
            ):
                service.get_media_item(str(canonical_tid), invalid_index)
        with self.assertRaisesRegex(MomentsSelectionError, "标识格式无效"):
            service.get_media_item("not-a-tid", 0)

    def test_posts_page_is_descending_and_only_hydrates_current_page(self):
        rows = []
        for offset in range(5):
            tid = BASE_TID + 40 + offset
            rows.append(
                (
                    tid,
                    FRIEND_USERNAME,
                    _timeline_xml(
                        tid=tid,
                        username=FRIEND_USERNAME,
                        create_time=100 + offset,
                        content=f"post-{offset}",
                    ),
                )
            )
        conn = _make_sns_connection(rows)
        self.addCleanup(conn.close)
        service = MomentsService(conn)

        with patch.object(
            service, "_load_comments", wraps=service._load_comments
        ) as comments_loader:
            page = service.get_posts_page(
                FRIEND_USERNAME, page=2, page_size=2
            )

        self.assertEqual(page["total"], 5)
        self.assertEqual(page["total_pages"], 3)
        self.assertTrue(page["has_previous"])
        self.assertTrue(page["has_next"])
        self.assertEqual(
            [post["content"] for post in page["posts"]],
            ["post-2", "post-1"],
        )
        hydrated_posts = comments_loader.call_args.args[0]
        self.assertEqual(len(hydrated_posts), 2)

    def test_pinned_posts_are_separate_from_timeline_and_guide_top_is_not_pinned(self):
        rows = []
        for offset, content, is_top, guide_top in (
            (1, "new timeline", False, False),
            (2, "guide only", False, True),
            (3, "old pinned", True, False),
        ):
            tid = BASE_TID + 50 + offset
            rows.append(
                (
                    tid,
                    FRIEND_USERNAME,
                    _timeline_xml(
                        tid=tid,
                        username=FRIEND_USERNAME,
                        create_time=100 + offset,
                        content=content,
                        is_top=is_top,
                        guide_top=guide_top,
                    ),
                )
            )
        conn = _make_sns_connection(rows)
        self.addCleanup(conn.close)

        page = MomentsService(conn).get_posts_page(
            FRIEND_USERNAME, page=1, page_size=20
        )

        self.assertEqual(page["pinned_total"], 1)
        self.assertEqual(
            [post["content"] for post in page["pinned_posts"]],
            ["old pinned"],
        )
        self.assertTrue(page["pinned_posts"][0]["is_top"])
        self.assertEqual(page["total"], 2)
        self.assertEqual(
            [post["content"] for post in page["posts"]],
            ["guide only", "new timeline"],
        )
        self.assertTrue(page["posts"][0]["guide_top"])
        self.assertFalse(page["posts"][0]["is_top"])

    def test_posts_page_keyword_filters_caption_before_pagination(self):
        rows = []
        fixtures = (
            # Pinned matches must stay in the separate pinned result.
            (1, FRIEND_USERNAME, 105, "置顶旅行", "", "", True),
            (2, FRIEND_USERNAME, 104, "旅行日记", "", "", False),
            (3, FRIEND_USERNAME, 103, "旅行指南", "", "", False),
            (4, FRIEND_USERNAME, 102, "Straße 旅行路线", "", "", False),
            (5, FRIEND_USERNAME, 101, "完全无关", "隐藏标题", "隐藏描述", False),
            # Contact and date filtering must happen before keyword results.
            (6, SECOND_FRIEND_USERNAME, 106, "旅行但联系人不符", "", "", False),
            (7, FRIEND_USERNAME, 50, "旅行但时间不符", "", "", False),
        )
        for offset, username, create_time, content, title, description, is_top in fixtures:
            tid = BASE_TID + 60 + offset
            rows.append(
                (
                    tid,
                    username,
                    _timeline_xml(
                        tid=tid,
                        username=username,
                        create_time=create_time,
                        content=content,
                        title=title,
                        description=description,
                        is_top=is_top,
                    ),
                )
            )
        conn = _make_sns_connection(rows)
        self.addCleanup(conn.close)
        service = MomentsService(conn)

        page = service.get_posts_page(
            FRIEND_USERNAME,
            page=2,
            page_size=1,
            start_time=100,
            keyword="  旅行  ",
        )

        self.assertEqual(page["pinned_total"], 1)
        self.assertEqual(page["pinned_posts"][0]["content"], "置顶旅行")
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["total_pages"], 3)
        self.assertTrue(page["has_previous"])
        self.assertTrue(page["has_next"])
        self.assertEqual(page["posts"][0]["content"], "旅行指南")

        # casefold provides Unicode-aware Latin matching; Chinese matching is
        # naturally unaffected by case conversion.
        folded = service.get_posts_page(
            FRIEND_USERNAME, start_time=100, keyword="STRASSE"
        )
        self.assertEqual(folded["total"], 1)
        self.assertEqual(folded["posts"][0]["content"], "Straße 旅行路线")

        # The UI promises a caption search, so metadata that happens to match
        # must not make a post appear when its caption does not.
        metadata_only = service.get_posts_page(
            FRIEND_USERNAME, start_time=100, keyword="隐藏标题"
        )
        self.assertEqual(metadata_only["total"], 0)

        no_result = service.get_posts_page(
            FRIEND_USERNAME, start_time=100, keyword="不存在的文案"
        )
        self.assertEqual(no_result["pinned_total"], 0)
        self.assertEqual(no_result["pinned_posts"], [])
        self.assertEqual(no_result["total"], 0)
        self.assertEqual(no_result["posts"], [])
        self.assertEqual(no_result["total_pages"], 0)

        whitespace = service.get_posts_page(
            FRIEND_USERNAME, start_time=100, keyword=" \t\r\n "
        )
        self.assertEqual(whitespace["pinned_total"], 1)
        self.assertEqual(whitespace["total"], 4)

    def test_wechat_profile_row_order_is_not_treated_as_a_pin_signal(self):
        # A profile refresh can produce this non-chronological insertion order,
        # but the rows do not carry persisted pin state.  Guessing three pins
        # from rowid previously misclassified ordinary image posts.
        row_order_desc = [1, 16, 38, 48] + [
            rank for rank in range(70, 1, -1) if rank not in {16, 38, 48}
        ]
        rows_by_rank = {}
        for rank in range(1, 71):
            tid = BASE_TID + 1_000 + rank
            rows_by_rank[rank] = (
                tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=tid,
                    username=FRIEND_USERNAME,
                    create_time=10_000 - rank,
                    content=f"rank-{rank}",
                ),
            )
        conn = _make_sns_connection(
            [rows_by_rank[rank] for rank in reversed(row_order_desc)]
        )
        self.addCleanup(conn.close)

        page = MomentsService(conn).get_posts_page(
            FRIEND_USERNAME, page=1, page_size=100
        )

        self.assertEqual(page["pinned_total"], 0)
        self.assertEqual(page["pinned_posts"], [])
        self.assertEqual(page["total"], 70)
        self.assertFalse(any(post["is_top"] for post in page["posts"]))

    def test_ordinary_backfill_order_is_not_misclassified_as_pinned(self):
        row_order_desc = [1, 2, 7, 6, 5, 4, 3, 8]
        rows_by_rank = {}
        for rank in range(1, 9):
            tid = BASE_TID + 2_000 + rank
            rows_by_rank[rank] = (
                tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=tid,
                    username=FRIEND_USERNAME,
                    create_time=1_000 - rank,
                    content=f"ordinary-{rank}",
                ),
            )
        conn = _make_sns_connection(
            [rows_by_rank[rank] for rank in reversed(row_order_desc)]
        )
        self.addCleanup(conn.close)

        page = MomentsService(conn).get_posts_page(FRIEND_USERNAME)

        self.assertEqual(page["pinned_total"], 0)
        self.assertEqual(page["total"], 8)

    def test_sns_top_history_does_not_promote_current_pins(self):
        visible_tids = [BASE_TID + 2_500 + index for index in range(7)]
        pinned_tids = visible_tids[:5]
        missing_tids = [BASE_TID + 2_600 + index for index in range(14)]
        conn = _make_sns_connection([
            (
                tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=tid,
                    username=FRIEND_USERNAME,
                    create_time=500 - index,
                    content=f"post-{index}",
                ),
            )
            for index, tid in enumerate(visible_tids)
        ])
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE SnsTopItem_1("
            "tid INTEGER, username TEXT, summary TEXT, create_time INTEGER, "
            "last_read_time INTEGER, is_read INTEGER)"
        )
        # SnsTopItem_1 is an append-like history/read-state cache.  Even exact
        # joins must not be presented as the current profile top list.
        top_rows = [
            (tid, FRIEND_USERNAME, 500 - index, 900, index % 2)
            for index, tid in enumerate(pinned_tids + missing_tids)
        ]
        top_rows.extend([
            top_rows[0],
            # Same tid under a different username must not promote this
            # contact's ordinary row.
            (visible_tids[5], SECOND_FRIEND_USERNAME, 495, 900, 1),
        ])
        conn.executemany(
            "INSERT INTO SnsTopItem_1 VALUES (?, ?, '', ?, ?, ?)",
            top_rows,
        )
        conn.commit()

        page = MomentsService(conn).get_posts_page(FRIEND_USERNAME)

        self.assertEqual(page["pinned_total"], 0)
        self.assertEqual(page["pinned_posts"], [])
        self.assertEqual(page["unavailable_pinned_total"], 0)
        self.assertEqual(page["total"], 7)
        self.assertFalse(any(post["is_top"] for post in page["posts"]))

    def test_user_top_breakflag_partition_is_exact_current_pin_snapshot(self):
        visible_tids = [BASE_TID + 2_700 + index for index in range(7)]
        pinned_tids = visible_tids[:5]
        missing_tids = [BASE_TID + 2_800, BASE_TID + 2_801]
        conn = _make_sns_connection([
            (
                tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=tid,
                    username=FRIEND_USERNAME,
                    create_time=700 - index,
                    content=f"post-{index}",
                ),
            )
            for index, tid in enumerate(visible_tids)
        ])
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE SnsUserTimeLineBreakFlagV2("
            "tid INTEGER, tid_heigh_bit INTEGER, tid_low_bit INTEGER, "
            "break_flag INTEGER, user_name TEXT)"
        )
        rows = [
            (tid, 0, 0, int(index == len(pinned_tids + missing_tids) - 1),
             f"{FRIEND_USERNAME}_user_top")
            for index, tid in enumerate(pinned_tids + missing_tids)
        ]
        rows.extend([
            # Ordinary pagination rows never promote posts.
            (visible_tids[5], 0, 0, 1, FRIEND_USERNAME),
            # Another contact's top partition must remain account/contact exact.
            (visible_tids[6], 0, 0, 1, f"{SECOND_FRIEND_USERNAME}_user_top"),
        ])
        conn.executemany(
            "INSERT INTO SnsUserTimeLineBreakFlagV2 VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        page = MomentsService(conn).get_posts_page(FRIEND_USERNAME)

        self.assertEqual(page["pinned_total"], 5)
        self.assertEqual(
            {post["tid"] for post in page["pinned_posts"]},
            {str(tid) for tid in pinned_tids},
        )
        self.assertEqual(page["unavailable_pinned_total"], 2)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["pinned_posts"]) + len(page["posts"]), 7)
        self.assertFalse(any(post["is_top"] for post in page["posts"]))

    def test_incomplete_user_top_partition_is_not_presented_as_current(self):
        tid = BASE_TID + 2_900
        conn = _make_sns_connection([(
            tid,
            FRIEND_USERNAME,
            _timeline_xml(
                tid=tid,
                username=FRIEND_USERNAME,
                create_time=800,
                content="partial top page",
            ),
        )])
        self.addCleanup(conn.close)
        conn.execute(
            "CREATE TABLE SnsUserTimeLineBreakFlagV2("
            "tid INTEGER, tid_heigh_bit INTEGER, tid_low_bit INTEGER, "
            "break_flag INTEGER, user_name TEXT)"
        )
        conn.execute(
            "INSERT INTO SnsUserTimeLineBreakFlagV2 VALUES (?, 0, 0, 0, ?)",
            (tid, f"{FRIEND_USERNAME}_user_top"),
        )
        conn.commit()

        page = MomentsService(conn).get_posts_page(FRIEND_USERNAME)

        self.assertEqual(page["pinned_total"], 0)
        self.assertEqual(page["unavailable_pinned_total"], 0)
        self.assertEqual(page["total"], 1)

    def test_rowid_and_without_rowid_schemas_both_load(self):
        for schema in (
            "CREATE TABLE SnsTimeLine("
            "tid INTEGER PRIMARY KEY, user_name TEXT, content TEXT)",
            "CREATE TABLE SnsTimeLine("
            "tid INTEGER PRIMARY KEY, user_name TEXT, content TEXT) WITHOUT ROWID",
        ):
            with self.subTest(schema=schema):
                conn = sqlite3.connect(":memory:")
                self.addCleanup(conn.close)
                conn.execute(schema)
                tid = BASE_TID + 3_000
                conn.execute(
                    "INSERT INTO SnsTimeLine VALUES (?, ?, ?)",
                    (
                        tid,
                        FRIEND_USERNAME,
                        _timeline_xml(
                            tid=tid,
                            username=FRIEND_USERNAME,
                            create_time=123,
                            content="schema fallback",
                        ),
                    ),
                )
                page = MomentsService(conn).get_posts_page(FRIEND_USERNAME)
                self.assertEqual(page["pinned_total"], 0)
                self.assertEqual(page["total"], 1)


class _NoNetworkDownloader:
    def __init__(self):
        self.calls = []

    def reset_budget(self):
        return None

    def download(self, url):
        self.calls.append(url)
        raise AssertionError("media downloader must not run by default")


class MomentsExporterTests(unittest.TestCase):
    def _make_export_service(self, *, two_contacts=False):
        malicious_name = '<img src=x onerror="alert(1)">'
        rows = []
        tid = BASE_TID + 100
        rows.append(
            (
                tid,
                FRIEND_USERNAME,
                _timeline_xml(
                    tid=tid,
                    username=FRIEND_USERNAME,
                    create_time=1_700_000_000,
                    content='=HYPERLINK("https://invalid") <script>alert(1)</script>',
                    title="+SUM(1,1)",
                    description="@cmd",
                    content_url="https://example.test/path?a=1&b=2",
                    media_url="https://shmmsns.qpic.cn/synthetic.jpg",
                ),
            )
        )
        contacts = [(FRIEND_USERNAME, malicious_name, "Friend")]
        if two_contacts:
            second_tid = BASE_TID + 101
            rows.append(
                (
                    second_tid,
                    SECOND_FRIEND_USERNAME,
                    _timeline_xml(
                        tid=second_tid,
                        username=SECOND_FRIEND_USERNAME,
                        create_time=1_700_000_100,
                        content="second post",
                    ),
                )
            )
            contacts = [
                (FRIEND_USERNAME, "Same name", "Friend one"),
                (SECOND_FRIEND_USERNAME, "Same name", "Friend two"),
            ]
        sns_conn = _make_sns_connection(rows)
        contact_conn = _make_contact_connection(contacts)
        self.addCleanup(sns_conn.close)
        self.addCleanup(contact_conn.close)
        return MomentsService(sns_conn, contact_conn)

    def test_legacy_positional_filename_and_media_arguments_still_work(self):
        service = self._make_export_service()
        with tempfile.TemporaryDirectory() as temporary:
            exporter = MomentsExporter(service, temporary, _NoNetworkDownloader())

            result = exporter.export(
                [FRIEND_USERNAME], "txt", None, None, "legacy-name", False
            )

            self.assertEqual(result.path.name, "legacy-name.txt")
            self.assertTrue(result.path.is_file())

    def _temporary_output(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_html_escapes_untrusted_content_and_does_not_network_by_default(self):
        service = self._make_export_service()
        downloader = _NoNetworkDownloader()
        result = MomentsExporter(
            service, self._temporary_output(), media_downloader=downloader
        ).export([FRIEND_USERNAME], fmt="html")

        document = result.path.read_text(encoding="utf-8")

        self.assertEqual(downloader.calls, [])
        self.assertEqual(result.media_downloaded, 0)
        self.assertEqual(result.media_failed, 0)
        self.assertNotIn("<script>alert(1)</script>", document)
        self.assertNotIn('<img src=x onerror="alert(1)">', document)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", document)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", document)
        self.assertIn("https://example.test/path?a=1&amp;b=2", document)
        self.assertIn("Content-Security-Policy", document)

    def test_html_embeds_already_loaded_local_media_without_network_opt_in(self):
        service = self._make_export_service()
        downloader = _NoNetworkDownloader()
        payload = b"already-validated-local-image"

        class LocalResolver:
            def __init__(self):
                self.calls = []

            def get_bytes(self, tid, media_index):
                self.calls.append((tid, media_index))
                status = type("Status", (), {"mime_type": "image/png"})()
                return status, payload

        resolver = LocalResolver()
        result = MomentsExporter(
            service,
            self._temporary_output(),
            media_downloader=downloader,
            local_media_resolver=resolver,
        ).export([FRIEND_USERNAME], fmt="html")
        document = result.path.read_text(encoding="utf-8")

        self.assertEqual(downloader.calls, [])
        self.assertEqual(result.media_downloaded, 0)
        self.assertEqual(
            resolver.calls,
            [(str(BASE_TID + 100), 0)],
        )
        self.assertIn(
            "data:image/png;base64,"
            + base64.b64encode(payload).decode("ascii"),
            document,
        )
        self.assertNotIn("媒体未嵌入", document)

    def test_html_renders_likes_horizontally_and_separate_from_comments(self):
        document = MomentsExporter._html_document(
            {"display_name": "Friend"},
            [{
                "tid": "1",
                "create_time_str": "2026-07-18 12:00:00",
                "content_type_name": "图文",
                "comments": [
                    {"type": 1, "from_display_name": "Alice"},
                    {"type": 1, "from_display_name": "Bob"},
                    {
                        "type": 2,
                        "from_display_name": "Carol",
                        "content": "单独的评论",
                    },
                ],
            }],
            {},
        )

        likes_start = document.index('<div class="likes">')
        likes_end = document.index("</div></div>", likes_start)
        comments_start = document.index('<div class="comments">')
        likes_section = document[likes_start:likes_end]
        comments_section = document[comments_start:]

        self.assertLess(likes_start, comments_start)
        self.assertIn('<div class="like-list">', likes_section)
        self.assertIn('<span class="like name">Alice</span>', likes_section)
        self.assertIn('<span class="like name">Bob</span>', likes_section)
        self.assertNotIn("单独的评论", likes_section)
        self.assertIn('<div class="interaction-title">评论</div>', comments_section)
        self.assertIn("Carol", comments_section)
        self.assertIn("单独的评论", comments_section)
        self.assertIn(".like-list{display:flex;flex-wrap:wrap", document)

    def test_html_separates_pinned_posts_from_timeline_without_duplicates(self):
        rows = []
        for offset, content, is_top, guide_top in (
            (1, "ordinary timeline post", False, False),
            (2, "guide-only timeline post", False, True),
            (3, "pinned post", True, False),
        ):
            tid = BASE_TID + 150 + offset
            rows.append(
                (
                    tid,
                    FRIEND_USERNAME,
                    _timeline_xml(
                        tid=tid,
                        username=FRIEND_USERNAME,
                        create_time=100 + offset,
                        content=content,
                        is_top=is_top,
                        guide_top=guide_top,
                    ),
                )
            )
        sns_conn = _make_sns_connection(rows)
        contact_conn = _make_contact_connection([
            (FRIEND_USERNAME, "Friend remark", "Friend nickname"),
        ])
        self.addCleanup(sns_conn.close)
        self.addCleanup(contact_conn.close)

        result = MomentsExporter(
            MomentsService(sns_conn, contact_conn), self._temporary_output()
        ).export([FRIEND_USERNAME], fmt="html")
        document = result.path.read_text(encoding="utf-8")

        pinned_start = document.index('id="pinned-moments"')
        pinned_end = document.index("</section>", pinned_start)
        timeline_start = document.index('id="moments-timeline"')
        timeline_end = document.index("</section>", timeline_start)
        pinned_section = document[pinned_start:pinned_end]
        timeline_section = document[timeline_start:timeline_end]

        self.assertLess(pinned_start, timeline_start)
        self.assertIn("置顶朋友圈（1）", pinned_section)
        self.assertIn("pinned post", pinned_section)
        self.assertNotIn("ordinary timeline post", pinned_section)
        self.assertNotIn("guide-only timeline post", pinned_section)
        self.assertIn("时间线（2）", timeline_section)
        self.assertIn("ordinary timeline post", timeline_section)
        self.assertIn("guide-only timeline post", timeline_section)
        self.assertNotIn("pinned post", timeline_section)
        self.assertEqual(document.count("pinned post"), 1)
        self.assertEqual(result.posts_count, 3)

    def test_json_export_is_structured_and_keeps_string_tid(self):
        service = self._make_export_service()
        result = MomentsExporter(service, self._temporary_output()).export(
            [FRIEND_USERNAME], fmt="json"
        )

        payload = json.loads(result.path.read_text(encoding="utf-8"))

        self.assertEqual(payload["username"], FRIEND_USERNAME)
        self.assertEqual(payload["total_posts"], 1)
        self.assertIsInstance(payload["posts"][0]["tid"], str)
        self.assertIn("<script>alert(1)</script>", payload["posts"][0]["content"])

    def test_csv_export_neutralizes_formula_cells(self):
        service = self._make_export_service()
        result = MomentsExporter(service, self._temporary_output()).export(
            [FRIEND_USERNAME], fmt="csv"
        )

        rows = list(
            csv.reader(
                io.StringIO(result.path.read_text(encoding="utf-8-sig"))
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][3].startswith("'="))
        self.assertTrue(rows[1][4].startswith("'+"))
        self.assertTrue(rows[1][5].startswith("'@"))

    def test_txt_export_contains_readable_timeline(self):
        service = self._make_export_service()
        result = MomentsExporter(service, self._temporary_output()).export(
            [FRIEND_USERNAME], fmt="txt"
        )

        document = result.path.read_text(encoding="utf-8")

        self.assertIn("的朋友圈", document)
        self.assertIn("共 1 条动态", document)
        self.assertIn("<script>alert(1)</script>", document)
        self.assertIn("标题: +SUM(1,1)", document)
        self.assertIn("链接: https://example.test/path?a=1&b=2", document)

    def test_multiple_contacts_are_exported_as_safe_zip(self):
        service = self._make_export_service(two_contacts=True)
        result = MomentsExporter(service, self._temporary_output()).export(
            [FRIEND_USERNAME, SECOND_FRIEND_USERNAME],
            fmt="html",
            filename="selected moments",
        )

        self.assertTrue(result.is_archive)
        self.assertEqual(result.contacts_count, 2)
        self.assertEqual(result.posts_count, 2)
        self.assertTrue(zipfile.is_zipfile(result.path))
        with zipfile.ZipFile(result.path) as archive:
            names = archive.namelist()
            self.assertIn("index.html", names)
            self.assertIn("Same name.html", names)
            self.assertIn("Same name(1).html", names)
            self.assertTrue(
                all(not name.startswith(("/", "\\")) and ".." not in name for name in names)
            )
            index = archive.read("index.html").decode("utf-8")
            self.assertIn("共 2 位联系人，2 条动态", index)


class MomentsMediaSecurityTests(unittest.TestCase):
    @staticmethod
    def _small_png() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(output, format="PNG")
        return output.getvalue()

    def test_historical_http_cdn_url_is_upgraded_to_tls(self):
        downloader = SafeMomentsMediaDownloader()
        with patch.object(
            downloader, "_resolve_public_ips", return_value=["8.8.8.8"]
        ):
            parsed, hostname, port, addresses = downloader._validate_url(
                "http://shmmsns.qpic.cn/path/image.jpg?token=synthetic"
            )

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(hostname, "shmmsns.qpic.cn")
        self.assertEqual(port, 443)
        self.assertEqual(addresses, ["8.8.8.8"])

    def test_reference_url_requests_original_quality_with_token(self):
        prepared = SafeMomentsMediaDownloader._prepare_reference_url(
            "http://shmmsns.qpic.cn/sns/example/150?existing=1",
            "alphaNUMERICtoken",
        )
        parsed = urlsplit(prepared)
        self.assertEqual(parsed.path, "/sns/example/0")
        self.assertIn("existing=1", parsed.query)
        self.assertIn("token=alphaNUMERICtoken", parsed.query)
        self.assertIn("idx=1", parsed.query)

        refreshed = SafeMomentsMediaDownloader._prepare_reference_url(
            "https://shmmsns.qpic.cn/sns/example/150?token=&idx=0",
            "freshToken",
        )
        self.assertIn("token=freshToken", refreshed)
        self.assertIn("idx=1", refreshed)
        self.assertNotIn("idx=0", refreshed)

        unchanged = SafeMomentsMediaDownloader._prepare_reference_url(
            "https://mmbiz.qpic.cn/article/300?wx_fmt=jpeg",
            "",
        )
        self.assertEqual(unchanged, "https://mmbiz.qpic.cn/article/300?wx_fmt=jpeg")

    def test_non_cdn_host_and_explicit_plaintext_port_are_rejected(self):
        downloader = SafeMomentsMediaDownloader()
        for url in (
            "https://qpic.cn.example.test/image.jpg",
            "http://shmmsns.qpic.cn:80/image.jpg",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(
                MomentsMediaDownloadError
            ):
                downloader._validate_url(url)

    def test_expired_total_deadline_fails_before_network_resolution(self):
        downloader = SafeMomentsMediaDownloader(max_total_seconds=1)
        downloader._deadline = time.monotonic() - 1
        with patch.object(downloader, "_resolve_public_ips") as resolve:
            with self.assertRaises(MomentsMediaDownloadError):
                downloader.download("https://shmmsns.qpic.cn/image.jpg")
        resolve.assert_not_called()

    def test_image_pixel_limit_is_enforced_before_embedding(self):
        data = self._small_png()
        SafeMomentsMediaDownloader(max_pixels=4)._validate_image_safety(data)
        with self.assertRaises(MomentsMediaDownloadError):
            SafeMomentsMediaDownloader(max_pixels=3)._validate_image_safety(data)

    def test_exporter_caps_unique_media_download_attempts(self):
        class Downloader:
            def __init__(self):
                self.calls = 0

            def download(self, _url):
                self.calls += 1
                return DownloadedMedia(b"x", "image/jpeg", ".jpg")

        posts = [
            {
                "tid": str(index),
                "media": [
                    {
                        "type": "2",
                        "url": f"https://shmmsns.qpic.cn/{index}.jpg",
                    }
                ],
            }
            for index in range(MEDIA_MAX_ATTEMPTS + 5)
        ]
        downloader = Downloader()
        exporter = MomentsExporter(None, Path(tempfile.gettempdir()))

        _assets, downloaded, failed = exporter._download_assets(posts, downloader)

        self.assertEqual(downloader.calls, MEDIA_MAX_ATTEMPTS)
        self.assertEqual(downloaded, MEDIA_MAX_ATTEMPTS)
        self.assertEqual(failed, 5)


if __name__ == "__main__":
    unittest.main()
