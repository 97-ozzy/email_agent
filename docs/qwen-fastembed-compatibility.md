# Qwen3 FastEmbed compatibility and migration

Verified 2026-11-12 before implementation. Initial installed/locked versions were
FastEmbed 0.9.1, ONNX Runtime 1.31.1, llama-index-core 0.14.25,
llama-index-vector-stores-qdrant 0.10.4, qdrant-client 1.20.0,
llama-index-embeddings-ollama 0.10.0 and Python 3.14.7.
The FastEmbed LlamaIndex adapter was not installed.

## Compatibility evidence

- Installed `TextEmbedding.list_supported_models()` registers the exact
  `Qwen/Qwen3-Embedding-0.6B` identifier. Its source is
  `Qdrant/Qwen3-Embedding-0.6B-onnx`, FP32 `onnx/model.onnx` plus external weights,
  approximately 2.38 GB, 1024 dimensions, normalized last-token pooling, up to 32768 tokens.
  [Pinned FastEmbed source](https://github.com/qdrant/fastembed/blob/v0.9.1/fastembed/text/last_token_normalized_embedding.py)
- The quantized `-Q` variant is a separate identifier requiring ORT >=1.23;
  it has not been substituted. [Export and runtime requirements](https://huggingface.co/Qdrant/Qwen3-Embedding-0.6B-onnx)
- LlamaIndex `FastEmbedEmbedding` 0.7.0 delegates document input to `.embed()` and
  queries to `.query_embed()`. Neither adapter adds the Qwen instruction. Use raw
  documents and dense queries prefixed with an English task instruction. Pass the original
  query plus its precomputed dense vector through `QueryBundle` into `as_retriever()`;
  sparse BM25 therefore receives the original query.
  [Official adapter](https://github.com/run-llama/llama_index/blob/main/llama-index-integrations/embeddings/llama-index-embeddings-fastembed/llama_index/embeddings/fastembed/base.py),
  [Qwen instructions](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- Adapter package metadata declares Python >=3.10,<3.13 and supports the installed core.
  Project runtime is now Python >=3.11,<3.13; verified on CPython 3.12.14 rather than
  bypassing that restriction or introducing a wrapper.
  [Package metadata](https://pypi.org/project/llama-index-embeddings-fastembed/)
- CPUExecutionProvider works on Windows x64 with installed ONNX Runtime 1.31.1.
  Windows requires the Visual C++ runtime; Linux/macOS have not been exercised here.
  [ONNX Runtime platform requirements](https://onnxruntime.ai/docs/install/#requirements)

## Migration

The active collection and document-store directory include a hash of provider, model,
artifact/precision, dimensions, normalization, instruction and contract version. Old shared
`email_agent_sources` vectors cannot be reused even though both models have 1024 dimensions.
Legacy derived collections and document stores remain preserved but unused. Each active
contract has one shared collection for email and future attachment nodes.

SQLite source records, raw bodies, attachments and extracted attachment text are preserved.
Per-document fingerprints include the embedding contract. First search indexes local bodies
under the new contract; `email-agent --reindex` forces local email rebuilding without creating
a Gmail adapter. Interruptions invalidate per-document completion before vector replacement,
so retries upsert deterministic nodes. Unified attachment indexing/rebuilding remains in
Tickets 06 and 12; no attachment extraction/download is repeated by this change.

No server runtime is required for embeddings. Model weights cache in data/models.
The already-running legacy embedding container/volume was not stopped or deleted; Compose
and the application no longer require or reference it.

## Verification

Mocked tests inject deterministic embeddings with in-process/local persistent Qdrant;
they cover exact/semantic routing, filters, budgets, persistence, adapter initialization,
equal-dimension model changes, source preservation and interrupted force rebuild retries.

Real downloaded FP32 Qwen3 inference produced 1024-dimensional document and instructed-query
vectors on Windows CPU. The opt-in `LiveQwenIntegrationTests` also passed against the running
Qdrant 1.20.0 server using a disposable synthetic collection: exact ZX-920 lookup, semantic
holiday/annual-leave match, idempotency and force rebuild. It does not use personal Gmail.
It also reconnects to the server and reconstructs the index/document store before verifying
retrieval and zero new indexing work again.

## Dependency inventory

- `pyproject.toml`, `uv.lock`: official FastEmbed adapter replaces the old adapter/client;
  direct FastEmbed version is pinned and Python runtime constrained to its declared support.
- `retrieval.py`: embedding initialization, instructions, 1024-dimensional contract,
  collection selection, document-store persistence, fingerprints, local rebuilding.
- `config.py`, `.env.example`: embedding model setting; no embedding-server URL.
- `services.py`, `cli.py`: CPU/registry diagnostics, on-demand download, local-only reindex.
- `compose.yaml`: Qdrant only, persistent Qdrant volume. No model-server service.
- `README.md`, spec and Tickets 01/02/05/06/07/10/11/12/13/14: setup, source independence,
  active generation, restart/recovery, future attachment indexing and reindex contracts.
- Tests: initialization, CLI diagnostics, command-boundary local rebuild, equal-dimensional
  model changes, interruption/retry, persistence, raw sparse/instructed dense retrieval.

The original Python 3.14 virtual environment is held open by a running application process.
Verification used `.venv-qwen` with Python 3.12.14 via
`$env:UV_PROJECT_ENVIRONMENT='.venv-qwen'`; close the old CLI before recreating `.venv`
with Python 3.12. No running user process was terminated.

Run separately in PowerShell:

```powershell
$env:EMAIL_AGENT_LIVE_QWEN = '1'
uv run --with pytest pytest tests/test_embedding_migration.py::LiveQwenIntegrationTests -q
```

Routine tests skip this model/server check. Full personal-mail end-to-end acceptance,
Linux/macOS verification and future attachment retrieval remain unverified.

Final validation: 62 routine tests passed, 1 opt-in live test skipped by default; the live
test passed separately with the downloaded model and running server, including reconnect and
document-store reload. Strict mypy, Ruff, whitespace checks and both independent review axes
passed before commit.
