# Email Agent

Email Agent is a local, read-only email AI agent. It provides a configurable CLI, local
Qdrant/Ollama diagnostics, and live read-only Gmail search and retrieval. Gmail ingestion and
chat arrive in later tickets.

## Requirements

- Python 3.11 or newer
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
`bge-m3` embedding model have defaults documented in `.env.example`.

## Local infrastructure

Start or stop Qdrant and Ollama with:

```shell
docker compose up -d
docker compose down
```

Named `qdrant_data` and `ollama_data` volumes retain indexes and models across normal container
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

Startup reports Gmail, Qdrant, and Ollama separately. A Gmail failure does not hide local-service
status. On the first authenticated run, the application imports the newest 100 unique messages
from the combined Inbox and Sent scope. Set `EMAIL_AGENT_INITIAL_IMPORT_LIMIT` to a value from 1
through 100 for a smaller import, and use `EMAIL_AGENT_DATA_DIR` to choose the archive directory
(default: `data`). Complete message bodies are stored under that directory and metadata plus
ingestion/indexing state are stored in `archive.sqlite3`. Later starts reuse completed records
without downloading them again. If an import is partial, the CLI reports actual counts and keeps
the completed records usable.

If Ollama is running but `bge-m3` is missing, the CLI asks before downloading it. You may also
install it yourself with `ollama pull bge-m3`.

Interactive commands currently available:

- `/help` — list supported commands
- `/status` - refresh Gmail, Qdrant, Ollama, and embedding-model diagnostics
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
- Use `docker compose logs qdrant ollama` to inspect container failures.
- Set `EMAIL_AGENT_DEBUG=true` to allow detailed Python tracebacks while developing.

## Tests

The CLI/application tests use controlled service adapters and do not require Docker or secrets:

```shell
$env:PYTHONPATH = "src"  # PowerShell
python -m unittest discover -s tests -v
```

