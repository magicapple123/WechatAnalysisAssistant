import base64
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from backend import api
from backend.moments import DownloadedMedia, MomentsMediaDownloadError
from backend.sticker_service import (
    SafeStickerDownloader,
    StickerResolutionError,
    StickerService,
    _unique_url_md5,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeDownloader:
    def __init__(self, payload=PNG, encrypted_payload=None):
        self.payload = payload
        self.encrypted_payload = encrypted_payload
        self.calls = []
        self.resets = 0

    def reset_budget(self):
        self.resets += 1

    def download(self, url):
        self.calls.append(url)
        return DownloadedMedia(self.payload, "image/png", ".png")

    def download_encrypted(self, url):
        self.calls.append(url)
        if self.encrypted_payload is None:
            raise MomentsMediaDownloadError("missing encrypted fixture")
        return self.encrypted_payload

    @staticmethod
    def _detect_image(data):
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise MomentsMediaDownloadError("not an image")
        return "image/png", ".png"

    @staticmethod
    def _validate_image_safety(data):
        if not data:
            raise MomentsMediaDownloadError("empty")


class MappingBlobDownloader(FakeDownloader):
    max_file_bytes = 15 * 1024 * 1024
    max_pixels = 40_000_000
    max_frames = 600

    def __init__(self, payloads):
        super().__init__()
        self.payloads = dict(payloads)

    def _payload(self, url):
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def download_blob(self, url):
        return self._payload(url)

    def download_encrypted(self, url):
        return self._payload(url)


class StickerServiceTests(unittest.TestCase):
    def test_account_cache_isolation_integrity_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = {
                "md5": hashlib.md5(PNG).hexdigest(),
                "cdn_url": "https://emoji.qpic.cn/sticker.png",
            }
            downloader = FakeDownloader()
            first = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            resolved = first.resolve(source)
            self.assertEqual(resolved.data, PNG)
            self.assertEqual(downloader.calls, [source["cdn_url"]])

            # A restarted service for the same account uses the validated cache.
            restarted_downloader = FakeDownloader(b"different")
            restarted = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=restarted_downloader,
            )
            self.assertEqual(restarted.resolve(source).data, PNG)
            self.assertEqual(restarted_downloader.calls, [])

            other = StickerService(
                "wxid_b", cache_root=Path(temporary), downloader=FakeDownloader()
            )
            self.assertNotEqual(first.cache_dir, other.cache_dir)

    def test_md5_mismatch_is_rejected_and_not_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=FakeDownloader(PNG),
            )
            with self.assertRaises(StickerResolutionError) as mismatch:
                service.resolve({
                    "md5": hashlib.md5(b"other").hexdigest(),
                    "cdn_url": "https://emoji.qpic.cn/sticker.png",
                })
            self.assertEqual(mismatch.exception.status_code, 422)
            self.assertEqual(list(service.cache_dir.glob("*")), [])

    def test_md5_only_message_uses_current_account_emoticon_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "emoticon.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE kNonStoreEmoticonTable "
                "(md5 TEXT, aes_key TEXT, cdn_url TEXT, "
                "encrypt_url TEXT, product_id TEXT)"
            )
            sticker_md5 = hashlib.md5(PNG).hexdigest()
            source_url = "https://emoji.qpic.cn/from-emoticon-db.png"
            connection.execute(
                "INSERT INTO kNonStoreEmoticonTable VALUES (?, '', ?, '', '')",
                (sticker_md5, source_url),
            )
            connection.commit()
            connection.close()
            downloader = FakeDownloader()
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary) / "cache",
                downloader=downloader,
                emoticon_db_path=database,
            )

            resolved = service.resolve({"md5": sticker_md5})

            self.assertEqual(resolved.data, PNG)
            self.assertEqual(downloader.calls, [source_url])

    def test_sticker_downloader_has_narrow_domain_allowlist(self):
        self.assertTrue(SafeStickerDownloader._host_allowed("emoji.qpic.cn"))
        self.assertTrue(SafeStickerDownloader._host_allowed("sub.mmbiz.qpic.cn"))
        self.assertTrue(SafeStickerDownloader._host_allowed("wxapp.tc.qq.com"))
        self.assertTrue(SafeStickerDownloader._host_allowed("edge.wxapp.tc.qq.com"))
        self.assertFalse(SafeStickerDownloader._host_allowed("qpic.cn.evil.test"))
        self.assertFalse(
            SafeStickerDownloader._host_allowed("wxapp.tc.qq.com.evil.test")
        )
        self.assertFalse(SafeStickerDownloader._host_allowed("example.com"))

    def test_wxapp_http_url_is_upgraded_to_https_before_dns_pinning(self):
        downloader = SafeStickerDownloader()
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ) as resolve_ips:
            parsed, hostname, port, addresses = downloader._validate_url(
                "http://wxapp.tc.qq.com/275/20304/stodownload?m=fixture"
            )

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(hostname, "wxapp.tc.qq.com")
        self.assertEqual(port, 443)
        self.assertEqual(addresses, ["1.1.1.1"])
        resolve_ips.assert_called_once_with("wxapp.tc.qq.com", 443)

    def test_legacy_vweixinf_http_is_exact_digest_bound_and_dns_pinned(self):
        downloader = SafeStickerDownloader()
        digest = hashlib.md5(PNG).hexdigest()
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ) as resolve_ips:
            parsed, hostname, port, addresses = downloader._validate_url(
                f"http://vweixinf.tc.qq.com/sticker?m={digest}"
            )

        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(hostname, "vweixinf.tc.qq.com")
        self.assertEqual(port, 80)
        self.assertEqual(addresses, ["1.1.1.1"])
        resolve_ips.assert_called_once_with("vweixinf.tc.qq.com", 80)

        for unsafe_url in (
            "http://vweixinf.tc.qq.com/sticker",
            f"http://vweixinf.tc.qq.com:8080/sticker?m={digest}",
            f"http://user@vweixinf.tc.qq.com/sticker?m={digest}",
            f"http://vweixinf.tc.qq.com.evil.test/sticker?m={digest}",
        ):
            with self.subTest(url=unsafe_url), self.assertRaises(
                MomentsMediaDownloadError
            ):
                downloader._validate_url(unsafe_url)

    def test_legacy_vweixinf_download_uses_plain_pinned_connection(self):
        downloader = SafeStickerDownloader()
        digest = hashlib.md5(PNG).hexdigest()
        response = Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = [PNG, b""]
        connection = Mock()
        connection.getresponse.return_value = response
        connection.sock = None
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ), patch(
            "backend.sticker_service._PinnedHTTPConnection",
            return_value=connection,
        ) as plain_connection, patch(
            "backend.sticker_service._PinnedHTTPSConnection"
        ) as tls_connection:
            downloaded = downloader.download_blob(
                f"http://vweixinf.tc.qq.com/sticker?m={digest}"
            )

        self.assertEqual(downloaded, PNG)
        plain_connection.assert_called_once()
        self.assertEqual(plain_connection.call_args.args[:3], (
            "vweixinf.tc.qq.com", 80, "1.1.1.1"
        ))
        tls_connection.assert_not_called()
        connection.close.assert_called_once()

    def test_legacy_plaintext_body_must_match_url_digest(self):
        downloader = SafeStickerDownloader()
        wrong_digest = hashlib.md5(b"different payload").hexdigest()
        response = Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = [PNG, b""]
        connection = Mock()
        connection.getresponse.return_value = response
        connection.sock = None
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ), patch(
            "backend.sticker_service._PinnedHTTPConnection",
            return_value=connection,
        ), self.assertRaises(StickerResolutionError) as mismatch:
            downloader.download_blob(
                f"http://vweixinf.tc.qq.com/sticker?m={wrong_digest}"
            )

        self.assertEqual(mismatch.exception.status_code, 422)
        connection.close.assert_called_once()

    def test_sticker_download_fails_over_between_validated_public_ips(self):
        downloader = SafeStickerDownloader()
        digest = hashlib.md5(PNG).hexdigest()
        failed_connection = Mock()
        failed_connection.request.side_effect = OSError("edge unavailable")
        successful_response = Mock(status=200)
        successful_response.getheader.return_value = None
        successful_response.read.side_effect = [PNG, b""]
        successful_connection = Mock()
        successful_connection.getresponse.return_value = successful_response
        successful_connection.sock = None
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1", "2.2.2.2"],
        ), patch(
            "backend.sticker_service._PinnedHTTPConnection",
            side_effect=[failed_connection, successful_connection],
        ) as pinned_connection:
            downloaded = downloader.download_blob(
                f"http://vweixinf.tc.qq.com/sticker?m={digest}"
            )

        self.assertEqual(downloaded, PNG)
        self.assertEqual(pinned_connection.call_count, 2)
        self.assertEqual(
            [call.args[2] for call in pinned_connection.call_args_list],
            ["1.1.1.1", "2.2.2.2"],
        )
        failed_connection.close.assert_called_once()
        successful_connection.close.assert_called_once()

    def test_legacy_http_redirect_target_is_fully_revalidated(self):
        downloader = SafeStickerDownloader()
        digest = hashlib.md5(PNG).hexdigest()
        redirect_response = Mock(status=302)
        redirect_response.getheader.side_effect = lambda name: (
            "http://127.0.0.1/private" if name == "Location" else None
        )
        connection = Mock()
        connection.getresponse.return_value = redirect_response
        connection.sock = None
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ), patch(
            "backend.sticker_service._PinnedHTTPConnection",
            return_value=connection,
        ), self.assertRaises(MomentsMediaDownloadError):
            downloader.download_blob(
                f"http://vweixinf.tc.qq.com/sticker?m={digest}"
            )

        connection.close.assert_called_once()

    def test_vweixinf_subdomain_http_is_not_allowed_plaintext(self):
        downloader = SafeStickerDownloader()
        digest = hashlib.md5(PNG).hexdigest()
        with patch.object(
            downloader,
            "_resolve_public_ips",
            return_value=["1.1.1.1"],
        ) as resolve_ips:
            parsed, hostname, port, _ = downloader._validate_url(
                f"http://edge.vweixinf.tc.qq.com/sticker?m={digest}"
            )

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(hostname, "edge.vweixinf.tc.qq.com")
        self.assertEqual(port, 443)
        resolve_ips.assert_called_once_with("edge.vweixinf.tc.qq.com", 443)

    def test_wxapp_resource_runs_through_sticker_resolve_path(self):
        class WxappDownloader(FakeDownloader):
            max_file_bytes = 15 * 1024 * 1024
            max_pixels = 40_000_000
            max_frames = 600

            def download_blob(self, url):
                self.calls.append(url)
                return self.payload

        with tempfile.TemporaryDirectory() as temporary:
            downloader = WxappDownloader()
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=downloader,
            )
            source = {
                "md5": hashlib.md5(PNG).hexdigest(),
                "cdn_url": (
                    "http://wxapp.tc.qq.com/275/20304/stodownload?m=fixture"
                ),
            }

            resolved = service.resolve(source)

        self.assertEqual(resolved.data, PNG)
        self.assertEqual(downloader.calls, [source["cdn_url"]])

    def test_encrypted_only_sticker_is_aes_decrypted_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = b"1234567890abcdef"
            encrypted = AES.new(key, AES.MODE_CBC, iv=key).encrypt(
                pad(PNG, 16)
            )
            downloader = FakeDownloader(encrypted_payload=encrypted)
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=downloader,
            )
            resolved = service.resolve({
                "md5": hashlib.md5(PNG).hexdigest(),
                "encrypt_url": "https://emoji.qpic.cn/encrypted",
                "aes_key": key.hex(),
            })
            self.assertEqual(resolved.data, PNG)
            self.assertEqual(
                downloader.calls,
                ["https://emoji.qpic.cn/encrypted"],
            )
            self.assertEqual(
                service._decrypt_aes_cbc(encrypted, key.decode("ascii")),
                PNG,
            )

    def test_candidate_order_uses_encrypted_main_after_cdn_failure(self):
        key = b"1234567890abcdef"
        encrypted = AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(PNG, 16))
        main_md5 = hashlib.md5(PNG).hexdigest()
        cdn_url = f"https://emoji.qpic.cn/cdn?m={main_md5}"
        encrypt_url = (
            "https://emoji.qpic.cn/encrypted?m="
            + hashlib.md5(encrypted).hexdigest()
        )
        downloader = MappingBlobDownloader({
            cdn_url: MomentsMediaDownloadError("cdn unavailable"),
            encrypt_url: encrypted,
        })
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            resolved = service.resolve({
                "md5": main_md5,
                "cdn_url": cdn_url,
                "encrypt_url": encrypt_url,
                "aes_key": key.hex(),
            })

        self.assertEqual(resolved.data, PNG)
        self.assertEqual(resolved.md5, main_md5)
        self.assertEqual(downloader.calls, [cdn_url, encrypt_url])

    def test_network_failure_is_502_but_integrity_error_keeps_422(self):
        logical_md5 = hashlib.md5(b"expected sticker").hexdigest()
        cipher_md5 = hashlib.md5(b"unavailable ciphertext").hexdigest()
        cdn_url = f"https://emoji.qpic.cn/cdn?m={logical_md5}"
        encrypt_url = f"https://emoji.qpic.cn/encrypt?m={cipher_md5}"
        source = {
            "md5": logical_md5,
            "cdn_url": cdn_url,
            "encrypt_url": encrypt_url,
            "aes_key": "1234567890abcdef",
        }

        with tempfile.TemporaryDirectory() as temporary:
            network_service = StickerService(
                "wxid_a",
                cache_root=Path(temporary) / "network",
                downloader=MappingBlobDownloader({
                    cdn_url: MomentsMediaDownloadError("offline"),
                    encrypt_url: MomentsMediaDownloadError("offline"),
                }),
            )
            with self.assertRaises(StickerResolutionError) as unavailable:
                network_service.resolve(source)
            self.assertEqual(unavailable.exception.status_code, 502)

            integrity_service = StickerService(
                "wxid_a",
                cache_root=Path(temporary) / "integrity",
                downloader=MappingBlobDownloader({
                    cdn_url: PNG,
                    encrypt_url: MomentsMediaDownloadError("offline"),
                }),
            )
            with self.assertRaises(StickerResolutionError) as mismatch:
                integrity_service.resolve(source)
            self.assertEqual(mismatch.exception.status_code, 422)

    def test_extern_aes_uses_extern_md5_and_v3_logical_cache(self):
        key = b"1234567890abcdef"
        logical_md5 = hashlib.md5(b"logical-main").hexdigest()
        wrong_main = AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(PNG, 16))
        wxgf = (
            b"WXGFprofile"
            b"\x00\x00\x00\x01\x40\x02vps"
            b"\x00\x00\x00\x01\x42\x02sps"
        )
        extern_md5 = hashlib.md5(wxgf).hexdigest()
        encrypted_extern = AES.new(key, AES.MODE_CBC, iv=key).encrypt(
            pad(wxgf, 16)
        )
        cdn_url = f"https://emoji.qpic.cn/cdn?m={logical_md5}"
        encrypt_url = (
            "https://emoji.qpic.cn/encrypt?m="
            + hashlib.md5(wrong_main).hexdigest()
        )
        extern_url = (
            "https://emoji.qpic.cn/extern?m="
            + hashlib.md5(encrypted_extern).hexdigest()
        )
        source = {
            "md5": logical_md5,
            "cdn_url": cdn_url,
            "encrypt_url": encrypt_url,
            "extern_url": extern_url,
            "extern_md5": extern_md5,
            "aes_key": key.hex(),
        }
        with tempfile.TemporaryDirectory() as temporary, patch(
            "backend.sticker_service._decode_wxgf_to_browser_image",
            return_value=DownloadedMedia(PNG, "image/png", ".png"),
        ) as decode:
            downloader = MappingBlobDownloader({
                cdn_url: MomentsMediaDownloadError("cdn unavailable"),
                encrypt_url: wrong_main,
                extern_url: encrypted_extern,
            })
            service = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            resolved = service.resolve(source)
            self.assertEqual(resolved.data, PNG)
            self.assertEqual(resolved.md5, logical_md5)
            self.assertEqual(
                downloader.calls, [cdn_url, encrypt_url, extern_url]
            )
            decode.assert_called_once()

            manifest = json.loads(
                (service.cache_dir / f"{logical_md5}.transcoded.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["version"], 3)
            self.assertEqual(manifest["logical_md5"], logical_md5)
            self.assertEqual(manifest["source_md5"], extern_md5)
            self.assertEqual(manifest["source_kind"], "extern")

            restarted_downloader = MappingBlobDownloader({})
            restarted = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=restarted_downloader,
            )
            self.assertEqual(restarted.resolve(source).data, PNG)
            self.assertEqual(restarted_downloader.calls, [])

    def test_historical_direct_extern_is_used_after_aes_interpretation_fails(self):
        key = b"1234567890abcdef"
        logical_md5 = hashlib.md5(b"logical-main").hexdigest()
        raw_md5 = hashlib.md5(PNG).hexdigest()
        extern_url = f"https://emoji.qpic.cn/extern?m={raw_md5}"
        downloader = MappingBlobDownloader({extern_url: PNG})
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            resolved = service.resolve({
                "md5": logical_md5,
                "extern_md5": "f" * 32,
                "extern_url": extern_url,
                "aes_key": key.hex(),
            })
            manifest = json.loads(
                (service.cache_dir / f"{logical_md5}.transcoded.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(resolved.data, PNG)
        self.assertEqual(manifest["source_kind"], "extern_direct")
        self.assertEqual(manifest["source_md5"], raw_md5)

    def test_thumbnail_fallback_is_not_persisted_and_main_is_retried(self):
        main_md5 = hashlib.md5(PNG).hexdigest()
        thumbnail = PNG + b"thumbnail"
        thumb_md5 = hashlib.md5(thumbnail).hexdigest()
        cdn_url = f"https://emoji.qpic.cn/cdn?m={main_md5}"
        thumb_url = f"https://emoji.qpic.cn/thumb?m={thumb_md5}"
        downloader = MappingBlobDownloader({
            cdn_url: MomentsMediaDownloadError("temporarily unavailable"),
            thumb_url: thumbnail,
        })
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a", cache_root=Path(temporary), downloader=downloader
            )
            source = {
                "md5": main_md5,
                "cdn_url": cdn_url,
                "thumb_url": thumb_url,
            }
            self.assertEqual(service.resolve(source).data, thumbnail)
            self.assertEqual(list(service.cache_dir.glob("*")), [])

            downloader.payloads[cdn_url] = PNG
            downloader.calls.clear()
            self.assertEqual(service.resolve(source).data, PNG)

        self.assertEqual(downloader.calls, [cdn_url])

    def test_raw_query_md5_and_decoded_md5_fail_closed(self):
        main_md5 = hashlib.md5(PNG).hexdigest()
        wrong_raw_url = "https://emoji.qpic.cn/cdn?m=" + "0" * 32
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=MappingBlobDownloader({wrong_raw_url: PNG}),
            )
            with self.assertRaises(StickerResolutionError) as raw_error:
                service.resolve({"md5": main_md5, "cdn_url": wrong_raw_url})
            self.assertEqual(raw_error.exception.status_code, 422)
            self.assertEqual(list(service.cache_dir.glob("*")), [])

        key = b"1234567890abcdef"
        encrypted = AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(PNG, 16))
        encrypt_url = (
            "https://emoji.qpic.cn/encrypt?m="
            + hashlib.md5(encrypted).hexdigest()
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=MappingBlobDownloader({encrypt_url: encrypted}),
            )
            with self.assertRaises(StickerResolutionError) as decoded_error:
                service.resolve({
                    "md5": hashlib.md5(b"different").hexdigest(),
                    "encrypt_url": encrypt_url,
                    "aes_key": key.hex(),
                })
            self.assertEqual(decoded_error.exception.status_code, 422)
            self.assertEqual(list(service.cache_dir.glob("*")), [])

    def test_url_m_digest_is_trusted_only_when_unique_and_valid(self):
        digest = hashlib.md5(PNG).hexdigest()
        self.assertEqual(
            _unique_url_md5(f"https://emoji.qpic.cn/a?x=1&m={digest}"),
            digest,
        )
        self.assertEqual(
            _unique_url_md5(
                f"https://emoji.qpic.cn/a?m={digest}&m={digest}"
            ),
            "",
        )
        self.assertEqual(
            _unique_url_md5("https://emoji.qpic.cn/a?m=not-a-digest"),
            "",
        )

    def test_one_download_budget_covers_all_fallback_candidates(self):
        class FallbackDownloader(FakeDownloader):
            def download(self, url):
                self.calls.append(url)
                if len(self.calls) == 1:
                    raise MomentsMediaDownloadError("first candidate failed")
                return DownloadedMedia(self.payload, "image/png", ".png")

        with tempfile.TemporaryDirectory() as temporary:
            downloader = FallbackDownloader()
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=downloader,
            )
            resolved = service.resolve({
                "md5": hashlib.md5(PNG).hexdigest(),
                "cdn_url": "https://emoji.qpic.cn/first",
                "extern_url": "https://emoji.qpic.cn/second",
            })

            self.assertEqual(resolved.data, PNG)
            self.assertEqual(downloader.resets, 1)
            self.assertEqual(
                downloader.calls,
                [
                    "https://emoji.qpic.cn/first",
                    "https://emoji.qpic.cn/second",
                ],
            )

    def test_concurrent_same_sticker_is_single_flight(self):
        class BlockingDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()
                self.call_lock = threading.Lock()

            def download(self, url):
                with self.call_lock:
                    self.calls.append(url)
                self.entered.set()
                if not self.release.wait(2):
                    raise MomentsMediaDownloadError("test release timed out")
                return DownloadedMedia(self.payload, "image/png", ".png")

        with tempfile.TemporaryDirectory() as temporary:
            downloader = BlockingDownloader()
            service = StickerService(
                "wxid_a",
                cache_root=Path(temporary),
                downloader=downloader,
            )
            source = {
                "md5": hashlib.md5(PNG).hexdigest(),
                "cdn_url": "https://emoji.qpic.cn/single-flight",
            }
            start = threading.Barrier(3)

            def resolve_after_barrier():
                start.wait()
                return service.resolve(source)

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(resolve_after_barrier)
                second = executor.submit(resolve_after_barrier)
                start.wait()
                self.assertTrue(downloader.entered.wait(1))
                # Let the second worker join the in-flight result before the
                # leader publishes it; no second network call should occur.
                time.sleep(0.05)
                downloader.release.set()
                first_result = first.result(timeout=2)
                second_result = second.result(timeout=2)

            self.assertIs(first_result, second_result)
            self.assertEqual(downloader.calls, [source["cdn_url"]])
            self.assertEqual(downloader.resets, 1)


class LocalApiHostSecurityTests(unittest.TestCase):
    def test_loopback_hosts_and_testclient_default_are_allowed(self):
        with TestClient(api.app) as client:
            self.assertEqual(
                client.get("/api/definitely-missing").status_code,
                404,
            )
            for host in ("127.0.0.1:8520", "LOCALHOST", "[::1]:8520"):
                with self.subTest(host=host):
                    self.assertEqual(
                        client.get(
                            "/api/definitely-missing",
                            headers={"host": host},
                        ).status_code,
                        404,
                    )

    def test_dns_rebinding_and_malformed_hosts_are_rejected(self):
        rejected = (
            "evil.test",
            "127.0.0.1.evil.test",
            "localhost.evil.test",
            "evil@127.0.0.1",
            "localhost:",
            "localhost:0",
            "localhost:65536",
            "::1",
            "localhost/path",
            "localhost,evil.test",
        )
        with TestClient(api.app) as client:
            for host in rejected:
                with self.subTest(host=host):
                    response = client.get(
                        "/api/definitely-missing",
                        headers={"host": host},
                    )
                    self.assertEqual(response.status_code, 400)
        self.assertFalse(
            api._is_allowed_local_host_header(
                "testserver", allow_testserver=False
            )
        )

    def test_cross_site_browser_context_is_rejected_before_routing(self):
        with TestClient(api.app) as client:
            for headers in (
                {"origin": "https://evil.test"},
                {"sec-fetch-site": "cross-site"},
            ):
                with self.subTest(headers=headers):
                    response = client.get(
                        "/api/definitely-missing",
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(
                        response.headers["x-content-type-options"], "nosniff"
                    )

    def test_trusted_and_same_origin_browser_requests_are_allowed(self):
        with TestClient(api.app) as client:
            for headers in (
                {"origin": "http://localhost:3000"},
                {
                    "host": "127.0.0.1:8765",
                    "origin": "http://127.0.0.1:8765",
                },
                {"sec-fetch-site": "same-origin"},
            ):
                with self.subTest(headers=headers):
                    response = client.get(
                        "/api/definitely-missing",
                        headers=headers,
                    )
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    self.assertEqual(
                        response.headers["x-content-type-options"], "nosniff"
                    )


class StickerAndContactsApiTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        api._sticker_service_cache.clear()

    async def test_sticker_response_is_attachment_and_never_cached(self):
        message = {
            "id": 7,
            "type": 47,
            "create_time": 100,
            "server_id": "700",
            "_sticker_source": {
                "md5": hashlib.md5(PNG).hexdigest(),
                "cdn_url": "https://emoji.qpic.cn/sticker.png",
            },
        }
        fake_service = SimpleNamespace(
            resolve=lambda source: SimpleNamespace(
                data=PNG,
                mime_type="image/png",
                extension=".png",
            )
        )
        message_key = api._message_key("friend", message)
        account_id = api.config.wxid or ""
        token = api._sticker_token(
            account_id,
            "friend",
            7,
            100,
            message_key,
            download=True,
        )
        service_thread_ids = []

        def get_service():
            service_thread_ids.append(threading.get_ident())
            return fake_service

        with patch.object(api, "get_parser", return_value=object()), patch.object(
            api, "_find_sticker_message", return_value=message
        ), patch.object(api, "get_sticker_service", side_effect=get_service):
            response = await api.get_chat_sticker(
                "friend",
                7,
                create_time=100,
                message_key=message_key,
                token=token,
                download=True,
            )

        self.assertEqual(response.body, PNG)
        self.assertEqual(response.media_type, "image/png")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(len(service_thread_ids), 1)
        self.assertNotEqual(service_thread_ids[0], threading.get_ident())

    async def test_sticker_token_is_account_identity_and_purpose_bound(self):
        message = {
            "id": 9,
            "type": 47,
            "create_time": 123,
            "server_id": "900",
        }
        message_key = api._message_key("朋友", message)
        display = api._sticker_token(
            "wxid_a", "朋友", 9, 123, message_key, download=False
        )
        export = api._sticker_token(
            "wxid_a", "朋友", 9, 123, message_key, download=True
        )
        self.assertRegex(display, r"^[0-9a-f]{64}$")
        self.assertNotEqual(display, export)
        self.assertTrue(api._valid_sticker_token(
            display,
            "wxid_a",
            "朋友",
            9,
            123,
            message_key,
            download=False,
        ))
        for account, talker, message_id, create_time, key, download in (
            ("wxid_b", "朋友", 9, 123, message_key, False),
            ("wxid_a", "别人", 9, 123, message_key, False),
            ("wxid_a", "朋友", 10, 123, message_key, False),
            ("wxid_a", "朋友", 9, 124, message_key, False),
            ("wxid_a", "朋友", 9, 123, message_key + "x", False),
            ("wxid_a", "朋友", 9, 123, message_key, True),
        ):
            self.assertFalse(api._valid_sticker_token(
                display,
                account,
                talker,
                message_id,
                create_time,
                key,
                download=download,
            ))

    async def test_invalid_sticker_token_is_rejected_before_parser_lookup(self):
        with patch.object(api, "get_parser") as get_parser, patch.object(
            api, "get_sticker_service"
        ) as get_service:
            with self.assertRaises(HTTPException) as rejected:
                await api.get_chat_sticker(
                    "friend",
                    7,
                    create_time=100,
                    message_key="friend:700:100:7",
                    token="0" * 64,
                    download=False,
                )
        self.assertEqual(rejected.exception.status_code, 403)
        get_parser.assert_not_called()
        get_service.assert_not_called()

    def test_public_message_replaces_private_sticker_sources_with_local_urls(self):
        secret_url = "https://emoji.qpic.cn/private-token"
        messages = [{
            "id": 9,
            "type": 47,
            "create_time": 123,
            "server_id": "900",
            "sticker": {
                "md5": "a" * 32,
                "description": "hello",
                "available": True,
            },
            "_sticker_source": {
                "cdn_url": secret_url,
                "aes_key": "do-not-expose",
                "extern_md5": "f" * 32,
            },
        }]
        api._enrich_sticker_messages("friend", messages)
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn(secret_url, serialized)
        self.assertNotIn("do-not-expose", serialized)
        self.assertNotIn("extern_md5", serialized)
        self.assertIn("/api/chat/friend/sticker/9", serialized)
        self.assertIn("download=1", serialized)
        sticker = messages[0]["sticker"]
        display_query = parse_qs(urlsplit(sticker["display_url"]).query)
        export_query = parse_qs(urlsplit(sticker["export_url"]).query)
        self.assertRegex(display_query["token"][0], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            display_query["token"][0], export_query["token"][0]
        )

    async def test_contacts_directory_marks_existing_chats(self):
        parser = SimpleNamespace(
            get_address_book=lambda: [
                {
                    "username": "wxid_friend",
                    "talker": "wxid_friend",
                    "display_name": "备注名",
                },
                {
                    "username": "wxid_no_chat",
                    "talker": "wxid_no_chat",
                    "display_name": "无聊天",
                },
            ],
            get_contacts=lambda: [{"talker": "wxid_friend"}],
        )
        with patch.object(api, "get_parser", return_value=parser), patch.object(
            api, "_enrich_chat_avatars"
        ):
            response = await api.get_contacts_directory()
        self.assertTrue(response["data"][0]["has_chat"])
        self.assertFalse(response["data"][1]["has_chat"])
        self.assertEqual(response["total"], 2)


class AboutSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_about_fields_are_bounded_and_saved(self):
        existing = {
            "about": {},
            "ui": {},
            "vision": {},
            "transcription": {},
            "analysis": {},
            "images": {"accounts": {}},
        }
        saved = {}

        def fake_save(update):
            saved.update(update)
            return {**existing, **update}

        with patch.object(api, "load_settings", return_value=existing), patch.object(
            api, "save_settings", side_effect=fake_save
        ):
            response = await api.update_settings(api.SettingsUpdate(about={
                "project_name": "我的项目",
                "version": "2.0",
                "author": "作者",
                "qq_group": "",
                "contact": "",
                "tutorial_url": "https://example.com/tutorial",
                "description": "介绍",
            }))
        self.assertEqual(saved["about"]["project_name"], "我的项目")
        self.assertEqual(response["data"]["about"]["version"], "2.0")

    async def test_about_rejects_unknown_fields_and_unsafe_tutorial_url(self):
        with patch.object(api, "load_settings", return_value={}):
            with self.assertRaises(HTTPException) as unknown:
                await api.update_settings(
                    api.SettingsUpdate(about={"untrusted": "value"})
                )
            self.assertEqual(unknown.exception.status_code, 400)

            with self.assertRaises(HTTPException) as unsafe:
                await api.update_settings(
                    api.SettingsUpdate(about={"tutorial_url": "file:///secret"})
                )
            self.assertEqual(unsafe.exception.status_code, 400)
