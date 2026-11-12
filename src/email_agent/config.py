"""Environment-backed application configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when environment configuration is missing or invalid."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Settings:
    """Validated settings needed to boot the local CLI."""

    openrouter_api_key: str
    openrouter_model: str
    qdrant_url: str = "http://localhost:6333"
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    debug: bool = False
    initial_import_limit: int = 100
    data_dir: Path = Path("data")
    attachment_max_bytes: int = 20 * 1024 * 1024
    search_context_chars: int = 1_000_000
    search_context_tokens: int = 6000

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Settings:
        problems: list[str] = []
        api_key = environ.get("OPENROUTER_API_KEY", "").strip()
        model = environ.get("OPENROUTER_MODEL", "").strip()
        if not api_key:
            problems.append("OPENROUTER_API_KEY is required")
        if not model:
            problems.append("OPENROUTER_MODEL is required")

        qdrant_url = environ.get("QDRANT_URL", cls.qdrant_url).strip().rstrip("/")
        for name, value in (("QDRANT_URL", qdrant_url),):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                problems.append(f"{name} must be an http(s) URL")

        embedding_model = environ.get("EMAIL_AGENT_EMBEDDING_MODEL", cls.embedding_model).strip()
        if embedding_model != cls.embedding_model:
            problems.append("EMAIL_AGENT_EMBEDDING_MODEL must be Qwen/Qwen3-Embedding-0.6B")

        debug_value = environ.get("EMAIL_AGENT_DEBUG", "false").strip().lower()
        if debug_value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            problems.append("EMAIL_AGENT_DEBUG must be true or false")

        import_limit_value = environ.get("EMAIL_AGENT_INITIAL_IMPORT_LIMIT", "100").strip()
        try:
            initial_import_limit = int(import_limit_value)
        except ValueError:
            initial_import_limit = 0
        if not 1 <= initial_import_limit <= 100:
            problems.append("EMAIL_AGENT_INITIAL_IMPORT_LIMIT must be between 1 and 100")

        data_dir_value = environ.get("EMAIL_AGENT_DATA_DIR", "data").strip()
        if not data_dir_value:
            problems.append("EMAIL_AGENT_DATA_DIR must not be empty")

        attachment_max_value = environ.get(
            "EMAIL_AGENT_ATTACHMENT_MAX_BYTES", str(cls.attachment_max_bytes)
        ).strip()
        try:
            attachment_max_bytes = int(attachment_max_value)
        except ValueError:
            attachment_max_bytes = 0
        if attachment_max_bytes < 1:
            problems.append("EMAIL_AGENT_ATTACHMENT_MAX_BYTES must be a positive integer")

        try:
            context_chars = int(environ.get("EMAIL_AGENT_SEARCH_CONTEXT_CHARS", "1000000"))
            if context_chars < 1:
                raise ValueError
        except ValueError:
            context_chars = 0
            problems.append("EMAIL_AGENT_SEARCH_CONTEXT_CHARS must be positive")
        try:
            context_tokens = int(environ.get("EMAIL_AGENT_SEARCH_CONTEXT_TOKENS", "6000"))
            if context_tokens < 1:
                raise ValueError
        except ValueError:
            context_tokens = 0
            problems.append("EMAIL_AGENT_SEARCH_CONTEXT_TOKENS must be positive")
        if problems:
            raise ConfigurationError(problems)

        return cls(
            openrouter_api_key=api_key,
            openrouter_model=model,
            qdrant_url=qdrant_url,
            embedding_model=embedding_model,
            debug=debug_value in {"1", "true", "yes", "on"},
            initial_import_limit=initial_import_limit,
            data_dir=Path(data_dir_value),
            attachment_max_bytes=attachment_max_bytes,
            search_context_chars=context_chars,
            search_context_tokens=context_tokens,
        )
