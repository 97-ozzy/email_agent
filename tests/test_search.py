from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from llama_index.core.base.embeddings.base import BaseEmbedding
from qdrant_client import QdrantClient

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
