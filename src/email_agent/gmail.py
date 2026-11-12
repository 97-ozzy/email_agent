"""Read-only Gmail boundary used by the application."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Literal, Protocol, cast

READONLY_LEVEL = "readonly"
ReadCommand = Literal["search", "message", "thread"]


class GmailAccessError(RuntimeError):
    """Raised when a read-only Gmail operation cannot be completed."""


class CommandRunner(Protocol):
    """Execute one gmail-agent command."""

    def __call__(self, command: list[str], capture_output: bool) -> CompletedProcess[str]: ...


@dataclass(frozen=True)
class GmailStatus:
    """Observable Gmail availability without exposing credential details."""

    available: bool
    account: str | None = None
    problem: str | None = None


class GmailAdapter(Protocol):
    """Read-only Gmail operations exposed to the application."""

    def ensure_authenticated(self) -> GmailStatus:
        """Reuse, refresh, or acquire read-only Gmail credentials."""

    def status(self) -> GmailStatus:
        """Return current Gmail availability."""

    def search_messages(self, query: str) -> object:
        """Search Gmail messages using Gmail query syntax."""

    def get_message(self, message_id: str) -> object:
        """Retrieve one Gmail message."""

    def get_thread(self, thread_id: str) -> object:
        """Retrieve one Gmail conversation."""


class GmailAgentAdapter:
    """Adapter for the open-source gmail-agent integration."""

    def __init__(self, *, runner: CommandRunner | None = None) -> None:
        self._runner = runner or _run_command

    def ensure_authenticated(self) -> GmailStatus:
        details = self._status_details()
        current = _status_from_details(details)
        if current.available:
            return current

        if details.get("token_file") is True and details.get("logged_in") is not True:
            return current

        command = "login" if details.get("credentials_file") else "setup"
        result = self._runner(
            ["gmail-agent", command, "--scope", READONLY_LEVEL],
            False,
        )
        if result.returncode != 0:
            return GmailStatus(
                available=False,
                problem=(
                    "Gmail authorization did not complete; run "
                    f"`gmail-agent {command} --scope readonly`"
                ),
            )
        return self.status()

    def status(self) -> GmailStatus:
        return _status_from_details(self._status_details())

    def search_messages(self, query: str) -> object:
        return self._read_json("search", query)

    def get_message(self, message_id: str) -> object:
        return self._read_json("message", message_id)

    def get_thread(self, thread_id: str) -> object:
        return self._read_json("thread", thread_id)

    def _status_details(self) -> dict[str, object]:
        result = self._runner(["gmail-agent", "status", "--json"], True)
        payload = _decode_json(result.stdout)
        if isinstance(payload, dict):
            return cast(dict[str, object], payload)
        return {"problem": "gmail-agent returned an invalid status response"}

    def _read_json(self, operation: ReadCommand, argument: str) -> object:
        result = self._runner(["gmail-agent", operation, argument, "--json"], True)
        payload = _decode_json(result.stdout)
        if result.returncode != 0:
            raise GmailAccessError(_safe_error(payload, operation))
        return payload


def _run_command(command: list[str], capture_output: bool) -> CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        return subprocess.run(  # noqa: S603 - fixed executable and read-only command vocabulary
            command,
            check=False,
            capture_output=capture_output,
            env=environment,
            text=True,
        )
    except FileNotFoundError:
        return CompletedProcess(command, 127, stdout="", stderr="gmail-agent is not installed")


def _decode_json(text: str | None) -> object:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}


def _status_from_details(details: dict[str, object]) -> GmailStatus:
    if details.get("logged_in") is True and details.get("level") == READONLY_LEVEL:
        account = details.get("email") or details.get("address")
        return GmailStatus(available=True, account=account if isinstance(account, str) else None)

    if details.get("logged_in") is True:
        problem = "Gmail authorization is not read-only; run `gmail-agent login --scope readonly`"
    else:
        raw_problem = details.get("problem")
        problem = (
            _redact_secrets(raw_problem)
            if isinstance(raw_problem, str)
            else "Gmail is not authenticated with read-only access"
        )
    return GmailStatus(available=False, problem=problem)


def _safe_error(payload: object, operation: str) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return _redact_secrets(cast(str, payload["error"]))
    return f"Gmail {operation} failed; run `gmail-agent status` for details"


def _redact_secrets(message: str) -> str:
    message = re.sub(
        r"(?i)\b(access_token|refresh_token|token|authorization)\s*[:=]\s*[^\s;,]+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"\b(?:ya29\.|GOCSPX-)[A-Za-z0-9._-]+", "[redacted]", message)
    return message
