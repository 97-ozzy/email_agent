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

