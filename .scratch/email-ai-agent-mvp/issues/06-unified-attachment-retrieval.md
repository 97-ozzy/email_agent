# 06: Search attachments through unified retrieval

**What to build:** A user can use the same `/search` experience to find evidence in both email bodies and extracted attachments, filter attachment results, and trace every passage to the correct source message and document.

**Blocked by:** 04: Archive and extract email attachments; 05: Search archived email bodies with hybrid retrieval

**Status:** ready-for-agent

**Implementation:** complete

- [x] Supported extracted attachments are indexed as documents separate from email bodies and separate from other attachments.
- [x] A single search can return fused results from email bodies and attachments.
- [x] Search supports attachment filename and document/source-type filters in addition to the existing metadata filters.
- [x] Attachment results include the stable attachment and document identifiers, source message and thread identifiers, original filename, MIME type, and reliable location metadata.
- [x] PDF page numbers are exposed only when the parser provides trustworthy page metadata.
- [x] Chunks and metadata from different documents are never combined or attributed to the wrong message.
- [x] Repeated indexing does not create duplicate documents, chunks, or vector records.
- [x] Tests find known facts in synthetic PDF, DOCX, and text attachments and verify filename filtering, source attribution, page-metadata rules, and separation between documents.
- [x] Each supported SHA-256-deduplicated attachment content item is represented by its own LlamaIndex `Document` with a stable document ID and source metadata; repeated message occurrences reuse that document, and distinct attachment content is never combined with an email body or another attachment.
- [x] Attachment nodes are added to the same hybrid Qdrant collection and retrieved through the same `VectorStoreIndex.as_retriever()` path as email nodes.
- [x] Every attachment result resolves through SQLite to all archived source-message relationships, including repeated occurrences of SHA-256-deduplicated bytes; vector-store metadata must not become the sole copy of those links.
- [x] Filename and source-type filtering uses the installed Qdrant/LlamaIndex metadata-filter functionality rather than a custom vector-search path.
- [x] Qdrant points are rebuildable derivatives of SQLite, original local files, and persisted extracted text.

- [x] Attachment nodes use the same active embedding-contract collection as email nodes and the exact FP32 Qwen3 model; migrations rebuild from persisted extracted text without Gmail downloads.

## Implementation notes

Unified indexing reuses persisted extracted text and the active Qwen3 embedding-contract
collection. Native Qdrant nested metadata filters correlate occurrence-level filename, sender,
and dates. Results resolve every source occurrence from SQLite and expose character offsets;
PDF page numbers remain absent because full-document extraction has no trustworthy boundaries.
Compatibility and validation scope: [unified retrieval notes](../../../docs/unified-attachment-retrieval.md).

Validation: 20 search tests, including seven new attachment service/CLI cases; strict mypy and
Ruff pass. Full-suite and independent standards/spec review results are recorded below.

Review fixes: attachment occurrence statuses now transition through pending/indexed/failed,
and interrupted embedding work retries from persisted extraction. Shared citation projection
is normalized once. The existing triage label is retained; completion is recorded separately.

Final validation: full suite ran 70 tests (69 passed, one opt-in real-Qwen/server test
skipped); 20 focused search tests passed. Strict mypy, Ruff, and whitespace checks passed.
Independent code review: Standards ? zero remaining findings; Spec ? zero remaining findings
after the persisted attachment indexing-state fix. Attachment checks used Windows/Python
3.12.14, deterministic dense embeddings, installed readers, and local Qdrant. Real Qwen
attachment retrieval and server-Qdrant attachment acceptance were not run.
