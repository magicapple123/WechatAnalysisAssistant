"""Locate and decode WeChat 4.x voice-message resources.

WeChat 4.x stores voice payloads in encrypted ``media_N.db`` shards.  This
module deliberately reuses the application's WCDB decryptor instead of
copying cryptographic code from the reference project.  Decrypted databases
and WAV files are temporary, account-scoped resources and are removed by
``close()`` or their context manager.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import tempfile
import threading
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .decrypt import DatabaseDecryptor


SILK_MAGIC = b"#!SILK_V3"
DEFAULT_SAMPLE_RATE = 24_000
MAX_VOICE_BYTES = 64 * 1024 * 1024
_MEDIA_DB_RE = re.compile(r"^media_(\d+)\.db$", re.IGNORECASE)


class VoiceServiceError(RuntimeError):
    """A safe, user-presentable voice resource/decode error."""

    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class VoiceResource:
    """An exactly matched encrypted-database voice payload."""

    account_id: str
    talker: str
    message_id: int
    create_time: int
    server_id: str
    voice_data: bytes
    audio_sha256: str
    source_shard: str


@dataclass(frozen=True)
class DecodedVoice:
    """A decoded PCM WAV kept in memory until an API provider needs a file."""

    resource: VoiceResource
    wav_data: bytes
    sample_rate: int
    channels: int
    sample_width: int
    pcm_bytes: int
    duration_seconds: float


@dataclass(frozen=True)
class _MediaSchema:
    name_table: str
    name_column: str
    voice_table: str
    chat_id_column: str
    local_id_column: str
    create_time_column: str
    voice_data_column: str
    server_id_column: Optional[str]


@dataclass
class _PreparedShard:
    source_path: Path
    query_path: Path
    decryptor: Optional[DatabaseDecryptor]
    schema: Optional[_MediaSchema] = None


def discover_media_databases(msg_dir: Path) -> list[Path]:
    """Return valid ``media_N.db`` shards in numeric shard order."""

    root = Path(msg_dir)
    if not root.is_dir():
        return []
    found = []
    try:
        candidates = root.iterdir()
    except OSError:
        return []
    for candidate in candidates:
        match = _MEDIA_DB_RE.match(candidate.name)
        if not match:
            continue
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                found.append((int(match.group(1)), candidate.name.lower(), candidate))
        except OSError:
            continue
    found.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in found]


def normalize_silk_data(voice_data: object) -> bytes:
    """Normalize the one-byte WeChat wrapper while rejecting invalid blobs."""

    if isinstance(voice_data, memoryview):
        voice_data = voice_data.tobytes()
    if not isinstance(voice_data, (bytes, bytearray)):
        raise VoiceServiceError("INVALID_AUDIO", "语音数据格式无效")
    data = bytes(voice_data)
    if not data:
        raise VoiceServiceError("EMPTY_AUDIO", "语音数据为空")
    if len(data) > MAX_VOICE_BYTES:
        raise VoiceServiceError("AUDIO_TOO_LARGE", "语音数据超过安全大小限制")
    if data.startswith(b"\x02" + SILK_MAGIC):
        data = data[1:]
    if not data.startswith(SILK_MAGIC):
        raise VoiceServiceError("UNSUPPORTED_AUDIO", "该语音不是受支持的微信 SILK 格式")
    return data


def decode_silk_to_pcm_bytes(
    voice_data: object,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """Decode a WeChat SILK blob into 16-bit mono PCM bytes."""

    try:
        rate = int(sample_rate)
    except (TypeError, ValueError) as exc:
        raise VoiceServiceError("INVALID_SAMPLE_RATE", "语音采样率无效") from exc
    if rate < 8_000 or rate > 48_000:
        raise VoiceServiceError("INVALID_SAMPLE_RATE", "语音采样率超出支持范围")

    silk_data = normalize_silk_data(voice_data)
    try:
        import pysilk
    except ImportError as exc:
        raise VoiceServiceError(
            "SILK_DECODER_MISSING",
            "缺少微信语音解码组件 silk-python，请重新运行安装程序",
            status_code=501,
        ) from exc

    output = io.BytesIO()
    try:
        pysilk.decode(io.BytesIO(silk_data), output, rate)
    except Exception as exc:
        raise VoiceServiceError("SILK_DECODE_FAILED", "微信语音 SILK 解码失败") from exc
    pcm = output.getvalue()
    if not pcm or len(pcm) % 2:
        raise VoiceServiceError("SILK_DECODE_FAILED", "微信语音解码结果无效")
    return pcm


def decode_silk_to_wav_bytes(
    voice_data: object,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> bytes:
    """Decode a WeChat SILK blob into a standard 16-bit mono WAV."""

    pcm = decode_silk_to_pcm_bytes(voice_data, sample_rate=sample_rate)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    return output.getvalue()


@contextmanager
def temporary_wav_file(wav_data: bytes) -> Iterator[Path]:
    """Materialize WAV bytes for SDKs that only accept a path, then erase it."""

    if not isinstance(wav_data, bytes) or not wav_data.startswith(b"RIFF"):
        raise VoiceServiceError("INVALID_WAV", "待上传的 WAV 数据无效")
    descriptor, raw_path = tempfile.mkstemp(prefix="wechat_voice_", suffix=".wav")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(wav_data)
            handle.flush()
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except TypeError:  # Python 3.7 compatibility for downstream packaging.
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        except OSError:
            pass


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _normalize_server_id(value: object) -> str:
    text = str(value or "").strip()
    return "" if text == "0" else text


class WeChatVoiceService:
    """Account-scoped resolver for WeChat 4.x ``media_N.db`` shards.

    The service is safe to construct on one thread and call from a worker
    thread.  SQLite connections are deliberately short-lived; only decrypted
    temporary database paths are cached until :meth:`close`.
    """

    def __init__(self, account_id: str, msg_dir: Path, database_key: str):
        self.account_id = str(account_id or "").strip()
        self.msg_dir = Path(msg_dir)
        self.database_key = str(database_key or "").strip()
        if not self.account_id:
            raise VoiceServiceError("ACCOUNT_REQUIRED", "未选择微信账号", 400)
        if not self.database_key:
            raise VoiceServiceError("DATABASE_KEY_REQUIRED", "未配置数据库解密密钥", 400)
        self._source_paths = discover_media_databases(self.msg_dir)
        if not self._source_paths:
            raise VoiceServiceError("MEDIA_DB_NOT_FOUND", "未找到微信语音数据库", 404)
        self._prepared: dict[Path, _PreparedShard] = {}
        self._closed = False
        self._lock = threading.RLock()

    @property
    def shard_count(self) -> int:
        return len(self._source_paths)

    @staticmethod
    def _is_plaintext_database(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return handle.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    def _prepare_shard(self, source_path: Path) -> _PreparedShard:
        prepared = self._prepared.get(source_path)
        if prepared is not None:
            return prepared
        if self._closed:
            raise VoiceServiceError("SERVICE_CLOSED", "语音资源服务已关闭", 409)

        if self._is_plaintext_database(source_path):
            prepared = _PreparedShard(source_path, source_path, None)
        else:
            decryptor = DatabaseDecryptor(source_path, self.database_key)
            try:
                query_path = decryptor.decrypt_to_temp()
            except Exception as exc:
                decryptor.close()
                raise VoiceServiceError(
                    "MEDIA_DB_DECRYPT_FAILED",
                    "微信语音数据库解密失败",
                    500,
                ) from exc
            prepared = _PreparedShard(source_path, query_path, decryptor)
        self._prepared[source_path] = prepared
        return prepared

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _inspect_schema(conn: sqlite3.Connection) -> _MediaSchema:
        tables = {
            str(row[0]).lower(): str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        name_table = tables.get("name2id") or tables.get("chatname2id")
        voice_table = tables.get("voiceinfo")
        if not name_table or not voice_table:
            raise VoiceServiceError("MEDIA_SCHEMA_UNSUPPORTED", "微信语音数据库结构不受支持")

        def columns(table: str) -> dict[str, str]:
            return {
                str(row[1]).lower(): str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(%s)" % _quote_identifier(table)
                ).fetchall()
            }

        name_columns = columns(name_table)
        voice_columns = columns(voice_table)
        name_column = name_columns.get("user_name") or name_columns.get("username")
        required = {
            "chat": voice_columns.get("chat_name_id") or voice_columns.get("chat_id"),
            "local": voice_columns.get("local_id") or voice_columns.get("message_local_id"),
            "time": voice_columns.get("create_time"),
            "data": voice_columns.get("voice_data"),
        }
        if not name_column or any(value is None for value in required.values()):
            raise VoiceServiceError("MEDIA_SCHEMA_UNSUPPORTED", "微信语音数据库字段不受支持")
        return _MediaSchema(
            name_table=name_table,
            name_column=name_column,
            voice_table=voice_table,
            chat_id_column=str(required["chat"]),
            local_id_column=str(required["local"]),
            create_time_column=str(required["time"]),
            voice_data_column=str(required["data"]),
            server_id_column=(
                voice_columns.get("svr_id")
                or voice_columns.get("server_id")
                or voice_columns.get("message_svr_id")
            ),
        )

    def _find_in_shard(
        self,
        prepared: _PreparedShard,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: str,
    ) -> list[VoiceResource]:
        conn = self._connect(prepared.query_path)
        try:
            schema = prepared.schema
            if schema is None:
                schema = self._inspect_schema(conn)
                prepared.schema = schema

            name_rows = conn.execute(
                "SELECT rowid FROM %s WHERE %s=? LIMIT 2"
                % (
                    _quote_identifier(schema.name_table),
                    _quote_identifier(schema.name_column),
                ),
                (talker,),
            ).fetchall()
            if not name_rows:
                return []
            if len(name_rows) > 1:
                raise VoiceServiceError("AMBIGUOUS_TALKER", "语音数据库中的聊天对象映射不唯一")
            chat_id = name_rows[0][0]

            clauses = [
                "%s=?" % _quote_identifier(schema.chat_id_column),
                "%s=?" % _quote_identifier(schema.local_id_column),
                "%s=?" % _quote_identifier(schema.create_time_column),
            ]
            params: list[object] = [chat_id, message_id, create_time]
            if server_id:
                if not schema.server_id_column:
                    # A requested exact server identity must never silently
                    # degrade to a weaker match on an unfamiliar schema.
                    return []
                clauses.append("CAST(%s AS TEXT)=?" % _quote_identifier(schema.server_id_column))
                params.append(server_id)
            selected_server = (
                _quote_identifier(schema.server_id_column)
                if schema.server_id_column
                else "''"
            )
            sql = (
                "SELECT %s AS voice_data, %s AS stored_server_id "
                "FROM %s WHERE %s LIMIT 3"
                % (
                    _quote_identifier(schema.voice_data_column),
                    selected_server,
                    _quote_identifier(schema.voice_table),
                    " AND ".join(clauses),
                )
            )
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        resources = []
        for row in rows:
            data = row["voice_data"]
            if isinstance(data, memoryview):
                data = data.tobytes()
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            payload = bytes(data)
            if len(payload) > MAX_VOICE_BYTES:
                raise VoiceServiceError("AUDIO_TOO_LARGE", "语音数据超过安全大小限制")
            stored_server = _normalize_server_id(row["stored_server_id"])
            resources.append(
                VoiceResource(
                    account_id=self.account_id,
                    talker=talker,
                    message_id=message_id,
                    create_time=create_time,
                    server_id=stored_server or server_id,
                    voice_data=payload,
                    audio_sha256=hashlib.sha256(payload).hexdigest(),
                    source_shard=prepared.source_path.name,
                )
            )
        return resources

    def get_voice(
        self,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
    ) -> VoiceResource:
        """Resolve one voice message using its complete browser-visible identity."""

        normalized_talker = str(talker or "").strip()
        if not normalized_talker:
            raise VoiceServiceError("TALKER_REQUIRED", "聊天对象不能为空", 400)
        try:
            normalized_message_id = int(message_id)
            normalized_time = int(create_time)
        except (TypeError, ValueError) as exc:
            raise VoiceServiceError("INVALID_MESSAGE_REF", "语音消息标识无效", 400) from exc
        normalized_server_id = _normalize_server_id(server_id)

        with self._lock:
            if self._closed:
                raise VoiceServiceError("SERVICE_CLOSED", "语音资源服务已关闭", 409)
            matches: list[VoiceResource] = []
            database_errors = []
            for source_path in self._source_paths:
                try:
                    prepared = self._prepare_shard(source_path)
                    matches.extend(
                        self._find_in_shard(
                            prepared,
                            normalized_talker,
                            normalized_message_id,
                            normalized_time,
                            normalized_server_id,
                        )
                    )
                except VoiceServiceError as exc:
                    if exc.code in ("MEDIA_DB_DECRYPT_FAILED", "MEDIA_SCHEMA_UNSUPPORTED"):
                        database_errors.append(exc)
                        continue
                    raise
                except sqlite3.DatabaseError as exc:
                    database_errors.append(exc)

            if not matches:
                if database_errors and len(database_errors) == len(self._source_paths):
                    raise VoiceServiceError("MEDIA_DB_READ_FAILED", "微信语音数据库读取失败", 500)
                raise VoiceServiceError("VOICE_NOT_FOUND", "未找到对应的微信语音数据", 404)

            # Identical rows can briefly coexist while WeChat rotates shards.
            # Deduplicate those, but reject conflicting content rather than
            # attaching a transcript to the wrong reused local_id.
            unique = {(item.audio_sha256, item.server_id): item for item in matches}
            if len(unique) != 1:
                raise VoiceServiceError("AMBIGUOUS_VOICE", "匹配到多条不同的微信语音数据", 409)
            return next(iter(unique.values()))

    # A semantic alias useful to callers that think in resource resolution.
    resolve_voice = get_voice

    def decode_voice(
        self,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> DecodedVoice:
        resource = self.get_voice(talker, message_id, create_time, server_id)
        pcm = decode_silk_to_pcm_bytes(resource.voice_data, sample_rate=sample_rate)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm)
        duration = len(pcm) / float(int(sample_rate) * 2)
        return DecodedVoice(
            resource=resource,
            wav_data=output.getvalue(),
            sample_rate=int(sample_rate),
            channels=1,
            sample_width=2,
            pcm_bytes=len(pcm),
            duration_seconds=duration,
        )

    # Short name used by cloud/local transcription adapters.
    get_wav = decode_voice

    @contextmanager
    def temporary_wav(
        self,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> Iterator[tuple[Path, DecodedVoice]]:
        decoded = self.decode_voice(
            talker,
            message_id,
            create_time,
            server_id,
            sample_rate=sample_rate,
        )
        with temporary_wav_file(decoded.wav_data) as path:
            yield path, decoded

    def close(self) -> None:
        """Close decryptors and securely discard their temporary DB copies."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            prepared = list(self._prepared.values())
            self._prepared.clear()
            for shard in prepared:
                if shard.decryptor is not None:
                    try:
                        shard.decryptor.close()
                    except Exception:
                        pass

    def __enter__(self) -> "WeChatVoiceService":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
