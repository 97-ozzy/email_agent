# 03: Import and persist recent email bodies

**What to build:** On first run, a user can import the newest configured set of Inbox and Sent messages into a durable local archive, see accurate results, and restart the application without downloading the same messages again.

**Blocked by:** 02: Authenticate Gmail with read-only access

**Status:** ready-for-agent

- [ ] The initial import limit defaults to 100, accepts smaller positive values, and rejects values above 100 with a clear error.
- [ ] Message discovery covers the union of Inbox and Sent, orders results newest first, handles pagination, and deduplicates Gmail message IDs.
- [ ] Each selected message stores the complete available original body and relevant metadata, including message ID, thread ID, participants, subject, timestamp, labels, and source reference.
- [ ] SQLite records durable ingestion and indexing state and preserves message-to-thread relationships.
- [ ] A repeated launch reuses locally archived messages and does not redownload completed records.
- [ ] The workflow reports requested, downloaded, successfully processed, and failed message counts based on actual outcomes.
- [ ] A partial import leaves completed records usable and clearly warns that the archive is incomplete.
- [ ] Tests cover 100-message, fewer-than-100, pagination, ordering, duplicate-ID, partial-failure, and restart scenarios through the application boundary.

