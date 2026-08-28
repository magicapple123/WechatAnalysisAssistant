import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend import api
from backend.config import WeChatAccount


class MomentsConfigTests(unittest.TestCase):
    def test_wechat_4_account_discovers_sns_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "xwechat_files"
            message_dir = root / "wxid_test" / "db_storage" / "message"
            sns_db = message_dir.parent / "sns" / "sns.db"
            message_dir.mkdir(parents=True)
            sns_db.parent.mkdir()
            sns_db.write_bytes(b"database")
            account = WeChatAccount("wxid_test", root, msg_dir=message_dir)

            self.assertEqual(account.sns_db, sns_db)
            self.assertTrue(account.to_dict()["supports_moments"])


class MomentsSnapshotCacheTests(unittest.TestCase):
    def test_fingerprint_tracks_main_db_and_wal_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            sns_path = Path(temporary) / "sns.db"
            sns_path.write_bytes(b"main-v1")

            initial = api._moments_source_fingerprint(sns_path)
            wal_path = Path(f"{sns_path}-wal")
            wal_path.write_bytes(b"wal-v1")
            with_wal = api._moments_source_fingerprint(sns_path)
            self.assertNotEqual(initial, with_wal)

            wal_stat = wal_path.stat()
            os.utime(
                wal_path,
                ns=(wal_stat.st_atime_ns, wal_stat.st_mtime_ns + 1_000_000_000),
            )
            wal_mtime_changed = api._moments_source_fingerprint(sns_path)
            self.assertNotEqual(with_wal, wal_mtime_changed)

            sns_path.write_bytes(b"main-v2-is-larger")
            self.assertNotEqual(
                wal_mtime_changed,
                api._moments_source_fingerprint(sns_path),
            )

    def test_service_rebuild_keeps_old_snapshot_and_unrelated_caches_alive(self):
        decryptors = []

        class FakeDecryptor:
            def __init__(self, path, _key):
                self.path = Path(path)
                self.connection = SimpleNamespace(snapshot=len(decryptors) + 1)
                self.closed = False
                decryptors.append(self)

            def open_decrypted(self):
                return self.connection

            def close(self):
                self.closed = True

        class FakeMomentsService:
            def __init__(
                self,
                sns_conn,
                *,
                contact_conn,
                account_id,
                display_name,
            ):
                self.sns_conn = sns_conn
                self.contact_conn = contact_conn
                self.account_id = account_id
                self.display_name = display_name

        with tempfile.TemporaryDirectory() as temporary:
            sns_path = Path(temporary) / "sns.db"
            sns_path.write_bytes(b"main-v1")
            fake_config = SimpleNamespace(
                key="ab" * 32,
                wxid="wxid_self",
                sns_db=sns_path,
                micromsg_db=None,
                display_name="Me",
            )
            unrelated = object()
            with patch.object(api, "config", fake_config), patch.object(
                api, "_is_xwechat", return_value=True
            ), patch.object(
                api, "DatabaseDecryptor", FakeDecryptor
            ), patch.object(
                api, "MomentsService", FakeMomentsService
            ), patch.dict(
                api._decryptor_cache,
                {"unrelated-chat-cache": unrelated},
                clear=True,
            ), patch.dict(
                api._moments_service_cache, {}, clear=True
            ), patch.dict(
                api._moments_service_fingerprints, {}, clear=True
            ):
                first = api.get_moments_service()
                self.assertIs(first, api.get_moments_service())
                self.assertEqual(len(decryptors), 1)

                wal_path = Path(f"{sns_path}-wal")
                wal_path.write_bytes(b"new-wal-frame")
                second = api.get_moments_service()
                self.assertIsNot(first, second)
                self.assertEqual(len(decryptors), 2)
                self.assertFalse(decryptors[0].closed)
                self.assertIs(
                    api._decryptor_cache["unrelated-chat-cache"], unrelated
                )

                sns_path.write_bytes(b"main-v2-is-larger")
                third = api.get_moments_service()
                self.assertIsNot(second, third)
                self.assertEqual(len(decryptors), 3)
                self.assertFalse(decryptors[0].closed)
                self.assertFalse(decryptors[1].closed)
                self.assertEqual(first.sns_conn.snapshot, 1)
                self.assertEqual(second.sns_conn.snapshot, 2)
                self.assertEqual(third.sns_conn.snapshot, 3)

class MomentsRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_media_loader_persists_only_valid_explicit_download_results(self):
        ready = set()

        class FakeStore:
            @staticmethod
            def save_bytes(tid, media_index, data, **_kwargs):
                self.assertEqual(data, b"validated-image")
                ready.add((str(tid), int(media_index)))

        class FakeResolver:
            store = FakeStore()

            @staticmethod
            def discover_cache_files():
                return []

            @staticmethod
            def scan_exact_cache_files(_posts, _files):
                return SimpleNamespace(to_dict=lambda: {
                    "files_seen": 0,
                    "files_decoded": 0,
                    "partial_files": 0,
                    "unmatched_files": 0,
                    "exact_content_matches": 0,
                    "media_bound": 0,
                })

            @staticmethod
            def hydrate_posts(posts):
                result = []
                for source_post in posts:
                    post = dict(source_post)
                    post["media"] = []
                    for index, source_media in enumerate(
                        source_post.get("media") or []
                    ):
                        media = dict(source_media)
                        media["local_media"] = {
                            "status": (
                                "ready"
                                if (str(post["tid"]), index) in ready
                                else "unbound"
                            )
                        }
                        post["media"].append(media)
                    result.append(post)
                return result

        class FakeDownloader:
            @staticmethod
            def reset_budget():
                return None

            @staticmethod
            def download(url):
                self.assertEqual(url, "https://shmmsns.qpic.cn/test")
                return api.DownloadedMedia(
                    b"validated-image", "image/jpeg", ".jpg"
                )

        posts = [{
            "tid": "12",
            "media": [{
                "type": "2",
                "url": "https://shmmsns.qpic.cn/test",
                "width": "640",
                "height": "480",
            }],
        }]
        with patch.object(
            api, "SafeMomentsMediaDownloader", return_value=FakeDownloader()
        ):
            result = api._load_moments_preview_media_sync(
                FakeResolver(), posts
            )

        self.assertEqual(result["loaded"], 1)
        self.assertEqual(result["ready_after"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(ready, {("12", 0)})

        # Complete-contact mode must advance beyond the legacy 60-item page
        # batch instead of silently reporting the rest as truncated.
        ready.clear()
        many_posts = [
            {
                "tid": str(index + 100),
                "media": [{
                    "type": "2",
                    "url": "https://shmmsns.qpic.cn/test",
                    "width": "640",
                    "height": "480",
                }],
            }
            for index in range(api.MOMENTS_PREVIEW_MEDIA_MAX_ITEMS + 1)
        ]
        with patch.object(
            api, "SafeMomentsMediaDownloader", return_value=FakeDownloader()
        ):
            all_result = api._load_moments_preview_media_sync(
                FakeResolver(), many_posts, process_all=True
            )

        self.assertEqual(
            all_result["loaded"], api.MOMENTS_PREVIEW_MEDIA_MAX_ITEMS + 1
        )
        self.assertEqual(all_result["truncated"], 0)
        self.assertEqual(all_result["remaining"], 0)

    async def test_contacts_and_export_require_local_client_header(self):
        with self.assertRaises(HTTPException) as contacts_error:
            await api.get_moments_contacts(None)
        self.assertEqual(contacts_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as export_error:
            await api.export_moments(
                api.MomentsExportRequest(usernames=["wxid_friend"]), None
            )
        self.assertEqual(export_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as preview_error:
            await api.preview_moments(
                api.MomentsPreviewRequest(username="wxid_friend"), None
            )
        self.assertEqual(preview_error.exception.status_code, 403)

        with self.assertRaises(HTTPException) as media_error:
            await api.load_moments_media(
                api.MomentsMediaLoadRequest(
                    username="wxid_friend", tids=["1"]
                ),
                None,
            )
        self.assertEqual(media_error.exception.status_code, 403)

    async def test_contacts_response_is_not_cached(self):
        service = SimpleNamespace(
            get_contacts=lambda: [
                {
                    "username": "wxid_self",
                    "display_name": "我自己",
                    "is_self": True,
                    "post_count": 2,
                    "start_time": 100,
                    "end_time": 200,
                }
            ]
        )
        with patch.object(api, "get_moments_service", return_value=service):
            response = await api.get_moments_contacts("1")

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["data"]["total_contacts"], 1)
        self.assertEqual(payload["data"]["total_posts"], 2)
        self.assertTrue(payload["data"]["contacts"][0]["is_self"])
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_export_rejects_invalid_scope_before_reading_database(self):
        invalid_requests = (
            api.MomentsExportRequest(usernames=[]),
            api.MomentsExportRequest(usernames=["wxid"], format="pdf"),
            api.MomentsExportRequest(
                usernames=["wxid"], format="json", download_media=True
            ),
            api.MomentsExportRequest(
                usernames=["wxid"], start_time=200, end_time=100
            ),
            api.MomentsExportRequest(usernames=["wxid"], tids=[]),
            api.MomentsExportRequest(
                usernames=["wxid"], start_time=100, tids=["1"]
            ),
        )
        with patch.object(api, "get_moments_service") as service:
            for request in invalid_requests:
                with self.subTest(request=request), self.assertRaises(HTTPException) as error:
                    await api.export_moments(request, "1")
                self.assertEqual(error.exception.status_code, 400)
        service.assert_not_called()

    async def test_export_moves_generated_file_and_returns_counts(self):
        class FakeResult:
            format = "html"
            contact_count = 1
            post_count = 3
            is_archive = False
            media_downloaded = 0
            media_failed = 0

            def __init__(self, path):
                self.path = path

            def to_dict(self):
                return {
                    "format": self.format,
                    "contact_count": self.contact_count,
                    "post_count": self.post_count,
                    "is_archive": self.is_archive,
                    "media_downloaded": self.media_downloaded,
                    "media_failed": self.media_failed,
                }

        captured = {}

        class FakeExporter:
            def __init__(self, service, output_dir, *, local_media_resolver=None):
                captured["service"] = service
                captured["output_dir"] = Path(output_dir)
                captured["local_media_resolver"] = local_media_resolver

            def export(self, usernames, **kwargs):
                captured["usernames"] = usernames
                captured["kwargs"] = kwargs
                path = captured["output_dir"] / "朋友圈.html"
                path.write_text("<html></html>", encoding="utf-8")
                return FakeResult(path)

        service = SimpleNamespace(
            get_contacts=lambda: [
                {"username": "wxid_self", "display_name": "我自己"}
            ],
            get_posts=lambda *_args, **_kwargs: [],
            parse_failures=0,
        )
        resolver = object()
        with tempfile.TemporaryDirectory() as temporary:
            export_dir = Path(temporary) / "exports"
            export_dir.mkdir()
            with patch.object(api, "get_moments_service", return_value=service), \
                    patch.object(
                        api, "get_moments_media_resolver", return_value=resolver
                    ), \
                    patch.object(api, "MomentsExporter", FakeExporter), \
                    patch.object(api, "get_export_dir", return_value=export_dir):
                response = await api.export_moments(
                    api.MomentsExportRequest(
                        usernames=["wxid_self", "wxid_self"],
                        format="html",
                        filename="我的朋友圈",
                        tids=["8100000000000000001"],
                    ),
                    "1",
                )

            saved = Path(response["path"])
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.parent, export_dir)
            self.assertEqual(response["posts_count"], 3)
            self.assertEqual(response["contacts_count"], 1)
            self.assertEqual(captured["usernames"], ["wxid_self"])
            self.assertEqual(captured["kwargs"]["filename"], "我的朋友圈")
            self.assertEqual(
                captured["kwargs"]["tids"], ["8100000000000000001"]
            )
            self.assertIs(captured["local_media_resolver"], resolver)

    async def test_preview_is_paginated_no_store_and_hides_media_secrets(self):
        service = SimpleNamespace(
            get_contacts=lambda: [
                {
                    "username": "wxid_friend",
                    "display_name": "朋友",
                    "post_count": 1,
                }
            ],
            get_posts_page=lambda *_args, **_kwargs: {
                "pinned_posts": [
                    {
                        "tid": "18000000000000000001",
                        "username": "wxid_friend",
                        "create_time": 100,
                        "create_time_str": "1970-01-01 00:01:40",
                        "content_type": 2,
                        "content_type_name": "纯文本",
                        "content": "pinned preview",
                        "is_top": True,
                        "media": [],
                        "comments": [],
                    }
                ],
                "pinned_total": 1,
                "unavailable_pinned_total": 14,
                "posts": [
                    {
                        "tid": "18000000000000000000",
                        "username": "wxid_friend",
                        "create_time": 200,
                        "create_time_str": "1970-01-01 00:03:20",
                        "content_type": 1,
                        "content_type_name": "图文",
                        "content": "preview",
                        "title": "title",
                        "description": "description",
                        "location": {
                            "poi_name": "地点",
                            "latitude": "1.23",
                            "longitude": "4.56",
                        },
                        "media": [
                            {
                                "type": "2",
                                "width": "640",
                                "height": "480",
                                "url": "https://shmmsns.qpic.cn/secret",
                                "url_token": "secret-token",
                                "local_media": {
                                    "status": "ready",
                                    "quality": "high",
                                    "mime_type": "image/jpeg",
                                    "width": 640,
                                    "height": 480,
                                    "size": 12345,
                                    "version": "version-1",
                                },
                            }
                        ],
                        "comments": [
                            {
                                "type": 2,
                                "type_name": "评论",
                                "from_username": "wxid_commenter",
                                "from_nickname": "评论者",
                                "from_display_name": "评论者备注",
                                "content": "好看",
                            }
                        ],
                    }
                ],
                "page": 1,
                "page_size": 20,
                "total": 1,
                "total_pages": 1,
                "has_previous": False,
                "has_next": False,
            },
            parse_failures=0,
        )

        with patch.object(api, "get_moments_service", return_value=service):
            response = await api.preview_moments(
                api.MomentsPreviewRequest(username="wxid_friend"), "1"
            )

        payload = json.loads(response.body.decode("utf-8"))
        post = payload["data"]["posts"][0]
        self.assertEqual(payload["data"]["pagination"]["total"], 1)
        self.assertEqual(payload["data"]["pinned_total"], 1)
        self.assertEqual(payload["data"]["unavailable_pinned_total"], 14)
        self.assertEqual(len(payload["data"]["pinned_posts"]), 1)
        self.assertTrue(payload["data"]["pinned_posts"][0]["is_top"])
        self.assertEqual(post["media_count"], 1)
        self.assertEqual(post["media_ready_count"], 1)
        self.assertEqual(post["media"][0]["status"], "ready")
        self.assertEqual(post["media"][0]["quality"], "high")
        self.assertEqual(post["media"][0]["version"], "version-1")
        self.assertEqual(post["interactions"][0]["from_name"], "评论者备注")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("shmmsns.qpic.cn", serialized)
        self.assertNotIn("latitude", serialized)
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_preview_projection_returns_every_interaction(self):
        comments = [
            {
                "type": 2,
                "from_username": f"wxid_{index}",
                "from_display_name": f"备注 {index}",
                "content": f"comment {index}",
            }
            for index in range(75)
        ]

        post = api._moments_preview_post({"tid": "1", "comments": comments})

        self.assertEqual(len(post["interactions"]), 75)
        self.assertEqual(post["interactions_total"], 75)
        self.assertFalse(post["interactions_truncated"])
        self.assertEqual(post["interactions"][-1]["from_name"], "备注 74")

    async def test_preview_does_not_misclassify_unknown_interactions_as_comments(self):
        post = api._moments_preview_post({
            "tid": "1",
            "comments": [
                {"type": 1, "from_display_name": "点赞者"},
                {"type": 2, "from_display_name": "评论者", "content": "正常评论"},
                {"type": 4, "from_display_name": "未知通知", "content": "不应显示"},
            ],
        })

        self.assertEqual(post["likes_count"], 1)
        self.assertEqual(post["comments_count"], 1)
        self.assertEqual(post["interactions_total"], 2)
        self.assertEqual(
            [item["type"] for item in post["interactions"]], [1, 2]
        )
        self.assertNotIn(
            "不应显示",
            json.dumps(post, ensure_ascii=False),
        )

    async def test_preview_passes_trimmed_keyword_and_returns_search_scope(self):
        captured = {}

        def get_posts_page(username, **kwargs):
            captured["username"] = username
            captured.update(kwargs)
            return {
                "pinned_posts": [],
                "pinned_total": 0,
                "posts": [],
                "page": kwargs["page"],
                "page_size": kwargs["page_size"],
                "total": 0,
                "total_pages": 0,
                "has_previous": False,
                "has_next": False,
            }

        service = SimpleNamespace(
            get_contacts=lambda: [{"username": "wxid_friend"}],
            get_posts_page=get_posts_page,
            parse_failures=0,
        )
        resolver = SimpleNamespace(hydrate_posts=lambda posts: list(posts))
        with patch.object(api, "get_moments_service", return_value=service), \
                patch.object(
                    api, "get_moments_media_resolver", return_value=resolver
                ):
            response = await api.preview_moments(
                api.MomentsPreviewRequest(
                    username="wxid_friend",
                    page=2,
                    page_size=10,
                    start_time=100,
                    end_time=200,
                    keyword="  测试文案  ",
                ),
                "1",
            )

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(captured["username"], "wxid_friend")
        self.assertEqual(captured["keyword"], "测试文案")
        self.assertEqual(captured["start_time"], 100)
        self.assertEqual(captured["end_time"], 200)
        self.assertEqual(payload["data"]["keyword"], "测试文案")
        self.assertEqual(payload["data"]["pagination"]["total"], 0)

    def test_preview_keyword_has_bounded_request_length(self):
        request = api.MomentsPreviewRequest(
            username="wxid_friend", keyword="文" * 200
        )
        self.assertEqual(len(request.keyword), 200)

        with self.assertRaises(ValidationError):
            api.MomentsPreviewRequest(
                username="wxid_friend", keyword="文" * 201
            )

    async def test_local_moments_media_route_validates_reference_and_disables_cache(self):
        status = SimpleNamespace(
            mime_type="image/png",
            quality="high",
        )
        service = SimpleNamespace(
            get_media_item=lambda tid, index: {
                "post": {"tid": tid},
                "media": {"type": "2"},
                "media_index": index,
            }
        )
        resolver = SimpleNamespace(
            get_bytes=lambda tid, index: (status, b"\x89PNG\r\n\x1a\n")
        )
        with patch.object(api, "get_moments_service", return_value=service), \
                patch.object(
                    api, "get_moments_media_resolver", return_value=resolver
                ):
            response = await api.get_moments_media("12", 0)

        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, b"\x89PNG\r\n\x1a\n")
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-moments-media-quality"], "high")

    async def test_load_moments_media_uses_only_explicit_selected_posts(self):
        captured = {}

        def get_posts(usernames, *, tids):
            captured["usernames"] = usernames
            captured["tids"] = tids
            return [{"tid": tids[0], "media": []}]

        service = SimpleNamespace(get_posts=get_posts)
        resolver = object()

        def load_sync(selected_resolver, posts):
            captured["resolver"] = selected_resolver
            captured["posts"] = posts
            return {
                "loaded": 1,
                "ready_after": 1,
                "remaining": 0,
            }

        with patch.object(api, "get_moments_service", return_value=service), \
                patch.object(
                    api, "get_moments_media_resolver", return_value=resolver
                ), patch.object(
                    api, "_load_moments_preview_media_sync", side_effect=load_sync
                ):
            response = await api.load_moments_media(
                api.MomentsMediaLoadRequest(
                    username="wxid_friend", tids=["12", "12"]
                ),
                "1",
            )

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(captured["usernames"], ["wxid_friend"])
        self.assertEqual(captured["tids"], ["12"])
        self.assertIs(captured["resolver"], resolver)
        self.assertEqual(captured["posts"][0]["tid"], "12")
        self.assertEqual(payload["data"]["loaded"], 1)
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_load_all_moments_media_uses_complete_contact_feed(self):
        captured = {}

        def get_posts(usernames, *, tids):
            captured["usernames"] = usernames
            captured["tids"] = tids
            return [
                {"tid": str(index + 1), "media": []}
                for index in range(125)
            ]

        def load_sync(selected_resolver, posts, *, process_all=False):
            captured["resolver"] = selected_resolver
            captured["posts"] = posts
            captured["process_all"] = process_all
            return {
                "loaded": 5,
                "ready_after": 8,
                "remaining": 2,
                "expired": 1,
                "failed": 1,
            }

        service = SimpleNamespace(get_posts=get_posts)
        resolver = object()
        with patch.object(api, "get_moments_service", return_value=service), \
                patch.object(
                    api, "get_moments_media_resolver", return_value=resolver
                ), patch.object(
                    api, "_load_moments_preview_media_sync", side_effect=load_sync
                ):
            response = await api.load_moments_media(
                api.MomentsMediaLoadRequest(
                    username="wxid_friend", all_posts=True
                ),
                "1",
            )

        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(captured["usernames"], ["wxid_friend"])
        self.assertIsNone(captured["tids"])
        self.assertEqual(len(captured["posts"]), 125)
        self.assertTrue(captured["process_all"])
        self.assertIs(captured["resolver"], resolver)
        self.assertEqual(payload["data"]["loaded"], 5)

    async def test_load_all_moments_media_rejects_mixed_explicit_ids(self):
        with patch.object(api, "get_moments_service") as service:
            with self.assertRaises(HTTPException) as error:
                await api.load_moments_media(
                    api.MomentsMediaLoadRequest(
                        username="wxid_friend",
                        tids=["12"],
                        all_posts=True,
                    ),
                    "1",
                )

        self.assertEqual(error.exception.status_code, 400)
        service.assert_not_called()

    async def test_export_rejects_result_outside_temporary_directory(self):
        class FakeResult:
            contact_count = 1
            post_count = 1
            path = Path(__file__)

            @staticmethod
            def to_dict():
                return {}

        fake_exporter = SimpleNamespace(export=lambda *_args, **_kwargs: FakeResult())
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(
                    api,
                    "get_moments_service",
                    return_value=SimpleNamespace(
                        get_contacts=lambda: [
                            {"username": "wxid_self", "display_name": "我自己"}
                        ],
                        get_posts=lambda *_args, **_kwargs: [],
                        parse_failures=0,
                    ),
                ), \
                patch.object(api, "MomentsExporter", return_value=fake_exporter), \
                patch.object(api, "get_export_dir", return_value=Path(temporary)):
            with self.assertRaises(HTTPException) as error:
                await api.export_moments(
                    api.MomentsExportRequest(usernames=["wxid_self"]), "1"
                )
        self.assertEqual(error.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
