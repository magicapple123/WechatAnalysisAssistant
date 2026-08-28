import hashlib
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from backend import api
from backend.avatar_service import (
    AvatarImage,
    AvatarNotFound,
    AvatarValidationError,
)


def make_request(
    *,
    if_none_match: str = "",
    client_host: str = "127.0.0.1",
) -> Request:
    headers = [(b"host", b"127.0.0.1:8520")]
    if if_none_match:
        headers.append((b"if-none-match", if_none_match.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/avatar/wxid_friend",
            "raw_path": b"/api/avatar/wxid_friend",
            "query_string": b"",
            "headers": headers,
            "client": (client_host, 12345),
            "server": ("127.0.0.1", 8520),
        }
    )


@contextmanager
def selected_account(wxid: str = "wxid_account_abcd"):
    account = SimpleNamespace(wxid=wxid)
    with patch.object(api.config, "accounts", [account]), patch.object(
        api.config, "active_index", 0
    ), patch.object(api.config, "key", "a" * 64), patch.object(
        api, "_avatar_url_secret", b"avatar-test-secret"
    ):
        yield


class AvatarUrlTests(unittest.TestCase):
    def test_public_avatar_url_is_signed_and_bound_to_username_and_account(self):
        username = "wxid_朋友+test"
        with selected_account("wxid_account_abcd"):
            url = api._avatar_public_url(username)
            parsed = urlsplit(url)
            token = parse_qs(parsed.query)["token"][0]

            self.assertEqual(unquote(parsed.path.rsplit("/", 1)[-1]), username)
            self.assertEqual(len(token), 64)
            self.assertTrue(api._valid_avatar_token(username, token))
            self.assertFalse(api._valid_avatar_token(username + "x", token))
            self.assertFalse(api._valid_avatar_token(username, "0" * 64))

        with selected_account("wxid_other_abcd"):
            self.assertFalse(api._valid_avatar_token(username, token))

    def test_public_avatar_url_requires_selected_unlocked_account(self):
        with patch.object(api.config, "accounts", []), patch.object(
            api.config, "active_index", -1
        ), patch.object(api.config, "key", None):
            self.assertEqual(api._avatar_public_url("wxid_friend"), "")


class AvatarRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_avatar_route_middleware_rejects_non_loopback_clients(self):
        request = make_request(client_host="192.0.2.10")
        call_next = AsyncMock()

        response = await api.protect_local_image_and_secret_routes(
            request, call_next
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("detail", response.body.decode("utf-8"))
        call_next.assert_not_awaited()

    async def test_avatar_response_has_verified_mime_etag_and_security_headers(self):
        payload = b"validated-avatar-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        avatar = AvatarImage(payload, "image/png", digest, "local")
        service = SimpleNamespace(get_avatar=lambda username: avatar)

        with patch.object(api, "_valid_avatar_token", return_value=True), patch.object(
            api, "get_avatar_service", return_value=service
        ):
            response = await api.get_avatar(
                " wxid_friend ", make_request(), "a" * 64
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, payload)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["content-length"], str(len(payload)))
        self.assertEqual(response.headers["etag"], f'"{digest}"')
        self.assertIn("must-revalidate", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-wechat-avatar-source"], "local")

    async def test_matching_if_none_match_returns_304_without_body(self):
        avatar = AvatarImage(b"avatar", "image/jpeg", "same-etag", "network-cache")
        service = SimpleNamespace(get_avatar=lambda _username: avatar)

        with patch.object(api, "_valid_avatar_token", return_value=True), patch.object(
            api, "get_avatar_service", return_value=service
        ):
            response = await api.get_avatar(
                "wxid_friend",
                make_request(if_none_match='"same-etag"'),
                "a" * 64,
            )

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["etag"], '"same-etag"')
        self.assertIn("must-revalidate", response.headers["cache-control"])

    async def test_invalid_signature_is_rejected_before_avatar_database_access(self):
        service_factory = Mock()
        with patch.object(api, "_valid_avatar_token", return_value=False), patch.object(
            api, "get_avatar_service", service_factory
        ):
            with self.assertRaises(HTTPException) as error:
                await api.get_avatar("wxid_friend", make_request(), "0" * 64)

        self.assertEqual(error.exception.status_code, 403)
        service_factory.assert_not_called()

    async def test_service_not_found_and_validation_errors_map_to_http_statuses(self):
        cases = (
            (AvatarNotFound("missing"), 404),
            (AvatarValidationError("invalid"), 422),
        )
        for service_error, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                service = Mock()
                service.get_avatar.side_effect = service_error
                with patch.object(
                    api, "_valid_avatar_token", return_value=True
                ), patch.object(api, "get_avatar_service", return_value=service):
                    with self.assertRaises(HTTPException) as error:
                        await api.get_avatar(
                            "wxid_friend", make_request(), "a" * 64
                        )
                self.assertEqual(error.exception.status_code, expected_status)


class AvatarEnrichmentTests(unittest.TestCase):
    def test_message_avatar_enrichment_handles_self_direct_group_and_system(self):
        messages = [
            {
                "talker": "wxid_friend",
                "is_sender": False,
                "sender_username": "",
            },
            {
                "talker": "room@chatroom",
                "is_sender": False,
                "sender_username": "wxid_member",
            },
            {
                "talker": "room@chatroom",
                "is_sender": False,
                "sender_username": "",
            },
            {"talker": "wxid_friend", "is_sender": True, "sender_name": ""},
            {"talker": "wxid_friend", "is_sender": None},
        ]
        with selected_account(), patch.object(api.config, "display_name", "本人"):
            api._enrich_message_avatars(messages)

        self.assertEqual(messages[0]["sender_username"], "wxid_friend")
        self.assertIn("/api/avatar/wxid_friend", messages[0]["sender_avatar_url"])
        self.assertEqual(messages[1]["sender_username"], "wxid_member")
        self.assertIn("/api/avatar/wxid_member", messages[1]["sender_avatar_url"])
        self.assertEqual(messages[2]["sender_username"], "")
        self.assertEqual(messages[2]["sender_avatar_url"], "")
        self.assertEqual(messages[3]["sender_username"], "__self__")
        self.assertEqual(messages[3]["sender_name"], "本人")
        self.assertIn("/api/avatar/__self__", messages[3]["sender_avatar_url"])
        self.assertEqual(messages[4]["sender_username"], "")
        self.assertEqual(messages[4]["sender_avatar_url"], "")


if __name__ == "__main__":
    unittest.main()
