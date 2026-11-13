from __future__ import annotations

from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from qdrant_client import QdrantClient
from test_attachments import AttachmentGmail, attachment
from test_ingestion import PagedGmail, PartiallyFailingGmail, SyntheticGmail, message
from test_search import AvailableServices, ControlledEmbedding, UnavailableAttachmentEmbedding

from email_agent.archive import SQLiteArchive
from email_agent.cli import run_application
from email_agent.config import Settings
from email_agent.ingestion import synchronize_messages
from email_agent.retrieval import HybridSearchIndex
from email_agent.search import SearchRequest


class TemporalGmail(SyntheticGmail):
    def list_messages(self, query: str, max_results: int, page_token: str | None = None) -> object:
        self.discovery_calls.append((query, max_results, page_token))
        selected = self.messages
        for token in query.split():
            if token.startswith(("after:", "before:")):
                operator, value = token.split(":")
                selected = [
                    m
                    for m in selected
                    if (
                        datetime.fromisoformat(str(m["date"])).timestamp() > int(value)
                        if operator == "after"
                        else datetime.fromisoformat(str(m["date"])).timestamp() < int(value)
                    )
                ]
        start = int(page_token or "0")
        end = start + min(max_results, 2)
        return {
            "messages": selected[start:end],
            "next_page_token": str(end) if end < len(selected) else None,
        }


class SynchronizationTests(TestCase):
    def test_cli_rejects_invalid_limits_and_removed_sync_command(self) -> None:
        gmail = TemporalGmail([])
        with TemporaryDirectory() as directory:
            SQLiteArchive(Path(directory)).mark_initial_import_attempted()
            output = StringIO()
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO(
                    "/sync_old\n/sync_old 0\n/sync_old -1\n/sync_old two\n"
                    "/sync_old 2 extra\n/sync_new extra\n/sync\n/exit\n"
                ),
                stdout=output,
                stderr=StringIO(),
                gmail_factory=lambda: gmail,
                services_factory=lambda settings: AvailableServices(),
            )
            self.assertEqual(output.getvalue().count("Usage: /sync_old N"), 5)
            self.assertIn("Usage: /sync_new", output.getvalue())
            self.assertIn("Unknown command: /sync.", output.getvalue())
            self.assertEqual(gmail.discovery_calls, [])

    def test_new_and_old_extend_opposite_archive_boundaries(self) -> None:
        messages = [
            message(str(i), body=f"Mail {i}", date=f"2020-01-0{i}T00:00:00+00:00")
            for i in range(6, 0, -1)
        ]
        gmail = TemporalGmail(messages)
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(messages[2])  # January 4
            archive.store_message(messages[3])  # January 3
            newer = synchronize_messages(gmail, archive)
            self.assertEqual(newer.downloaded, 2)
            self.assertEqual(gmail.downloaded_ids, ["6", "5"])
            older = synchronize_messages(gmail, archive, old_limit=1)
            self.assertEqual(older.downloaded, 1)
            self.assertEqual(gmail.downloaded_ids, ["6", "5", "2"])
            synchronize_messages(gmail, archive, old_limit=1)
            self.assertEqual(gmail.downloaded_ids, ["6", "5", "2", "1"])
            self.assertEqual(synchronize_messages(gmail, archive).downloaded, 0)
            self.assertEqual(synchronize_messages(gmail, archive, old_limit=2).downloaded, 0)

    def test_old_empty_archive_is_capped_and_equal_timestamps_are_not_lost(self) -> None:
        gmail = TemporalGmail([message(str(i), body="same timestamp") for i in range(5)])
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            self.assertEqual(synchronize_messages(gmail, archive, old_limit=2).downloaded, 2)
            self.assertEqual(synchronize_messages(gmail, archive, old_limit=2).downloaded, 2)
            self.assertEqual(synchronize_messages(gmail, archive, old_limit=2).downloaded, 1)
            with self.assertRaises(ValueError):
                synchronize_messages(gmail, archive, old_limit=0)

    def test_repeated_sync_retries_failed_attachments_without_repeating_completed_work(
        self,
    ) -> None:
        contents: dict[str, bytes | Exception] = {
            "good": b"Invoice complete",
            "retry": RuntimeError("temporary failure"),
        }
        gmail = AttachmentGmail(
            [
                attachment("good", "good.txt", "text/plain", b"Invoice complete"),
                attachment("retry", "retry.txt", "text/plain", b"Invoice recovered"),
            ],
            contents,
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            first = synchronize_messages(gmail, archive)
            self.assertEqual((first.attachments.parsed, first.attachments.failed), (1, 1))
            contents["retry"] = b"Invoice recovered"
            second = synchronize_messages(gmail, archive)
            self.assertEqual((second.downloaded, second.skipped), (0, 1))
            self.assertEqual(
                (
                    second.attachments.downloaded,
                    second.attachments.parsed,
                    second.attachments.failed,
                ),
                (1, 1, 0),
            )
            self.assertEqual(
                gmail.downloaded_parts,
                [("message-1", "good"), ("message-1", "retry"), ("message-1", "retry")],
            )

    def test_discovery_failure_keeps_partial_results_and_stops_repeated_page_tokens(self) -> None:
        gmail = PagedGmail(
            {
                None: ([message("first", body="Archived")], "loop"),
                "loop": ([message("second", body="Also archived")], "loop"),
            }
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            report = synchronize_messages(gmail, archive)
            self.assertEqual((report.discovered, report.downloaded, report.failed), (2, 2, 1))
            self.assertEqual(archive.completed_message_ids(), {"first", "second"})

    def test_failed_download_is_reported_and_retried_without_redownloading_completed_mail(
        self,
    ) -> None:
        gmail = PartiallyFailingGmail(
            [message("good", body="Usable"), message("retry", body="Recovered")], "retry"
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            first = synchronize_messages(gmail, archive)
            self.assertEqual((first.downloaded, first.processed, first.failed), (1, 1, 1))
            gmail.failed_id = None
            second = synchronize_messages(gmail, archive)
            self.assertEqual((second.downloaded, second.skipped, second.failed), (1, 1, 0))
            self.assertEqual(archive.failed_message_ids(), [])
            self.assertEqual(gmail.downloaded_ids, ["good", "retry"])

    def test_paginated_inbox_and_sent_sync_skips_overlap_and_preserves_local_mail(self) -> None:
        received = [message(f"received-{i}", body=f"Invoice {i}") for i in range(101)]
        sent = message("sent", body="Sent invoice confirmation")
        sent["labels"] = ["SENT"]
        known = message("known", body="Original archived content")
        gmail = PagedGmail(
            {None: ([known, *received[:99]], "next"), "next": ([known, *received[99:], sent], None)}
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(known)
            archive.store_message(message("removed", body="Preserved after Gmail deletion"))
            report = synchronize_messages(gmail, archive)
            self.assertEqual(
                (
                    report.discovered,
                    report.downloaded,
                    report.processed,
                    report.skipped,
                    report.failed,
                ),
                (103, 102, 102, 1, 0),
            )
            self.assertEqual(len(archive.completed_message_ids()), 104)
            self.assertEqual(
                archive.message_body_path("removed").read_text(encoding="utf-8"),
                "Preserved after Gmail deletion",
            )
            self.assertEqual(
                gmail.discovery_calls,
                [
                    ("(in:inbox OR in:sent) after:1794479399", 100, None),
                    ("(in:inbox OR in:sent) after:1794479399", 100, "next"),
                ],
            )
            repeated = synchronize_messages(gmail, archive)
            self.assertEqual(
                (repeated.downloaded, repeated.processed, repeated.skipped, repeated.failed),
                (0, 0, 103, 0),
            )
            self.assertEqual(len(gmail.downloaded_ids), 102)

    def test_cli_sync_indexes_new_attachments_and_repeat_does_zero_new_work(self) -> None:
        content = b"Invoice SYNC-420 total 42 EUR"
        gmail = AttachmentGmail(
            [
                attachment("text", "invoice.txt", "text/plain", content),
                attachment("binary", "opaque.bin", "application/octet-stream", b"opaque"),
            ],
            {"text": content, "binary": b"opaque"},
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.mark_initial_import_attempted()
            client = QdrantClient(":memory:")
            backend = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            output = StringIO()
            result = run_application(
                environ={
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO(
                    "/sync_new\n/sync_new\n/search SYNC-420 --source attachment\n/exit\n"
                ),
                stdout=output,
                stderr=StringIO(),
                gmail_factory=lambda: gmail,
                services_factory=lambda settings: AvailableServices(),
                search_factory=lambda settings, archive: backend,
            )
            self.assertEqual(result, 0)
            self.assertIn(
                "Sync: discovered 1, downloaded 1, processed 1, skipped 0, failed 0",
                output.getvalue(),
            )
            self.assertIn("downloaded 2, parsed 1, unsupported 1", output.getvalue())
            self.assertIn("Sync index: indexed 2 documents", output.getvalue())
            self.assertIn("indexed attachments 1", output.getvalue())
            self.assertIn("indexed attachments 0", output.getvalue())
            self.assertIn("Sync index: indexed 0 documents", output.getvalue())
            self.assertIn(
                "Sync: discovered 1, downloaded 0, processed 0, skipped 1, failed 0",
                output.getvalue(),
            )
            self.assertIn("Invoice SYNC-420 total 42 EUR", output.getvalue())
            self.assertIn("filename: invoice.txt", output.getvalue())
            self.assertEqual(len(gmail.downloaded_parts), 2)
            self.assertEqual(client.count(backend.collection, exact=True).count, 2)
            self.assertEqual(backend.refresh(), 0)
            client.close()

    def test_sync_rebuilds_known_email_and_attachment_under_active_embedding_contract(self) -> None:
        content = b"Invoice migration attachment"
        gmail = AttachmentGmail(
            [attachment("text", "invoice.txt", "text/plain", content)], {"text": content}
        )
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            synchronize_messages(gmail, archive)
            original = archive.message_body_path("message-1").read_bytes()
            client = QdrantClient(":memory:")
            legacy = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory), embedding_model="legacy"),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            self.assertEqual(legacy.refresh(), 2)
            active = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            output = StringIO()
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO("/sync_new\n/exit\n"),
                stdout=output,
                stderr=StringIO(),
                gmail_factory=lambda: gmail,
                services_factory=lambda settings: AvailableServices(),
                search_factory=lambda settings, archive: active,
            )
            self.assertIn("downloaded 0, processed 0, skipped 1, failed 0", output.getvalue())
            self.assertIn("Sync index: indexed 2 documents", output.getvalue())
            self.assertIn("indexed attachments 1", output.getvalue())
            self.assertNotEqual(legacy.collection, active.collection)
            self.assertEqual(client.count(legacy.collection, exact=True).count, 2)
            self.assertEqual(client.count(active.collection, exact=True).count, 2)
            self.assertEqual(
                active.search(SearchRequest("invoice", source_type="attachment"))[0]
                .sources[0]
                .gmail_message_id,
                "message-1",
            )
            self.assertEqual(archive.message_body_path("message-1").read_bytes(), original)
            self.assertEqual(len(gmail.downloaded_parts), 1)
            client.close()

    def test_index_failure_retains_sources_and_keeps_cli_available(self) -> None:
        gmail = PagedGmail({None: ([message("new", body="ATTACHFAIL invoice")], None)})
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.mark_initial_import_attempted()
            client = QdrantClient(":memory:")
            backend = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=client,
                embed_model=UnavailableAttachmentEmbedding(),
            )
            output = StringIO()
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO("/sync_new\n/help\n/exit\n"),
                stdout=output,
                stderr=StringIO(),
                gmail_factory=lambda: gmail,
                services_factory=lambda settings: AvailableServices(),
                search_factory=lambda settings, archive: backend,
            )
            self.assertIn("Sync index failed: indexing incomplete", output.getvalue())
            self.assertNotIn("Sync index: indexed", output.getvalue())
            self.assertIn("Supported commands:", output.getvalue())
            self.assertEqual(archive.completed_message_ids(), {"new"})
            self.assertEqual(
                archive.message_body_path("new").read_text(encoding="utf-8"), "ATTACHFAIL invoice"
            )
            client.close()
