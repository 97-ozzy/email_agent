# Email Agent

Email Agent is a local, read-only email AI agent. Ticket 01 provides the configurable CLI
bootstrap and local Qdrant/Ollama diagnostics; Gmail ingestion and chat arrive in later tickets.

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

Startup reports Qdrant and Ollama separately. If Ollama is running but `bge-m3` is missing, the
CLI asks before downloading it. You may also install it yourself with `ollama pull bge-m3`.

Interactive commands in this ticket:

- `/help` — list supported commands
- `/status` — refresh Qdrant, Ollama, and embedding-model diagnostics
- `/exit` — leave cleanly

## Troubleshooting

- A configuration error names every missing or invalid environment variable and exits without a
  traceback.
- If a service is unavailable, start it with `docker compose up -d` and use `/status` again.
- Use `docker compose logs qdrant ollama` to inspect container failures.
- Set `EMAIL_AGENT_DEBUG=true` to allow detailed Python tracebacks while developing.

## Tests

The CLI/application tests use controlled service adapters and do not require Docker or secrets:

```shell
$env:PYTHONPATH = "src"  # PowerShell
python -m unittest discover -s tests -v
```

