"""WeChat 4.x Moments image stream decryption.

Encrypted Moments image responses (``x-Enc: 1``) use the 64-bit ISAAC
generator as a stream cipher.  The decimal media key is placed in the first
seed slot, generated words are consumed in reverse order, serialized as big
endian bytes, and XORed with the complete image payload.

The implementation follows Bob Jenkins' public-domain ISAAC-64 reference
algorithm.  It is intentionally small and has no network or filesystem I/O.
"""

from __future__ import annotations

from typing import Any


_MASK_64 = (1 << 64) - 1
_STATE_SIZE = 256
# Exact constant from Bob Jenkins' ISAAC-64 reference.  WeChat 4.x encrypted
# Moments media responses have also been verified against this variant.
_GOLDEN_RATIO_64 = 0x9E3779B97F4A7C13


class SnsMediaCryptoError(ValueError):
    """The Moments media key or encrypted payload is invalid."""


def normalize_sns_media_key(value: Any) -> int:
    """Return a WeChat decimal media key as an unsigned 64-bit integer."""

    text = str("" if value is None else value).strip()
    if not text or not text.isdecimal():
        raise SnsMediaCryptoError("朋友圈图片缺少有效的十进制解密 key")
    # A uint64 never needs more than 20 decimal digits.  Bound the input
    # before int() so Python's oversized-integer guard cannot leak a raw
    # ValueError for hostile XML.
    if len(text) > 20:
        raise SnsMediaCryptoError("朋友圈图片解密 key 超出 64 位范围")
    try:
        number = int(text, 10)
    except ValueError as exc:
        raise SnsMediaCryptoError("朋友圈图片缺少有效的十进制解密 key") from exc
    if number < 0 or number > _MASK_64:
        raise SnsMediaCryptoError("朋友圈图片解密 key 超出 64 位范围")
    return number


class Isaac64:
    """Minimal ISAAC-64 generator with WeChat's seed/consumption contract."""

    def __init__(self, seed: Any):
        self._memory = [0] * _STATE_SIZE
        self._results = [0] * _STATE_SIZE
        self._results[0] = normalize_sns_media_key(seed)
        self._aa = 0
        self._bb = 0
        self._cc = 0
        self._count = 0
        self._keystream_tail = b""
        self._initialize()

    @staticmethod
    def _mix(values: list[int]) -> list[int]:
        a, b, c, d, e, f, g, h = values
        a = (a - e) & _MASK_64
        f = (f ^ (h >> 9)) & _MASK_64
        h = (h + a) & _MASK_64
        b = (b - f) & _MASK_64
        g = (g ^ ((a << 9) & _MASK_64)) & _MASK_64
        a = (a + b) & _MASK_64
        c = (c - g) & _MASK_64
        h = (h ^ (b >> 23)) & _MASK_64
        b = (b + c) & _MASK_64
        d = (d - h) & _MASK_64
        a = (a ^ ((c << 15) & _MASK_64)) & _MASK_64
        c = (c + d) & _MASK_64
        e = (e - a) & _MASK_64
        b = (b ^ (d >> 14)) & _MASK_64
        d = (d + e) & _MASK_64
        f = (f - b) & _MASK_64
        c = (c ^ ((e << 20) & _MASK_64)) & _MASK_64
        e = (e + f) & _MASK_64
        g = (g - c) & _MASK_64
        d = (d ^ (f >> 17)) & _MASK_64
        f = (f + g) & _MASK_64
        h = (h - d) & _MASK_64
        e = (e ^ ((g << 14) & _MASK_64)) & _MASK_64
        g = (g + h) & _MASK_64
        return [a, b, c, d, e, f, g, h]

    def _initialize(self) -> None:
        mixed = [_GOLDEN_RATIO_64] * 8
        for _ in range(4):
            mixed = self._mix(mixed)

        for offset in range(0, _STATE_SIZE, 8):
            mixed = [
                (mixed[index] + self._results[offset + index]) & _MASK_64
                for index in range(8)
            ]
            mixed = self._mix(mixed)
            self._memory[offset:offset + 8] = mixed

        for offset in range(0, _STATE_SIZE, 8):
            mixed = [
                (mixed[index] + self._memory[offset + index]) & _MASK_64
                for index in range(8)
            ]
            mixed = self._mix(mixed)
            self._memory[offset:offset + 8] = mixed

        self._generate()
        self._count = _STATE_SIZE

    def _generate(self) -> None:
        self._cc = (self._cc + 1) & _MASK_64
        self._bb = (self._bb + self._cc) & _MASK_64
        for index in range(_STATE_SIZE):
            value = self._memory[index]
            operation = index & 3
            if operation == 0:
                self._aa = ~(
                    self._aa ^ ((self._aa << 21) & _MASK_64)
                ) & _MASK_64
            elif operation == 1:
                self._aa = (self._aa ^ (self._aa >> 5)) & _MASK_64
            elif operation == 2:
                self._aa = (
                    self._aa ^ ((self._aa << 12) & _MASK_64)
                ) & _MASK_64
            else:
                self._aa = (self._aa ^ (self._aa >> 33)) & _MASK_64

            self._aa = (
                self._aa + self._memory[(index + 128) & 255]
            ) & _MASK_64
            mixed = (
                self._memory[(value >> 3) & 255] + self._aa + self._bb
            ) & _MASK_64
            self._memory[index] = mixed
            self._bb = (
                self._memory[(mixed >> 11) & 255] + value
            ) & _MASK_64
            self._results[index] = self._bb

    def next_uint64(self) -> int:
        """Return the next word using the reference generator's reverse pool."""

        if self._count == 0:
            self._generate()
            self._count = _STATE_SIZE
        self._count -= 1
        return self._results[self._count]

    def xor_big_endian(self, payload: bytes) -> bytes:
        """XOR ``payload`` with the WeChat big-endian ISAAC-64 byte stream."""

        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise SnsMediaCryptoError("朋友圈图片密文类型无效")
        # bytes() also normalizes non-contiguous memoryviews into a safe,
        # contiguous one-byte representation.
        source = memoryview(bytes(payload))
        output = bytearray(len(source))
        offset = 0
        if self._keystream_tail:
            block_size = min(len(self._keystream_tail), len(source))
            for index in range(block_size):
                output[index] = source[index] ^ self._keystream_tail[index]
            self._keystream_tail = self._keystream_tail[block_size:]
            offset = block_size
        while offset < len(source):
            key_block = self.next_uint64().to_bytes(8, "big")
            block_size = min(8, len(source) - offset)
            for index in range(block_size):
                output[offset + index] = source[offset + index] ^ key_block[index]
            if block_size < len(key_block):
                self._keystream_tail = key_block[block_size:]
            offset += block_size
        return bytes(output)


def decrypt_sns_image(payload: bytes, key: Any) -> bytes:
    """Decrypt one complete WeChat 4.x Moments image response in memory."""

    if not payload:
        raise SnsMediaCryptoError("朋友圈图片密文为空")
    return Isaac64(key).xor_big_endian(payload)
