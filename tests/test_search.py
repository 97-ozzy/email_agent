from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from llama_index.core.base.embeddings.base import BaseEmbedding
from qdrant_client import QdrantClient
from test_attachments import (
    AttachmentGmail,
    attachment,
    docx_with_text,
    pdf_with_text,
    run_import,
)

from email_agent.archive import SQLiteArchive
from email_agent.cli import run_application
from email_agent.config import Settings
from email_agent.gmail import GmailStatus
from email_agent.retrieval import HybridSearchIndex
from email_agent.search import (
    SearchRequest,
    SearchResult,
    clean_email_body,
)
from email_agent.services import ServiceStatus


def message(body: str, body_format: str) -> dict[str, object]:
    return {
        "id": "message-1",
        "thread_id": "thread-1",
        "date": "2026-11-12T10:30:00+00:00",
        "from": "Alice <alice@example.com>",
        "to": "reader@example.com",
        "subject": "Quarterly plan",
        "labels": ["INBOX"],
        "body": body,
        "body_format": body_format,
    }


def archived_message(
    message_id: str,
    body: str,
    *,
    sender: str = "Alice <alice@example.com>",
    timestamp: str = "2026-11-12T10:30:00+00:00",
) -> dict[str, object]:
    value = message(body, "text/plain")
    value.update(
        {
            "id": message_id,
            "thread_id": f"thread-{message_id}",
            "from": sender,
            "date": timestamp,
            "subject": f"Subject {message_id}",
        }
    )
    return value


class ControlledEmbedding(BaseEmbedding):
    """Semantic examples without loading real model weights."""

    @staticmethod
    def vector(text: str) -> list[float]:
        lowered = text.lower()
        if any(term in lowered for term in ("annual leave", "holiday", "vacation")):
            return [1.0, 0.0, 0.0]
        if "invoice" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self.vector(query)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self.vector(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self.vector(text)


class UnavailableAttachmentEmbedding(ControlledEmbedding):
    def _get_text_embedding(self, text: str) -> list[float]:
        if "ATTACHFAIL" in text:
            raise RuntimeError("embedding unavailable")
        return super()._get_text_embedding(text)


class EmailBodyCleaningTests(TestCase):
    def test_html_is_converted_to_readable_text(self) -> None:
        cleaned = clean_email_body(
            "<html><body><h1>Invoice</h1><p>Total: <b>42 EUR</b></p></body></html>",
            "text/html",
        )

        self.assertEqual(cleaned, "Invoice Total: 42 EUR")

    def test_plain_text_and_cyrillic_are_preserved_while_whitespace_is_normalized(self) -> None:
        cleaned = clean_email_body(
            "  Здравствуйте, Анна!  \n\n\n  Счёт АБВ-123 оплачен.  ",
            "text/plain",
        )

        self.assertEqual(cleaned, "Здравствуйте, Анна!\n\nСчёт АБВ-123 оплачен.")

    def test_reliably_identified_reply_and_signature_are_removed(self) -> None:
        cleaned = clean_email_body(
            "Current answer.\n\n-- \nAlice\n\nOn Tue, Bob wrote:\n> Historical question",
            "text/plain",
        )

        self.assertEqual(cleaned, "Current answer.")

    def test_cleaning_does_not_modify_the_archived_source(self) -> None:
        original = "<p>Current answer.</p><blockquote>Historical question</blockquote>"
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(message(original, "text/html"))

            stored_path = archive.message_body_path("message-1")
            clean_email_body(stored_path.read_text(encoding="utf-8"), "text/html")

            self.assertEqual(stored_path.read_text(encoding="utf-8"), original)


class AvailableServices:
    def status(self, embedding_model: str) -> ServiceStatus:
        return ServiceStatus(True, True, True)

    def pull_model(self, model: str) -> None:
        raise AssertionError("model is already installed")


class EmptyGmail:
    def ensure_authenticated(self) -> GmailStatus:
        return GmailStatus(True, account="reader@example.com")

    def status(self) -> GmailStatus:
        return self.ensure_authenticated()

    def list_messages(
        self, query: str, *, max_results: int, page_token: str | None = None
    ) -> object:
        return {"messages": [], "next_page_token": None}

    def search_messages(self, query: str) -> object:
        raise AssertionError("live Gmail search was not requested")

    def get_message(self, message_id: str) -> object:
        raise AssertionError("no messages exist")

    def get_thread(self, thread_id: str) -> object:
        raise AssertionError("no threads exist")


class ControlledSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.requests: list[SearchRequest] = []
        self.refreshes = 0

    def refresh(self) -> int:
        self.refreshes += 1
        return 2

    def search(self, request: SearchRequest) -> list[SearchResult]:
        self.requests.append(request)
        return self.results[: request.limit]


class SearchCliTests(TestCase):
    def run_search(self, command: str, search: ControlledSearch) -> str:
        with TemporaryDirectory() as directory:
            stdout = StringIO()
            exit_code = run_application(
                environ={
                    "OPENROUTER_API_KEY": "unused-test-key",
                    "OPENROUTER_MODEL": "unused/test-model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO(f"{command}\n/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=EmptyGmail,
                search_factory=lambda settings, archive: search,
            )
            self.assertEqual(exit_code, 0)
            return stdout.getvalue()

    def test_search_returns_bounded_attributable_evidence(self) -> None:
        search = ControlledSearch(
            [
                SearchResult(
                    text="The invoice total is 42 EUR.",
                    score=0.875,
                    document_id="email:message-1",
                    gmail_message_id="message-1",
                    thread_id="thread-1",
                    source_type="email",
                    title="November invoice",
                    timestamp="2026-11-12T10:30:00+00:00",
                    sender="Alice <alice@example.com>",
                    location="https://mail.google.com/mail/u/0/#all/message-1",
                )
            ]
        )

        output = self.run_search(
            "/search invoice --limit 3 --sender alice@example.com "
            "--after 2026-11-01 --before 2026-11-30 --source email",
            search,
        )

        self.assertEqual(search.refreshes, 1)
        self.assertEqual(
            search.requests,
            [
                SearchRequest(
                    query="invoice",
                    limit=3,
                    sender="alice@example.com",
                    date_from="2026-11-01",
                    date_to="2026-11-30",
                    source_type="email",
                )
            ],
        )
        for value in (
            "The invoice total is 42 EUR.",
            "0.875",
            "email:message-1",
            "message-1",
            "thread-1",
            "email",
            "November invoice",
            "2026-11-12T10:30:00+00:00",
            "Alice <alice@example.com>",
            "https://mail.google.com/mail/u/0/#all/message-1",
        ):
            self.assertIn(value, output)

    def test_search_defaults_to_eight_results_and_reports_no_matches(self) -> None:
        search = ControlledSearch([])

        output = self.run_search("/search missing terminology", search)

        self.assertEqual(
            search.requests,
            [SearchRequest(query="missing terminology", limit=8)],
        )
        self.assertIn("No archived email passages matched", output)

    def test_search_rejects_result_limits_outside_one_to_twenty(self) -> None:
        search = ControlledSearch([])

        output = self.run_search("/search invoice --limit 21", search)

        self.assertEqual(search.requests, [])
        self.assertIn("--limit must be between 1 and 20", output)

    def test_attachment_filters_and_all_source_citations_are_exposed(self) -> None:
        from email_agent.search import AttachmentReference

        source = AttachmentReference(
            1,
            "gmail-attachment",
            "message",
            "thread",
            "report.pdf",
            "application/pdf",
            "Alice",
            "2026-11-12",
            "gmail-link",
            "local.pdf",
        )
        search = ControlledSearch(
            [
                SearchResult(
                    "Invoice PDF-42",
                    0.75,
                    "attachment:stable",
                    "message",
                    "thread",
                    "attachment",
                    "report.pdf",
                    "2026-11-12",
                    "Alice",
                    attachment_id="gmail-attachment",
                    filename="report.pdf",
                    mime_type="application/pdf",
                    document_type="pdf",
                    sources=(source,),
                    start_char=0,
                    end_char=14,
                )
            ]
        )
        output = self.run_search(
            '/search PDF-42 --filename "report.pdf" --type pdf --source attachment', search
        )
        self.assertEqual(
            search.requests[0],
            SearchRequest(
                "PDF-42", filename="report.pdf", document_type="pdf", source_type="attachment"
            ),
        )
        for value in (
            "attachment:stable",
            "gmail-attachment",
            "report.pdf",
            "application/pdf",
            "characters: 0:14",
            '"occurrence_id": 1',
            '"content_path": "local.pdf"',
        ):
            self.assertIn(value, output)
        self.assertNotIn("page_number:", output)


class HybridRetrievalTests(TestCase):
    def make_index(
        self,
        directory: str,
        archive: SQLiteArchive,
        *,
        context_budget: int = 12_000,
        client: QdrantClient | None = None,
    ) -> HybridSearchIndex:
        settings = Settings(
            "unused-key",
            "unused/model",
            data_dir=Path(directory),
            search_context_chars=context_budget,
        )
        return HybridSearchIndex(
            settings,
            archive,
            client=client or QdrantClient(":memory:"),
            embed_model=ControlledEmbedding(),
        )

    def test_exact_and_semantic_matches_are_attributable_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(
                archived_message("leave", "Your annual leave request was approved.")
            )
            archive.store_message(
                archived_message("invoice", "Invoice АБВ-123 is payable this month.")
            )
            client = QdrantClient(":memory:")
            index = self.make_index(directory, archive, client=client)

            self.assertEqual(index.refresh(), 2)
            self.assertEqual(index.refresh(), 0)
            self.assertEqual(client.count(index.collection, exact=True).count, 2)

            semantic = index.search(SearchRequest("holiday plans", limit=1))
            exact = index.search(SearchRequest("АБВ-123", limit=1))

            self.assertEqual(semantic[0].gmail_message_id, "leave")
            self.assertEqual(exact[0].gmail_message_id, "invoice")
            self.assertEqual(exact[0].document_id, "email:invoice")
            self.assertEqual(exact[0].thread_id, "thread-invoice")
            self.assertEqual(exact[0].source_type, "email")
            self.assertEqual(exact[0].title, "Subject invoice")
            self.assertEqual(exact[0].timestamp, "2026-11-12T10:30:00+00:00")
            self.assertEqual(exact[0].sender, "Alice <alice@example.com>")
            self.assertTrue(exact[0].location and exact[0].location.endswith("/invoice"))

    def test_filters_limits_and_context_budget_are_enforced(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(
                archived_message(
                    "alice",
                    "Invoice ALICE-1 " + "A" * 80,
                    timestamp="2026-11-10T10:30:00+00:00",
                )
            )
            archive.store_message(
                archived_message(
                    "bob",
                    "Invoice BOB-2 " + "B" * 80,
                    sender="Bob <bob@example.com>",
                    timestamp="2026-10-10T10:30:00+00:00",
                )
            )
            index = self.make_index(directory, archive, context_budget=40)
            index.refresh()

            results = index.search(
                SearchRequest(
                    "invoice",
                    limit=1,
                    sender="alice@example.com",
                    date_from="2026-11-01",
                    date_to="2026-11-30",
                    source_type="email",
                )
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].gmail_message_id, "alice")
            self.assertLessEqual(sum(len(result.text) for result in results), 40)
            self.assertEqual(results[0].document_id, "email:alice")
            self.assertEqual(index.search(SearchRequest("invoice", source_type="attachment")), [])
            self.assertEqual(index.search(SearchRequest("invoice", date_from="2027-01-01")), [])
            self.assertEqual(
                index.search(SearchRequest("invoice", sender="missing@example.com")), []
            )

    def test_processing_state_and_vectors_survive_restart(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("persisted", "Invoice PERSIST-7"))
            qdrant_path = str(Path(directory) / "qdrant-test")
            first_client = QdrantClient(path=qdrant_path)
            first_index = self.make_index(directory, archive, client=first_client)
            self.assertEqual(first_index.refresh(), 1)
            first_client.close()

            second_client = QdrantClient(path=qdrant_path)
            second_index = self.make_index(directory, archive, client=second_client)
            self.assertEqual(second_index.refresh(), 0)
            results = second_index.search(SearchRequest("PERSIST-7", limit=1))
            second_client.close()

            self.assertEqual(results[0].gmail_message_id, "persisted")
            self.assertTrue(second_index.docstore_path.exists())

    def test_empty_archive_returns_no_results(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            index = self.make_index(directory, archive)

            self.assertEqual(index.refresh(), 0)
            self.assertEqual(index.search(SearchRequest("anything")), [])

    def test_changed_source_replaces_stale_chunks(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("changed", "Old sentence. " * 700))
            client = QdrantClient(":memory:")
            index = self.make_index(directory, archive, client=client)
            index.refresh()
            self.assertGreater(client.count(index.collection, exact=True).count, 1)
            archive.store_message(archived_message("changed", "Invoice REPLACED-9"))

            self.assertEqual(index.refresh(), 1)
            self.assertEqual(client.count(index.collection, exact=True).count, 1)
            results = index.search(SearchRequest("REPLACED-9"))
            self.assertEqual(results[0].text, "Invoice REPLACED-9")

    def test_token_budget_retains_identifiers(self) -> None:
        import tiktoken

        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("budget", "Invoice details. " * 100))
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory), search_context_tokens=5),
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            results = index.search(SearchRequest("invoice"))
            tokenizer = tiktoken.get_encoding("cl100k_base")
            self.assertLessEqual(sum(len(tokenizer.encode(r.text)) for r in results), 5)
            self.assertEqual(results[0].document_id, "email:budget")

    def test_supported_attachments_share_hybrid_search_without_mixing_documents(self) -> None:
        with TemporaryDirectory() as directory:
            contents = {
                "pdf": pdf_with_text("Invoice PDF-429 total 429 EUR"),
                "docx": docx_with_text("Invoice DOCX-731 total 731 EUR"),
                "text": b"Invoice TEXT-915 total 915 EUR",
            }
            types = {
                "pdf": "application/pdf",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text": "text/plain",
            }
            gmail = AttachmentGmail(
                [
                    attachment(kind, f"invoice.{kind}", types[kind], data)
                    for kind, data in contents.items()
                ],
                contents,
            )
            run_import(directory, gmail)
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("body", "Invoice BODY-212 total 212 EUR"))
            index = self.make_index(directory, archive)
            self.assertEqual(index.refresh(), 5)
            results = index.search(SearchRequest("Invoice", limit=10))
            self.assertEqual({r.source_type for r in results}, {"email", "attachment"})
            for kind, fact in (("pdf", "PDF-429"), ("docx", "DOCX-731"), ("text", "TEXT-915")):
                with self.subTest(kind=kind):
                    hits = index.search(SearchRequest(fact, filename=f"invoice.{kind}"))
                    self.assertEqual(len(hits), 1)
                    hit = hits[0]
                    self.assertIn(fact, hit.text)
                    self.assertEqual(hit.document_type, kind)
                    self.assertEqual(hit.attachment_id, f"gmail-{kind}")
                    self.assertEqual(hit.mime_type, types[kind])
                    self.assertEqual(hit.gmail_message_id, "message-1")
                    self.assertEqual(hit.thread_id, "thread-1")
                    self.assertEqual(
                        hit.document_id,
                        index.search(SearchRequest(fact, document_type=kind))[0].document_id,
                    )
                    self.assertEqual(len(hit.sources), 1)
                    self.assertTrue(Path(hit.sources[0].content_path).is_file())
                    self.assertIsNotNone(hit.start_char)
                    self.assertIsNotNone(hit.end_char)
                    self.assertIsNone(hit.page_number)
                    for other in ("PDF-429", "DOCX-731", "TEXT-915", "BODY-212"):
                        if other != fact:
                            self.assertNotIn(other, hit.text)
            self.assertEqual(index.refresh(), 0)
            self.assertEqual(index.client.count(index.collection, exact=True).count, 5)

    def test_shared_attachment_sources_filter_together_and_survive_restart(self) -> None:
        with TemporaryDirectory() as directory:
            data = b"Invoice SHARED-42 payable on Friday"
            gmail = AttachmentGmail(
                [attachment("a", "alice.txt", "text/plain", data)],
                {"a": data},
                message_ids=("alice",),
            )
            run_import(directory, gmail)
            archive = SQLiteArchive(Path(directory))
            archive.store_message(
                archived_message(
                    "bob",
                    "Another copy",
                    sender="Bob <bob@example.com>",
                    timestamp="2026-10-10T10:30:00+00:00",
                )
            )
            from email_agent.attachments import process_message_attachments

            second = AttachmentGmail([attachment("b", "bob.txt", "text/plain", data)], {"b": data})
            process_message_attachments(second, archive, "bob", 1000)
            path = str(Path(directory) / "qdrant")
            client = QdrantClient(path=path)
            index = self.make_index(directory, archive, client=client)
            self.assertEqual(index.refresh(), 3)
            hit = index.search(
                SearchRequest(
                    "SHARED-42", filename="bob.txt", sender="bob@example.com", date_to="2026-10-31"
                )
            )[0]
            self.assertEqual(hit.gmail_message_id, "bob")
            self.assertEqual(hit.filename, "bob.txt")
            self.assertEqual({s.gmail_message_id for s in hit.sources}, {"alice", "bob"})
            self.assertEqual({s.attachment_id for s in hit.sources}, {"gmail-a", "gmail-b"})
            self.assertEqual(
                index.search(
                    SearchRequest("SHARED-42", filename="bob.txt", sender="alice@example.com")
                ),
                [],
            )
            self.assertEqual(
                index.search(
                    SearchRequest("SHARED-42", filename="alice.txt", date_to="2026-10-31")
                ),
                [],
            )
            stable_id = hit.document_id
            stable_sources = hit.sources
            client.close()
            occurrence = archive.attachment_occurrence("bob", "b")
            assert occurrence is not None
            self.assertEqual(occurrence.indexing_status, "indexed")
            client = QdrantClient(path=path)
            try:
                index = self.make_index(directory, archive, client=client)
                self.assertEqual(index.refresh(), 0)
                hit = index.search(SearchRequest("SHARED-42", source_type="attachment"))[0]
                self.assertEqual(hit.document_id, stable_id)
                self.assertEqual(hit.sources, stable_sources)
                self.assertEqual(index.refresh(force=True), 3)
                self.assertEqual(client.count(index.collection, exact=True).count, 3)
            finally:
                client.close()

    def test_distinct_content_with_same_filename_has_separate_documents_and_chunks(self) -> None:
        with TemporaryDirectory() as directory:
            data = {"a": b"Invoice ORIGINAL-71. " * 700, "b": b"Invoice REVISED-82. " * 700}
            gmail = AttachmentGmail(
                [attachment(k, "contract.txt", "text/plain", v) for k, v in data.items()], data
            )
            run_import(directory, gmail)
            archive = SQLiteArchive(Path(directory))
            index = self.make_index(directory, archive, context_budget=50_000)
            index.refresh()
            results = index.search(SearchRequest("Invoice", filename="contract.txt", limit=20))
            self.assertEqual(len({r.document_id for r in results}), 2)
            self.assertGreater(len(results), 2)
            for result in results:
                fact = "ORIGINAL-71" if result.attachment_id == "gmail-a" else "REVISED-82"
                other = "REVISED-82" if fact == "ORIGINAL-71" else "ORIGINAL-71"
                self.assertIn(fact, result.text)
                self.assertNotIn(other, result.text)
                original = data["a" if fact == "ORIGINAL-71" else "b"].decode()
                self.assertEqual(result.text, original[result.start_char : result.end_char])

    def test_truncated_unicode_passage_still_matches_its_character_location(self) -> None:
        with TemporaryDirectory() as directory:
            text = "A🚀 invoice total 42 EUR"
            data = text.encode()
            run_import(
                directory,
                AttachmentGmail([attachment("a", "unicode.txt", "text/plain", data)], {"a": data}),
            )
            archive = SQLiteArchive(Path(directory))
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory), search_context_tokens=2),
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            result = index.search(SearchRequest("invoice", source_type="attachment"))[0]
            self.assertEqual(result.text, text[result.start_char : result.end_char])

    def test_new_embedding_contract_rebuilds_attachments_from_persisted_text(self) -> None:
        from email_agent.attachments import process_message_attachments

        with TemporaryDirectory() as directory:
            data = b"Invoice MIGRATE-317 is payable in May"
            gmail = AttachmentGmail(
                [attachment("a", "invoice.txt", "text/plain", data)], {"a": data}
            )
            run_import(directory, gmail)
            archive = SQLiteArchive(Path(directory))
            first = self.make_index(directory, archive)
            first.refresh()
            original = first.search(SearchRequest("MIGRATE-317", source_type="attachment"))[0]
            archive.store_message(archived_message("forward", "Forwarded invoice"))
            process_message_attachments(gmail, archive, "forward", 1000)
            # Even before refreshing vector payloads, citations come from current SQLite links.
            refreshed = first.search(SearchRequest("MIGRATE-317", source_type="attachment"))[0]
            self.assertEqual(
                {s.gmail_message_id for s in refreshed.sources}, {"message-1", "forward"}
            )
            downloads = list(gmail.downloaded_parts)
            source_files = {
                s.content_path: Path(s.content_path).read_bytes() for s in refreshed.sources
            }
            migrated = HybridSearchIndex(
                Settings(
                    "key", "model", data_dir=Path(directory), embedding_model="test-new-contract"
                ),
                archive,
                client=first.client,
                embed_model=ControlledEmbedding(),
            )
            self.assertNotEqual(first.collection, migrated.collection)
            self.assertEqual(migrated.refresh(), 3)
            hit = migrated.search(SearchRequest("MIGRATE-317", source_type="attachment"))[0]
            self.assertEqual(hit.document_id, original.document_id)
            self.assertEqual(hit.sources, refreshed.sources)
            self.assertIn("MIGRATE-317", hit.text)
            self.assertEqual(gmail.downloaded_parts, downloads)
            for path, content in source_files.items():
                self.assertEqual(Path(path).read_bytes(), content)
            self.assertTrue(first.client.collection_exists(first.collection))

    def test_failed_attachment_indexing_is_observable_and_retried(self) -> None:
        with TemporaryDirectory() as directory:
            data = b"Invoice ATTACHFAIL-99"
            run_import(
                directory,
                AttachmentGmail([attachment("a", "invoice.txt", "text/plain", data)], {"a": data}),
            )
            archive = SQLiteArchive(Path(directory))
            settings = Settings("key", "model", data_dir=Path(directory))
            client = QdrantClient(":memory:")
            index = HybridSearchIndex(
                settings, archive, client=client, embed_model=UnavailableAttachmentEmbedding()
            )
            with self.assertRaisesRegex(RuntimeError, "embedding unavailable"):
                index.refresh()
            occurrence = archive.attachment_occurrence("message-1", "a")
            assert occurrence is not None
            self.assertEqual(occurrence.indexing_status, "failed")
            index = HybridSearchIndex(
                settings, archive, client=client, embed_model=ControlledEmbedding()
            )
            self.assertEqual(index.refresh(), 1)
            occurrence = archive.attachment_occurrence("message-1", "a")
            assert occurrence is not None
            self.assertEqual(occurrence.indexing_status, "indexed")
            self.assertIn(
                "ATTACHFAIL-99",
                index.search(SearchRequest("ATTACHFAIL-99", source_type="attachment"))[0].text,
            )
