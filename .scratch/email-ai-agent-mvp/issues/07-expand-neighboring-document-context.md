# 07: Expand neighboring document context

**What to build:** After finding a relevant passage, a user or agent can retrieve a bounded region around that passage from the same local document without repeating semantic search or mixing in another source.

**Blocked by:** 06: Search attachments through unified retrieval

**Status:** ready-for-agent

- [x] Persistent document nodes retain stable document and chunk identifiers, chunk positions, and previous/next relationships.
- [x] `get_local_document` accepts a document ID, a target chunk identifier or position, and a requested number of neighboring chunks.
- [x] Returned chunks are ordered, belong to the requested document, and include source metadata sufficient for citation.
- [x] Requests at the beginning or end of a document return the available range without failure.
- [x] Expansion enforces a configurable output-size bound.
- [x] Context expansion reads the local document store and does not perform a new vector search, regenerate embeddings, or call a generative model.
- [x] Unknown documents and chunks return clear structured errors.
- [x] Tests verify ordering, boundaries, size limits, persistence, unknown identifiers, and strict source isolation.
- [x] Nodes are persisted locally with LlamaIndex `SimpleDocumentStore`, including stable IDs and previous/next relationships, and reload successfully after a process restart.
- [x] Do not automatically expand every /search result. Expand context only when get_local_document is called. Prefer LlamaIndex PrevNextNodePostprocessor if compatible with the installed version; otherwise use simple traversal of stored previous/next relationships. Do not perform a new vector search.
- [x] The document store is a rebuildable derived representation; SQLite, original local source files, and persisted extracted text remain authoritative.
- [x] Before implementation, the installed-version persistence/reload behavior, relationship serialization, deterministic node-ID behavior, and `PrevNextNodePostprocessor` interface are verified and captured in implementation notes or tests.

- [x] Resolve nodes from the document store belonging to the active embedding-contract collection; never mix legacy and current generations.

- [x] Context expansion defaults to 1 previous and 1 next chunk (num_nodes=1). Allow 0–2 neighboring chunks per direction, with a hard maximum of 2.

- [x] get_local_document enforces a configurable maximum of 3000 output tokens per call, including the target chunk. Prefer complete chunks; explicitly truncate the target only if it alone exceeds the budget.

- [x] Search results must expose a stable chunk_id or chunk_index, allowing get_local_document to identify the exact matching passage.

## Implementation notes

Completed neighboring context expansion through HybridSearchIndex.get_local_document.
Search exposes stable UUID5 chunk IDs and zero-based positions. The active
embedding-contract SimpleDocumentStore persists chunk positions and previous/next
relationships; PrevNextNodePostprocessor reads local nodes on explicit requests.
The target takes budget priority, complete neighbors are preferred, and oversized
targets are explicitly marked truncated. The configurable passage budget defaults
to 3000 tokens; citation metadata remains complete. Existing derived nodes refresh
to the new position schema without changing embedding contracts or source files.

Compatibility gate and validation: [document context notes](../../../docs/document-context.md).
Five context tests cover ordering, boundaries, structured errors, source and
contract isolation, attachment citations, size limits, rebuild stability, and reload
in a separate Python process. Full suite: 75 tests, 74 passed, one opt-in
real-Qwen/server-Qdrant check skipped. Strict mypy, Ruff, and whitespace checks pass.
Independent code review: Standards: zero findings; Spec: zero findings.
Tests use deterministic dense embeddings and local Qdrant on Windows/Python 3.12.14;
real-Qwen retrieval quality and server-Qdrant acceptance were not rerun.
The existing triage label is retained; completion is recorded by this checklist.
