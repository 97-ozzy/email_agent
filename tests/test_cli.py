from __future__ import annotations

from io import StringIO
from unittest import TestCase

from email_agent.cli import run_application
from email_agent.services import ServiceStatus


VALID_ENV = {
    "OPENROUTER_API_KEY": "test-key",
    "OPENROUTER_MODEL": "test/model",
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
            ollama_available=True,
            embedding_model_available=True,
        )


class FailingServices(ControlledServices):
    def status(self, embedding_model: str) -> ServiceStatus:
        raise RuntimeError("diagnostic adapter failed")


class ConfigurationTests(TestCase):
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


class InteractiveCliTests(TestCase):
    def test_help_status_and_exit_are_available(self) -> None:
        services = ControlledServices(ServiceStatus(True, False, False))
        stdout = StringIO()

        exit_code = run_application(
            environ=VALID_ENV,
            stdin=StringIO("/help\n/status\n/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
        )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("/help", output)
        self.assertIn("/status", output)
        self.assertIn("/exit", output)
        self.assertIn("Qdrant: available", output)
        self.assertIn("Ollama: unavailable", output)
        self.assertIn("Goodbye.", output)

    def test_status_distinguishes_each_unavailable_service(self) -> None:
        services = ControlledServices(ServiceStatus(False, True, True))
        stdout = StringIO()

        run_application(
            environ=VALID_ENV,
            stdin=StringIO("/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
        )

        self.assertIn("Qdrant: unavailable", stdout.getvalue())
        self.assertIn("Ollama: available", stdout.getvalue())
        self.assertIn("Embedding model bge-m3: installed", stdout.getvalue())

    def test_missing_embedding_model_is_not_downloaded_without_opt_in(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, False))
        stdout = StringIO()

        run_application(
            environ=VALID_ENV,
            stdin=StringIO("n\n/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
        )

        self.assertEqual(services.pulled_models, [])
        self.assertIn("Download bge-m3 now? [y/N]", stdout.getvalue())
        self.assertIn("ollama pull bge-m3", stdout.getvalue())

    def test_missing_embedding_model_can_be_downloaded_by_opt_in(self) -> None:
        services = ControlledServices(ServiceStatus(True, True, False))
        stdout = StringIO()

        run_application(
            environ=VALID_ENV,
            stdin=StringIO("y\n/exit\n"),
            stdout=stdout,
            stderr=StringIO(),
            services_factory=lambda settings: services,
        )

        self.assertEqual(services.pulled_models, ["bge-m3"])
        self.assertIn("Downloaded bge-m3.", stdout.getvalue())

    def test_unexpected_startup_error_has_no_traceback_in_normal_mode(self) -> None:
        stderr = StringIO()
        services = FailingServices(ServiceStatus(False, False, False))

        exit_code = run_application(
            environ=VALID_ENV,
            stdin=StringIO(""),
            stdout=StringIO(),
            stderr=stderr,
            services_factory=lambda settings: services,
        )

        self.assertEqual(exit_code, 1)
        self.assertIn("diagnostic adapter failed", stderr.getvalue())
        self.assertIn("EMAIL_AGENT_DEBUG=true", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
