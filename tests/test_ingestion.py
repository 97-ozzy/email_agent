from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from email_agent.cli import run_application
from email_agent.gmail import GmailStatus
from email_agent.services import ServiceStatus


class AvailableServices:
    def status(self, embedding_model: str) -> ServiceStatus:
        return ServiceStatus(True, True, True)

    def pull_model(self, model: str) -> None:
        raise AssertionError("the configured embedding model is already available")


class SyntheticGmail:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages
        self.downloaded_ids: list[str] = []
        self.discovery_calls: list[tuple[str, int, str | None]] = []

    def ensure_authenticated(self) -> GmailStatus:
        return GmailStatus(available=True, account="reader@example.com")

    def status(self) -> GmailStatus:
        return GmailStatus(available=True, account="reader@example.com")

    def list_messages(
        self,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        self.discovery_calls.append((query, max_results, page_token))
        return {"messages": self.messages, "next_page_token": None}

    def search_messages(self, query: str) -> object:
        return {"messages": self.messages}

    def get_message(self, message_id: str) -> object:
        self.downloaded_ids.append(message_id)
        return next(message for message in self.messages if message["id"] == message_id)

    def get_thread(self, thread_id: str) -> object:
        return {"id": thread_id, "messages": []}


class PagedGmail(SyntheticGmail):
    def __init__(self, pages: dict[str | None, tuple[list[dict[str, object]], str | None]]) -> None:
        messages_by_id: dict[object, dict[str, object]] = {}
        for page, _ in pages.values():
            for item in page:
                messages_by_id.setdefault(item["id"], item)
        super().__init__(list(messages_by_id.values()))
        self.pages = pages

    def list_messages(
        self,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        self.discovery_calls.append((query, max_results, page_token))
        messages, next_token = self.pages[page_token]
        return {"messages": messages, "next_page_token": next_token}


class PartiallyFailingGmail(SyntheticGmail):
    def __init__(self, messages: list[dict[str, object]], failed_id: str | None) -> None:
        super().__init__(messages)
        self.failed_id = failed_id

    def get_message(self, message_id: str) -> object:
        if self.failed_id is not None and message_id == self.failed_id:
            raise RuntimeError("temporary Gmail failure")
        return super().get_message(message_id)


class FailingDiscoveryGmail(SyntheticGmail):
    def list_messages(
        self,
        query: str,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        raise RuntimeError("temporary discovery failure")


def message(
    message_id: str,
    *,
    body: str,
    date: str = "2026-11-12T10:30:00+00:00",
) -> dict[str, object]:
    return {
        "id": message_id,
        "thread_id": "thread-1",
        "date": date,
        "from": "Alice <alice@example.com>",
        "to": "reader@example.com, Bob <bob@example.com>",
        "cc": "carol@example.com",
        "bcc": "",
        "reply_to": "alice@example.com",
        "subject": "Quarterly plan",
        "labels": ["INBOX", "IMPORTANT"],
        "body": body,
        "body_format": "text/plain",
        "body_truncated": False,
    }


class InitialImportTests(TestCase):
    def test_default_limit_selects_exactly_100_newest_inbox_and_sent_messages(self) -> None:
        messages = [
            message(
                f"message-{number:03}",
                body=f"body {number}",
                date=f"2026-11-12T{number // 60:02}:{number % 60:02}:00+00:00",
            )
            for number in reversed(range(105))
        ]
        gmail = SyntheticGmail(messages)

        with TemporaryDirectory() as directory:
            stdout = StringIO()
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO("/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            self.assertIn(
                "Initial import: requested 100, downloaded 100, processed 100, failed 0",
                stdout.getvalue(),
            )
            self.assertEqual(len(gmail.downloaded_ids), 100)
            self.assertEqual(gmail.downloaded_ids[0], "message-104")
            self.assertNotIn("message-000", gmail.downloaded_ids)
            self.assertEqual(gmail.discovery_calls[0][0], "in:inbox OR in:sent")

    def test_pagination_preserves_newest_first_order_and_deduplicates_ids(self) -> None:
        oldest = message("message-1", body="one", date="2026-11-09T10:00:00+00:00")
        duplicate = message("message-2", body="two", date="2026-11-10T10:00:00+00:00")
        newer = message("message-3", body="three", date="2026-11-11T10:00:00+00:00")
        newest = message("message-4", body="four", date="2026-11-12T10:00:00+00:00")
        gmail = PagedGmail(
            {
                None: ([newest, newer], "page-2"),
                "page-2": ([newer, duplicate, oldest], None),
            }
        )

        with TemporaryDirectory() as directory:
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "4",
                },
                stdin=StringIO("/exit\n"),
                stdout=StringIO(),
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

        self.assertEqual(
            gmail.discovery_calls,
            [
                ("in:inbox OR in:sent", 4, None),
                ("in:inbox OR in:sent", 2, "page-2"),
            ],
        )
        self.assertEqual(
            gmail.downloaded_ids,
            ["message-4", "message-3", "message-2", "message-1"],
        )

    def test_fewer_than_limit_messages_are_archived_with_complete_metadata(self) -> None:
        messages = [message("message-1", body="Complete original body")]
        gmail = SyntheticGmail(messages)

        with TemporaryDirectory() as directory:
            stdout = StringIO()
            exit_code = run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "5",
                },
                stdin=StringIO("/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            self.assertEqual(exit_code, 0)
            self.assertIn(
                "Initial import: requested 5, downloaded 1, processed 1, failed 0",
                stdout.getvalue(),
            )
            self.assertEqual(gmail.downloaded_ids, ["message-1"])

            database = sqlite3.connect(Path(directory) / "archive.sqlite3")
            database.row_factory = sqlite3.Row
            stored = database.execute(
                "SELECT * FROM messages WHERE gmail_message_id = ?", ("message-1",)
            ).fetchone()
            thread = database.execute(
                "SELECT gmail_thread_id FROM threads WHERE gmail_thread_id = ?", ("thread-1",)
            ).fetchone()
            database.close()

            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["gmail_thread_id"], "thread-1")
            self.assertEqual(stored["sender"], "Alice <alice@example.com>")
            self.assertEqual(
                json.loads(stored["recipients"]),
                {
                    "to": "reader@example.com, Bob <bob@example.com>",
                    "cc": "carol@example.com",
                    "bcc": "",
                    "reply_to": "alice@example.com",
                },
            )
            self.assertEqual(stored["subject"], "Quarterly plan")
            self.assertEqual(stored["message_timestamp"], "2026-11-12T10:30:00+00:00")
            self.assertEqual(json.loads(stored["labels"]), ["INBOX", "IMPORTANT"])
            self.assertEqual(stored["ingestion_status"], "downloaded")
            self.assertEqual(stored["indexing_status"], "pending")
            self.assertEqual(
                (Path(directory) / stored["body_path"]).read_text(encoding="utf-8"),
                "Complete original body",
            )
            self.assertEqual(thread["gmail_thread_id"], "thread-1")

    def test_russian_subject_snippet_and_complete_body_are_saved_as_utf8(self) -> None:
        russian = message(
            "message-ru",
            body="Здравствуйте, Анна!\n\nВаш платёж успешно обработан.\nС уважением, Банк",
        )
        russian["subject"] = "Счёт за ноябрь"
        russian["snippet"] = "Оплата получена — спасибо"
        gmail = SyntheticGmail([russian])

        with TemporaryDirectory() as directory:
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "1",
                },
                stdin=StringIO("/exit\n"),
                stdout=StringIO(),
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            with closing(sqlite3.connect(Path(directory) / "archive.sqlite3")) as database:
                stored = database.execute(
                    "SELECT subject, snippet, body_path FROM messages WHERE gmail_message_id = ?",
                    ("message-ru",),
                ).fetchone()
            assert stored is not None
            stored_body = (Path(directory) / stored[2]).read_text(encoding="utf-8")

        self.assertEqual(stored[0], "Счёт за ноябрь")
        self.assertEqual(stored[1], "Оплата получена — спасибо")
        self.assertEqual(
            stored_body,
            "Здравствуйте, Анна!\n\nВаш платёж успешно обработан.\nС уважением, Банк",
        )

    def test_partial_failure_preserves_completed_messages_and_warns(self) -> None:
        gmail = PartiallyFailingGmail(
            [
                message("message-good", body="usable"),
                message("message-failed", body="unavailable"),
            ],
            failed_id="message-failed",
        )

        with TemporaryDirectory() as directory:
            stdout = StringIO()
            run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "2",
                },
                stdin=StringIO("/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            self.assertIn(
                "Initial import: requested 2, downloaded 1, processed 1, failed 1",
                stdout.getvalue(),
            )
            self.assertIn("archive is incomplete", stdout.getvalue())

            database = sqlite3.connect(Path(directory) / "archive.sqlite3")
            statuses = dict(
                database.execute(
                    "SELECT gmail_message_id, ingestion_status FROM messages"
                ).fetchall()
            )
            database.close()

        self.assertEqual(
            statuses,
            {"message-good": "downloaded", "message-failed": "failed"},
        )

    def test_restart_retries_only_failed_messages_without_duplicates(self) -> None:
        gmail = PartiallyFailingGmail(
            [
                message("message-good", body="already complete"),
                message("message-retry", body="полный текст после повтора"),
            ],
            failed_id="message-retry",
        )

        with TemporaryDirectory() as directory:
            environment = {
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "test/model",
                "EMAIL_AGENT_DATA_DIR": directory,
                "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "2",
            }
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=StringIO(),
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )
            gmail.failed_id = None
            restart_output = StringIO()
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=restart_output,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            database = sqlite3.connect(Path(directory) / "archive.sqlite3")
            rows = database.execute(
                "SELECT gmail_message_id, ingestion_status, body_path FROM messages "
                "ORDER BY gmail_message_id"
            ).fetchall()
            failed_count = database.execute(
                "SELECT COUNT(*) FROM ingestion_failures"
            ).fetchone()[0]
            database.close()
            retried_body_path = next(row[2] for row in rows if row[0] == "message-retry")
            retried_body = (Path(directory) / retried_body_path).read_text(encoding="utf-8")

        self.assertEqual(gmail.downloaded_ids, ["message-good", "message-retry"])
        self.assertEqual(
            gmail.discovery_calls,
            [("in:inbox OR in:sent", 2, None)],
        )
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [("message-good", "downloaded"), ("message-retry", "downloaded")],
        )
        self.assertEqual(failed_count, 0)
        self.assertEqual(retried_body, "полный текст после повтора")
        self.assertIn(
            "Retry import: requested 1, downloaded 1, processed 1, failed 0",
            restart_output.getvalue(),
        )

    def test_restart_reuses_completed_messages_without_redownloading(self) -> None:
        gmail = SyntheticGmail([message("message-1", body="persisted")])

        with TemporaryDirectory() as directory:
            environment = {
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "test/model",
                "EMAIL_AGENT_DATA_DIR": directory,
                "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "1",
            }
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=StringIO(),
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )
            gmail.messages.append(message("message-new", body="must wait for sync"))
            restart_output = StringIO()
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=restart_output,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

            database = sqlite3.connect(Path(directory) / "archive.sqlite3")
            message_count = database.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            database.close()

        self.assertEqual(gmail.downloaded_ids, ["message-1"])
        self.assertEqual(
            gmail.discovery_calls,
            [("in:inbox OR in:sent", 1, None)],
        )
        self.assertEqual(message_count, 1)
        self.assertIn(
            "Local archive: reused 1 completed messages, failed 0",
            restart_output.getvalue(),
        )

    def test_empty_initial_import_does_not_absorb_new_mail_on_restart(self) -> None:
        gmail = SyntheticGmail([])

        with TemporaryDirectory() as directory:
            environment = {
                "OPENROUTER_API_KEY": "test-key",
                "OPENROUTER_MODEL": "test/model",
                "EMAIL_AGENT_DATA_DIR": directory,
                "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "5",
            }
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=StringIO(),
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )
            gmail.messages.append(message("message-new", body="wait for sync"))
            restart_output = StringIO()
            run_application(
                environ=environment,
                stdin=StringIO("/exit\n"),
                stdout=restart_output,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

        self.assertEqual(gmail.discovery_calls, [("in:inbox OR in:sent", 5, None)])
        self.assertEqual(gmail.downloaded_ids, [])
        self.assertIn(
            "Local archive: reused 0 completed messages, failed 0",
            restart_output.getvalue(),
        )

    def test_discovery_failure_warns_but_keeps_cli_usable(self) -> None:
        gmail = FailingDiscoveryGmail([])

        with TemporaryDirectory() as directory:
            stdout = StringIO()
            exit_code = run_application(
                environ={
                    "OPENROUTER_API_KEY": "test-key",
                    "OPENROUTER_MODEL": "test/model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "5",
                },
                stdin=StringIO("/status\n/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: gmail,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "Initial import: requested 5, downloaded 0, processed 0, failed 1",
            stdout.getvalue(),
        )
        self.assertIn("archive is incomplete", stdout.getvalue())
        self.assertGreaterEqual(stdout.getvalue().count("Gmail: available"), 2)
