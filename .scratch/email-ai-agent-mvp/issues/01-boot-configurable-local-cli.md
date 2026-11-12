# 01: Boot a configurable local CLI

**What to build:** A user can install and launch the application locally, receive clear configuration and service diagnostics, discover the supported commands, and start or stop the required local infrastructure without encountering unexplained tracebacks.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The project installs reproducibly with uv using standard Python project metadata.
- [ ] Launching the CLI validates required environment settings and reports actionable configuration errors without a raw traceback in normal mode.
- [ ] The CLI implements `/help`, `/status`, and `/exit`, and ordinary startup reaches an interactive prompt when the minimum configuration is valid.
- [ ] Docker Compose starts Qdrant and Ollama with persistent storage that survives ordinary container restarts.
- [ ] `/status` distinguishes available and unavailable Qdrant and Ollama services.
- [ ] When `bge-m3` is missing, startup offers a clear opt-in path to download it rather than downloading it silently.
- [ ] Automated tests exercise the CLI/application boundary with controlled service adapters and assert terminal output.

