# 05: Search archived email bodies with hybrid retrieval

**What to build:** A user can run `/search` to find archived email passages by either meaning or exact terminology and receive bounded, attributable results without a generative-model call.

**Blocked by:** 01: Boot a configurable local CLI; 03: Import and persist recent email bodies

**Status:** ready-for-agent

- [ ] Email bodies are represented as separate documents and split with SentenceSplitter at approximately 512 tokens with approximately 64 tokens of overlap, while short messages remain whole when appropriate.
- [ ] Dense retrieval uses Ollama `bge-m3`, lexical retrieval uses BM25, and reciprocal rank fusion combines their rankings.
- [ ] Qdrant and the document-processing state persist across application and container restarts.
- [ ] `/search` returns matching text, relevance score, document ID, Gmail message ID, thread ID, source type, title, timestamp, and available location metadata.
- [ ] Search supports sender, date or date range, and source-type filters.
- [ ] The caller can request 1 to 20 results, with a default of 8.
- [ ] Returned text respects the configurable context budget while retaining complete source identifiers and metadata.
- [ ] Retrieval does not invoke OpenRouter or any other generative model.
- [ ] Tests demonstrate exact-term and semantic matches, filters, result limits, context-budget enforcement, persistence, and no-result behavior using synthetic messages.

