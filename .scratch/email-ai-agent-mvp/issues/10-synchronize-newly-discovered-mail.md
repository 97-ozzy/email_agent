# 10: Synchronize newly discovered mail

**What to build:** A user can run `/sync` to add newly received and sent messages and their attachments to the local archive and search index without reprocessing completed records.

**Blocked by:** 06: Search attachments through unified retrieval

**Status:** implemented

- [x] Synchronization searches the Inbox and Sent scope with pagination and is not capped by the initial-import limit.
- [x] Known Gmail message IDs are skipped without duplicate downloads, document records, chunks, or vector entries.
- [x] New messages, attachment relationships, supported extracted text, and retrieval data become available through `/search` after synchronization.
- [x] An overlapping discovery window may inspect known records to avoid missing new correspondence without reprocessing them.
- [x] Locally archived messages remain present when Gmail messages are later moved, relabeled, or deleted.
- [x] A no-change synchronization completes successfully and reports zero new work.
- [x] The CLI reports discovered, downloaded, parsed, indexed, skipped, unsupported, and failed outcomes accurately.
- [x] Tests cover new received mail, new sent mail, pagination, overlapping known records, new attachments, no-change repeats, and preservation of locally archived records.

- [x] Index using the active FastEmbed Qwen3 contract; changing models must rebuild local vectors rather than skip records because Gmail IDs already exist.

## Comments

Implemented 2026-11-13. `/sync` scans the full Inbox/Sent scope, deduplicates discovered IDs,
reuses completed source records, retries unfinished attachment work, and refreshes the active
hybrid index even when all message IDs are known. Full-scope discovery also archives previously
unarchived older messages. Gmail moves, relabeling, or deletion never remove local sources.
Reports distinguish message outcomes, attachment extraction outcomes, total indexed documents,
and indexed attachment documents. Indexing failure preserves archived sources and CLI access.

Seven sync tests cover uncapped pagination, received/sent mail, known overlap, retained local
sources, searchable attachments, zero-work repeats, download/attachment retries, repeated page
tokens, indexing failure, and active-contract rebuilds of email and attachment documents.
Tests use controlled Gmail/embeddings and local Qdrant on Windows, Python 3.12.14; no live Gmail
or downloaded-Qwen/server-Qdrant acceptance is claimed. Standards and spec reviews have no
remaining findings. Strict mypy and Ruff pass.
The full suite ran 108 tests successfully (one skipped: downloaded-Qwen/server-Qdrant acceptance).
