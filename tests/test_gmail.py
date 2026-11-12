from __future__ import annotations

import json
from collections.abc import Mapping
from subprocess import CompletedProcess
from unittest import TestCase
from unittest.mock import patch

from email_agent.gmail import GmailAccessError, GmailAgentAdapter


class ScriptedRunner:
    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, command: list[str], capture_output: bool) -> CompletedProcess[str]:
        self.calls.append((command, capture_output))
        return_code, payload = self.responses.pop(0)
        stdout = json.dumps(payload) if capture_output else ""
        return CompletedProcess(command, return_code, stdout=stdout, stderr="")


class ScriptedDiscovery:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, int, str | None]] = []

    def __call__(
        self,
        query: str,
        max_results: int,
        page_token: str | None,
    ) -> object:
        self.calls.append((query, max_results, page_token))
        return self.response


class GmailAgentAuthenticationTests(TestCase):
    def test_existing_readonly_login_is_reused(self) -> None:
        runner = ScriptedRunner(
            [(0, {"logged_in": True, "level": "readonly", "email": "reader@example.com"})]
        )

        status = GmailAgentAdapter(runner=runner).ensure_authenticated()

        self.assertTrue(status.available)
        self.assertEqual(status.account, "reader@example.com")
        self.assertEqual(runner.calls, [(["gmail-agent", "status", "--json"], True)])

    def test_missing_token_starts_readonly_browser_login(self) -> None:
        runner = ScriptedRunner(
            [
                (0, {"logged_in": False, "credentials_file": True}),
                (0, {}),
                (
                    0,
                    {"logged_in": True, "level": "readonly", "email": "reader@example.com"},
                ),
            ]
        )

        status = GmailAgentAdapter(runner=runner).ensure_authenticated()

        self.assertTrue(status.available)
        self.assertIn(
            (["gmail-agent", "login", "--scope", "readonly"], False), runner.calls
        )

    def test_missing_oauth_client_starts_readonly_setup(self) -> None:
        runner = ScriptedRunner(
            [
                (0, {"logged_in": False, "credentials_file": False}),
                (0, {}),
                (
                    0,
                    {"logged_in": True, "level": "readonly", "email": "reader@example.com"},
                ),
            ]
        )

        status = GmailAgentAdapter(runner=runner).ensure_authenticated()

        self.assertTrue(status.available)
        self.assertIn(
            (["gmail-agent", "setup", "--scope", "readonly"], False), runner.calls
        )

    def test_non_readonly_login_is_replaced_with_readonly_login(self) -> None:
        runner = ScriptedRunner(
            [
                (0, {"logged_in": True, "credentials_file": True, "level": "modify"}),
                (0, {}),
                (
                    0,
                    {"logged_in": True, "level": "readonly", "email": "reader@example.com"},
                ),
            ]
        )

        status = GmailAgentAdapter(runner=runner).ensure_authenticated()

        self.assertTrue(status.available)
        self.assertIn(
            (["gmail-agent", "login", "--scope", "readonly"], False), runner.calls
        )

    def test_existing_failed_login_preserves_actionable_problem_without_reauthorizing(self) -> None:
        problem = (
            "Could not reach Google to refresh the token; check the network and retry status"
        )
        runner = ScriptedRunner(
            [
                (
                    1,
                    {
                        "logged_in": False,
                        "credentials_file": True,
                        "token_file": True,
                        "problem": problem,
                    },
                )
            ]
        )

        status = GmailAgentAdapter(runner=runner).ensure_authenticated()

        self.assertFalse(status.available)
        self.assertEqual(status.problem, problem)
        self.assertEqual(runner.calls, [(["gmail-agent", "status", "--json"], True)])


class GmailAgentReadOperationsTests(TestCase):
    def test_paginated_message_discovery_uses_read_only_search_options(self) -> None:
        discovery = ScriptedDiscovery(
            {"messages": [{"id": "message-2"}], "next_page_token": "page-3"}
        )

        result = GmailAgentAdapter(discovery_runner=discovery).list_messages(
            "in:inbox OR in:sent",
            max_results=40,
            page_token="page-2",
        )

        self.assertEqual(
            result,
            {"messages": [{"id": "message-2"}], "next_page_token": "page-3"},
        )
        self.assertEqual(
            discovery.calls,
            [("in:inbox OR in:sent", 40, "page-2")],
        )

    def test_search_handles_unicode_not_supported_by_windows_console_encoding(self) -> None:
        def encoding_sensitive_process(
            command: list[str],
            **kwargs: object,
        ) -> CompletedProcess[str]:
            environment = kwargs.get("env")
            if not isinstance(environment, Mapping) or environment.get(
                "PYTHONIOENCODING"
            ) != "utf-8":
                return CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=(
                        "UnicodeEncodeError: 'charmap' codec can't encode "
                        "character '\\u200c'"
                    ),
                )
            return CompletedProcess(
                command,
                0,
                stdout='{"messages": [{"subject": "Ozon\u200c"}]}',
                stderr="",
            )

        with patch("email_agent.gmail.subprocess.run", side_effect=encoding_sensitive_process):
            result = GmailAgentAdapter().search_messages("Ozon")

        self.assertEqual(result, {"messages": [{"subject": "Ozon\u200c"}]})

    def test_only_read_commands_are_exposed(self) -> None:
        runner = ScriptedRunner(
            [
                (0, {"messages": [{"id": "message-1"}]}),
                (0, {"id": "message-1", "body": "hello"}),
                (0, {"id": "thread-1", "messages": []}),
            ]
        )
        gmail = GmailAgentAdapter(runner=runner)

        gmail.search_messages("from:reader@example.com")
        gmail.get_message("message-1")
        gmail.get_thread("thread-1")

        self.assertEqual(
            runner.calls,
            [
                (
                    [
                        "gmail-agent",
                        "search",
                        "from:reader@example.com",
                        "--json",
                    ],
                    True,
                ),
                (["gmail-agent", "message", "message-1", "--json"], True),
                (["gmail-agent", "thread", "thread-1", "--json"], True),
            ],
        )

    def test_api_errors_are_actionable_without_exposing_tokens(self) -> None:
        runner = ScriptedRunner(
            [
                (
                    2,
                    {
                        "error": (
                            "Google rejected access_token=top-secret; "
                            "run gmail-agent login --scope readonly"
                        )
                    },
                )
            ]
        )

        with self.assertRaises(GmailAccessError) as caught:
            GmailAgentAdapter(runner=runner).search_messages("newer_than:7d")

        self.assertNotIn("top-secret", str(caught.exception))
        self.assertIn("gmail-agent login --scope readonly", str(caught.exception))
