# 07: Expand neighboring document context

**What to build:** After finding a relevant passage, a user or agent can retrieve a bounded region around that passage from the same local document without repeating semantic search or mixing in another source.

**Blocked by:** 06: Search attachments through unified retrieval

**Status:** ready-for-agent

- [ ] Persistent document nodes retain stable document and chunk identifiers, chunk positions, and previous/next relationships.
- [ ] `get_local_document` accepts a document ID, a target chunk identifier or position, and a requested number of neighboring chunks.
- [ ] Returned chunks are ordered, belong to the requested document, and include source metadata sufficient for citation.
- [ ] Requests at the beginning or end of a document return the available range without failure.
- [ ] Expansion enforces a configurable output-size bound.
- [ ] Context expansion reads the local document store and does not perform a new vector search, regenerate embeddings, or call a generative model.
- [ ] Unknown documents and chunks return clear structured errors.
- [ ] Tests verify ordering, boundaries, size limits, persistence, unknown identifiers, and strict source isolation.
- [ ] Nodes are persisted locally with LlamaIndex `SimpleDocumentStore`, including stable IDs and previous/next relationships, and reload successfully after a process restart.
- [ ] Do not automatically expand every /search result. Expand context only when get_local_document is called. Prefer LlamaIndex PrevNextNodePostprocessor if compatible with the installed version; otherwise use simple traversal of stored previous/next relationships. Do not perform a new vector search. 
- [ ] The document store is a rebuildable derived representation; SQLite, original local source files, and persisted extracted text remain authoritative.
- [ ] Before implementation, the installed-version persistence/reload behavior, relationship serialization, deterministic node-ID behavior, and `PrevNextNodePostprocessor` interface are verified and captured in implementation notes or tests.

- [ ] Resolve nodes from the document store belonging to the active embedding-contract collection; never mix legacy and current generations.

- [ ] Context expansion defaults to 1 previous and 1 next chunk (num_nodes=1). Allow 0–2 neighboring chunks per direction, with a hard maximum of 2.

- [ ] get_local_document enforces a configurable maximum of 3000 output tokens per call, including the target chunk. Prefer complete chunks; explicitly truncate the target only if it alone exceeds the budget.

- [ ] Search results must expose a stable chunk_id or chunk_index, allowing get_local_document to identify the exact matching passage.