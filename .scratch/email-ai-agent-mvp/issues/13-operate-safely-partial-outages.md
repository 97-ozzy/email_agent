# 13: Operate safely through partial outages

**What to build:** A user can continue using whichever information sources remain available during temporary failures, receive bounded retries and actionable diagnostics, and avoid leaking credentials or complete correspondence through source control or logs.

**Blocked by:** 09: Extend answers with live Gmail evidence; 11: Recover interrupted ingestion and synchronization; 12: Rebuild retrieval from the local archive

**Status:** ready-for-agent

- [ ] Transient Gmail, Qdrant, Ollama, and OpenRouter failures use bounded exponential backoff and honor server retry guidance when available.
- [ ] When Qdrant or Ollama is unavailable, live Gmail questions can still work and the unavailable local capability is disclosed.
- [ ] When Gmail is unavailable, archived search and local-agent questions can still work and the unavailable live coverage is disclosed.
- [ ] A partially successful import permits entry to the CLI with a clear incomplete-index warning when usable data exists.
- [ ] Configuration and authentication failures produce actionable messages, while an explicit debug mode can expose technical exception details.
- [ ] Environment secrets, OAuth credentials and tokens, local mail data, attachments, indexes, and sensitive state are excluded from version control.
- [ ] Normal logs include operation names, durations, counts, and safe error summaries but not complete bodies, attachment contents, access tokens, or private model reasoning.
- [ ] Only evidence needed for an active answer and bounded conversation context is sent to OpenRouter; the full archive is never sent by default.
- [ ] Tests simulate each service outage, transient retry, partial import, and representative sensitive value to verify degraded behavior and log redaction.

