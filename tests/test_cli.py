from __future__ import annotations

from io import StringIO
from tempfile import TemporaryDirectory
from unittest import TestCase

from email_agent.cli import run_application
from email_agent.config import Settings
from email_agent.gmail import GmailAccessError, GmailStatus
from email_agent.services import ServiceStatus

TEST_DATA_DIRECTORY = TemporaryDirectory()
VALID_ENV = {
    "OPENROUTER_API_KEY": "test-key",
    "OPENROUTER_MODEL": "test/model",
    "EMAIL_AGENT_DATA_DIR": TEST_DATA_DIRECTORY.name,
}


class ControlledServices:
    def __init__(self, status: ServiceStatus) -> None:
        self.current_status = status
        self.pulled_models: list[str] = []

    def status(self, embedding_model: str) -> ServiceStatus:
        return self.current_status

    def pull_model(self, model: str) -> None:
        self.pulled_models.append(model)
        self.current_status = ServiceStatus(
            qdrant_available=self.current_status.qdrant_available,
            fastembed_available=True,
            embedding_model_available=True,
        )


class FailingServices(ControlledServices):
    def status(self, embedding_model: str) -> ServiceStatus:
        raise RuntimeError("diagnostic adapter failed")


class FailingRefreshServices(ControlledServices):
    def __init__(self, status: ServiceStatus) -> None:
        super().__init__(status)
        self.status_calls = 0

    def status(self, embedding_model: str) -> ServiceStatus:
        self.status_calls += 1
        if self.status_calls > 1:
            raise RuntimeError("diagnostic adapter failed")
        return self.current_status


class ControlledGmail:
    def __init__(self, status: GmailStatus) -> None:
        self.current_status = status
        self.authentication_attempts = 0
        self.searches: list[str] = []
        self.message_ids: list[str] = []
        self.thread_ids: list[str] = []

    def ensure_authenticated(self) -> GmailStatus:
        self.authentication_attempts += 1
        return self.current_status

    def status(self) -> GmailStatus:
        return self.current_status

    def search_messages(self, query: str) -> object:
        self.searches.append(query)
        return {"messages": [{"id": "message-1", "subject": "Quarterly report"}]}

    def list_messages(
        self,
        query: str,
        *,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        return {"messages": [], "next_page_token": None}

    def get_message(self, message_id: str) -> object:
        self.message_ids.append(message_id)
        return {"id": message_id, "body": "Message body"}

    def get_thread(self, thread_id: str) -> object:
        self.thread_ids.append(thread_id)
        return {"id": thread_id, "messages": [{"id": "message-1"}]}


class FailingReadGmail(ControlledGmail):
    def search_messages(self, query: str) -> object:
        raise GmailAccessError("Gmail token expired; run gmail-agent login --scope readonly")


class FailingStartupGmail(ControlledGmail):
    def ensure_authenticated(self) -> GmailStatus:
        raise OSError("Gmail subprocess could not start")


class FailingRefreshGmail(ControlledGmail):
    def status(self) -> GmailStatus:
        raise OSError("Gmail subprocess could not start")


AVAILABLE_GMAIL = GmailStatus(available=True, account="reader@example.com")


def run_with_services(
    *,
    services: ControlledServices,
    stdin: StringIO,
    stdout: StringIO,
    stderr: StringIO | None = None,
    gmail: ControlledGmail | None = None,
) -> int:
    controlled_gmail = gmail or ControlledGmail(AVAILABLE_GMAIL)
    return run_application(
        environ=VALID_ENV,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr or StringIO(),
        services_factory=lambda settings: services,
        gmail_factory=lambda: controlled_gmail,
    )


class ConfigurationTests(TestCase):
    def test_non_positive_attachment_limit_is_rejected(self) -> None:
        stderr = StringIO()

        exit_code = run_application(
            environ={**VALID_ENV, "EMAIL_AGENT_ATTACHMENT_MAX_BYTES": "0"},
            stdin=StringIO(""),
            stdout=StringIO(),
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn(
            "EMAIL_AGENT_ATTACHMENT_MAX_BYTES must be a positive integer",
            stderr.getvalue(),
        )

    def test_attachment_limit_defaults_to_20_mebibytes(self) -> None:
        settings = Settings.from_environ(VALID_ENV)

        self.assertEqual(settings.attachment_max_bytes, 20 * 1024 * 1024)

    def test_missing_required_configuration_is_actionable(self) -> None:
        stdout = StringIO()
        stderr = StringIO()

        exit_code = run_application(
            environ={},
            stdin=StringIO(""),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Missing required configuration", stderr.getvalue())
        self.assertIn("OPENROUTER_API_KEY", stderr.getvalue())
        self.assertIn("OPENROUTER_MODEL", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_invalid_service_url_is_actionable(self) -> None:
        stderr = StringIO()

        exit_code = run_application(
            environ={**VALID_ENV, "QDRANT_URL": "localhost:6333"},
            stdin=StringIO(""),
            stdout=StringIO(),
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("QDRANT_URL must be an http(s) URL", stderr.getvalue())

    def test_initial_import_limit_above_100_is_rejected(self) -> None:
        stderr = StringIO()

        exit_code = run_application(
            environ={**VALID_ENV, "EMAIL_AGENT_INITIAL_IMPORT_LIMIT": "101"},
            stdin=StringIO(""),
            stdout=StringIO(),
            stderr=stderr,
        )

        self.assertEqual(exit_code, 2)
        self.assertIn(
            "EMAIL_AGENT_INITIAL_IMPORT_LIMIT must be between 1 and 100",
            stderr.getvalue(),
        )


class InteractiveCliTests(TestCase):
    def test_first_start_authenticates_gmail_and_reports_it_independently(self) -> None:
        services = ControlledServices(ServiceStatus(False, False, False))
        gmail = ControlledGmail(AVAILABLE_GMAIL)
        stdout = StringIO()

        exit_code = run_with_services(
            services=services,
            gmail=gmail,
            stdin=StringIO("/exit\n"),
            stdout=stdout,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(gmail.authentication_attempts, 1)
        self.assertIn("Gmail: available (reader@example.com)", stdout.getvalue())
        self.assertIn("Qdrant: unavailable", stdout.getvalue())
        self.assertIn("FastEmbed: unavailable", stdout.getvalue())

    def test_read_only_gmail_commands_search_and_open_messages_and_threads(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, True))
        gmail = ControlledGmail(AVAILABLE_GMAIL)
        stdout = StringIO()

        exit_code = run_with_services(
            services=services,
            gmail=gmail,
            stdin=StringIO(
                "/gmail-search from:billing@example.com\n"
                "/gmail-message message-1\n"
                "/gmail-thread thread-1\n"
                "/exit\n"
            ),
            stdout=stdout,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(gmail.searches, ["from:billing@example.com"])
        self.assertEqual(gmail.message_ids, ["message-1"])
        self.assertEqual(gmail.thread_ids, ["thread-1"])
        self.assertIn('"subject": "Quarterly report"', stdout.getvalue())
        self.assertIn('"body": "Message body"', stdout.getvalue())
        self.assertNotIn('"id": "thread-1"', stdout.getvalue())
        self.assertNotIn("message-1", stdout.getvalue())
        self.assertNotIn("thread-1", stdout.getvalue())

    def test_gmail_read_failure_is_actionable_and_cli_remains_available(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, True))
        gmail = FailingReadGmail(AVAILABLE_GMAIL)
        stdout = StringIO()

        exit_code = run_with_services(
            services=services,
            gmail=gmail,
            stdin=StringIO("/gmail-search newer_than:7d\n/status\n/exit\n"),
            stdout=stdout,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Gmail token expired", stdout.getvalue())
        self.assertGreaterEqual(stdout.getvalue().count("Gmail: available"), 2)

    def test_help_status_and_exit_are_available(self) -> None:
        services = ControlledServices(ServiceStatus(True, False, False))
        stdout = StringIO()

        exit_code = run_application(
            environ=VALID_ENV,
            stdin=StringIO("/help\n/status\n/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
            gmail_factory=lambda: ControlledGmail(AVAILABLE_GMAIL),
        )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("/help", output)
        self.assertIn("/status", output)
        self.assertIn("/exit", output)
        self.assertIn("Qdrant: available", output)
        self.assertIn("FastEmbed: unavailable", output)
        self.assertIn("Goodbye.", output)
        self.assertNotIn("/search", output)
        self.assertNotIn("/gmail-search", output)
        self.assertNotIn("/gmail-message", output)
        self.assertNotIn("/gmail-thread", output)

    def test_status_distinguishes_each_unavailable_service(self) -> None:
        services = ControlledServices(ServiceStatus(False, True, True))
        stdout = StringIO()

        run_application(
            environ=VALID_ENV,
            stdin=StringIO("/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
            gmail_factory=lambda: ControlledGmail(AVAILABLE_GMAIL),
        )

        self.assertIn("Qdrant: unavailable", stdout.getvalue())
        self.assertIn("FastEmbed: available", stdout.getvalue())
        self.assertIn("Embedding model Qwen/Qwen3-Embedding-0.6B: supported", stdout.getvalue())

    def test_startup_reports_support_without_loading_or_downloading_model(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, True))
        stdout = StringIO()
        run_with_services(services=services, stdin=StringIO("/exit\n"), stdout=stdout)
        self.assertEqual(services.pulled_models, [])
        self.assertIn("supported (downloaded on first search)", stdout.getvalue())

    def test_local_diagnostic_failure_does_not_suppress_gmail_status(self) -> None:
        services = FailingServices(ServiceStatus(False, False, False))
        stdout = StringIO()

        exit_code = run_application(
            environ=VALID_ENV,
            stdin=StringIO("/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
            gmail_factory=lambda: ControlledGmail(AVAILABLE_GMAIL),
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Gmail: available (reader@example.com)", stdout.getvalue())
        self.assertIn("Local service diagnostics: unavailable", stdout.getvalue())

    def test_status_keeps_reporting_gmail_when_local_diagnostics_fail(self) -> None:
        services = FailingRefreshServices(ServiceStatus(True, True, True))
        stdout = StringIO()

        exit_code = run_application(
            environ=VALID_ENV,
            stdin=StringIO("/status\n/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
            gmail_factory=lambda: ControlledGmail(AVAILABLE_GMAIL),
        )

        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(stdout.getvalue().count("Gmail: available"), 2)
        self.assertIn("Local service diagnostics: unavailable", stdout.getvalue())

    def test_gmail_startup_failure_does_not_suppress_local_status(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, True))
        stdout = StringIO()

        exit_code = run_with_services(
            services=services,
            gmail=FailingStartupGmail(AVAILABLE_GMAIL),
            stdin=StringIO("/exit\n"),
            stdout=stdout,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Gmail: unavailable", stdout.getvalue())
        self.assertIn("Qdrant: available", stdout.getvalue())
        self.assertIn("FastEmbed: available", stdout.getvalue())

    def test_status_keeps_reporting_local_services_when_gmail_diagnostics_fail(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, True))
        stdout = StringIO()

        exit_code = run_with_services(
            services=services,
            gmail=FailingRefreshGmail(AVAILABLE_GMAIL),
            stdin=StringIO("/status\n/exit\n"),
            stdout=stdout,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Gmail: unavailable", stdout.getvalue())
        self.assertGreaterEqual(stdout.getvalue().count("Qdrant: available"), 2)
