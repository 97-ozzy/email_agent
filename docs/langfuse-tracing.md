# Langfuse tracing (ticket 10.5)

## Start the standard local UI

The application and Langfuse run separately. Existing `docker compose up -d` still starts
only Qdrant. Langfuse uses the **official six-service stack**, not an application-specific UI.
From the repository root in PowerShell:

```powershell
./infra/langfuse/setup.ps1
docker compose --project-name email-agent-langfuse --env-file infra/langfuse/.env -f infra/langfuse/compose.upstream.yaml -f infra/langfuse/compose.local.yaml config --quiet
docker compose --project-name email-agent-langfuse --env-file infra/langfuse/.env -f infra/langfuse/compose.upstream.yaml -f infra/langfuse/compose.local.yaml up -d
```

Open **http://localhost:3000**, register your local account, create an organization and a
project, then open Project Settings → API Keys → Create new API keys. Copy the project keys
into the application's root `.env` (not the infrastructure `.env`):

```dotenv
LANGFUSE_ENABLED=true
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_PUBLIC_KEY=pk-lf-your-project-public-key
LANGFUSE_SECRET_KEY=sk-lf-your-project-secret-key
LANGFUSE_ENVIRONMENT=local
LANGFUSE_CAPTURE_CONTENT=false
```

The infrastructure setup script generates independent random secrets once and preserves
existing settings on subsequent runs. `infra/langfuse/.env` is ignored by Git. Never commit
it or the application's `.env`. The local overlay requires generated secrets instead of
upstream example credentials, disables upstream product telemetry, removes infrastructure
ports from the host, and exposes only web (3000) and object media (9090) on 127.0.0.1.
This requires Docker Compose 2.24.4+ (`!reset` / `!override`). Named volumes persist data.

Install and run the application with Python 3.11/3.12:

```powershell
uv sync --python 3.12
uv run python -m email_agent
```

Exit an already running `email-agent.exe` before `uv sync` on Windows: its executable may
otherwise be locked. A library caller should call `ChatSession.close()` in `finally`.
The CLI closes tracing on `/exit`, EOF, exceptions, and keyboard interruption.

Ask a question, wait a few seconds, then open the project's Tracing view. Filter by trace
name `email_agent.ask`; its metadata contains `email_agent.turn_id` and `email_agent.outcome`.
Copy its session ID and filter Tracing by that session ID (or open the Sessions view) to
see all turns. `/clear` clears conversation history but retains the session ID. A new
ChatSession gets a new session ID. No prompt text is needed to find a trace.

Set `LANGFUSE_ENABLED=false` and restart the application to disable tracing. This creates
no exporter and no identity file. To stop the UI without deleting stored traces:

```powershell
docker compose --project-name email-agent-langfuse --env-file infra/langfuse/.env -f infra/langfuse/compose.upstream.yaml -f infra/langfuse/compose.local.yaml down
```

### Network addresses

- Host application → Langfuse: `http://localhost:3000`.
- Application container on the same Docker network → `http://langfuse-web:3000`.
  The separate Compose projects do not share a network by default; attach the application
  to the Langfuse network explicitly before using that DNS name.
- Application container → a Langfuse port published on the host:
  `http://host.docker.internal:3000` on Docker Desktop (Linux may need
  `host.docker.internal:host-gateway` and a reachable host binding).
- A container's `localhost` always means that container. The stack uses internal service
  DNS for Postgres, Redis, ClickHouse and MinIO. The browser media URL uses host port 9090.

## Architecture and privacy

`tracing.py` provides `Tracing.turn`, `stage`, `outcome`, native PydanticAI instrumentation
settings, an export sanitizer, and lifecycle management. `ChatSession.ask` owns exactly one
root and classifies the completed turn. Native PydanticAI instrumentation owns the agent,
model and tool spans. Business spans measure archive refresh, hybrid retrieval, context
reading, and Gmail client calls. A ContextVar carries the selected provider and turn
metadata across async boundaries and PydanticAI's context-preserving worker threads.
No global provider is installed. Independent `/search`, sync and reindex operations do
not create orphan roots. Concurrent independent sessions are supported; concurrent mutation
of one session's history is not introduced.

The reference [FoodTech tracing architecture](https://github.com/iagurev/hse_agents_foodtech_project/blob/main/src/app/tracing.py)
and its config, environment example, Compose and operations guide were inspected. We retain
its small turn/stage abstraction and native model enrichment, replacing its content writes
and global assumptions with an allowlisted exporter and instance-scoped providers.

The transport is Langfuse's [official direct OpenTelemetry ingestion](https://langfuse.com/integrations/native/opentelemetry),
`POST /api/public/otel/v1/traces`, using project-key Basic authentication. SDK v4.18.0 was
evaluated, but the application deliberately uses the supported native OTel path instead of
creating Langfuse client singletons/media workers: this gives explicit queue, sanitizer and
shutdown ownership without SDK-private lifecycle hooks. Langfuse SDK is not a runtime
dependency. Native PydanticAI uses the current `Instrumentation` capability with per-Agent
`InstrumentationSettings`; the deprecated `Agent(instrument=...)` argument is avoided.
This is a documented departure from the ticket's SDK-v4 preference, not a second model
instrumentation mechanism or a replacement server.

Default telemetry excludes prompts, answers, tool arguments/results, email bodies, chunks,
attachment data, filter values, original document/message IDs, account addresses, exception
messages and stacks, unknown attributes/events, and status descriptions. The export boundary
sanitizes actual span events as well as attributes, resources and status descriptions.
Safe counts, filter names/types, operation/backend names, UUIDs and HMAC document IDs remain.
Turn outcomes are `error` > `limit_exceeded` > `partial` > `successful`, with separate Gmail,
limit and local-source degradation flags. Gmail unavailability without a Gmail tool attempt, including an absent Gmail adapter,
still produces `partial`; existing unavailable-source answer behavior remains unchanged.
No answer, fallback, history or advertised tool support behavior is changed.

`LANGFUSE_CAPTURE_CONTENT=true` is a separate **local textual debugging** opt-in. It enables
textual prompts, answers and tool payloads. Binary capture remains disabled. Known configured
keys, bearer/provider tokens and credential assignments are redacted, including nested JSON
strings and structured credential/error fields; raw exception events/status text are always
removed. This is not a general-purpose secret detector for arbitrary prose. Treat debug
traces as sensitive correspondence and restrict local UI access.

A random user UUID and an independent random HMAC key are persisted transactionally in
`EMAIL_AGENT_DATA_DIR/tracing-identity.sqlite3` (default `data/tracing-identity.sqlite3`).
Only the user UUID and HMAC outputs are exported; the HMAC key is never exported. No email
address is used to derive identity. To reset identity, exit all application instances and
remove that file, then restart. This resets future user/document pseudonyms; old traces
remain in Langfuse. Each session and turn uses fresh UUIDs.

The exporter has a 512-span bounded queue, batches of 64, a 500 ms background schedule,
and a 1-second HTTP export deadline. Export failures are swallowed at the boundary. There
is no per-tool flush. CLI shutdown waits at most two seconds for flush/shutdown; queued
telemetry can be lost on outage, overload or exit. Application answers remain available.

## Versions and compatibility evidence

Verified 2026-11-13, Python 3.12, Windows:

| Component | Tested version |
| --- | --- |
| pydantic-ai-slim | 1.107.10 (minimum constrained to this tested capability API) |
| OpenTelemetry SDK / OTLP HTTP exporter | 1.45.2 / 1.45.2 (pinned) |
| OpenAI client used by OpenRouterProvider | 3.50.0 |
| httpx | 0.28.1 |
| llama-index-core | 0.14.25 |
| qdrant-client | 1.20.0 |
| Langfuse SDK evaluated, not used at runtime | 4.18.0 |

The PydanticAI [exception-content advisory](https://github.com/pydantic/pydantic-ai/security/advisories/GHSA-4x9p-g9wm-8q7f)
is fixed since 1.107.6. In addition to using a patched version, regression tests inspect
exported exception events and status descriptions in both content modes. Upstream docs
now describe newer SDK/server majors; this integration deliberately targets self-hosted
Langfuse **v3.160.0**, compatible with the v3/v4 SDK-era OTLP interface. Do not silently
switch to SDK v5 (requires a newer v4 server).

`infra/langfuse/compose.upstream.yaml` is an unmodified copy of
[official docker-compose.yml at v3.160.0](https://github.com/langfuse/langfuse/blob/f78b1cdc9aad598c518dc41eb9a9fbbdb4802ade/docker-compose.yml),
commit `f78b1cdc9aad598c518dc41eb9a9fbbdb4802ade`.
SHA256: `6b11fa41fd2f01b47886908e487ca78d02620ac246404227a5ce9ee8f66953e7`.
The overlay pins Langfuse web/worker to `3.160.0` and ClickHouse to `25.3`; Postgres 17,
Redis 7 and Chainguard MinIO retain upstream image sources. These infrastructure tags
are not all digest-pinned; upgrading them needs a new UI smoke test and volume backup.

Each native model request creates one Langfuse generation, including failed requests and
agent-level retries. Tokens map from PydanticAI `RequestUsage.input_tokens` / `output_tokens`;
total is their sum. Reasoning is already included in output and is not added again.
Only generations carry accounting. Actual cost is taken from OpenRouter `provider_details.cost`
and exported as `langfuse.observation.cost_details.total`, rather than PydanticAI's tariff
estimate. Missing cost/usage fields stay missing/zero as represented by the provider; there
is no invented cost. PydanticAI 1.107.10 drops provider `cost=0` while mapping responses,
so a genuine zero-cost response may have no explicit cost attribute. Langfuse can still
show its own model-price estimates; those are not our recorded provider cost.

Compatibility limitation: retries hidden *inside* the OpenAI HTTP client remain inside one
native PydanticAI model-request span. The existing client and its retry behavior are retained;
this integration does not pretend those transport attempts are separately observed model
requests or add duplicate generation spans around the HTTP layer.

## Validation and representative trace

The following is an **actual in-memory-exported trace tree**, using a FunctionModel and a
real local Qdrant/LlamaIndex index with controlled embeddings, not a fabricated UI screenshot:

```text
email_agent.ask
  invoke_agent agent
    chat function:model:
    execute_tool search_email_rag
      archive.refresh
      rag.hybrid_search
    chat function:model:
    execute_tool get_local_document
      document.context
    chat function:model:
```

Gmail tests additionally export `execute_tool search_gmail → gmail.search_messages →
gmail.async_boundary` and equivalent message/thread operations, proving parent preservation
through native synchronous-tool workers and `asyncio.run`. OpenRouter accounting tests use
the real OpenRouterModel/OpenRouterProvider with httpx.MockTransport, not a live model.

```powershell
uv run --no-sync python -m unittest discover -s tests -p test_tracing.py -v
uv run --no-sync mypy src tests
uv run --no-sync ruff check src tests
uv run --no-sync python -m unittest discover -s tests -v
```

Local exporter checks cover root/session/turn identity, `/clear`, disabled/no-orphan behavior,
concurrent-session isolation, native tool parentage, real retrieval stages, async Gmail context,
propagated and caught failures, outcome precedence/history, privacy of actual exported events,
content opt-in, nested credential sanitization, failed generations, actual cost/token mapping,
export failure and bounded shutdown. All these checks require no Gmail/OpenRouter credentials.

Final validation after review corrections: **125 tests run in 49.678 seconds, OK,
1 skipped** (the existing downloaded-Qwen/server-Qdrant acceptance test). The 14 tracing
tests passed. Strict `mypy src tests`: success, 26 source files. `ruff check src tests`:
all checks passed. `git diff --check`: clean. The two-axis review found one outcome
classification bug and one duplicated discriminator map; both were fixed. See the
[review record](../.scratch/email-ai-agent-mvp/review-10.5.md).

Docker Compose configuration validation passed. A live UI startup was attempted but image
pulling was blocked: upstream `cgr.dev/chainguard/minio:latest` returned HTTP 403. Docker Hub
MinIO was unavailable and the historical official Quay tag was absent. No replacement server
or unverified third-party image was substituted. **Live Langfuse UI ingestion and live
Gmail/OpenRouter smoke tests are not verified in this environment.** Run the commands above
where the official image is available, create project keys, ask a turn, and inspect its trace.

Changed application files: `config.py`, `chat.py`, `retrieval.py`, `cli.py`, new `tracing.py`.
Delivery also includes `tests/test_tracing.py`, `pyproject.toml`, `uv.lock`, `.env.example`,
`.gitignore`, this guide, the README link, and the three `infra/langfuse/` setup files.
