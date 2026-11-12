"""Application entry point and interactive terminal loop."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date
from typing import TextIO

from dotenv import load_dotenv

from email_agent.archive import ArchiveState, SQLiteArchive
from email_agent.attachments import AttachmentReport, process_pending_attachments
from email_agent.chat import ChatError, ChatSession, LocalArchive, openrouter_model
from email_agent.config import ConfigurationError, Settings
from email_agent.gmail import GmailAccessError, GmailAdapter, GmailAgentAdapter, GmailStatus
from email_agent.ingestion import ImportReport, import_recent_messages, retry_failed_messages
from email_agent.search import SearchBackend, SearchRequest, SearchResult
from email_agent.services import HttpServiceAdapter, ServiceAdapter, ServiceStatus

load_dotenv()

HELP_TEXT = """Supported commands:
  /help    Display supported commands
  /clear   Clear in-memory conversation history
  /status  Check Gmail, Qdrant, FastEmbed, and the embedding model
  /search QUERY [--limit N] [--sender ADDRESS] [--after DATE] [--before DATE]
                [--source email|attachment] [--filename NAME] [--type pdf|docx|text|email]
            Search the persistent local hybrid index
  /gmail-search QUERY  Search live Gmail
  /gmail-message ID    Open one live Gmail message
  /gmail-thread ID     Open one live Gmail conversation
  /reindex Rebuild email and attachment vectors from local content without contacting Gmail
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
    search_factory: Callable[[Settings, SQLiteArchive], SearchBackend] | None = None,
    chat_factory: Callable[[Settings, LocalArchive, Callable[[str], None]], ChatSession]
    | None = None,
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
        return run_cli(
            settings,
            factory(settings),
            create_gmail(),
            stdin,
            stdout,
            search_factory=search_factory,
            chat_factory=chat_factory,
        )
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
    *,
    search_factory: Callable[[Settings, SQLiteArchive], SearchBackend] | None = None,
    chat_factory: Callable[[Settings, LocalArchive, Callable[[str], None]], ChatSession]
    | None = None,
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
    archive = SQLiteArchive(settings.data_dir)
    if gmail_status.available:
        archive_state = archive.state()
        if archive_state.failed:
            report = retry_failed_messages(gmail, archive, settings.attachment_max_bytes)
            _write_import_report(stdout, report, action="Retry import")
        elif archive_state.exists:
            _write_archive_reuse(stdout, archive_state)
            _write_attachment_report(
                stdout,
                process_pending_attachments(
                    gmail,
                    archive,
                    settings.attachment_max_bytes,
                ),
            )
        else:
            report = import_recent_messages(
                gmail,
                archive,
                settings.initial_import_limit,
                settings.attachment_max_bytes,
            )
            _write_import_report(stdout, report)

    search_backend: SearchBackend | None = None
    chat: ChatSession | None = None

    def get_search_backend() -> SearchBackend:
        nonlocal search_backend
        if search_backend is None:
            if search_factory is None:
                from email_agent.retrieval import HybridSearchIndex

                search_backend = HybridSearchIndex(settings, archive)
            else:
                search_backend = search_factory(settings, archive)
        return search_backend

    stdout.write("Ask a question about your local archive. Type /help for commands.\n")
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
        elif command == "/clear":
            if chat is not None:
                chat.clear()
            stdout.write("Conversation history cleared.\n")
        elif command == "/status":
            status, local_problem = _checked_service_status(services, settings.embedding_model)
            _write_status(
                stdout,
                status,
                _checked_gmail_status(gmail.status),
                settings.embedding_model,
                local_problem=local_problem,
            )
        elif command == "/reindex":
            try:
                from email_agent.retrieval import HybridSearchIndex

                backend = get_search_backend()
                if not isinstance(backend, HybridSearchIndex):
                    raise RuntimeError("The configured search backend cannot rebuild the index.")
                count = backend.refresh(force=True)
                search_backend = backend
                stdout.write(f"Reindexed {count} archived documents from local content.\n")
            except Exception as exc:
                stdout.write(f"Reindex unavailable: {exc}\n")
        elif command.startswith("/search"):
            try:
                request = _parse_search_request(command)
                backend = get_search_backend()
                backend.refresh()
                _write_search_results(stdout, backend.search(request))
            except ValueError as exc:
                stdout.write(f"Search error: {exc}\n")
            except Exception as exc:
                stdout.write(f"Search unavailable: {exc}\n")
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
            try:
                if chat is None:

                    def notify(name: str) -> None:
                        stdout.write(f"Tool: {name}\n")
                        stdout.flush()

                    model = openrouter_model(settings) if chat_factory is None else None
                    backend = get_search_backend()
                    if not isinstance(backend, LocalArchive):
                        raise ChatError(
                            "The search backend does not support local document context."
                        )
                    if chat_factory is not None:
                        chat = chat_factory(settings, backend, notify)
                    else:
                        chat = ChatSession(
                            settings,
                            backend,
                            model=model,
                            notify=notify,
                        )
                stdout.write(chat.ask(command) + "\n")
            except ChatError as exc:
                stdout.write(f"{exc}\n")
            except Exception:
                stdout.write(
                    "Chat unavailable. Check OpenRouter credentials, model tool support, "
                    "and local retrieval services, then retry.\n"
                )


def _parse_search_request(command: str) -> SearchRequest:
    tokens = shlex.split(command)[1:]
    query_parts: list[str] = []
    values: dict[str, str] = {}
    option_names = {
        "--limit": "limit",
        "--sender": "sender",
        "--after": "date_from",
        "--before": "date_to",
        "--source": "source_type",
        "--filename": "filename",
        "--type": "document_type",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in option_names:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ValueError(f"{token} requires a value")
            values[option_names[token]] = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--"):
            raise ValueError(f"unknown search option: {token}")
        query_parts.append(token)
        index += 1

    query = " ".join(query_parts).strip()
    if not query:
        raise ValueError("provide a search query")
    try:
        limit = int(values.get("limit", "8"))
    except ValueError as exc:
        raise ValueError("--limit must be an integer") from exc
    if not 1 <= limit <= 20:
        raise ValueError("--limit must be between 1 and 20")
    for option, key in (("--after", "date_from"), ("--before", "date_to")):
        if key in values:
            try:
                date.fromisoformat(values[key])
            except ValueError as exc:
                raise ValueError(f"{option} must use YYYY-MM-DD") from exc
    source_type = values.get("source_type")
    if source_type not in {None, "email", "attachment"}:
        raise ValueError("--source must be email or attachment")
    document_type = values.get("document_type")
    if document_type not in {None, "pdf", "docx", "text", "email"}:
        raise ValueError("--type must be pdf, docx, text, or email")
    return SearchRequest(
        query=query,
        limit=limit,
        sender=values.get("sender"),
        date_from=values.get("date_from"),
        date_to=values.get("date_to"),
        source_type=source_type,
        filename=values.get("filename"),
        document_type=document_type,
    )


def _write_search_results(stdout: TextIO, results: list[SearchResult]) -> None:
    if not results:
        stdout.write("No archived email passages matched the search.\n")
        return
    for position, result in enumerate(results, start=1):
        stdout.write(f"Result {position} (score {result.score:.3f})\n")
        stdout.write(f"{result.text}\n")
        stdout.write(
            f"document_id: {result.document_id}\n"
            f"gmail_message_id: {result.gmail_message_id}\n"
            f"thread_id: {result.thread_id}\n"
            f"source_type: {result.source_type}\n"
            f"title: {result.title}\n"
            f"timestamp: {result.timestamp}\n"
            f"sender: {result.sender}\n"
        )
        if result.location:
            stdout.write(f"location: {result.location}\n")
        if result.chunk_id is not None:
            stdout.write(f"chunk_id: {result.chunk_id}\nchunk_index: {result.chunk_index}\n")
        if result.filename is not None:
            stdout.write(
                f"attachment_id: {result.attachment_id}\n"
                f"filename: {result.filename}\n"
                f"mime_type: {result.mime_type}\n"
                f"document_type: {result.document_type}\n"
                f"characters: {result.start_char}:{result.end_char}\n"
            )
            if result.page_number is not None:
                stdout.write(f"page_number: {result.page_number}\n")
            stdout.write(
                "sources: "
                + json.dumps([asdict(source) for source in result.sources], ensure_ascii=False)
                + "\n"
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
    _write_attachment_report(stdout, report.attachments)
    if report.failed:
        _write_incomplete_archive_warning(stdout)


def _write_attachment_report(stdout: TextIO, attachments: AttachmentReport) -> None:
    stdout.write(
        "Attachments: "
        f"downloaded {attachments.downloaded}, parsed {attachments.parsed}, "
        f"indexed {attachments.indexed}, unsupported {attachments.unsupported}, "
        f"oversized {attachments.oversized}, failed {attachments.failed}.\n"
    )


def _write_archive_reuse(stdout: TextIO, state: ArchiveState) -> None:
    stdout.write(
        f"Local archive: reused {state.completed} completed messages, failed {state.failed}.\n"
    )
    if state.failed:
        _write_incomplete_archive_warning(stdout)


def _write_incomplete_archive_warning(stdout: TextIO) -> None:
    stdout.write("Warning: the local archive is incomplete; completed messages remain usable.\n")


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
    stdout.write(f"FastEmbed: {'available' if status.fastembed_available else 'unavailable'}\n")
    if local_problem:
        stdout.write(f"Local service diagnostics: unavailable ({local_problem})\n")
    if not status.fastembed_available:
        model_status = "unavailable (FastEmbed is unavailable)"
    else:
        model_status = (
            "supported (downloaded on first search)"
            if status.embedding_model_available
            else "unsupported"
        )
    stdout.write(f"Embedding model {embedding_model}: {model_status}\n")


def _checked_service_status(
    services: ServiceAdapter,
    embedding_model: str,
) -> tuple[ServiceStatus, str | None]:
    try:
        return services.status(embedding_model), None
    except Exception:
        return ServiceStatus(False, False, False), "check Qdrant and FastEmbed, then retry /status"


def _checked_gmail_status(check: Callable[[], GmailStatus]) -> GmailStatus:
    try:
        return check()
    except Exception:
        return GmailStatus(
            available=False,
            problem="Gmail diagnostics failed; run `gmail-agent status` for details",
        )


def main() -> None:
    """Console-script entry point."""
    if sys.argv[1:] == ["--reindex"]:
        from email_agent.retrieval import HybridSearchIndex

        try:
            settings = Settings.from_environ(os.environ)
            archive = SQLiteArchive(settings.data_dir)
            count = HybridSearchIndex(settings, archive).refresh(force=True)
            print(f"Reindexed {count} local documents; no Gmail access.")
        except Exception as exc:
            print(f"Reindex failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return
    raise SystemExit(
        run_application(
            environ=os.environ,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
