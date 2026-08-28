import unittest

from backend.sns_media_crypto import (
    Isaac64,
    SnsMediaCryptoError,
    decrypt_sns_image,
    normalize_sns_media_key,
)


class SnsMediaCryptoTests(unittest.TestCase):
    def test_known_wechat_big_endian_stream(self):
        generator = Isaac64("1234567890123456789")
        stream = b"".join(
            generator.next_uint64().to_bytes(8, "big") for _ in range(4)
        )
        self.assertEqual(
            stream.hex(),
            "981c44954c02d3c62fd66a014dfdf392"
            "85b4e3e282339bff6115553361443053",
        )

    def test_known_stream_crosses_result_pool_refill(self):
        generator = Isaac64("1234567890123456789")
        stream = b"".join(
            generator.next_uint64().to_bytes(8, "big") for _ in range(260)
        )
        self.assertEqual(
            stream[2040:2080].hex(),
            "f0cf4b31556d05fc86670c98a8c553e9"
            "42bde7d61948e2fa523ebcb60479fc82"
            "9e8e22ec6f7c5d93",
        )

    def test_full_payload_xor_is_symmetric(self):
        plain = b"\xff\xd8\xff" + bytes(range(251)) + b"\xff\xd9"
        encrypted = Isaac64("18446744073709551615").xor_big_endian(plain)
        self.assertNotEqual(encrypted, plain)
        self.assertEqual(
            decrypt_sns_image(encrypted, "18446744073709551615"), plain
        )

    def test_chunked_xor_matches_one_shot_and_accepts_strided_view(self):
        seed = "1234567890123456789"
        payload = bytes(range(251)) * 20
        expected = Isaac64(seed).xor_big_endian(payload)

        generator = Isaac64(seed)
        chunks = (
            payload[:3],
            payload[3:2051],
            payload[2051:4098],
            payload[4098:],
        )
        self.assertEqual(
            b"".join(generator.xor_big_endian(chunk) for chunk in chunks),
            expected,
        )

        strided = memoryview(payload)[::2]
        self.assertEqual(
            Isaac64(seed).xor_big_endian(strided),
            Isaac64(seed).xor_big_endian(bytes(strided)),
        )

    def test_key_must_be_unsigned_decimal_64_bit(self):
        self.assertEqual(normalize_sns_media_key("42"), 42)
        self.assertEqual(normalize_sns_media_key(0), 0)
        for value in (
            "",
            "-1",
            "0x2a",
            "not-a-key",
            str(1 << 64),
            "9" * 5_000,
        ):
            with self.subTest(value=value), self.assertRaises(
                SnsMediaCryptoError
            ):
                normalize_sns_media_key(value)


if __name__ == "__main__":
    unittest.main()
