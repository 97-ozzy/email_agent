"""Exercise the real gmail-agent MCP server over stdio without credentials or Gmail."""

from __future__ import annotations

import asyncio
import os
import sys
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_attachments import AvailableServices
from test_cli import ControlledGmail

from email_agent.archive import SQLiteArchive
from email_agent.cli import run_application
from email_agent.gmail import GmailAccessError, GmailMCPAdapter, GmailStatus


class SyntheticGmail:
    def search(
        self, query: str, max_results: int, page_token: str | None, include_spam_trash: bool
    ) -> dict[str, object]:
        return {"messages": [{"id": "live-id", "thread_id": "live-thread", "subject": query}]}

    def get_message(self, message_id: str, max_body_chars: int | None) -> dict[str, object]:
        body = "x" * 10000 + " Agreed to 45 EUR."
        if max_body_chars is not None:
            body = body[:max_body_chars]
        return {"id": message_id, "body": body, "body_truncated": max_body_chars is not None}

    def get_thread(self, thread_id: str, max_body_chars: int | None) -> dict[str, object]:
        if thread_id == "broken":
            raise RuntimeError("private correspondence diagnostic")
        return {"id": thread_id, "messages": [self.get_message("live-id", max_body_chars)]}


def server_parameters() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), "--serve"],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


class GmailMCPTests(TestCase):
    def test_cli_live_thread_works_without_initializing_local_rag(self) -> None:
        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            part = messages[-1].parts[0]
            if not isinstance(part, ToolReturnPart):
                return ModelResponse(
                    parts=[ToolCallPart("get_gmail_thread", {"thread_id": "live-thread"})]
                )
            assert isinstance(part.content, dict)
            self.assertEqual(part.content["source"], "live_gmail")
            body = part.content["data"]["messages"][0]["body"]
            self.assertTrue(body.endswith("Agreed to 45 EUR."))
            return ModelResponse(
                parts=[
                    TextPart("Live Gmail: agreed to 45 EUR [message:live-id] [thread:live-thread].")
                ]
            )

        with (
            TemporaryDirectory() as directory,
            patch("email_agent.cli.openrouter_model", return_value=FunctionModel(model)),
        ):
            stdout = StringIO()
            code = run_application(
                environ={
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                },
                stdin=StringIO("What is the latest agreement?\n/exit\n"),
                stdout=stdout,
                stderr=StringIO(),
                services_factory=lambda settings: AvailableServices(),
                gmail_factory=lambda: ControlledGmail(GmailStatus(True)),
                live_gmail_factory=lambda gmail: GmailMCPAdapter(
                    gmail.status, server=server_parameters()
                ),
                search_factory=lambda settings, archive: self.fail("Local RAG unavailable"),
            )
            self.assertEqual(code, 0)
            self.assertIn(
                "45 EUR https://mail.google.com/mail/u/0/#all/live-id"
                " https://mail.google.com/mail/u/0/#all/live-thread",
                stdout.getvalue(),
            )
            self.assertEqual(SQLiteArchive(Path(directory)).completed_message_ids(), set())

    def test_real_stdio_search_message_and_complete_thread(self) -> None:
        gmail = GmailMCPAdapter(lambda: GmailStatus(True), server=server_parameters())
        result = gmail.search_messages("Quarterly report")
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["messages"][0]["subject"], "Quarterly report")
        message = gmail.get_message("live-id")
        assert isinstance(message, dict)
        self.assertTrue(message["body"].endswith("Agreed to 45 EUR."))
        self.assertFalse(message["body_truncated"])
        thread = gmail.get_thread("live-thread")
        assert isinstance(thread, dict)
        self.assertEqual(thread["messages"][0], message)

    def test_server_errors_become_safe_unavailable_diagnostics(self) -> None:
        gmail = GmailMCPAdapter(lambda: GmailStatus(True), server=server_parameters())
        with self.assertRaisesRegex(GmailAccessError, "Gmail MCP read failed") as raised:
            gmail.get_thread("broken")
        self.assertNotIn("private correspondence", str(raised.exception))

    def test_real_server_exposes_read_only_tools(self) -> None:
        async def names() -> set[str]:
            with open(os.devnull, "w") as errlog:
                async with (
                    stdio_client(server_parameters(), errlog=errlog) as (read, write),
                    ClientSession(read, write, read_timeout_seconds=30) as session,
                ):
                    await session.initialize()
                    tools = await session.list_tools()
                    return {tool.name for tool in tools.tools}

        tools = asyncio.run(names())
        self.assertTrue({"search_messages", "get_message", "get_thread"}.issubset(tools))
        self.assertFalse(
            tools.intersection(
                {
                    "send_message",
                    "reply_to_message",
                    "forward_message",
                    "create_draft",
                    "update_draft",
                    "send_draft",
                    "delete_draft",
                    "create_label",
                    "rename_label",
                    "delete_label",
                    "modify_labels",
                    "mark_messages",
                    "trash",
                    "untrash",
                    "delete_permanently",
                }
            )
        )


if __name__ == "__main__" and "--serve" in sys.argv:
    from gmail_agent import config  # type: ignore[import-untyped]
    from gmail_agent.server import build_server  # type: ignore[import-untyped]

    build_server(connect=SyntheticGmail, scopes=config.LEVELS["readonly"], read_only=True).run()
