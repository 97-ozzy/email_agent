# 09: Extend answers with live Gmail evidence

**What to build:** A user can receive answers informed by older or newly changed Gmail correspondence when the local archive is insufficient, while retaining a clear distinction between live mailbox evidence and indexed local evidence.

**Blocked by:** 02: Authenticate Gmail with read-only access; 08: Chat with the local archive

**Status:** implemented

- [x] The agent has clearly described live tools for searching messages, retrieving a message, and retrieving a thread.
- [x] The agent can choose local retrieval, live Gmail, or both based on the question rather than a hardcoded sequence.
- [x] Questions about current or recent changes cause the agent to consult live Gmail when it is available.
- [x] An unsuccessful local search is not described as proof that the full mailbox lacks the information.
- [x] The agent can open a complete thread when conversational context is needed to interpret a decision or agreement.
- [x] Live results remain separate from persistent ingestion and do not silently expand the local archive.
- [x] Answers distinguish local-index evidence from live Gmail evidence and cite the original message or thread information available.
- [x] If Gmail is unavailable, locally indexed questions remain usable and the answer discloses that live coverage could not be checked.
- [x] Tests cover local-only, live-only, combined, recent-information, full-thread, insufficient-coverage, and Gmail-unavailable scenarios.

## Comments

Implemented 2026-11-13. Live tools use the read-only gmail-agent MCP stdio server through
the official MCP SDK; ingestion remains independent. Local retrieval initializes only if
selected. Shared tool limits, source attribution, safe Gmail outage disclosure, and live
evidence in partial-answer fallbacks are covered by controlled-model CLI tests.

Validation on Windows, Python 3.12.14: full suite ran 101 tests successfully (1 skipped:
downloaded Qwen/server-Qdrant acceptance); strict mypy and Ruff passed. Real gmail-agent
MCP stdio tests use a synthetic Gmail client and verify full-thread bodies and safe errors.
Standards and spec reviews report no remaining findings. No personal Gmail/OpenRouter
acceptance was performed. Installed API compatibility and the SDK adaptation are documented
in README.md.

