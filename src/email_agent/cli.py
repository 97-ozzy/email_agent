"""Application entry point and interactive terminal loop."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

from dotenv import load_dotenv

from email_agent.archive import ArchiveState, SQLiteArchive
from email_agent.config import ConfigurationError, Settings
from email_agent.gmail import GmailAccessError, GmailAdapter, GmailAgentAdapter, GmailStatus
from email_agent.ingestion import ImportReport, import_recent_messages, retry_failed_messages
from email_agent.services import HttpServiceAdapter, ServiceAdapter, ServiceStatus

load_dotenv()

HELP_TEXT = """Supported commands:
  /help    Display supported commands
  /status  Check Gmail, Qdrant, Ollama, and the embedding model
  /gmail-search QUERY  Search live Gmail
  /gmail-message ID    Open one live Gmail message
  /gmail-thread ID     Open one live Gmail conversation
  /exit    Exit the application
"""


def run_application(
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    services_factory: Callable[[Settings], ServiceAdapter] | None = None,
    gmail_factory: Callable[[], GmailAdapter] | None = None,
) -> int:
    """Validate configuration and run the interactive application."""
    try:
        settings = Settings.from_environ(environ)
    except ConfigurationError as exc:
        stderr.write("Missing required configuration or invalid settings:\n")
        for problem in exc.problems:
            stderr.write(f"  - {problem}\n")
        stderr.write("Copy .env.example and export the values before trying again.\n")
        return 2

    factory = services_factory or HttpServiceAdapter
    create_gmail = gmail_factory or GmailAgentAdapter
    try:
        return run_cli(settings, factory(settings), create_gmail(), stdin, stdout)
    except Exception as exc:
        if settings.debug:
            raise
        stderr.write(f"Email Agent could not start: {exc}\n")
        stderr.write("Set EMAIL_AGENT_DEBUG=true for a detailed traceback.\n")
        return 1


def run_cli(
    settings: Settings,
    services: ServiceAdapter,
    gmail: GmailAdapter,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    """Run the terminal conversation using an injected service boundary."""
    stdout.write("Email Agent\n")
    gmail_status = _checked_gmail_status(gmail.ensure_authenticated)
    status, local_problem = _checked_service_status(services, settings.embedding_model)
    _write_status(
        stdout,
        status,
        gmail_status,
        settings.embedding_model,
        local_problem=local_problem,
    )
    if gmail_status.available:
        archive = SQLiteArchive(settings.data_dir)
        archive_state = archive.state()
        if archive_state.failed:
            report = retry_failed_messages(gmail, archive)
            _write_import_report(stdout, report, action="Retry import")
        elif archive_state.exists:
            _write_archive_reuse(stdout, archive_state)
        else:
            report = import_recent_messages(
                gmail,
                archive,
                settings.initial_import_limit,
            )
            _write_import_report(stdout, report)
    if status.ollama_available and not status.embedding_model_available:
        _offer_model_download(settings, services, stdin, stdout)

    stdout.write("Type /help for commands.\n")
    while True:
        stdout.write("email-agent> ")
        stdout.flush()
        line = stdin.readline()
        if not line:
            stdout.write("\nGoodbye.\n")
            return 0

        command = line.strip()
        if not command:
            continue
        if command == "/exit":
            stdout.write("Goodbye.\n")
            return 0
        if command == "/help":
            stdout.write(HELP_TEXT)
        elif command == "/status":
            status, local_problem = _checked_service_status(
                services, settings.embedding_model
            )
            _write_status(
                stdout,
                status,
                _checked_gmail_status(gmail.status),
                settings.embedding_model,
                local_problem=local_problem,
            )
        elif command.startswith("/gmail-search "):
            query = command.removeprefix("/gmail-search ")
            _try_write_gmail_result(
                stdout,
                gmail.search_messages,
                query,
            )
        elif command.startswith("/gmail-message "):
            message_id = command.removeprefix("/gmail-message ")
            _try_write_gmail_result(
                stdout,
                gmail.get_message,
                message_id,
            )
        elif command.startswith("/gmail-thread "):
            thread_id = command.removeprefix("/gmail-thread ")
            _try_write_gmail_result(
                stdout,
                gmail.get_thread,
                thread_id,
            )
        elif command.startswith("/"):
            stdout.write(f"Unknown command: {command}. Type /help for supported commands.\n")
        else:
            stdout.write(
                "Chat is not configured in this bootstrap ticket. Type /help for commands.\n"
            )


def _write_import_report(
    stdout: TextIO,
    report: ImportReport,
    *,
    action: str = "Initial import",
) -> None:
    stdout.write(
        f"{action}: "
        f"requested {report.requested}, downloaded {report.downloaded}, "
        f"processed {report.processed}, failed {report.failed}"
    )
    if report.reused:
        stdout.write(f", reused {report.reused}")
    stdout.write(".\n")
    if report.failed:
        _write_incomplete_archive_warning(stdout)


def _write_archive_reuse(stdout: TextIO, state: ArchiveState) -> None:
    stdout.write(
        f"Local archive: reused {state.completed} completed messages, "
        f"failed {state.failed}.\n"
    )
    if state.failed:
        _write_incomplete_archive_warning(stdout)


def _write_incomplete_archive_warning(stdout: TextIO) -> None:
    stdout.write(
        "Warning: the local archive is incomplete; completed messages remain usable.\n"
    )


def _try_write_gmail_result(
    stdout: TextIO,
    operation: Callable[[str], object],
    argument: str,
) -> None:
    try:
        result = operation(argument)
    except GmailAccessError as exc:
        stdout.write(f"Gmail unavailable: {exc}\n")
    else:
        stdout.write(json.dumps(result, indent=2, ensure_ascii=False))
        stdout.write("\n")


def _write_status(
    stdout: TextIO,
    status: ServiceStatus,
    gmail_status: GmailStatus,
    embedding_model: str,
    *,
    local_problem: str | None = None,
) -> None:
    gmail_text = "available"
    if gmail_status.available and gmail_status.account:
        gmail_text += f" ({gmail_status.account})"
    elif not gmail_status.available:
        gmail_text = "unavailable"
        if gmail_status.problem:
            gmail_text += f" ({gmail_status.problem})"
    stdout.write(f"Gmail: {gmail_text}\n")
    stdout.write(f"Qdrant: {'available' if status.qdrant_available else 'unavailable'}\n")
    stdout.write(f"Ollama: {'available' if status.ollama_available else 'unavailable'}\n")
    if local_problem:
        stdout.write(f"Local service diagnostics: unavailable ({local_problem})\n")
    if not status.ollama_available:
        model_status = "unavailable (Ollama is unavailable)"
    else:
        model_status = "installed" if status.embedding_model_available else "missing"
    stdout.write(f"Embedding model {embedding_model}: {model_status}\n")


def _checked_service_status(
    services: ServiceAdapter,
    embedding_model: str,
) -> tuple[ServiceStatus, str | None]:
    try:
        return services.status(embedding_model), None
    except Exception:
        return ServiceStatus(False, False, False), "check Qdrant and Ollama, then retry /status"


def _checked_gmail_status(check: Callable[[], GmailStatus]) -> GmailStatus:
    try:
        return check()
    except Exception:
        return GmailStatus(
            available=False,
            problem="Gmail diagnostics failed; run `gmail-agent status` for details",
        )


def _offer_model_download(
    settings: Settings,
    services: ServiceAdapter,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    stdout.write(f"Download {settings.embedding_model} now? [y/N] ")
    stdout.flush()
    answer = stdin.readline().strip().lower()
    if answer not in {"y", "yes"}:
        stdout.write(
            f"Model download skipped. Run `ollama pull {settings.embedding_model}` when ready.\n"
        )
        return

    stdout.write(f"Downloading {settings.embedding_model} with Ollama...\n")
    try:
        services.pull_model(settings.embedding_model)
    except Exception as exc:
        stdout.write(f"Could not download {settings.embedding_model}: {exc}\n")
        stdout.write(f"Try `ollama pull {settings.embedding_model}` manually.\n")
        return
    stdout.write(f"Downloaded {settings.embedding_model}.\n")


def main() -> None:
    """Console-script entry point."""
    raise SystemExit(
        run_application(
            environ=os.environ,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
