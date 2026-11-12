"""Read-only Gmail boundary used by the application."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from typing import Any, Literal, Protocol, cast

READONLY_LEVEL = "readonly"
ReadCommand = Literal["search", "message", "thread"]


class GmailAccessError(RuntimeError):
    """Raised when a read-only Gmail operation cannot be completed."""


class CommandRunner(Protocol):
    """Execute one gmail-agent command."""

    def __call__(self, command: list[str], capture_output: bool) -> CompletedProcess[str]: ...


class DiscoveryRunner(Protocol):
    """List Gmail message IDs without downloading message contents."""

    def __call__(
        self,
        query: str,
        max_results: int,
        page_token: str | None,
    ) -> object: ...


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

    def list_messages(
        self,
        query: str,
        *,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        """Retrieve one newest-first page for deterministic ingestion."""

    def get_message(self, message_id: str) -> object:
        """Retrieve one Gmail message."""

    def get_thread(self, thread_id: str) -> object:
        """Retrieve one Gmail conversation."""

class GmailAgentAdapter:
    """Adapter for the open-source gmail-agent integration."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        discovery_runner: DiscoveryRunner | None = None,
    ) -> None:
        self._runner = runner or _run_command
        self._discovery_runner = discovery_runner or _list_message_ids

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

    def list_messages(
        self,
        query: str,
        *,
        max_results: int,
        page_token: str | None = None,
    ) -> object:
        return self._discovery_runner(query, max_results, page_token)

    def get_message(self, message_id: str) -> object:
        return self._run_json_command(
            ["gmail-agent", "message", message_id, "--json", "--max-chars", "0"],
            "message",
        )

    def get_thread(self, thread_id: str) -> object:
        return self._read_json("thread", thread_id)

    def list_attachments(self, message_id: str) -> object:
        return self._run_json_command(
            ["gmail-agent", "attachments", message_id, "--json"],
            "attachment listing",
        )

    def download_attachment(self, message_id: str, part_id: str) -> bytes:
        with TemporaryDirectory() as directory:
            payload = self._run_json_command(
                [
                    "gmail-agent",
                    "download",
                    message_id,
                    "--json",
                    "--out-dir",
                    directory,
                    "--name",
                    part_id,
                ],
                "attachment download",
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("saved"), list):
                raise GmailAccessError("Gmail attachment download returned an invalid response")
            saved = payload["saved"]
            if len(saved) != 1 or not isinstance(saved[0], dict):
                raise GmailAccessError("Gmail attachment download returned an invalid file list")
            raw_path = saved[0].get("path")
            if not isinstance(raw_path, str):
                raise GmailAccessError("Gmail attachment download returned an invalid path")
            path = Path(raw_path).resolve()
            root = Path(directory).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise GmailAccessError("Gmail attachment download returned an unsafe path")
            return path.read_bytes()

    def _status_details(self) -> dict[str, object]:
        result = self._runner(["gmail-agent", "status", "--json"], True)
        payload = _decode_json(result.stdout)
        if isinstance(payload, dict):
            return cast(dict[str, object], payload)
        return {"problem": "gmail-agent returned an invalid status response"}

    def _read_json(self, operation: ReadCommand, argument: str) -> object:
        return self._run_json_command(["gmail-agent", operation, argument, "--json"], operation)

    def _run_json_command(self, command: list[str], operation: str) -> object:
        result = self._runner(command, True)
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
            encoding="utf-8",
            env=environment,
            errors="strict",
            text=True,
        )
    except FileNotFoundError:
        return CompletedProcess(command, 127, stdout="", stderr="gmail-agent is not installed")


def _list_message_ids(
    query: str,
    max_results: int,
    page_token: str | None,
) -> object:
    """Use gmail-agent's saved credentials for an IDs-only Gmail API request."""
    try:
        from gmail_agent import auth  # type: ignore[import-untyped]

        credentials = auth.load_credentials()
        service: Any = auth.build_service(credentials)
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max(1, min(max_results, 100)),
                pageToken=page_token or None,
            )
            .execute(num_retries=2)
        )
    except Exception as exc:
        raise GmailAccessError(
            "Gmail message discovery failed; run `gmail-agent status` for details"
        ) from exc

    if not isinstance(response, dict):
        raise GmailAccessError("Gmail message discovery returned an invalid response")
    raw_messages = response.get("messages", [])
    messages = []
    if isinstance(raw_messages, list):
        messages = [
            {"id": item["id"]}
            for item in raw_messages
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    return {
        "query": query,
        "messages": messages,
        "next_page_token": response.get("nextPageToken"),
    }


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
