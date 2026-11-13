# 10: Synchronize newer mail and bounded older batches

**What to build:** A user can run `/sync_new` to add messages newer than the newest archived message, and `/sync_old N` to add the next N previously unarchived messages older than the oldest archived message and their attachments to the local archive and search index without reprocessing completed records.

**Blocked by:** 06: Search attachments through unified retrieval

**Status:** implemented

- [x] Both commands paginate Inbox/Sent; `/sync_new` is uncapped and `/sync_old N` is capped by N unknown IDs.
- [x] `/sync_new` starts at the newest archived timestamp; `/sync_old N` starts at the oldest and continues backward.
- [x] Empty archives, equal timestamps, invalid N, and repeated historical batches are covered.
- [x] Known Gmail message IDs are skipped without duplicate downloads, document records, chunks, or vector entries.
- [x] New messages, attachment relationships, supported extracted text, and retrieval data become available through `/search` after synchronization.
- [x] An overlapping discovery window may inspect known records to avoid missing new correspondence without reprocessing them.
- [x] Locally archived messages remain present when Gmail messages are later moved, relabeled, or deleted.
- [x] A no-change synchronization completes successfully and reports zero new work.
- [x] The CLI reports discovered, downloaded, parsed, indexed, skipped, unsupported, and failed outcomes accurately.
- [x] Tests cover new received mail, new sent mail, pagination, overlapping known records, new attachments, no-change repeats, and preservation of locally archived records.

- [x] Index using the active FastEmbed Qwen3 contract; changing models must rebuild local vectors rather than skip records because Gmail IDs already exist.

## Comments

Updated 2026-11-13: replaced `/sync` with `/sync_new` and `/sync_old N`.
New mail discovery starts at the newest archived timestamp and has no message cap.
Historical discovery starts at the oldest archived timestamp and selects up to N
unknown IDs, newest first, paginating past duplicates. Repeated historical calls
extend the archive backward. Positive N is required. Empty/undated archives start
at the newest mail. Both modes overlap the boundary second, retain local sources,
retry unfinished attachments, and refresh the active embedding contract.

Validation: 111 tests ran successfully (one skipped: downloaded-Qwen/server-Qdrant
acceptance); strict mypy and Ruff passed. New controlled-Gmail tests cover opposite
time bounds, repeated bounded historical batches, equal timestamps, empty archives,
and CLI argument validation. No live Gmail acceptance was performed.
