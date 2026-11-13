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
from typing import Literal

from email_agent.search import AttachmentReference

AttachmentProcessingStatus = Literal["downloaded", "parsed", "skipped", "unsupported", "failed"]


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
    snippet: str
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
            field: _optional_text(message.get(field)) for field in ("to", "cc", "bcc", "reply_to")
        }
        return cls(
            message_id=message_id,
            thread_id=_required_text(message, "thread_id"),
            sender=_optional_text(message.get("from")),
            recipients=json.dumps(recipients, ensure_ascii=False),
            subject=_optional_text(message.get("subject")),
            snippet=_optional_text(message.get("snippet")),
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


@dataclass(frozen=True)
class AttachmentSource:
    """Original Gmail metadata for one message-to-attachment occurrence."""

    part_id: str
    attachment_id: str
    filename: str
    mime_type: str
    reported_size: int
    inline: bool
    source_metadata: str


@dataclass(frozen=True)
class ExtractedAttachmentDocument:
    """One stable, persisted document produced by an attachment reader."""

    document_id: str
    text: str
    metadata: str


@dataclass(frozen=True)
class StoredAttachmentOccurrence:
    """Previously persisted state used to resume attachment processing."""

    occurrence_id: int
    content_sha256: str | None
    content_path: Path | None
    processing_status: str
    indexing_status: str


class ArchiveError(ValueError):
    """Raised when a Gmail message cannot be represented in the local archive."""


class SQLiteArchive:
    """Persist original message bodies and ingestion state under one data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "archive.sqlite3"
        self.messages_dir = data_dir / "messages"
        self.attachments_dir = data_dir / "attachments"
        self.extracted_dir = data_dir / "extracted"
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def completed_message_ids(self) -> set[str]:
        """Return message IDs whose original bodies were durably archived."""
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT gmail_message_id FROM messages WHERE ingestion_status = 'downloaded'"
            )
            return {str(row[0]) for row in rows}

    def message_time_bounds(self) -> tuple[int | None, int | None]:
        """Return oldest/newest archived message timestamps as UTC epoch seconds."""
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT message_timestamp FROM messages WHERE ingestion_status='downloaded'"
            )
            timestamps = []
            for (value,) in rows:
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                timestamps.append(int(parsed.timestamp()))
        return (min(timestamps), max(timestamps)) if timestamps else (None, None)

    def failed_message_ids(self) -> list[str]:
        """Return only message IDs that need another download attempt."""
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT gmail_message_id FROM ingestion_failures "
                "ORDER BY updated_at, gmail_message_id"
            )
            return [str(row[0]) for row in rows]

    def pending_attachment_message_ids(self) -> list[str]:
        """Return archived messages whose attachment scan is absent or has failures."""
        with closing(self._connect()) as database:
            rows = database.execute(
                """
                SELECT DISTINCT messages.gmail_message_id
                FROM messages
                LEFT JOIN attachment_scan_state
                    ON attachment_scan_state.gmail_message_id = messages.gmail_message_id
                LEFT JOIN message_attachments
                    ON message_attachments.gmail_message_id = messages.gmail_message_id
                WHERE messages.ingestion_status = 'downloaded'
                  AND (
                    attachment_scan_state.gmail_message_id IS NULL
                    OR message_attachments.processing_status = 'failed'
                  )
                ORDER BY messages.gmail_message_id
                """
            )
            return [str(row[0]) for row in rows]

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

    def message_body_path(self, message_id: str) -> Path:
        """Return the preserved source-body path for an archived message."""
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT body_path FROM messages "
                "WHERE gmail_message_id = ? AND ingestion_status = 'downloaded'",
                (message_id,),
            ).fetchone()
        if row is None or not row[0]:
            raise ArchiveError(f"message {message_id} has no archived body")
        return self.data_dir / str(row[0])

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

    def store_attachment(
        self,
        message_id: str,
        source: AttachmentSource,
        content: bytes | None,
        *,
        status: AttachmentProcessingStatus,
        problem: str | None = None,
    ) -> tuple[int, str | None, Path | None]:
        """Persist an occurrence and, when available, its content-addressed bytes."""
        digest: str | None = None
        relative_path: Path | None = None
        if content is not None:
            digest = hashlib.sha256(content).hexdigest()
            relative_path = Path("attachments") / digest[:2] / digest
            destination = self.data_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary_path = destination.with_suffix(".tmp")
                temporary_path.write_bytes(content)
                temporary_path.replace(destination)

        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            if digest is not None and relative_path is not None and content is not None:
                database.execute(
                    """
                    INSERT OR IGNORE INTO attachment_blobs (
                        content_sha256, byte_size, storage_path, extraction_status,
                        failure_reason, created_at, updated_at
                    ) VALUES (?, ?, ?, 'downloaded', NULL, ?, ?)
                    """,
                    (digest, len(content), relative_path.as_posix(), now, now),
                )
            database.execute(
                """
                INSERT INTO message_attachments (
                    gmail_message_id, part_id, gmail_attachment_id, filename, mime_type,
                    reported_size, inline, source_metadata, content_sha256,
                    processing_status, indexing_status, failure_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(gmail_message_id, part_id) DO UPDATE SET
                    gmail_attachment_id = excluded.gmail_attachment_id,
                    filename = excluded.filename,
                    mime_type = excluded.mime_type,
                    reported_size = excluded.reported_size,
                    inline = excluded.inline,
                    source_metadata = excluded.source_metadata,
                    content_sha256 = COALESCE(
                        excluded.content_sha256, message_attachments.content_sha256
                    ),
                    processing_status = excluded.processing_status,
                    failure_reason = excluded.failure_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    source.part_id,
                    source.attachment_id,
                    source.filename,
                    source.mime_type,
                    source.reported_size,
                    int(source.inline),
                    source.source_metadata,
                    digest,
                    status,
                    problem[:1000] if problem else None,
                    now,
                ),
            )
            row = database.execute(
                "SELECT id FROM message_attachments WHERE gmail_message_id = ? AND part_id = ?",
                (message_id, source.part_id),
            ).fetchone()
        if row is None:
            raise ArchiveError("attachment occurrence could not be stored")
        return int(row[0]), digest, self.data_dir / relative_path if relative_path else None

    def attachment_occurrence(
        self,
        message_id: str,
        part_id: str,
    ) -> StoredAttachmentOccurrence | None:
        """Return resumable state for one known Gmail attachment occurrence."""
        with closing(self._connect()) as database:
            row = database.execute(
                """
                SELECT message_attachments.id, message_attachments.content_sha256,
                       attachment_blobs.storage_path,
                       message_attachments.processing_status,
                       message_attachments.indexing_status
                FROM message_attachments
                LEFT JOIN attachment_blobs
                    ON attachment_blobs.content_sha256 = message_attachments.content_sha256
                WHERE gmail_message_id = ? AND part_id = ?
                """,
                (message_id, part_id),
            ).fetchone()
        if row is None:
            return None
        return StoredAttachmentOccurrence(
            occurrence_id=int(row[0]),
            content_sha256=str(row[1]) if row[1] is not None else None,
            content_path=self.data_dir / str(row[2]) if row[2] is not None else None,
            processing_status=str(row[3]),
            indexing_status=str(row[4]),
        )

    def attachment_content_status(self, content_sha256: str) -> str | None:
        """Return the shared extraction state for content-addressed bytes."""
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT extraction_status FROM attachment_blobs WHERE content_sha256 = ?",
                (content_sha256,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def attachment_sources(self, document_id: str) -> tuple[AttachmentReference, ...]:
        """Resolve every archived occurrence through SQLite, never vector payloads."""
        with closing(self._connect()) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                """
                SELECT a.id, a.gmail_attachment_id, a.filename, a.mime_type,
                       m.gmail_message_id, m.gmail_thread_id, m.sender,
                       m.message_timestamp, m.source_reference, b.storage_path
                FROM attachment_documents d
                JOIN attachment_blobs b ON b.content_sha256 = d.content_sha256
                JOIN message_attachments a ON a.content_sha256 = d.content_sha256
                JOIN messages m ON m.gmail_message_id = a.gmail_message_id
                WHERE d.document_id = ? AND m.ingestion_status = 'downloaded'
                ORDER BY a.id
                """,
                (document_id,),
            ).fetchall()
        return tuple(
            AttachmentReference(
                occurrence_id=row["id"],
                attachment_id=row["gmail_attachment_id"],
                gmail_message_id=row["gmail_message_id"],
                thread_id=row["gmail_thread_id"],
                filename=row["filename"],
                mime_type=row["mime_type"],
                sender=row["sender"],
                timestamp=row["message_timestamp"],
                location=row["source_reference"],
                content_path=str(self.data_dir / row["storage_path"]),
            )
            for row in rows
        )

    def mark_attachment_scan_complete(self, message_id: str) -> None:
        """Record that Gmail attachment metadata was successfully enumerated."""
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            database.execute(
                """
                INSERT INTO attachment_scan_state (gmail_message_id, scanned_at)
                VALUES (?, ?)
                ON CONFLICT(gmail_message_id) DO UPDATE SET scanned_at = excluded.scanned_at
                """,
                (message_id, now),
            )

    def set_attachment_result(
        self,
        occurrence_id: int,
        status: AttachmentProcessingStatus,
        *,
        problem: str | None = None,
    ) -> None:
        """Record a non-parsed terminal attachment outcome."""
        with closing(self._connect()) as database, database:
            database.execute(
                """
                UPDATE message_attachments
                SET processing_status = ?, failure_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    problem[:1000] if problem else None,
                    datetime.now(UTC).isoformat(),
                    occurrence_id,
                ),
            )
            if status == "failed":
                database.execute(
                    """
                    UPDATE attachment_blobs
                    SET extraction_status = 'failed', failure_reason = ?, updated_at = ?
                    WHERE content_sha256 = (
                        SELECT content_sha256 FROM message_attachments WHERE id = ?
                    ) AND extraction_status != 'parsed'
                    """,
                    (
                        problem[:1000] if problem else None,
                        datetime.now(UTC).isoformat(),
                        occurrence_id,
                    ),
                )

    def store_attachment_documents(
        self,
        content_sha256: str,
        documents: list[ExtractedAttachmentDocument],
    ) -> None:
        """Persist extracted text and reader metadata independently of the vector index."""
        stored: list[tuple[ExtractedAttachmentDocument, str]] = []
        for document in documents:
            filename = hashlib.sha256(document.document_id.encode("utf-8")).hexdigest() + ".txt"
            relative_path = Path("extracted") / filename[:2] / filename
            destination = self.data_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = destination.with_suffix(".tmp")
            temporary_path.write_text(document.text, encoding="utf-8")
            temporary_path.replace(destination)
            stored.append((document, relative_path.as_posix()))

        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as database, database:
            database.execute(
                "DELETE FROM attachment_documents WHERE content_sha256 = ?",
                (content_sha256,),
            )
            database.executemany(
                """
                INSERT INTO attachment_documents (
                    document_id, content_sha256, ordinal, text_path, metadata
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        document.document_id,
                        content_sha256,
                        ordinal,
                        path,
                        document.metadata,
                    )
                    for ordinal, (document, path) in enumerate(stored)
                ],
            )
            database.execute(
                """
                UPDATE attachment_blobs
                SET extraction_status = 'parsed', failure_reason = NULL, updated_at = ?
                WHERE content_sha256 = ?
                """,
                (now, content_sha256),
            )
            database.execute(
                """
                UPDATE message_attachments
                SET processing_status = 'parsed', failure_reason = NULL, updated_at = ?
                WHERE content_sha256 = ?
                  AND processing_status IN ('downloaded', 'failed', 'parsed')
                """,
                (now, content_sha256),
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
                gmail_message_id, gmail_thread_id, sender, recipients, subject, snippet,
                message_timestamp, labels, source_reference, body_path, body_format,
                ingestion_status, indexing_status, failure_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(gmail_message_id) DO UPDATE SET
                gmail_thread_id = excluded.gmail_thread_id,
                sender = excluded.sender,
                recipients = excluded.recipients,
                subject = excluded.subject,
                snippet = excluded.snippet,
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
                metadata.snippet,
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
                    snippet TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS attachment_blobs (
                    content_sha256 TEXT PRIMARY KEY,
                    byte_size INTEGER NOT NULL,
                    storage_path TEXT NOT NULL UNIQUE,
                    extraction_status TEXT NOT NULL
                        CHECK (extraction_status IN ('downloaded', 'parsed', 'failed')),
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_message_id TEXT NOT NULL REFERENCES messages(gmail_message_id),
                    part_id TEXT NOT NULL,
                    gmail_attachment_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    reported_size INTEGER NOT NULL,
                    inline INTEGER NOT NULL,
                    source_metadata TEXT NOT NULL,
                    content_sha256 TEXT REFERENCES attachment_blobs(content_sha256),
                    processing_status TEXT NOT NULL CHECK (
                        processing_status IN (
                            'downloaded', 'parsed', 'skipped', 'unsupported', 'failed'
                        )
                    ),
                    indexing_status TEXT NOT NULL CHECK (
                        indexing_status IN ('pending', 'indexed', 'failed')
                    ),
                    failure_reason TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(gmail_message_id, part_id)
                );

                CREATE INDEX IF NOT EXISTS attachments_by_content
                    ON message_attachments(content_sha256);

                CREATE TABLE IF NOT EXISTS attachment_documents (
                    document_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL
                        REFERENCES attachment_blobs(content_sha256) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text_path TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    UNIQUE(content_sha256, ordinal)
                );

                CREATE TABLE IF NOT EXISTS attachment_scan_state (
                    gmail_message_id TEXT PRIMARY KEY REFERENCES messages(gmail_message_id),
                    scanned_at TEXT NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in database.execute("PRAGMA table_info(messages)")}
            if "snippet" not in columns:
                database.execute("ALTER TABLE messages ADD COLUMN snippet TEXT NOT NULL DEFAULT ''")

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
