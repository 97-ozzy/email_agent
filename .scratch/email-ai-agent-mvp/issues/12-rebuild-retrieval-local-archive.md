# 12: Rebuild retrieval from the local archive

**What to build:** A developer can explicitly rebuild the hybrid search index from preserved local messages and extracted attachments, including after an embedding-model change, without downloading Gmail data again.

**Blocked by:** 06: Search attachments through unified retrieval; 11: Recover interrupted ingestion and synchronization

**Status:** ready-for-agent

- [ ] A dedicated non-interactive reindex operation is separate from normal startup and `/sync`.
- [ ] Reindexing reads preserved original and extracted local content and performs no Gmail download.
- [ ] The operation rebuilds dense, sparse, document-store, chunk, and source-metadata state consistently.
- [ ] The source archive, attachment relationships, and SQLite source records remain intact.
- [ ] An interrupted rebuild can resume or restart safely without duplicate retrieval records.
- [ ] After rebuilding, known email and attachment facts remain searchable with correct source references.
- [ ] Changing the configured embedding model requires or clearly prompts an explicit reindex.
- [ ] Tests verify Gmail independence, source preservation, interruption recovery, stable associations, and post-rebuild search behavior.

