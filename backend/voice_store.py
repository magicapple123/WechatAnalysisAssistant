"""Persistent, account-scoped storage for voice transcriptions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from .app_paths import app_data_path


def _default_store_path() -> Path:
    return app_data_path("voice_transcriptions.sqlite3")


def build_transcription_signature(
    provider: str,
    model: str,
    language: str = "auto",
    settings_hash: str = "",
) -> str:
    """Build a stable cache signature without including an API key."""

    encoded = json.dumps(
        [
            str(provider or "").strip().lower(),
            str(model or "").strip(),
            str(language or "auto").strip().lower(),
            str(settings_hash or "").strip(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VoiceTranscriptionStore:
    """SQLite cache isolated by account and exact message identity."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else _default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS voice_transcriptions (
                    account_id TEXT NOT NULL,
                    talker TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    create_time INTEGER NOT NULL,
                    server_id TEXT NOT NULL DEFAULT '',
                    audio_sha256 TEXT NOT NULL DEFAULT '',
                    transcription TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'auto',
                    settings_hash TEXT NOT NULL DEFAULT '',
                    transcription_signature TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL NOT NULL DEFAULT 0.0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (
                        account_id, talker, message_id, create_time, server_id
                    )
                )
                """
            )
            # Add duration column to existing databases (safe no-op if present).
            try:
                conn.execute(
                    "ALTER TABLE voice_transcriptions ADD COLUMN duration_seconds REAL NOT NULL DEFAULT 0.0"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_voice_transcription_reuse
                ON voice_transcriptions (
                    account_id, audio_sha256, transcription_signature, status,
                    updated_at
                )
                """
            )

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        return dict(row) if row is not None else None

    @staticmethod
    def _identity(
        account_id: str,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object,
    ) -> tuple[str, str, int, int, str]:
        return (
            str(account_id or "").strip(),
            str(talker or "").strip(),
            int(message_id),
            int(create_time or 0),
            str(server_id or ""),
        )

    def get(
        self,
        account_id: str,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
    ) -> Optional[dict]:
        identity = self._identity(
            account_id, talker, message_id, create_time, server_id
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM voice_transcriptions
                WHERE account_id=? AND talker=? AND message_id=?
                  AND create_time=? AND server_id=?
                """,
                identity,
            ).fetchone()
        return self._row_to_dict(row)

    def get_many(
        self,
        account_id: str,
        talker: str,
        messages: Iterable[dict],
    ) -> dict[tuple[int, int, str], dict]:
        refs = {
            (
                int(message.get("id") or 0),
                int(message.get("create_time") or 0),
                str(message.get("server_id") or ""),
            )
            for message in messages
        }
        if not refs:
            return {}

        result: dict[tuple[int, int, str], dict] = {}
        ref_list = list(refs)
        with self._connection() as conn:
            # 3 parameters per row plus account/talker: comfortably below the
            # most conservative SQLite parameter limit.
            for start in range(0, len(ref_list), 200):
                chunk = ref_list[start:start + 200]
                conditions = " OR ".join(
                    "(message_id=? AND create_time=? AND server_id=?)"
                    for _item in chunk
                )
                params: list[object] = [str(account_id or ""), str(talker or "")]
                for item in chunk:
                    params.extend(item)
                rows = conn.execute(
                    """
                    SELECT * FROM voice_transcriptions
                    WHERE account_id=? AND talker=? AND (%s)
                    """ % conditions,
                    params,
                ).fetchall()
                for row in rows:
                    record = dict(row)
                    result[
                        (
                            int(record["message_id"]),
                            int(record["create_time"]),
                            str(record["server_id"]),
                        )
                    ] = record
        return result

    def find_reusable(
        self,
        account_id: str,
        audio_sha256: str,
        provider: str,
        model: str,
        language: str = "auto",
        settings_hash: str = "",
    ) -> Optional[dict]:
        audio_hash = str(audio_sha256 or "").strip().lower()
        if not audio_hash:
            return None
        signature = build_transcription_signature(
            provider, model, language, settings_hash
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM voice_transcriptions
                WHERE account_id=? AND audio_sha256=?
                  AND transcription_signature=? AND status='success'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (str(account_id or ""), audio_hash, signature),
            ).fetchone()
        return self._row_to_dict(row)

    # ``reuse`` is intentionally an alias, making the manager call expressive.
    reuse = find_reusable

    def save(
        self,
        *,
        account_id: str,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
        audio_sha256: str = "",
        transcription: str = "",
        status: str,
        error: str = "",
        provider: str = "",
        model: str = "",
        language: str = "auto",
        settings_hash: str = "",
        duration_seconds: float = 0.0,
    ) -> dict:
        identity = self._identity(
            account_id, talker, message_id, create_time, server_id
        )
        if not identity[0] or not identity[1]:
            raise ValueError("account_id and talker are required")
        normalized_status = str(status or "").strip().lower()
        if not normalized_status:
            raise ValueError("status is required")
        provider_value = str(provider or "").strip()
        model_value = str(model or "").strip()
        language_value = str(language or "auto").strip().lower() or "auto"
        settings_value = str(settings_hash or "").strip()
        signature = build_transcription_signature(
            provider_value, model_value, language_value, settings_value
        )
        values = identity + (
            str(audio_sha256 or "").strip().lower(),
            str(transcription or "").strip(),
            normalized_status,
            str(error or "")[:1000],
            provider_value,
            model_value,
            language_value,
            settings_value,
            signature,
            float(duration_seconds),
            int(time.time()),
        )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO voice_transcriptions (
                    account_id, talker, message_id, create_time, server_id,
                    audio_sha256, transcription, status, error, provider,
                    model, language, settings_hash, transcription_signature,
                    duration_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    account_id, talker, message_id, create_time, server_id
                ) DO UPDATE SET
                    audio_sha256=excluded.audio_sha256,
                    transcription=excluded.transcription,
                    status=excluded.status,
                    error=excluded.error,
                    provider=excluded.provider,
                    model=excluded.model,
                    language=excluded.language,
                    settings_hash=excluded.settings_hash,
                    transcription_signature=excluded.transcription_signature,
                    duration_seconds=excluded.duration_seconds,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return self.get(*identity) or {}
