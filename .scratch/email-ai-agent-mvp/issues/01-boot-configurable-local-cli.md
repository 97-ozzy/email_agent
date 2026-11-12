# 01: Boot a configurable local CLI

**What to build:** A user can install and launch the application locally, receive clear configuration and service diagnostics, discover the supported commands, and start or stop the required local infrastructure without encountering unexplained tracebacks.

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] The project installs reproducibly with uv using standard Python project metadata.
- [ ] Launching the CLI validates required environment settings and reports actionable configuration errors without a raw traceback in normal mode.
- [ ] The CLI implements `/help`, `/status`, and `/exit`, and ordinary startup reaches an interactive prompt when the minimum configuration is valid.
- [ ] Docker Compose starts Qdrant with persistent storage; FastEmbed runs in-process with a persistent model cache that survives ordinary container restarts.
- [ ] `/status` distinguishes Qdrant availability and FastEmbed CPU/runtime and model-registry support.
- [ ] Startup/status do not load weights. The first explicit search/reindex downloads the exact Qwen3 model into data/models and reports download failures clearly.
- [ ] Automated tests exercise the CLI/application boundary with controlled service adapters and assert terminal output.
