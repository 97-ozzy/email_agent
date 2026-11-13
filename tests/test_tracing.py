from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from time import monotonic
from typing import cast
from unittest import TestCase

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from qdrant_client import QdrantClient
from test_search import ControlledEmbedding, archived_message

from email_agent.archive import SQLiteArchive
from email_agent.chat import ChatError, ChatSession, LocalArchive
from email_agent.config import ConfigurationError, Settings
from email_agent.gmail import GmailMCPAdapter, GmailStatus
from email_agent.retrieval import HybridSearchIndex
from email_agent.search import SearchRequest
from email_agent.tracing import Tracing, stage


class TracingTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(
            "sk-or-test-secret",
            "model",
            data_dir=Path(directory.name),
            langfuse_enabled=True,
        )
        self.exporter = InMemorySpanExporter()
        self.tracing = Tracing(self.settings, exporter=self.exporter)
        self.addCleanup(self.tracing.close)

    def spans(self) -> tuple[ReadableSpan, ...]:
        self.tracing.flush()
        return self.exporter.get_finished_spans()

    def root(self) -> ReadableSpan:
        return next(s for s in self.spans() if s.parent is None)

    def chat(
        self,
        function: Callable[[list[ModelMessage], AgentInfo], ModelResponse] | None = None,
        *,
        gmail: GmailMCPAdapter | None = None,
    ) -> ChatSession:
        def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart("answer")])

        return ChatSession(
            self.settings,
            cast(LocalArchive, None),
            tracing=self.tracing,
            gmail=gmail,
            model=FunctionModel(function or answer),
        )

    def test_turns_share_session_and_identity_but_have_unique_roots(self) -> None:
        chat = self.chat()
        self.assertEqual(chat.ask("private prompt"), "answer")
        chat.clear()
        chat.ask("second private prompt")
        spans = self.spans()
        roots = [s for s in spans if s.parent is None]
        self.assertEqual([s.name for s in roots], ["email_agent.ask", "email_agent.ask"])
        self.assertEqual(len({(s.attributes or {})["session.id"] for s in roots}), 1)
        self.assertEqual(len({(s.attributes or {})["user.id"] for s in roots}), 1)
        self.assertEqual(len({(s.attributes or {})["email_agent.turn_id"] for s in roots}), 2)
        self.assertNotIn("private prompt", "\n".join(s.to_json() for s in spans))
        self.assertEqual(len(chat.history), 2)
        second = Tracing(self.settings, exporter=InMemorySpanExporter())
        self.addCleanup(second.close)
        self.assertEqual(self.tracing.user_id, second.user_id)
        self.assertEqual(self.tracing.pseudonym("doc"), second.pseudonym("doc"))
        self.assertNotEqual(self.tracing.user_id, self.tracing.document_key)
        self.assertNotIn(self.tracing.document_key, "\n".join(s.to_json() for s in spans))
        self.assertNotEqual(chat.session_id, self.chat().session_id)

    def test_disabled_and_independent_operations_do_not_trace(self) -> None:
        disabled = Tracing(replace(self.settings, langfuse_enabled=False), exporter=self.exporter)
        ChatSession(
            self.settings,
            cast(LocalArchive, None),
            tracing=disabled,
            model=FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")])),
        ).ask("private prompt")
        with stage("independent_command"):
            pass
        self.assertEqual(self.spans(), ())

    def test_real_retrieval_and_native_tool_parentage_preserve_evidence(self) -> None:
        archive = SQLiteArchive(self.settings.data_dir)
        archive.store_message(archived_message("private-id", "Private invoice total: 42 EUR."))
        client = QdrantClient(":memory:")
        self.addCleanup(client.close)
        index = HybridSearchIndex(
            self.settings,
            archive,
            client=client,
            embed_model=ControlledEmbedding(),
        )
        calls = 0

        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "search_email_rag",
                            {
                                "query": "Private invoice",
                                "sender": "person@example.com",
                            },
                        )
                    ]
                )
            if calls == 2:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "get_local_document",
                            {
                                "document_id": "email:private-id",
                                "chunk_index": 0,
                            },
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("Private answer")])

        chat = ChatSession(self.settings, index, model=FunctionModel(model), tracing=self.tracing)
        self.assertEqual(chat.ask("Private question"), "Private answer")
        spans = self.spans()
        by_id = {s.context.span_id: s for s in spans if s.context}
        for name, tool in [
            ("archive.refresh", "search_email_rag"),
            ("rag.hybrid_search", "search_email_rag"),
            ("document.context", "get_local_document"),
        ]:
            child = next(s for s in spans if s.name == name)
            assert child.parent is not None
            self.assertIn(tool, by_id[child.parent.span_id].name)
        self.assertEqual(len([s for s in spans if s.parent is None]), 1)
        self.assertEqual(
            len(
                [
                    s
                    for s in spans
                    if (s.attributes or {}).get("langfuse.observation.type") == "generation"
                ]
            ),
            3,
        )
        exported = "\n".join(s.to_json() for s in spans)
        for private in ["Private", "person@example.com", "email:private-id"]:
            self.assertNotIn(private, exported)
        index.refresh()
        index.search(SearchRequest("Private invoice"))
        self.assertEqual(len(self.spans()), len(spans))

    def test_gmail_context_survives_native_worker_and_asyncio_run(self) -> None:
        class AsyncGmail(GmailMCPAdapter):
            async def _call(self, name: str, arguments: dict[str, object]) -> object:
                await asyncio.sleep(0)
                with stage("gmail.async_boundary"):
                    return {"messages": [{"id": "private-id", "body": "private mail"}]}

        gmail = AsyncGmail(lambda: GmailStatus(True, account="private@example.com"))
        calls = 0

        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls <= 3:
                name, argument = [
                    ("search_gmail", {"query": "private query"}),
                    ("get_gmail_message", {"message_id": "private-id"}),
                    ("get_gmail_thread", {"thread_id": "private-thread"}),
                ][calls - 1]
                return ModelResponse(parts=[ToolCallPart(name, argument)])
            return ModelResponse(parts=[TextPart("answer")])

        self.chat(model, gmail=gmail).ask("private question")
        spans = self.spans()
        root = self.root()
        self.assertEqual((root.attributes or {})["email_agent.outcome"], "successful")
        self.assertEqual(len([s for s in spans if s.parent is None]), 1)
        by_id = {s.context.span_id: s for s in spans if s.context}
        async_spans = [s for s in spans if s.name == "gmail.async_boundary"]
        self.assertEqual(len(async_spans), 3)
        for span in async_spans:
            assert span.parent is not None
            operation = by_id[span.parent.span_id]
            self.assertTrue(operation.name.startswith("gmail."))
            assert operation.parent is not None
            self.assertIn("tool", by_id[operation.parent.span_id].name)
            self.assertEqual(span.context.trace_id, root.context.trace_id)
        self.assertNotIn("private", "\n".join(s.to_json() for s in spans))

    def test_outcomes_preserve_failure_and_limit_history(self) -> None:
        gmail = GmailMCPAdapter(lambda: GmailStatus(False))
        chat = self.chat(gmail=gmail)
        self.assertIn("Gmail unavailable", chat.ask("question"))
        self.assertEqual((self.root().attributes or {})["email_agent.outcome"], "partial")
        self.exporter.clear()

        def limited(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise UsageLimitExceeded("private limit error")

        chat = self.chat(limited, gmail=gmail)
        self.assertIn("Investigation limit reached", chat.ask("question"))
        attrs = self.root().attributes or {}
        self.assertEqual(attrs["email_agent.outcome"], "limit_exceeded")
        self.assertTrue(attrs["email_agent.gmail_unavailable"])
        self.assertTrue(attrs["email_agent.limit_reached"])
        self.assertEqual(len(chat.history), 2)
        self.exporter.clear()

        def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise ValueError("private exception text")

        chat = self.chat(broken, gmail=gmail)
        with self.assertRaises(ChatError):
            chat.ask("private question")
        self.assertEqual((self.root().attributes or {})["email_agent.outcome"], "error")
        self.assertEqual(chat.history, [])
        self.assertNotIn("private", "\n".join(s.to_json() for s in self.spans()))

    def test_export_boundary_cleans_events_status_and_secrets_in_debug_mode(self) -> None:
        settings = replace(self.settings, langfuse_capture_content=True)
        tracing = Tracing(settings, exporter=self.exporter)
        self.addCleanup(tracing.close)
        with (
            tracing.turn("session", "visible prompt sk-or-test-secret"),
            stage("test.stage") as span,
        ):
            span.set_attribute("arbitrary", "private filter value")
            span.set_attribute(
                "gen_ai.tool.call.result",
                json.dumps(
                    {
                        "text": "visible tool text",
                        "access_token": "private credential",
                        "error": "private exception message",
                        "media": {"type": "blob", "content": "private bytes"},
                    }
                ),
            )
            span.add_event("private event", {"body": "private message"})
            span.record_exception(ValueError("private exception message"))
            span.set_status(Status(StatusCode.ERROR, "private error status"))
        tracing.flush()
        spans = self.exporter.get_finished_spans()
        exported = "\n".join(s.to_json() for s in spans)
        self.assertIn("visible prompt", exported)
        self.assertIn("visible tool text", exported)
        self.assertNotIn("private", exported)
        self.assertNotIn("sk-or-test-secret", exported)
        self.assertTrue(any(s.events for s in spans))
        for exported_span in spans:
            self.assertIsNone(exported_span.status.description)
            for event in exported_span.events:
                self.assertEqual(
                    dict(event.attributes or {}), {"exception.type": "operation_error"}
                )

    def test_concurrent_sessions_keep_context_isolated(self) -> None:
        barrier = Barrier(2)

        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            barrier.wait(timeout=5)
            with stage("concurrent.work"):
                return ModelResponse(parts=[TextPart("answer")])

        chats = [self.chat(model), self.chat(model)]

        def ask(chat: ChatSession) -> str:
            try:
                return chat.ask("question")
            finally:
                asyncio.get_event_loop().close()
                asyncio.set_event_loop(None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(ask, chats)), ["answer", "answer"])
        roots = [s for s in self.spans() if s.parent is None]
        self.assertEqual(len(roots), 2)
        self.assertEqual(len({(s.attributes or {})["session.id"] for s in roots}), 2)
        for root in roots:
            assert root.context is not None
            children = [
                s for s in self.spans() if s.context and s.context.trace_id == root.context.trace_id
            ]
            self.assertGreaterEqual(len(children), 4)
            self.assertTrue(
                all(
                    (s.attributes or {})["session.id"] == (root.attributes or {})["session.id"]
                    for s in children
                )
            )

    def test_exporter_failure_and_bounded_shutdown_do_not_break_chat(self) -> None:
        release = Event()

        class BlockedExporter(SpanExporter):
            def export(self, spans: object) -> SpanExportResult:
                release.wait(timeout=10)
                raise RuntimeError("private server failure")

        tracing = Tracing(self.settings, exporter=BlockedExporter())
        try:
            chat = ChatSession(
                self.settings,
                cast(LocalArchive, None),
                tracing=tracing,
                model=FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")])),
            )
            self.assertEqual(chat.ask("question"), "ok")
            started = monotonic()
            tracing.close()
            self.assertLess(monotonic() - started, 2.5)
        finally:
            release.set()

    def test_openrouter_generations_export_actual_cost_and_usage_only_once(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "id": "private-provider-id",
                        "model": "openai/gpt-4.1-mini",
                        "object": "chat.completion",
                        "provider": "OpenAI",
                        "created": 1,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "private answer"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18,
                            "cost": 0.00042,
                            "completion_tokens_details": {"reasoning_tokens": 3},
                        },
                    },
                )
            )
        )
        model = OpenRouterModel(
            "openai/gpt-4.1-mini",
            provider=OpenRouterProvider(
                api_key=self.settings.openrouter_api_key,
                http_client=client,
            ),
        )
        try:
            chat = ChatSession(
                self.settings, cast(LocalArchive, None), model=model, tracing=self.tracing
            )
            self.assertEqual(chat.ask("private prompt"), "private answer")
        finally:
            asyncio.get_event_loop().run_until_complete(client.aclose())
        spans = self.spans()
        generations = [
            s
            for s in spans
            if (s.attributes or {}).get("langfuse.observation.type") == "generation"
        ]
        self.assertEqual(len(generations), 1)
        attrs = generations[0].attributes or {}
        self.assertEqual(
            json.loads(str(attrs["langfuse.observation.usage_details"])),
            {"input": 11, "output": 7, "total": 18},
        )
        self.assertEqual(
            json.loads(str(attrs["langfuse.observation.cost_details"])), {"total": 0.00042}
        )
        for span in spans:
            if span not in generations:
                self.assertFalse(
                    any(
                        k.startswith("gen_ai.usage.") or "cost_details" in k
                        for k in (span.attributes or {})
                    )
                )
        self.assertNotIn("private", "\n".join(s.to_json() for s in spans))

    def test_settings_validate_enabled_tracing_without_echoing_secrets(self) -> None:
        environ = {"OPENROUTER_API_KEY": "secret", "OPENROUTER_MODEL": "model"}
        self.assertFalse(Settings.from_environ(environ).langfuse_enabled)
        with self.assertRaises(ConfigurationError) as caught:
            Settings.from_environ(
                {
                    **environ,
                    "LANGFUSE_ENABLED": "true",
                    "LANGFUSE_BASE_URL": "http://user:secret@host",
                }
            )
        self.assertIn("LANGFUSE_PUBLIC_KEY", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        configured = Settings.from_environ(
            {
                **environ,
                "LANGFUSE_ENABLED": "true",
                "LANGFUSE_PUBLIC_KEY": "pk",
                "LANGFUSE_SECRET_KEY": "sk",
                "LANGFUSE_CAPTURE_CONTENT": "true",
            }
        )
        self.assertTrue(configured.langfuse_enabled)
        self.assertTrue(configured.langfuse_capture_content)

    def test_failed_generation_keeps_error_status_without_exporting_provider_body(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    404,
                    json={"error": {"message": "private provider exception", "code": 404}},
                )
            )
        )
        model = OpenRouterModel(
            "openai/gpt-4.1-mini",
            provider=OpenRouterProvider(
                api_key=self.settings.openrouter_api_key,
                http_client=client,
            ),
        )
        try:
            chat = ChatSession(
                self.settings, cast(LocalArchive, None), model=model, tracing=self.tracing
            )
            with self.assertRaises(ChatError):
                chat.ask("private prompt")
        finally:
            asyncio.get_event_loop().run_until_complete(client.aclose())
        generations = [
            s
            for s in self.spans()
            if (s.attributes or {}).get("langfuse.observation.type") == "generation"
        ]
        self.assertEqual(len(generations), 1)
        self.assertEqual(generations[0].status.status_code, StatusCode.ERROR)
        self.assertNotIn("private", "\n".join(s.to_json() for s in self.spans()))
        self.assertNotIn("langfuse.observation.cost_details", generations[0].attributes or {})

    def test_caught_source_failure_and_exhausted_budget_are_reported(self) -> None:
        class FailedGmail(GmailMCPAdapter):
            async def _call(self, name: str, arguments: dict[str, object]) -> object:
                raise ValueError("private Gmail API failure")

        gmail = FailedGmail(lambda: GmailStatus(True))
        calls = 0

        def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            return (
                ModelResponse(parts=[ToolCallPart("search_gmail", {"query": "private"})])
                if (calls == 1)
                else ModelResponse(parts=[TextPart("answer")])
            )

        settings = replace(self.settings, tool_call_limit=1)
        chat = ChatSession(
            settings,
            cast(LocalArchive, None),
            tracing=self.tracing,
            gmail=gmail,
            model=FunctionModel(model),
        )
        answer = chat.ask("private question")
        self.assertIn("Gmail unavailable", answer)
        self.assertIn("tool-call limit reached", answer)
        attrs = self.root().attributes or {}
        self.assertEqual(attrs["email_agent.outcome"], "limit_exceeded")
        self.assertTrue(attrs["email_agent.gmail_unavailable"])
        operation = next(s for s in self.spans() if s.name == "gmail.search_messages")
        self.assertEqual(operation.status.status_code, StatusCode.ERROR)
        self.assertNotIn("private", "\n".join(s.to_json() for s in self.spans()))

    def test_debug_redacts_credentials_inside_serialized_tool_payloads(self) -> None:
        tracing = Tracing(
            replace(self.settings, langfuse_capture_content=True), exporter=self.exporter
        )
        self.addCleanup(tracing.close)
        with tracing.turn("session", "visible question") as span:
            span.set_attribute(
                "gen_ai.tool.call.result",
                json.dumps(
                    {
                        "response": json.dumps(
                            {"access_token": "opaque-private-token", "text": "visible evidence"}
                        ),
                        "text": (
                            "refresh_token=opaque-private-refresh "
                            "authorization: Bearer opaque-secret"
                        ),
                    }
                ),
            )
        tracing.flush()
        exported = "\n".join(s.to_json() for s in self.exporter.get_finished_spans())
        self.assertIn("visible evidence", exported)
        self.assertNotIn("opaque-", exported)

    def test_missing_gmail_adapter_marks_existing_unavailability_answer_partial(self) -> None:
        chat = self.chat(
            lambda messages, info: ModelResponse(
                parts=[TextPart("Gmail unavailable; answer from local evidence.")]
            )
        )
        answer = chat.ask("question")
        self.assertEqual(answer, "Gmail unavailable; answer from local evidence.")
        attrs = self.root().attributes or {}
        self.assertEqual(attrs["email_agent.outcome"], "partial")
        self.assertTrue(attrs["email_agent.gmail_unavailable"])
