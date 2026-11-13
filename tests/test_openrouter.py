from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import cast
from unittest import TestCase
from unittest.mock import patch

import httpx
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from email_agent.chat import ChatError, ChatSession, LocalArchive, openrouter_model
from email_agent.config import Settings


class OpenRouterTests(TestCase):
    def session(self, handler: Callable[[httpx.Request], httpx.Response]) -> ChatSession:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                handler,
            )
        )
        self.addCleanup(lambda: asyncio.get_event_loop().run_until_complete(client.aclose()))
        model = OpenRouterModel(
            "openai/gpt-4.1-mini",
            provider=OpenRouterProvider(
                api_key="sk-or-v1-test-secret",
                http_client=client,
            ),
        )
        # A greeting must not touch retrieval; only the real model/HTTP boundary is exercised.
        return ChatSession(
            Settings("sk-or-v1-test-secret", "openai/gpt-4.1-mini"),
            cast(LocalArchive, None),
            model=model,
        )

    def test_greeting_routes_to_tool_endpoint_without_optional_parallel_parameter(self) -> None:
        def endpoint(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(payload["model"], "openai/gpt-4.1-mini")
            self.assertEqual(payload["provider"], {"require_parameters": True})
            self.assertEqual(payload["tool_choice"], "auto")
            self.assertEqual(
                {t["function"]["name"] for t in payload["tools"]},
                {
                    "search_email_rag",
                    "get_local_document",
                    "search_gmail",
                    "get_gmail_message",
                    "get_gmail_thread",
                },
            )
            if "parallel_tool_calls" in payload:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "message": (
                                "No endpoints found that can handle the requested parameters."
                            ),
                            "code": 404,
                            "metadata": {"failed_routing_step": "Filter by Parameters"},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "provider": "OpenAI",
                    "created": 0,
                    "model": "openai/gpt-4.1-mini",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Hello!"},
                        }
                    ],
                },
            )

        self.assertEqual(self.session(endpoint).ask("Hello"), "Hello!")

    def test_model_http_error_displays_sanitized_error_body(self) -> None:
        def endpoint(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "message": "No endpoints for Bearer sk-or-v1-test-secret",
                        "code": 404,
                        "metadata": {
                            "failed_routing_step": "Filter by Parameters",
                            "raw": "private request dump",
                        },
                    }
                },
            )

        with self.assertRaises(ChatError) as caught:
            self.session(endpoint).ask("Hello")
        diagnostic = str(caught.exception)
        self.assertIn("404", diagnostic)
        self.assertIn("No endpoints", diagnostic)
        self.assertIn("Filter by Parameters", diagnostic)
        self.assertIn("<REDACTED>", diagnostic)
        self.assertNotIn("sk-or-v1-test-secret", diagnostic)
        self.assertNotIn("private request dump", diagnostic)

    def test_catalog_model_without_available_tool_endpoint_is_rejected(self) -> None:
        for endpoints in (
            [],
            [{"status": 1, "supported_parameters": ["tools", "tool_choice"]}],
            [{"status": 0, "supported_parameters": ["temperature"]}],
        ):

            def response(
                url: str,
                *,
                endpoint_data: object = endpoints,
                **kwargs: object,
            ) -> httpx.Response:
                data: object = [{"id": "openai/gpt-4.1-mini", "supported_parameters": ["tools"]}]
                if url.endswith("/endpoints"):
                    data = {"endpoints": endpoint_data}
                return httpx.Response(200, request=httpx.Request("GET", url), json={"data": data})

            with (
                self.subTest(endpoints=endpoints),
                patch("httpx.get", side_effect=response),
                self.assertRaisesRegex(ChatError, "no available endpoint.*tool"),
            ):
                openrouter_model(Settings("key", "openai/gpt-4.1-mini"))
