# 08: Chat with the local archive

**What to build:** A user can ask natural-language questions in the CLI and receive evidence-grounded answers from the local archive, with citations, bounded investigation, safe treatment of retrieved content, and conversational follow-up support.

**Blocked by:** 05: Search archived email bodies with hybrid retrieval; 07: Expand neighboring document context

**Status:** ready-for-agent

- [x] Pydantic AI uses the configured OpenRouter model and detects missing credentials or unsupported tool-calling capability as early as practical.
- [x] The agent can call `search_email_rag` and `get_local_document` more than once when additional evidence is needed.
- [x] Tool results contain evidence and metadata rather than a separately generated RAG answer.
- [x] Email-related factual claims cite available message and attachment evidence near the supported claims.
- [x] The agent discloses insufficient evidence, identifies conflicting sources, and does not present retrieval scores as factual probabilities.
- [x] System instructions treat email and attachment contents as untrusted data and prohibit following instructions embedded in them.
- [x] A configurable tool-call limit defaults to approximately 10; reaching it returns the best supported partial answer and discloses the limitation.
- [x] The latest 10 user-assistant exchanges are retained in memory with structurally valid tool messages, and `/clear` resets only conversation history.
- [x] The CLI shows concise tool-activity notifications without exposing private reasoning or full email contents in logs.
- [x] Controlled-agent tests assert observable grounded outcomes, citations, conflict handling, prompt-injection resistance, follow-ups, history clearing, and execution-limit behavior.


## Implementation notes

Implemented `ChatSession` with Pydantic AI 1.107.10 and its native OpenRouterModel /
OpenRouterProvider. The first CLI question checks the API key and model catalog before
initializing retrieval. Provider routing requires parameter support. Local search and
neighbor expansion return the existing evidence dataclasses without a second LLM.

The ten-exchange deque stores complete runs, including paired tool calls and replies.
Tools execute serially; a per-question counter bounds actual retrieval calls even when
a model returns multiple calls in one response. Tool preparation removes tools at the
limit, and an independent model-request budget bounds invalid/repeated requests.
Limit fallback preserves cited source excerpts when final synthesis fails.

Twelve controlled-model tests cover the chat and CLI boundaries agreed with the user.
They use actual SQLite/archive, LlamaIndex and local Qdrant with controlled dense
embeddings. They verify source delivery, citations, conflicting evidence, injection
policy delivery, missing evidence, follow-ups, clearing, history pairing/eviction,
privacy of tool notifications, capability checks, and limit behavior. Policy delivery
and scripted outputs do not prove real-model answer quality or universal injection
resistance. Live OpenRouter and real-Qwen/server-Qdrant acceptance were not run.

Verified installed public interfaces: Agent.run_sync, tool(prepare=..., sequential=True),
RunContext, FunctionModel, UsageLimits(request_limit=...), new_messages(),
OpenRouterModelSettings.openrouter_provider.require_parameters. References:
[Pydantic AI OpenRouter](https://pydantic.dev/docs/ai/models/openrouter/),
[usage limits](https://pydantic.dev/docs/ai/core-concepts/agent/), and
[OpenRouter model capabilities](https://github.com/OpenRouterTeam/docs/blob/main/guides/overview/models.mdx).

The existing triage label is retained; completion is recorded by this checklist.

Review corrections: chat, direct search, and reindex now share one lazy retrieval
backend, preventing stale in-memory document stores. Limit fallback also uses
retained conversation evidence when a follow-up discovers no new sources.
Both regressions are covered through the agreed public boundaries. Independent
reviews after these fixes: Standards ? no remaining findings; Spec ? no remaining
findings. Verified on Windows 11 / Python 3.12.14 with Pydantic AI 1.107.10.

Final validation: 87 tests run, 86 passed and one opt-in real-Qwen/server-Qdrant
acceptance test skipped. Strict mypy (src and tests), Ruff, whitespace checks,
and locked dependency synchronization (--inexact --no-install-project) pass.
