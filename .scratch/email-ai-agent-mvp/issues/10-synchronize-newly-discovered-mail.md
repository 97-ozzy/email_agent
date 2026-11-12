# 10: Synchronize newly discovered mail

**What to build:** A user can run `/sync` to add newly received and sent messages and their attachments to the local archive and search index without reprocessing completed records.

**Blocked by:** 06: Search attachments through unified retrieval

**Status:** ready-for-agent

- [ ] Synchronization searches the Inbox and Sent scope with pagination and is not capped by the initial-import limit.
- [ ] Known Gmail message IDs are skipped without duplicate downloads, document records, chunks, or vector entries.
- [ ] New messages, attachment relationships, supported extracted text, and retrieval data become available through `/search` after synchronization.
- [ ] An overlapping discovery window may inspect known records to avoid missing new correspondence without reprocessing them.
- [ ] Locally archived messages remain present when Gmail messages are later moved, relabeled, or deleted.
- [ ] A no-change synchronization completes successfully and reports zero new work.
- [ ] The CLI reports discovered, downloaded, parsed, indexed, skipped, unsupported, and failed outcomes accurately.
- [ ] Tests cover new received mail, new sent mail, pagination, overlapping known records, new attachments, no-change repeats, and preservation of locally archived records.

