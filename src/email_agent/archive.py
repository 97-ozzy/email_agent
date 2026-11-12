"""Durable local storage for imported Gmail messages."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ArchiveState:
    """Counts that decide whether startup should import or reuse local state."""

    completed: int
    failed: int
    initialized: bool

    @property
    def exists(self) -> bool:
        return self.initialized or self.completed > 0 or self.failed > 0


@dataclass(frozen=True)
class _MessageMetadata:
    message_id: str
    thread_id: str
    sender: str
    recipients: str
    subject: str
    timestamp: str
    labels: str
    source_reference: str

    @classmethod
    def from_message(cls, message: Mapping[str, object]) -> _MessageMetadata:
        message_id = _required_text(message, "id")
        labels = message.get("labels", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ArchiveError(f"message {message_id} has invalid labels")
        recipients = {
            field: _optional_text(message.get(field))
            for field in ("to", "cc", "bcc", "reply_to")
        }
        return cls(
            message_id=message_id,
            thread_id=_required_text(message, "thread_id"),
            sender=_optional_text(message.get("from")),
            recipients=json.dumps(recipients, ensure_ascii=False),
            subject=_optional_text(message.get("subject")),
            timestamp=_optional_text(message.get("date")),
            labels=json.dumps(labels, ensure_ascii=False),
            source_reference=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        )


@dataclass(frozen=True)
class _StoredMessageState:
    body_path: str
    body_format: str
    ingestion_status: str
    problem: str | None
    updated_at: str


class ArchiveError(ValueError):
    """Raised when a Gmail message cannot be represented in the local archive."""


class SQLiteArchive:
    """Persist original message bodies and ingestion state under one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "archive.sqlite3"
        self.messages_dir = data_dir / "messages"
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def completed_message_ids(self) -> set[str]:
        """Return message IDs whose original bodies were durably archived."""
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT gmail_message_id FROM messages WHERE ingestion_status = 'downloaded'"
            )
            return {str(row[0]) for row in rows}

    def state(self) -> ArchiveState:
        """Summarize durable import state without contacting Gmail."""
        with closing(self._connect()) as database:
            completed = database.execute(
                "SELECT COUNT(*) FROM messages WHERE ingestion_status = 'downloaded'"
            ).fetchone()[0]
            failed = database.execute("SELECT COUNT(*) FROM ingestion_failures").fetchone()[0]
            initialized = database.execute(
                "SELECT COUNT(*) FROM initial_import_state WHERE singleton = 1"
            ).fetchone()[0]
        return ArchiveState(int(completed), int(failed), bool(initialized))

    def mark_initial_import_attempted(self) -> None:
        """Prevent later startups from treating newly arrived mail as the initial archive."""
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            database.execute(
                """
                INSERT INTO initial_import_state (singleton, attempted_at)
                VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET attempted_at = excluded.attempted_at
                """,
                (now,),
            )

    def store_message(self, message: Mapping[str, object]) -> None:
        """Commit one complete Gmail message and its thread relationship."""
        metadata = _MessageMetadata.from_message(message)
        body = message.get("body")
        if not isinstance(body, str):
            raise ArchiveError(f"message {metadata.message_id} has no complete text body")

        relative_body_path = self._store_body(metadata.message_id, body)
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            self._upsert_message(
                database,
                metadata,
                _StoredMessageState(
                    body_path=relative_body_path.as_posix(),
                    body_format=_optional_text(message.get("body_format")),
                    ingestion_status="downloaded",
                    problem=None,
                    updated_at=now,
                ),
            )
            database.execute(
                "DELETE FROM ingestion_failures WHERE gmail_message_id = ?",
                (metadata.message_id,),
            )

    def record_failure(self, message: Mapping[str, object], problem: str) -> None:
        """Persist a failed download so incomplete archive state is observable and retryable."""
        message_id = _required_text(message, "id")
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            database.execute(
                """
                INSERT INTO ingestion_failures (gmail_message_id, problem, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(gmail_message_id) DO UPDATE SET
                    problem = excluded.problem,
                    updated_at = excluded.updated_at
                """,
                (message_id, problem[:1000], now),
            )

        try:
            metadata = _MessageMetadata.from_message(message)
        except ArchiveError:
            return
        with closing(self._connect()) as database, database:
            self._upsert_message(
                database,
                metadata,
                _StoredMessageState(
                    body_path="",
                    body_format="",
                    ingestion_status="failed",
                    problem=problem[:1000],
                    updated_at=now,
                ),
            )

    def _store_body(self, message_id: str, body: str) -> Path:
        body_name = hashlib.sha256(message_id.encode("utf-8")).hexdigest() + ".txt"
        relative_path = Path("messages") / body_name
        body_path = self.data_dir / relative_path
        temporary_path = body_path.with_suffix(".tmp")
        temporary_path.write_text(body, encoding="utf-8")
        temporary_path.replace(body_path)
        return relative_path

    @staticmethod
    def _upsert_message(
        database: sqlite3.Connection,
        metadata: _MessageMetadata,
        state: _StoredMessageState,
    ) -> None:
        database.execute(
            "INSERT OR IGNORE INTO threads (gmail_thread_id, created_at) VALUES (?, ?)",
            (metadata.thread_id, state.updated_at),
        )
        database.execute(
            """
            INSERT INTO messages (
                gmail_message_id, gmail_thread_id, sender, recipients, subject,
                message_timestamp, labels, source_reference, body_path, body_format,
                ingestion_status, indexing_status, failure_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                gmail_thread_id = excluded.gmail_thread_id,
                sender = excluded.sender,
                recipients = excluded.recipients,
                subject = excluded.subject,
                message_timestamp = excluded.message_timestamp,
                labels = excluded.labels,
                source_reference = excluded.source_reference,
                body_path = excluded.body_path,
                body_format = excluded.body_format,
                ingestion_status = excluded.ingestion_status,
                failure_reason = excluded.failure_reason,
                updated_at = excluded.updated_at
            """,
            (
                metadata.message_id,
                metadata.thread_id,
                metadata.sender,
                metadata.recipients,
                metadata.subject,
                metadata.timestamp,
                metadata.labels,
                metadata.source_reference,
                state.body_path,
                state.body_format,
                state.ingestion_status,
                state.problem,
                state.updated_at,
            ),
        )

    def _initialize(self) -> None:
        with closing(self._connect()) as database, database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    gmail_thread_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    gmail_message_id TEXT PRIMARY KEY,
                    gmail_thread_id TEXT NOT NULL REFERENCES threads(gmail_thread_id),
                    sender TEXT NOT NULL,
                    recipients TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    message_timestamp TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    source_reference TEXT NOT NULL,
                    body_path TEXT NOT NULL,
                    body_format TEXT NOT NULL,
                    ingestion_status TEXT NOT NULL,
                    indexing_status TEXT NOT NULL,
                    failure_reason TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS messages_by_thread
                    ON messages(gmail_thread_id, message_timestamp);

                CREATE TABLE IF NOT EXISTS ingestion_failures (
                    gmail_message_id TEXT PRIMARY KEY,
                    problem TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS initial_import_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    attempted_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database_path)
        database.execute("PRAGMA foreign_keys = ON")
        return database


def _required_text(message: Mapping[str, object], field: str) -> str:
    value = message.get(field)
    if not isinstance(value, str) or not value:
        raise ArchiveError(f"message is missing {field}")
    return value


def _optional_text(value: object) -> str:
    return value if isinstance(value, str) else ""
