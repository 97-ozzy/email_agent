# Email Agent

Email Agent is a local, read-only email AI agent. It provides a configurable CLI, local
Qdrant/FastEmbed diagnostics, and live read-only Gmail search and retrieval. Gmail ingestion and
chat arrive in later tickets.

## Requirements

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/)
- Docker with Docker Compose

## Install

Create a reproducible environment from the checked-in lockfile:

```shell
uv sync --locked
```

Copy `.env.example` to `.env`, replace the placeholder values, then export those settings in
your shell. The application reads environment variables directly and never reads or commits the
`.env` file itself.

PowerShell example:

```powershell
$env:OPENROUTER_API_KEY = "your-key"
$env:OPENROUTER_MODEL = "openai/gpt-4.1-mini"
```

`OPENROUTER_API_KEY` and `OPENROUTER_MODEL` are required. The local service URLs and the
`Qwen/Qwen3-Embedding-0.6B` embedding model have defaults documented in `.env.example`.

## Local infrastructure

Start or stop Qdrant with:

```shell
docker compose up -d
docker compose down
```

Named `qdrant_data` volume retains indexes across normal container
restarts and `docker compose down`. To deliberately remove all service data, run
`docker compose down --volumes`.

## Run the CLI

```shell
uv run --locked email-agent
```

On the first run, the application delegates Gmail authorization to the open-source
[`gmail-agent`](https://github.com/dremok/gmail-agent) integration. Its setup opens the Google
Cloud and OAuth pages in your browser and always requests the `gmail.readonly` scope. If an OAuth
client is already installed but its login is missing or expired, a read-only browser login starts
instead. Later runs reuse the saved token and refresh it when possible.

`gmail-agent` stores its OAuth client and token outside this repository in its user configuration
directory (normally `~/.config/gmail-agent`). It writes the directory and token with owner-only
permissions. Treat both files as secrets and never copy them into the project. To inspect the
login without printing credentials, run `uv run --locked gmail-agent status`.

Startup reports Gmail, Qdrant and FastEmbed separately. A Gmail failure does not hide local-service
status. On the first authenticated run, the application imports the newest 100 unique messages
from the combined Inbox and Sent scope. Set `EMAIL_AGENT_INITIAL_IMPORT_LIMIT` to a value from 1
through 100 for a smaller import, use `EMAIL_AGENT_DATA_DIR` to choose the archive directory,
and set `EMAIL_AGENT_ATTACHMENT_MAX_BYTES` to cap text extraction by attachment byte size
(default: 20971520). The data directory defaults to `data`. Complete message bodies are stored
under that directory and metadata plus
ingestion/indexing state are stored in `archive.sqlite3`. Later starts reuse completed records
without downloading them again. If an import is partial, the CLI reports actual counts and keeps
the completed records usable.

FastEmbed downloads the exact FP32 Qwen3 ONNX model (~2.38 GB) on first search/reindex into data/models. Startup/status do not load model weights.

Interactive commands currently available:

- `/help` — list supported commands
- `/status` - refresh Gmail, Qdrant, FastEmbed, and embedding-model diagnostics
- `/gmail-search QUERY` - search live Gmail using Gmail search syntax
- `/gmail-message ID` - retrieve one complete live Gmail message
- `/gmail-thread ID` - retrieve one live Gmail conversation
- `/exit` — leave cleanly

The application exposes no Gmail send, draft, delete, archive, or label-modification command.
If an existing `gmail-agent` login has a broader access level, startup replaces it with a new
read-only authorization before Gmail becomes available.

## Troubleshooting

- A configuration error names every missing or invalid environment variable and exits without a
  traceback.
- If a service is unavailable, start it with `docker compose up -d` and use `/status` again.
- If Gmail authorization is cancelled or rejected, follow the exact `gmail-agent setup` or
  `gmail-agent login --scope readonly` command shown by `/status`.
- If a personal OAuth app remains in Google Testing mode, its refresh token may expire after seven
  days; publish the app for personal production use or repeat the read-only login.
- Use `docker compose logs qdrant` to inspect container failures.
- Set `EMAIL_AGENT_DEBUG=true` to allow detailed Python tracebacks while developing.

## Tests

### Local email search

Run `/search QUERY` to index archived email bodies and extracted PDF, DOCX, and text attachments
and retrieve passages using FastEmbed `Qwen/Qwen3-Embedding-0.6B`
and FastEmbed BM25 with LlamaIndex's built-in relative-score fusion. Qdrant must be
running. The first BM25 use downloads its small tokenizer resources.

For example: `/search invoice --sender alice@example.com --after 2026-11-01 --before 2026-11-30
--limit 3 --source email`. Dates are inclusive UTC calendar dates; sender matches an email
address. Results default to 8, with a range of 1–20. `EMAIL_AGENT_SEARCH_CONTEXT_TOKENS` bounds
total passage text (default 6000 tokens), while source metadata remains complete. An optional
additional character ceiling is available through `EMAIL_AGENT_SEARCH_CONTEXT_CHARS`.

Index processing state and the LlamaIndex document store persist beneath the data directory;
Qdrant uses the existing Compose volume. Changed sources replace old chunks. Original archived
bodies remain intact. Search returns evidence directly without calling a generative model.
Use `--source attachment`, `--filename "invoice.pdf"` (exact, case-sensitive original name),
or `--type pdf|docx|text|email` to narrow results. Combined filename, sender, and date filters
must match the same attachment occurrence. Identical bytes share one document; each result
includes all archived message occurrences resolved from SQLite, including filenames, MIME
types, Gmail attachment IDs, and paths to the retained files. The singular citation selects
an occurrence matching the filters. Character ranges refer to persisted extracted text.
PDF page numbers are omitted because the current full-document parser does not preserve
trustworthy page boundaries. Reindexing reuses extracted text without downloading or parsing
attachments again.

### Running checks

The CLI/application tests use controlled service adapters and do not require Docker or secrets:

```shell
$env:PYTHONPATH = "src"  # PowerShell
python -m unittest discover -s tests -v
```

### Qwen3 runtime and migration

Use Python 3.11 or 3.12 (`uv sync --python 3.12`). The official LlamaIndex FastEmbed
adapter currently declares Python <3.13. Windows CPU inference requires ONNX Runtime and
the Visual C++ runtime. No embedding server is needed.

`EMAIL_AGENT_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B` selects the exact FP32 model,
1024 dimensions. Queries use Qwen's instruction format; documents stay unprefixed.
Status means CPU/runtime and model registry support, not downloaded weights or tested inference.

New embedding contracts use `email_agent_sources_<contract-hash>` and
`data/index/<contract-hash>/docstore.json`. The legacy collection is preserved but never queried.
Even equal-dimension model changes rebuild vectors from local bodies. Run
`email-agent --reindex` to force a local email and attachment rebuild without Gmail access.
Attachments and their extracted text remain preserved; indexing/rebuilding them is still
assigned to Tickets 06/12. No unrelated future ticket is implemented here.
See [compatibility evidence](docs/qwen-fastembed-compatibility.md) for real versus mocked checks.

### Neighboring document context

Search results expose `chunk_id` and zero-based `chunk_index`. Call
`HybridSearchIndex.get_local_document(document_id, chunk_id=..., num_nodes=1)`
or pass `chunk_index=...` instead of `chunk_id`. `num_nodes` accepts 0�2 per
direction, defaulting to one previous and one next chunk. Results contain ordered
chunks, source metadata, and authoritative attachment references. Unknown IDs,
invalid requests, or incomplete derived nodes return a structured `error` with
`code` and `message` and no chunks.

`EMAIL_AGENT_DOCUMENT_CONTEXT_TOKENS` limits returned passage text to 3000 tokens
by default. The target takes priority; only complete neighbors that fit are
included. A target exceeding the whole budget is truncated and marked
`truncated=True`. Citation metadata remains complete. Search does not automatically
expand results. Expansion reads the active collection's local document store,
without vector queries, embedding generation, or generative model calls.
Document stores are derived data: rebuild from SQLite and preserved source text
using `--reindex` if needed. See [compatibility notes](docs/document-context.md).
