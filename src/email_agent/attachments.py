"""Attachment archival and extraction using installed LlamaIndex readers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from llama_index.core.schema import Document
from llama_index.readers.file import DocxReader, FlatReader, PDFReader  # type: ignore[import-untyped]

from email_agent.archive import (
    AttachmentSource,
    ExtractedAttachmentDocument,
    SQLiteArchive,
)
from email_agent.gmail import GmailAdapter

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class AttachmentReport:
    """Observable outcomes from attachment processing."""

    downloaded: int = 0
    parsed: int = 0
    indexed: int = 0
    unsupported: int = 0
    oversized: int = 0
    failed: int = 0

    def plus(self, **changes: int) -> AttachmentReport:
        values = {
            "downloaded": self.downloaded,
            "parsed": self.parsed,
            "indexed": self.indexed,
            "unsupported": self.unsupported,
            "oversized": self.oversized,
            "failed": self.failed,
        }
        for name, amount in changes.items():
            values[name] += amount
        return AttachmentReport(**values)

    def merge(self, other: AttachmentReport) -> AttachmentReport:
        return AttachmentReport(
            downloaded=self.downloaded + other.downloaded,
            parsed=self.parsed + other.parsed,
            indexed=self.indexed + other.indexed,
            unsupported=self.unsupported + other.unsupported,
            oversized=self.oversized + other.oversized,
            failed=self.failed + other.failed,
        )


def process_message_attachments(
    gmail: GmailAdapter,
    archive: SQLiteArchive,
    message_id: str,
    maximum_bytes: int,
) -> AttachmentReport:
    """Retain and independently process every attachment occurrence for a message."""
    list_attachments = getattr(gmail, "list_attachments", None)
    download_attachment = getattr(gmail, "download_attachment", None)
    if not callable(list_attachments) or not callable(download_attachment):
        return AttachmentReport()

    try:
        payload = list_attachments(message_id)
        raw_attachments = _attachment_list(payload)
    except Exception:
        return AttachmentReport(failed=1)

    report = AttachmentReport()
    for raw in raw_attachments:
        source = _source(raw)

        existing = archive.attachment_occurrence(message_id, source.part_id)
        if existing is not None and existing.processing_status != "failed":
            continue
        if (
            existing is not None
            and existing.content_sha256 is not None
            and existing.content_path is not None
            and existing.content_path.is_file()
        ):
            occurrence_id = existing.occurrence_id
            digest = existing.content_sha256
            content_path = existing.content_path
            content_size = content_path.stat().st_size
        else:
            try:
                content = download_attachment(message_id, source.part_id)
                if not isinstance(content, bytes):
                    raise TypeError("Gmail returned non-byte attachment content")
            except Exception as exc:
                archive.store_attachment(
                    message_id,
                    source,
                    None,
                    status="failed",
                    problem=_problem("download failed", exc),
                )
                report = report.plus(failed=1)
                continue

            stored_occurrence_id, stored_digest, stored_content_path = archive.store_attachment(
                message_id,
                source,
                content,
                status="downloaded",
            )
            assert stored_digest is not None and stored_content_path is not None
            occurrence_id = stored_occurrence_id
            digest = stored_digest
            content_path = stored_content_path
            content_size = len(content)
            report = report.plus(downloaded=1)

        assert digest is not None and content_path is not None
        if source.inline and source.mime_type.startswith("image/"):
            archive.set_attachment_result(occurrence_id, "skipped", problem="inline asset")
            continue
        if content_size > maximum_bytes:
            archive.set_attachment_result(occurrence_id, "skipped", problem="oversized")
            report = report.plus(oversized=1)
            continue

        reader = _reader(source)
        if reader is None:
            archive.set_attachment_result(
                occurrence_id,
                "unsupported",
                problem="unsupported media type",
            )
            report = report.plus(unsupported=1)
            continue

        if archive.attachment_content_status(digest) == "parsed":
            archive.set_attachment_result(occurrence_id, "parsed")
            report = report.plus(parsed=1)
            continue
        try:
            loaded = reader.load_data(
                content_path,
                extra_info={
                    "source_filename": source.filename,
                    "source_mime_type": source.mime_type,
                    "content_sha256": digest,
                },
            )
            documents = _documents(loaded, digest)
            archive.store_attachment_documents(digest, documents)
        except Exception as exc:
            archive.set_attachment_result(
                occurrence_id,
                "failed",
                problem=_problem(f"{_reader_name(source)} extraction failed", exc),
            )
            report = report.plus(failed=1)
            continue
        report = report.plus(parsed=1)

    archive.mark_attachment_scan_complete(message_id)
    return report


def process_pending_attachments(
    gmail: GmailAdapter,
    archive: SQLiteArchive,
    maximum_bytes: int,
) -> AttachmentReport:
    """Backfill or retry attachment work from already archived messages."""
    report = AttachmentReport()
    for message_id in archive.pending_attachment_message_ids():
        report = report.merge(
            process_message_attachments(gmail, archive, message_id, maximum_bytes)
        )
    return report


def _attachment_list(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        payload = payload.get("attachments")
    if not isinstance(payload, list):
        raise ValueError("Gmail returned invalid attachment metadata")
    return [cast(dict[str, object], item) for item in payload if isinstance(item, dict)]


def _source(raw: dict[str, object]) -> AttachmentSource:
    def text(name: str, default: str = "") -> str:
        value = raw.get(name)
        return value if isinstance(value, str) else default

    filename = text("filename")
    part_id = text("part_id") or text("attachment_id") or filename
    if not part_id:
        encoded = json.dumps(raw, default=str, ensure_ascii=False, sort_keys=True).encode()
        part_id = "unknown-" + hashlib.sha256(encoded).hexdigest()[:16]
    if not filename:
        filename = f"attachment-{part_id}"
    size = raw.get("size", 0)
    if not isinstance(size, int) or size < 0:
        size = 0
    inline = raw.get("inline", False)
    if not isinstance(inline, bool):
        inline = False
    return AttachmentSource(
        part_id=part_id,
        attachment_id=text("attachment_id"),
        filename=filename,
        mime_type=text("mime_type", "application/octet-stream").lower(),
        reported_size=size,
        inline=inline,
        source_metadata=json.dumps(raw, default=str, ensure_ascii=False, sort_keys=True),
    )


def _reader(source: AttachmentSource) -> PDFReader | DocxReader | FlatReader | None:
    suffix = Path(source.filename).suffix.lower()
    if source.mime_type == "application/pdf" or suffix == ".pdf":
        return PDFReader(return_full_document=True)
    if source.mime_type == DOCX_MIME or suffix == ".docx":
        return DocxReader()
    if source.mime_type.startswith("text/") or suffix in {
        ".csv",
        ".json",
        ".log",
        ".md",
        ".text",
        ".txt",
        ".xml",
    }:
        return FlatReader()
    return None


def _reader_name(source: AttachmentSource) -> str:
    reader = _reader(source)
    if isinstance(reader, PDFReader):
        return "PDF"
    if isinstance(reader, DocxReader):
        return "DOCX"
    return "text"


def _documents(
    loaded: list[Document],
    content_sha256: str,
) -> list[ExtractedAttachmentDocument]:
    if not loaded:
        metadata: dict[str, object] = {}
        text = ""
    else:
        metadata = dict(loaded[0].metadata)
        text = "\n".join(document.text for document in loaded)
        if len(loaded) > 1:
            metadata["reader_documents"] = [document.metadata for document in loaded]
    return [
        ExtractedAttachmentDocument(
            document_id=f"attachment:{content_sha256}",
            text=text,
            metadata=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    ]


def _problem(context: str, error: Exception) -> str:
    detail = str(error).strip() or type(error).__name__
    return f"{context}: {detail}"
