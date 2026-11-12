# Neighboring context implementation notes

Implementation gate verified before code changes on 2026-11-12, Windows,
Python 3.12, llama-index-core 0.14.25. A local probe used SentenceSplitter
with the existing UUID5 id_func, SimpleDocumentStore.add_documents/persist,
from_persist_path, and PrevNextNodePostprocessor(docstore=..., num_nodes=2,
mode="both"). Repeated parsing produced identical IDs. Reloaded nodes retained
SOURCE, PREVIOUS, and NEXT links; expansion returned the expected five nodes.
The public postprocess_nodes interface needs no query, embedding, or model.
The adapter sorts by persisted chunk_index and validate document membership.

Official interfaces: https://developers.llamaindex.ai/python/framework/module_guides/storing/docstores/
and https://developers.llamaindex.ai/python/framework/module_guides/querying/node_postprocessors/node_postprocessors/

Implemented stable chunk IDs/positions with existing relationships, a local-only
get_local_document method with structured errors and a text budget, and tests for
reload, ordering, limits, and collection/source isolation. Independent standards
and specification reviews both returned zero findings.

Validation completed on Windows/Python 3.12.14 with deterministic dense embeddings
and the installed local Qdrant client. Five public-interface context tests cover
search IDs, ordered expansion, boundaries, complete-neighbor budgeting, explicit
target truncation, structured errors, cross-document rejection, attachment
citations, contract migration, deterministic rebuilds, and reload in a separate
Python process. Full suite: 75 tests, 74 passed, one opt-in real-Qwen/server-Qdrant
check skipped. Ruff and strict mypy pass. These checks do not establish real-model
retrieval quality or server-Qdrant acceptance.

The chunk-position schema participates in SQLite fingerprints, so the next
refresh replaces earlier derived nodes with positions. UUID5 chunk IDs and the
embedding contract remain stable. Sources and extracted text remain authoritative.
