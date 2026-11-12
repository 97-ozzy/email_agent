"""Deterministic initial import of recent Inbox and Sent messages."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from email_agent.archive import SQLiteArchive
from email_agent.attachments import AttachmentReport, process_message_attachments
from email_agent.gmail import GmailAdapter

IMPORT_QUERY = "in:inbox OR in:sent"


@dataclass(frozen=True)
class ImportReport:
    """Observable outcomes from one initial-import attempt."""

    requested: int
    downloaded: int
    processed: int
    failed: int
    reused: int
    attachments: AttachmentReport = AttachmentReport()


def import_recent_messages(
    gmail: GmailAdapter,
    archive: SQLiteArchive,
    limit: int,
    attachment_maximum_bytes: int = 20 * 1024 * 1024,
) -> ImportReport:
    """Discover, download, and archive up to ``limit`` newest unique messages."""
    selected, discovery_failures = _discover_recent_messages(gmail, limit)
    completed = archive.completed_message_ids()
    downloaded = 0
    processed = 0
    failed = discovery_failures
    reused = 0
    attachments = AttachmentReport()

    for summary in selected:
        message_id = cast(str, summary["id"])
        if message_id in completed:
            reused += 1
            continue
        try:
            detail = gmail.get_message(message_id)
            if not isinstance(detail, dict):
                raise ValueError(f"Gmail returned an invalid message for {message_id}")
            downloaded += 1
            archive.store_message({**summary, **cast(dict[str, object], detail)})
            processed += 1
            attachments = attachments.merge(
                process_message_attachments(
                    gmail,
                    archive,
                    message_id,
                    attachment_maximum_bytes,
                )
            )
        except Exception as exc:
            failed += 1
            with suppress(Exception):
                archive.record_failure(summary, str(exc) or type(exc).__name__)

    archive.mark_initial_import_attempted()
    return ImportReport(limit, downloaded, processed, failed, reused, attachments)


def retry_failed_messages(
    gmail: GmailAdapter,
    archive: SQLiteArchive,
    attachment_maximum_bytes: int = 20 * 1024 * 1024,
) -> ImportReport:
    """Retry failed IDs without discovering or redownloading completed messages."""
    failed_ids = archive.failed_message_ids()
    downloaded = 0
    processed = 0
    failed = 0
    attachments = AttachmentReport()

    for message_id in failed_ids:
        summary: dict[str, object] = {"id": message_id}
        try:
            detail = gmail.get_message(message_id)
            if not isinstance(detail, dict):
                raise ValueError(f"Gmail returned an invalid message for {message_id}")
            downloaded += 1
            archive.store_message({**cast(dict[str, object], detail), "id": message_id})
            processed += 1
            attachments = attachments.merge(
                process_message_attachments(
                    gmail,
                    archive,
                    message_id,
                    attachment_maximum_bytes,
                )
            )
        except Exception as exc:
            failed += 1
            with suppress(Exception):
                archive.record_failure(summary, str(exc) or type(exc).__name__)

    return ImportReport(
        len(failed_ids), downloaded, processed, failed, reused=0, attachments=attachments
    )


def _discover_recent_messages(
    gmail: GmailAdapter, limit: int
) -> tuple[list[dict[str, object]], int]:
    messages_by_id: dict[str, dict[str, object]] = {}
    page_token: str | None = None
    seen_tokens: set[str] = set()
    failed = 0

    while len(messages_by_id) < limit:
        try:
            page = gmail.list_messages(
                IMPORT_QUERY,
                max_results=min(100, max(1, limit - len(messages_by_id))),
                page_token=page_token,
            )
            if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                raise ValueError("Gmail returned an invalid message-discovery page")
        except Exception:
            failed = 1
            break
        for item in page["messages"]:
            if not isinstance(item, dict):
                continue
            message_id = item.get("id")
            if isinstance(message_id, str) and message_id:
                messages_by_id.setdefault(message_id, cast(dict[str, object], item))

        next_token = page.get("next_page_token")
        if not isinstance(next_token, str) or not next_token:
            break
        if next_token in seen_tokens:
            failed = 1
            break
        seen_tokens.add(next_token)
        page_token = next_token

    return list(messages_by_id.values())[:limit], failed
