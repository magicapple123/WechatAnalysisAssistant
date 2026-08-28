"""Persistent storage for image-recognition results.

The database contains descriptions only.  Decrypted image bytes stay in a
per-process temporary directory managed by :mod:`backend.image_service`.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from .app_paths import app_data_path


def _default_store_path() -> Path:
    return app_data_path("image_descriptions.sqlite3")


class ImageDescriptionStore:
    """Small SQLite-backed cache, isolated by WeChat account and message."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else _default_store_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
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
            existing = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='image_descriptions'"
            ).fetchone()
            if existing:
                info = conn.execute(
                    "PRAGMA table_info(image_descriptions)"
                ).fetchall()
                primary_key = [
                    row[1]
                    for row in sorted(
                        (row for row in info if row[5]),
                        key=lambda row: row[5],
                    )
                ]
                desired = [
                    "account_id",
                    "talker",
                    "message_id",
                    "create_time",
                    "server_id",
                ]
                if primary_key != desired:
                    conn.execute("DROP TABLE IF EXISTS image_descriptions_legacy")
                    conn.execute(
                        "ALTER TABLE image_descriptions "
                        "RENAME TO image_descriptions_legacy"
                    )
                    self._create_table(conn)
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO image_descriptions (
                            account_id, talker, message_id, create_time,
                            server_id, image_md5, image_sha256, description,
                            status, error, provider, model, prompt_hash, updated_at
                        )
                        SELECT account_id, talker, message_id, create_time,
                               COALESCE(server_id, ''), image_md5, image_sha256,
                               description, status, error, provider, model,
                               prompt_hash, updated_at
                        FROM image_descriptions_legacy
                        """
                    )
                    conn.execute("DROP TABLE image_descriptions_legacy")
            else:
                self._create_table(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_image_description_content
                ON image_descriptions (
                    account_id, image_sha256, model, prompt_hash, status
                )
                """
            )

    @staticmethod
    def _create_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_descriptions (
                account_id TEXT NOT NULL,
                talker TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                create_time INTEGER NOT NULL,
                server_id TEXT NOT NULL DEFAULT '',
                image_md5 TEXT NOT NULL DEFAULT '',
                image_sha256 TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT 'openai-compatible',
                model TEXT NOT NULL DEFAULT '',
                prompt_hash TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (
                    account_id, talker, message_id, create_time, server_id
                )
            )
            """
        )

    @staticmethod
    def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
        return dict(row) if row is not None else None

    def get(
        self,
        account_id: str,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
    ) -> Optional[dict]:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM image_descriptions
                WHERE account_id=? AND talker=? AND message_id=? AND create_time=?
                  AND server_id=?
                """,
                (
                    account_id,
                    talker,
                    int(message_id),
                    int(create_time or 0),
                    str(server_id or ""),
                ),
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
        # Keep each statement well under SQLite's default parameter limit.
        ref_list = list(refs)
        with self._connection() as conn:
            for start in range(0, len(ref_list), 200):
                chunk = ref_list[start:start + 200]
                conditions = " OR ".join(
                    "(message_id=? AND create_time=? AND server_id=?)" for _ in chunk
                )
                params: list[object] = [account_id, talker]
                for message_id, create_time, server_id in chunk:
                    params.extend((message_id, create_time, server_id))
                rows = conn.execute(
                    f"""
                    SELECT * FROM image_descriptions
                    WHERE account_id=? AND talker=? AND ({conditions})
                    """,
                    params,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    result[
                        (
                            item["message_id"],
                            item["create_time"],
                            item["server_id"],
                        )
                    ] = item
        return result

    def find_reusable(
        self,
        account_id: str,
        image_sha256: str,
        provider: str,
        model: str,
        prompt_hash: str,
    ) -> Optional[dict]:
        if not image_sha256:
            return None
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM image_descriptions
                WHERE account_id=? AND image_sha256=? AND provider=? AND model=?
                  AND prompt_hash=? AND status='success'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (account_id, image_sha256, provider, model, prompt_hash),
            ).fetchone()
        return self._row_to_dict(row)

    def save(
        self,
        *,
        account_id: str,
        talker: str,
        message_id: int,
        create_time: int,
        server_id: object = "",
        image_md5: str = "",
        image_sha256: str = "",
        description: str = "",
        status: str,
        error: str = "",
        provider: str = "openai-compatible",
        model: str = "",
        prompt_hash: str = "",
    ) -> dict:
        now = int(time.time())
        values = (
            account_id,
            talker,
            int(message_id),
            int(create_time or 0),
            str(server_id or ""),
            image_md5 or "",
            image_sha256 or "",
            (description or "").strip(),
            status,
            (error or "")[:1000],
            provider,
            model,
            prompt_hash,
            now,
        )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO image_descriptions (
                    account_id, talker, message_id, create_time, server_id,
                    image_md5, image_sha256, description, status, error,
                    provider, model, prompt_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    account_id, talker, message_id, create_time, server_id
                )
                DO UPDATE SET
                    server_id=excluded.server_id,
                    image_md5=excluded.image_md5,
                    image_sha256=excluded.image_sha256,
                    description=excluded.description,
                    status=excluded.status,
                    error=excluded.error,
                    provider=excluded.provider,
                    model=excluded.model,
                    prompt_hash=excluded.prompt_hash,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return self.get(
            account_id,
            talker,
            message_id,
            create_time,
            server_id=server_id,
        ) or {}
