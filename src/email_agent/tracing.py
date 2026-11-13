"""Turn-scoped OpenTelemetry tracing with an allowlisted export boundary.

Native PydanticAI instrumentation owns model/tool spans. No global provider is installed.
The standard Langfuse OTLP endpoint accepts these spans without a second model client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from threading import Thread
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode
from opentelemetry.util.types import AttributeValue
from pydantic_ai import InstrumentationSettings
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters

from email_agent.config import Settings

_active: ContextVar[Tracing | None] = ContextVar("email_agent_tracing", default=None)
_turn: ContextVar[dict[str, str] | None] = ContextVar("email_agent_turn", default=None)

# Unknown attributes are dropped, including future library additions and provider dumps.
_SAFE = frozenset(
    {
        "session.id",
        "user.id",
        "langfuse.environment",
        "email_agent.turn_id",
        "gen_ai.operation.name",
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.tool.name",
        "langfuse.observation.type",
        "langfuse.observation.model.name",
        "langfuse.observation.usage_details",
        "langfuse.observation.cost_details",
        "email_agent.outcome",
        "email_agent.limit_reached",
        "email_agent.gmail_unavailable",
        "email_agent.source_degraded",
        "email_agent.status",
        "email_agent.error_type",
        "email_agent.backend",
        "email_agent.result_limit",
        "email_agent.results_returned",
        "email_agent.chunks_found",
        "email_agent.documents_changed",
        "email_agent.empty",
        "email_agent.filters",
        "email_agent.document_id",
        "email_agent.document_ids",
        "email_agent.messages_read",
        "email_agent.force",
        "email_agent.num_nodes",
    }
)
_CONTENT = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "pydantic_ai.all_messages",
        "final_result",
        "langfuse.trace.input",
        "langfuse.trace.output",
    }
)


class PrivacyExporter(SpanExporter):
    """Sanitize actual exported attributes, events, statuses and resources in every mode."""

    def __init__(self, exporter: SpanExporter, settings: Settings) -> None:
        self.exporter = exporter
        self.capture_content = settings.langfuse_capture_content
        self.secrets = tuple(
            s
            for s in (
                settings.openrouter_api_key,
                settings.langfuse_public_key,
                settings.langfuse_secret_key,
            )
            if s
        )

    def _redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        text = re.sub(
            r'(?i)(?:Bearer\s+[^\s"\\]+|sk-[\w-]+|ya29\.[\w.-]+|GOCSPX-[\w-]+)', "[redacted]", text
        )
        text = re.sub(
            r"(?i)\b(?:access_token|refresh_token|token|authorization|password|secret|api[_-]?key)"
            r"[\s\"']*[:=][\s\"']*(?:Bearer\s+)?[^\s,\"'<>;]+",
            "[redacted]",
            text,
        )
        return text

    def _content(self, value: AttributeValue) -> str:
        def clean(item: object) -> object:
            if isinstance(item, dict):
                if item.get("type") in {"blob", "image", "audio", "video", "uri"}:
                    return {"type": "redacted_media"}
                return {
                    str(k): "[redacted]"
                    if re.search(
                        r"(?i)token|secret|password|authorization|credential|api[_-]?key|exception|error|base64|binary",
                        str(k),
                    )
                    else clean(v)
                    for k, v in item.items()
                }
            if isinstance(item, list):
                return [clean(v) for v in item]
            if isinstance(item, str):
                try:
                    nested = json.loads(item)
                except (ValueError, TypeError):
                    return self._redact(item)
                if isinstance(nested, (dict, list)):
                    return json.dumps(clean(nested), ensure_ascii=False)
                return self._redact(item)
            return item

        raw = str(value)
        try:
            return json.dumps(clean(json.loads(raw)), ensure_ascii=False)
        except (ValueError, TypeError):
            return self._redact(raw)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            cleaned = []
            for span in spans:
                attributes = dict(span.attributes or {})
                safe: dict[str, AttributeValue] = {
                    k: self._redact(v) if isinstance(v, str) else v
                    for k, v in attributes.items()
                    if k in _SAFE or k.startswith("gen_ai.usage.")
                }
                generation = attributes.get("gen_ai.operation.name") == "chat"
                if generation:
                    safe["langfuse.observation.type"] = "generation"
                else:
                    safe = {k: v for k, v in safe.items() if not k.startswith("gen_ai.usage.")}
                if self.capture_content:
                    safe.update(
                        {k: self._content(v) for k, v in attributes.items() if k in _CONTENT}
                    )
                # Never forward arbitrary event names, exception messages, stacks or descriptions.
                events = [
                    Event("exception", {"exception.type": "operation_error"}, e.timestamp)
                    for e in span.events
                    if e.name == "exception"
                ]
                cleaned.append(
                    ReadableSpan(
                        name=self._redact(span.name),
                        context=span.context,
                        parent=span.parent,
                        resource=Resource({"service.name": "email-agent"}),
                        attributes=safe,
                        events=events,
                        kind=span.kind,
                        status=Status(span.status.status_code),
                        start_time=span.start_time,
                        end_time=span.end_time,
                        instrumentation_scope=span.instrumentation_scope,
                    )
                )
            return self.exporter.export(cleaned)
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        with suppress(Exception):
            self.exporter.shutdown()


class TurnAttributes(SpanProcessor):
    def __init__(self, environment: str) -> None:
        self.environment = environment

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        span.set_attribute("langfuse.environment", self.environment)
        for key, value in (_turn.get() or {}).items():
            span.set_attribute(key, value)


class ModelInstrumentation(InstrumentationSettings):
    """Enrich each native generation using actual provider usage; never sum reasoning twice."""

    def handle_messages(
        self,
        input_messages: list[ModelMessage],
        response: ModelResponse,
        system: str,
        span: trace.Span,
        parameters: ModelRequestParameters | None = None,
    ) -> None:
        super().handle_messages(input_messages, response, system, span, parameters)
        usage = response.usage
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps(
                {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "total": usage.input_tokens + usage.output_tokens,
                }
            ),
        )
        cost = (response.provider_details or {}).get("cost")
        if isinstance(cost, (int, float)) and cost >= 0:
            span.set_attribute("langfuse.observation.cost_details", json.dumps({"total": cost}))


class Tracing:
    def __init__(self, settings: Settings, *, exporter: SpanExporter | None = None) -> None:
        self.provider: TracerProvider | None = None
        self.settings = settings
        self.user_id = ""
        self.document_key = ""
        self.closed = False
        if not settings.langfuse_enabled:
            return
        try:
            self.user_id, self.document_key = _identity(settings.data_dir)
            if exporter is None:
                auth = base64.b64encode(
                    f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
                ).decode()
                exporter = OTLPSpanExporter(
                    endpoint=f"{settings.langfuse_base_url}/api/public/otel/v1/traces",
                    headers={"Authorization": f"Basic {auth}"},
                    timeout=1,
                )
            self.provider = TracerProvider(
                resource=Resource({"service.name": "email-agent"}),
                shutdown_on_exit=False,
            )
            self.provider.add_span_processor(TurnAttributes(settings.langfuse_environment))
            self.provider.add_span_processor(
                BatchSpanProcessor(
                    PrivacyExporter(exporter, settings),
                    max_queue_size=512,
                    max_export_batch_size=64,
                    schedule_delay_millis=500,
                    export_timeout_millis=1000,
                )
            )
        except Exception:
            self.provider = None

    def instrumentation(self) -> list[Instrumentation]:
        if self.provider is None:
            return []
        return [
            Instrumentation(
                settings=ModelInstrumentation(
                    tracer_provider=self.provider,
                    include_content=self.settings.langfuse_capture_content,
                    include_binary_content=False,
                    version=3,
                    use_aggregated_usage_attribute_names=True,
                )
            )
        ]

    @contextmanager
    def turn(self, session_id: str, question: str) -> Iterator[trace.Span]:
        if self.provider is None:
            yield trace.INVALID_SPAN
            return
        token = _active.set(self)
        attributes_token = _turn.set(
            {
                "session.id": session_id,
                "user.id": self.user_id,
                "email_agent.turn_id": str(uuid4()),
            }
        )
        try:
            # A chat turn is a root even when the caller has unrelated OTel context.
            with self.provider.get_tracer("email_agent").start_as_current_span(
                "email_agent.ask",
                context=Context(),
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                if self.settings.langfuse_capture_content:
                    span.set_attribute("langfuse.trace.input", question)
                yield span
        finally:
            _turn.reset(attributes_token)
            _active.reset(token)

    def pseudonym(self, identifier: str) -> str:
        return hmac.new(self.document_key.encode(), identifier.encode(), hashlib.sha256).hexdigest()

    def flush(self) -> None:
        if self.provider is not None:
            with suppress(Exception):
                self.provider.force_flush(timeout_millis=1000)

    def close(self) -> None:
        if self.provider is None or self.closed:
            return
        self.closed = True
        # SDK shutdown can wait for an exporter; bound the CLI's wait, including on failure.
        worker = Thread(target=self._shutdown, daemon=True, name="email-agent-telemetry-close")
        worker.start()
        worker.join(timeout=2)

    def _shutdown(self) -> None:
        try:
            self.flush()
            if self.provider is not None:
                self.provider.shutdown()
        except Exception:
            pass


def _identity(directory: Path) -> tuple[str, str]:
    # SQLite serializes creation across independent sessions/processes; no partial file reads.
    import sqlite3
    from contextlib import closing

    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "tracing-identity.sqlite3")) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, user TEXT, key TEXT)"
        )
        db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?, ?)", (str(uuid4()), str(uuid4())))
        user, key = db.execute("SELECT user, key FROM identity WHERE id=1").fetchone()
        return str(UUID(user)), str(UUID(key))


@contextmanager
def stage(name: str, **metadata: AttributeValue) -> Iterator[trace.Span]:
    tracing = _active.get()
    if tracing is None or tracing.provider is None:
        yield trace.INVALID_SPAN
        return
    with tracing.provider.get_tracer("email_agent").start_as_current_span(
        name,
        record_exception=False,
        set_status_on_exception=False,
        attributes={f"email_agent.{k}": v for k, v in metadata.items()},
    ) as span:
        try:
            yield span
        except Exception:
            failure(span)
            raise
        else:
            if not isinstance(span, Span) or span.status.status_code != StatusCode.ERROR:
                span.set_attribute("email_agent.status", "successful")
                span.set_status(StatusCode.OK)


def failure(span: trace.Span, code: str = "operation_error") -> None:
    span.set_attribute("email_agent.error_type", code)
    span.set_attribute("email_agent.status", "error")
    span.set_status(StatusCode.ERROR)


def outcome(
    span: trace.Span,
    *,
    error: bool = False,
    limit: bool = False,
    gmail_unavailable: bool = False,
    degraded: bool = False,
) -> None:
    value = (
        "error"
        if error
        else "limit_exceeded"
        if limit
        else ("partial" if gmail_unavailable or degraded else "successful")
    )
    span.set_attribute("email_agent.outcome", value)
    span.set_attribute("email_agent.limit_reached", limit)
    span.set_attribute("email_agent.gmail_unavailable", gmail_unavailable)
    span.set_attribute("email_agent.source_degraded", degraded)
    span.set_status(StatusCode.ERROR if error else StatusCode.OK)


def document_id(identifier: str) -> str:
    tracing = _active.get()
    return tracing.pseudonym(identifier) if tracing else ""


def current_failure(code: str) -> None:
    if _active.get() is not None:
        failure(trace.get_current_span(), code)
