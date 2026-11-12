from __future__ import annotations

import json
import sqlite3
import zipfile
from collections.abc import Mapping
from contextlib import closing
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from gmail_agent import mime as gmail_mime  # type: ignore[import-untyped]
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from email_agent.archive import SQLiteArchive
from email_agent.cli import run_application
from email_agent.gmail import GmailStatus
from email_agent.services import ServiceStatus


class AvailableServices:
    def status(self, embedding_model: str) -> ServiceStatus:
        return ServiceStatus(True, True, True)

    def pull_model(self, model: str) -> None:
        raise AssertionError("the model is already available")


class AttachmentGmail:
    def __init__(
        self,
        attachments: list[dict[str, object]],
        contents: Mapping[str, bytes | Exception],
        *,
        message_ids: tuple[str, ...] = ("message-1",),
    ) -> None:
        self._attachments = attachments
        self._contents = contents
        self._message_ids = message_ids
        self.downloaded_parts: list[tuple[str, str]] = []

    def ensure_authenticated(self) -> GmailStatus:
        return GmailStatus(True, "reader@example.com")

    def status(self) -> GmailStatus:
        return GmailStatus(True, "reader@example.com")

    def list_messages(
        self,
        query: str,
        *,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        return {
            "messages": [{"id": message_id} for message_id in self._message_ids],
            "next_page_token": None,
        }

    def get_message(self, message_id: str) -> object:
        return {
            "id": message_id,
            "thread_id": "thread-1",
            "date": "2026-11-12T10:30:00+00:00",
            "from": "alice@example.com",
            "to": "reader@example.com",
            "subject": "Documents",
            "labels": ["INBOX"],
            "body": "Attached documents",
            "body_format": "plain",
        }

    def list_attachments(self, message_id: str) -> object:
        return {"message_id": message_id, "attachments": self._attachments}

    def download_attachment(self, message_id: str, part_id: str) -> bytes:
        self.downloaded_parts.append((message_id, part_id))
        content = self._contents[part_id]
        if isinstance(content, Exception):
            raise content
        return content

    def search_messages(self, query: str) -> object:
        return {"messages": []}

    def get_thread(self, thread_id: str) -> object:
        return {"id": thread_id, "messages": []}


def attachment(
    part_id: str,
    filename: str,
    mime_type: str,
    content: bytes,
    *,
    inline: bool = False,
) -> dict[str, object]:
    return {
        "attachment_id": f"gmail-{part_id}",
        "part_id": part_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": len(content),
        "inline": inline,
    }


def run_import(
    directory: str,
    gmail: AttachmentGmail,
    *,
    maximum_bytes: int = 25 * 1024 * 1024,
    import_limit: int = 1,
) -> str:
    stdout = StringIO()
    exit_code = run_application(
        environ={
            "OPENROUTER_API_KEY": "test-key",
            "OPENROUTER_MODEL": "test/model",
            "EMAIL_AGENT_DATA_DIR": directory,
            "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": str(import_limit),
            "EMAIL_AGENT_ATTACHMENT_MAX_BYTES": str(maximum_bytes),
        },
        stdin=StringIO("/exit\n"),
        stdout=stdout,
        stderr=StringIO(),
        services_factory=lambda settings: AvailableServices(),
        gmail_factory=lambda: gmail,
    )
    assert exit_code == 0
    return stdout.getvalue()


def pdf_with_text(text: str) -> bytes:
    writer = PdfWriter()
    for page_text in (text, "PDF second page"):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_reference = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_reference}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({page_text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def docx_with_text(text: str) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?>'
            '<Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>',
        )
    return output.getvalue()


class AttachmentImportTests(TestCase):
    def test_nested_mime_attachment_discovered_by_installed_gmail_client_is_processed(
        self,
    ) -> None:
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": "bWVzc2FnZQ"},
                        }
                    ],
                },
                {
                    "partId": "2.1",
                    "filename": "nested.txt",
                    "mimeType": "text/plain",
                    "headers": [],
                    "body": {"attachmentId": "gmail-nested", "size": 15},
                },
            ],
        }
        discovered = gmail_mime.attachments(payload)
        gmail = AttachmentGmail(discovered, {"2.1": b"nested evidence"})

        with TemporaryDirectory() as directory:
            run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                stored = database.execute(
                    "SELECT part_id, processing_status FROM message_attachments"
                ).fetchone()

        self.assertEqual(stored, ("2.1", "parsed"))

    def test_supported_documents_are_archived_and_extracted_for_reindexing(self) -> None:
        pdf = pdf_with_text("PDF evidence")
        docx = docx_with_text("DOCX evidence")
        text = b"TXT evidence"
        csv = b"name,value\nalpha,42"
        attachments = [
            attachment("1", "evidence.pdf", "application/pdf", pdf),
            attachment(
                "2",
                "evidence.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                docx,
            ),
            attachment("3", "notes.txt", "text/plain", text),
            attachment("4", "values.csv", "text/csv", csv),
        ]
        gmail = AttachmentGmail(
            attachments,
            {"1": pdf, "2": docx, "3": text, "4": csv},
        )

        with TemporaryDirectory() as directory:
            output = run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                rows = database.execute(
                    "SELECT processing_status, indexing_status FROM message_attachments "
                    "ORDER BY part_id"
                ).fetchall()
                documents = database.execute(
                    "SELECT document_id, text_path, metadata FROM attachment_documents "
                    "ORDER BY document_id"
                ).fetchall()
            extracted = [
                (Path(directory) / text_path).read_text(encoding="utf-8")
                for _, text_path, _ in documents
            ]
            metadata = [json.loads(value) for _, _, value in documents]

        self.assertIn(
            "Attachments: downloaded 4, parsed 4, indexed 0, unsupported 0, oversized 0, failed 0",
            output,
        )
        self.assertEqual(rows, [("parsed", "pending")] * 4)
        self.assertTrue(any("PDF evidence" in value for value in extracted))
        self.assertTrue(any("PDF second page" in value for value in extracted))
        self.assertTrue(any("DOCX evidence" in value for value in extracted))
        self.assertTrue(any("TXT evidence" in value for value in extracted))
        self.assertTrue(any("alpha,42" in value for value in extracted))
        self.assertTrue(all(identifier.startswith("attachment:") for identifier, _, _ in documents))
        self.assertEqual(len(documents), 4)
        self.assertEqual(
            {item["source_filename"] for item in metadata},
            {"evidence.pdf", "evidence.docx", "notes.txt", "values.csv"},
        )

    def test_duplicate_content_is_stored_once_while_occurrences_remain_distinct(self) -> None:
        repeated = b"same bytes"
        different = b"different bytes"
        gmail = AttachmentGmail(
            [
                attachment("1", "report.txt", "text/plain", repeated),
                attachment("2", "report.txt", "text/plain", different),
                attachment("3", "copy.txt", "text/plain", repeated),
            ],
            {"1": repeated, "2": different, "3": repeated},
        )

        with TemporaryDirectory() as directory:
            run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                blob_count = database.execute("SELECT COUNT(*) FROM attachment_blobs").fetchone()[0]
                occurrences = database.execute(
                    "SELECT filename, content_sha256 FROM message_attachments ORDER BY part_id"
                ).fetchall()
                stored_paths = [
                    row[0]
                    for row in database.execute(
                        "SELECT storage_path FROM attachment_blobs ORDER BY content_sha256"
                    ).fetchall()
                ]
            stored_bytes = [(Path(directory) / path).read_bytes() for path in stored_paths]

        self.assertEqual(blob_count, 2)
        self.assertEqual([row[0] for row in occurrences], ["report.txt", "report.txt", "copy.txt"])
        self.assertEqual(occurrences[0][1], occurrences[2][1])
        self.assertNotEqual(occurrences[0][1], occurrences[1][1])
        self.assertCountEqual(stored_bytes, [repeated, different])

    def test_repeated_file_remains_associated_with_every_source_message(self) -> None:
        repeated = b"shared attachment"
        gmail = AttachmentGmail(
            [attachment("1", "shared.txt", "text/plain", repeated)],
            {"1": repeated},
            message_ids=("message-1", "message-2"),
        )

        with TemporaryDirectory() as directory:
            run_import(directory, gmail, import_limit=2)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                blob_count = database.execute("SELECT COUNT(*) FROM attachment_blobs").fetchone()[0]
                document_count = database.execute(
                    "SELECT COUNT(*) FROM attachment_documents"
                ).fetchone()[0]
                source_messages = [
                    row[0]
                    for row in database.execute(
                        "SELECT gmail_message_id FROM message_attachments "
                        "ORDER BY gmail_message_id"
                    ).fetchall()
                ]

        self.assertEqual(blob_count, 1)
        self.assertEqual(document_count, 1)
        self.assertEqual(source_messages, ["message-1", "message-2"])

    def test_existing_message_archive_is_backfilled_without_redownloading_message(self) -> None:
        content = b"backfilled attachment"
        gmail = AttachmentGmail(
            [attachment("1", "backfill.txt", "text/plain", content)],
            {"1": content},
        )

        with TemporaryDirectory() as directory:
            message = gmail.get_message("message-1")
            assert isinstance(message, dict)
            SQLiteArchive(Path(directory)).store_message(message)
            output = run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                status = database.execute(
                    "SELECT processing_status FROM message_attachments"
                ).fetchone()

        self.assertEqual(status, ("parsed",))
        self.assertIn("Local archive: reused 1 completed messages", output)
        self.assertEqual(gmail.downloaded_parts, [("message-1", "1")])

    def test_failed_attachment_is_retried_from_persisted_message_state(self) -> None:
        contents: dict[str, bytes | Exception] = {
            "1": RuntimeError("temporary attachment failure")
        }
        descriptor = attachment("1", "retry.txt", "text/plain", b"retry succeeds")
        gmail = AttachmentGmail([descriptor], contents)

        with TemporaryDirectory() as directory:
            first_output = run_import(directory, gmail)
            contents["1"] = b"retry succeeds"
            second_output = run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                status = database.execute(
                    "SELECT processing_status FROM message_attachments"
                ).fetchone()

        self.assertIn("failed 1", first_output)
        self.assertIn("parsed 1", second_output)
        self.assertEqual(status, ("parsed",))
        self.assertEqual(
            gmail.downloaded_parts,
            [("message-1", "1"), ("message-1", "1")],
        )

    def test_inline_oversized_and_unsupported_files_are_retained_but_not_parsed(self) -> None:
        inline = b"inline-image"
        oversized = b"too-large"
        signature = b"png"
        inline_text = b"text"
        gmail = AttachmentGmail(
            [
                attachment("1", "logo.png", "image/png", inline, inline=True),
                attachment("2", "large.txt", "text/plain", oversized),
                attachment("3", "signature.png", "image/png", signature),
                attachment("4", "inline.txt", "text/plain", inline_text, inline=True),
            ],
            {"1": inline, "2": oversized, "3": signature, "4": inline_text},
        )

        with TemporaryDirectory() as directory:
            output = run_import(directory, gmail, maximum_bytes=5)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                statuses = database.execute(
                    "SELECT filename, processing_status, failure_reason, content_sha256 "
                    "FROM message_attachments ORDER BY part_id"
                ).fetchall()
                documents = database.execute(
                    "SELECT COUNT(*) FROM attachment_documents"
                ).fetchone()[0]
                blob_paths = [
                    row[0]
                    for row in database.execute("SELECT storage_path FROM attachment_blobs")
                ]

        self.assertIn(
            "Attachments: downloaded 4, parsed 1, indexed 0, unsupported 1, oversized 1, failed 0",
            output,
        )
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in statuses],
            [
                ("logo.png", "skipped", "inline asset"),
                ("large.txt", "skipped", "oversized"),
                ("signature.png", "unsupported", "unsupported media type"),
                ("inline.txt", "parsed", None),
            ],
        )
        self.assertTrue(all(row[3] for row in statuses))
        self.assertEqual(documents, 1)
        self.assertEqual(len(blob_paths), 4)

    def test_malformed_nonessential_metadata_does_not_drop_accessible_attachment(self) -> None:
        content = b"metadata fallback"
        gmail = AttachmentGmail(
            [
                {
                    "part_id": "1",
                    "filename": "fallback.txt",
                    "size": "unknown",
                    "inline": "unknown",
                }
            ],
            {"1": content},
        )

        with TemporaryDirectory() as directory:
            output = run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                stored = database.execute(
                    "SELECT processing_status, source_metadata FROM message_attachments"
                ).fetchone()

        self.assertIn("parsed 1", output)
        self.assertEqual(stored[0], "parsed")
        self.assertEqual(json.loads(stored[1])["size"], "unknown")

    def test_corrupt_and_unreadable_attachments_fail_independently(self) -> None:
        valid = b"still searchable"
        corrupt_pdf = b"not a pdf"
        gmail = AttachmentGmail(
            [
                attachment("1", "broken.pdf", "application/pdf", corrupt_pdf),
                attachment("2", "good.txt", "text/plain", valid),
                attachment("3", "unreadable.txt", "text/plain", b"missing"),
            ],
            {
                "1": corrupt_pdf,
                "2": valid,
                "3": RuntimeError("Gmail attachment download failed"),
            },
        )

        with TemporaryDirectory() as directory:
            output = run_import(directory, gmail)
            retry_output = run_import(directory, gmail)
            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                statuses = database.execute(
                    "SELECT filename, processing_status, failure_reason, content_sha256 "
                    "FROM message_attachments ORDER BY part_id"
                ).fetchall()
                texts = [
                    (Path(directory) / row[0]).read_text(encoding="utf-8")
                    for row in database.execute(
                        "SELECT text_path FROM attachment_documents"
                    ).fetchall()
                ]

        self.assertIn(
            "Attachments: downloaded 2, parsed 1, indexed 0, unsupported 0, oversized 0, failed 2",
            output,
        )
        self.assertIn(
            "Attachments: downloaded 0, parsed 0, indexed 0, unsupported 0, oversized 0, failed 2",
            retry_output,
        )
        self.assertEqual(statuses[0][1], "failed")
        self.assertIn("PDF", statuses[0][2])
        self.assertIsNotNone(statuses[0][3])
        self.assertEqual(statuses[1][1], "parsed")
        self.assertEqual(statuses[2][1], "failed")
        self.assertIn("download failed", statuses[2][2])
        self.assertIsNone(statuses[2][3])
        self.assertEqual(texts, ["still searchable"])
        self.assertEqual(gmail.downloaded_parts.count(("message-1", "1")), 1)
        self.assertEqual(gmail.downloaded_parts.count(("message-1", "2")), 1)
        self.assertEqual(gmail.downloaded_parts.count(("message-1", "3")), 2)
