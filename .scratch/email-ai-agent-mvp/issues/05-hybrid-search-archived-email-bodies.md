# 05: Search archived email bodies with hybrid retrieval

**What to build:** A user can run `/search` to find archived email passages by either meaning or exact terminology and receive bounded, attributable results without a generative-model call.

**Blocked by:** 01: Boot a configurable local CLI; 03: Import and persist recent email bodies

**Status:** needs-info
- [] Before chunking and indexing, email bodies are converted into clean, readable text using existing, lightweight Python libraries. Prefer BeautifulSoup for HTML-to-text conversion and email-reply-parser for removing quoted reply chains when applicable. Reuse existing dependencies where possible. Avoid custom HTML parsers, complex regex-based cleaning pipelines, and unnecessary abstractions.
- [] Normalize whitespace and conservatively remove obvious email noise, such as quoted replies and signatures, only when reliably identifiable. If a library cannot safely identify removable content, preserve it rather than risk losing useful information. Do not introduce LLM-based cleaning.
- [] Cleaning applies only to the derived text used for RAG indexing; original archived email bodies remain unchanged. Add tests for HTML, plain text, Cyrillic, quoted replies, signatures, and source preservation.
- [ ] Email bodies are represented as separate documents and split with SentenceSplitter at approximately 512 tokens with approximately 64 tokens of overlap, while short messages remain whole when appropriate.
- [ ] Dense retrieval uses Ollama `bge-m3`, lexical retrieval uses BM25, and reciprocal rank fusion combines their rankings.
- [ ] Qdrant and the document-processing state persist across application and container restarts.
- [ ] `/search` returns matching text, relevance score, document ID, Gmail message ID, thread ID, source type, title, timestamp, and available location metadata.
- [ ] Search supports sender, date or date range, and source-type filters.
- [ ] The caller can request 1 to 20 results, with a default of 8.
- [ ] Returned text respects the configurable context budget while retaining complete source identifiers and metadata.
- [ ] Retrieval does not invoke OpenRouter or any other generative model.
- [ ] Tests demonstrate exact-term and semantic matches, filters, result limits, context-budget enforcement, persistence, and no-result behavior using synthetic messages.
- [ ] Each email body is a separate LlamaIndex `Document` with explicit stable source metadata and deterministic document/node identifiers; unchanged reprocessing upserts rather than duplicates it.
- [ ] `SentenceSplitter` is configured with `chunk_size=512` and `chunk_overlap=64`.
- [ ] Hybrid retrieval uses LlamaIndex `QdrantVectorStore` with hybrid mode enabled, `OllamaEmbedding` with `bge-m3` for dense vectors, FastEmbed BM25 for sparse vectors, and the integration's existing RRF fusion rather than application-defined ranking or fusion.
- [ ] A LlamaIndex `VectorStoreIndex` and `as_retriever()` return evidence directly, without a query engine, response synthesizer, OpenRouter call, or other generative-model call.
- [ ] Email nodes use the one shared Qdrant collection reserved for both email and attachment nodes, with source metadata distinguishing their types.
- [ ] SQLite and locally archived source text remain authoritative; Qdrant is treated as a rebuildable derived index.
- [ ] Before implementation, installed-version compatibility is verified for Russian-language FastEmbed BM25 tokenization, Qdrant/LlamaIndex RRF, supported metadata filters, deterministic node IDs, and idempotent point updates; findings are captured in implementation notes or tests.

## Implementation notes

Compatibility gate started on 2026-11-12 by temporarily resolving and installing the
following current package releases with `uv add`; they were removed again after the blocking
contract mismatch was confirmed so the project would not retain a partial retrieval stack:

- `llama-index-core==0.14.25`
- `llama-index-vector-stores-qdrant==0.10.4`
- `llama-index-embeddings-ollama==0.10.0`
- `qdrant-client==1.20.0`
- `fastembed==0.9.1`

FastEmbed's `Qdrant/bm25` tokenizer preserves Russian terms (for example, `Оплата
по счёту договор АБВ-123` tokenizes to the corresponding Cyrillic words and
identifier parts), so Russian lexical retrieval is viable.

Implementation is blocked by the required fusion contract. In
`llama-index-vector-stores-qdrant==0.10.4`, `QdrantVectorStore(enable_hybrid=True)`
defaults to LlamaIndex's `relative_score_fusion`; the integration does not expose an existing
RRF implementation. `qdrant-client==1.20.0` exposes Qdrant's native `Fusion.RRF`, but using it
would require bypassing or extending the required LlamaIndex hybrid retriever. Supplying a
project-defined `hybrid_fusion_fn` would directly violate the requirement against
application-defined fusion.

The specification requires implementation to stop when a required interface is unavailable.
Consequently, metadata-filter support, deterministic node IDs, and idempotent point updates
were not evaluated and remain pending after the fusion contract is resolved. The compatibility
gate is not complete.

Choose one of these contract changes before returning the ticket to `ready-for-agent`:

1. Accept LlamaIndex's built-in relative-score fusion.
2. Allow direct Qdrant Query API hybrid retrieval with native RRF.
3. Allow a project-defined RRF callback passed to `QdrantVectorStore`.

