"""Hybrid retrieval over locally archived email evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup
from email_reply_parser import EmailReplyParser  # type: ignore[import-untyped]


@dataclass(frozen=True)
class SearchRequest:
    """Validated user request for direct evidence retrieval."""

    query: str
    limit: int = 8
    sender: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    source_type: str | None = None
    filename: str | None = None
    document_type: str | None = None


@dataclass(frozen=True)
class AttachmentReference:
    """One authoritative archived occurrence of a shared attachment document."""

    occurrence_id: int
    attachment_id: str
    gmail_message_id: str
    thread_id: str
    filename: str
    mime_type: str
    sender: str
    timestamp: str
    location: str
    content_path: str


@dataclass(frozen=True)
class SearchResult:
    """One bounded passage with complete source attribution."""

    text: str
    score: float
    document_id: str
    gmail_message_id: str
    thread_id: str
    source_type: str
    title: str
    timestamp: str
    sender: str
    location: str | None = None
    attachment_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    document_type: str | None = None
    sources: tuple[AttachmentReference, ...] = ()
    start_char: int | None = None
    end_char: int | None = None
    page_number: int | None = None
    chunk_id: str | None = None
    chunk_index: int | None = None


@dataclass(frozen=True)
class DocumentChunk:
    """One local chunk and its source attribution."""

    document_id: str
    chunk_id: str
    chunk_index: int
    text: str
    metadata: dict[str, object]
    sources: tuple[AttachmentReference, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class DocumentContextError:
    code: str
    message: str


@dataclass(frozen=True)
class DocumentContext:
    chunks: tuple[DocumentChunk, ...] = ()
    error: DocumentContextError | None = None


class SearchBackend(Protocol):
    """Public retrieval boundary used by the CLI."""

    def refresh(self) -> int: ...

    def search(self, request: SearchRequest) -> list[SearchResult]: ...


def clean_email_body(body: str, body_format: str) -> str:
    """Return conservative, readable text for indexing without changing the source."""
    if body_format.lower() == "text/html":
        text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    else:
        text = body

    visible_text = EmailReplyParser.parse_reply(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in visible_text.splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    return "\n".join(normalized).strip()
