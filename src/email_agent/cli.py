"""Application entry point and interactive terminal loop."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import TextIO

from dotenv import load_dotenv

from email_agent.config import ConfigurationError, Settings
from email_agent.services import HttpServiceAdapter, ServiceAdapter, ServiceStatus

load_dotenv()

HELP_TEXT = """Supported commands:
  /help    Display supported commands
  /status  Check Qdrant, Ollama, and the embedding model
  /exit    Exit the application
"""


def run_application(
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    services_factory: Callable[[Settings], ServiceAdapter] | None = None,
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
    try:
        return run_cli(settings, factory(settings), stdin, stdout)
    except Exception as exc:
        if settings.debug:
            raise
        stderr.write(f"Email Agent could not start: {exc}\n")
        stderr.write("Set EMAIL_AGENT_DEBUG=true for a detailed traceback.\n")
        return 1


def run_cli(
    settings: Settings,
    services: ServiceAdapter,
    stdin: TextIO,
    stdout: TextIO,
) -> int:
    """Run the terminal conversation using an injected service boundary."""
    stdout.write("Email Agent\n")
    status = services.status(settings.embedding_model)
    _write_status(stdout, status, settings.embedding_model)
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
            _write_status(
                stdout, services.status(settings.embedding_model), settings.embedding_model
            )
        elif command.startswith("/"):
            stdout.write(f"Unknown command: {command}. Type /help for supported commands.\n")
        else:
            stdout.write(
                "Chat is not configured in this bootstrap ticket. Type /help for commands.\n"
            )


def _write_status(stdout: TextIO, status: ServiceStatus, embedding_model: str) -> None:
    stdout.write(f"Qdrant: {'available' if status.qdrant_available else 'unavailable'}\n")
    stdout.write(f"Ollama: {'available' if status.ollama_available else 'unavailable'}\n")
    if not status.ollama_available:
        model_status = "unavailable (Ollama is unavailable)"
    else:
        model_status = "installed" if status.embedding_model_available else "missing"
    stdout.write(f"Embedding model {embedding_model}: {model_status}\n")


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
