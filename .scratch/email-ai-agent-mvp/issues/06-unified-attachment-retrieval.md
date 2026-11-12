# 06: Search attachments through unified retrieval

**What to build:** A user can use the same `/search` experience to find evidence in both email bodies and extracted attachments, filter attachment results, and trace every passage to the correct source message and document.

**Blocked by:** 04: Archive and extract email attachments; 05: Search archived email bodies with hybrid retrieval

**Status:** ready-for-agent

- [ ] Supported extracted attachments are indexed as documents separate from email bodies and separate from other attachments.
- [ ] A single search can return fused results from email bodies and attachments.
- [ ] Search supports attachment filename and document/source-type filters in addition to the existing metadata filters.
- [ ] Attachment results include the stable attachment and document identifiers, source message and thread identifiers, original filename, MIME type, and reliable location metadata.
- [ ] PDF page numbers are exposed only when the parser provides trustworthy page metadata.
- [ ] Chunks and metadata from different documents are never combined or attributed to the wrong message.
- [ ] Repeated indexing does not create duplicate documents, chunks, or vector records.
- [ ] Tests find known facts in synthetic PDF, DOCX, and text attachments and verify filename filtering, source attribution, page-metadata rules, and separation between documents.
- [ ] Each supported SHA-256-deduplicated attachment content item is represented by its own LlamaIndex `Document` with a stable document ID and source metadata; repeated message occurrences reuse that document, and distinct attachment content is never combined with an email body or another attachment.
- [ ] Attachment nodes are added to the same hybrid Qdrant collection and retrieved through the same `VectorStoreIndex.as_retriever()` path as email nodes.
- [ ] Every attachment result resolves through SQLite to all archived source-message relationships, including repeated occurrences of SHA-256-deduplicated bytes; vector-store metadata must not become the sole copy of those links.
- [ ] Filename and source-type filtering uses the installed Qdrant/LlamaIndex metadata-filter functionality rather than a custom vector-search path.
- [ ] Qdrant points are rebuildable derivatives of SQLite, original local files, and persisted extracted text.

