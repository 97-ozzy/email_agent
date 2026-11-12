# Unified attachment retrieval compatibility

Verified on Windows 11, Python 3.12.14, on 2026-11-12. Installed versions:
llama-index-core 0.14.25, llama-index-vector-stores-qdrant 0.10.4,
qdrant-client 1.20.0, fastembed 0.9.1, llama-index-embeddings-fastembed 0.7.0,
ONNX Runtime 1.31.1. Reader versions and extraction behavior are recorded in
[attachment-reader-compatibility.md](attachment-reader-compatibility.md).

Before extending retrieval, inspected the installed QdrantVectorStore.query() and
_build_subfilter() implementations and the NestedCondition/Nested model signatures.
The existing as_retriever(vector_store_kwargs={"qdrant_filters": ...}) interface forwards
native Qdrant filters to both dense and sparse queries before built-in relative-score
fusion. Native nested filters correlate filename, sender, and date on a single occurrence;
flat array predicates could incorrectly combine values from different messages.
See [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/).
No custom vector search, sparse ranking, or fusion was introduced.

Each content SHA-256 document uses its persisted attachment document ID. SentenceSplitter
creates deterministic node IDs within that document. Email and attachment nodes share the
active FP32 Qwen3 embedding-contract collection and matching persisted SimpleDocumentStore.
SQLite fingerprints cover text, source occurrences, chunking, and embedding contract.
Unchanged refreshes do no indexing; changed occurrences replace the document's derived nodes.
Parsed occurrences transition through pending/indexed/failed; failed embeddings retry without
repeating attachment downloads or extraction.
Search resolves all attachment citations through SQLite rather than trusting vector links.

The PDF reader currently extracts full-document text, so even page counts or page labels
cannot locate individual chunks reliably. Attachment results expose character offsets into
persisted extracted text, never inferred PDF pages. UTF-8 truncation drops incomplete final
characters so offsets still identify the returned text.

Service tests use real installed readers with synthetic PDF, DOCX, and text bytes, local
in-process and on-disk Qdrant, and deterministic mocked dense embeddings. They cover mixed
email/attachment results, type and filename filters, correlated occurrence filters, source
attribution from SQLite, separate multi-chunk documents with identical filenames, Unicode
locations, force rebuilds, restart persistence, and contract migration from persisted text.
CLI tests verify filter forwarding and attachment citations. These checks do not establish
real Qwen attachment retrieval quality or server-Qdrant acceptance. The prior email-only live
Qwen verification is recorded separately in qwen-fastembed-compatibility.md.
