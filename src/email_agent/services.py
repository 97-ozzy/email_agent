"""Adapters for local Qdrant and Ollama diagnostics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from email_agent.config import Settings


@dataclass(frozen=True)
class ServiceStatus:
    """Availability of the local infrastructure required by the CLI."""

    qdrant_available: bool
    ollama_available: bool
    embedding_model_available: bool


class ServiceAdapter(Protocol):
    """Public boundary used by the CLI for service diagnostics."""

    def status(self, embedding_model: str) -> ServiceStatus:
        """Return current local-service and embedding-model availability."""

    def pull_model(self, model: str) -> None:
        """Download an Ollama model after explicit user consent."""


class HttpServiceAdapter:
    """Query Qdrant and Ollama through their local HTTP APIs."""

    def __init__(self, settings: Settings, *, timeout_seconds: float = 2.0) -> None:
        self._qdrant_url = settings.qdrant_url
        self._ollama_url = settings.ollama_url
        self._timeout_seconds = timeout_seconds

    def status(self, embedding_model: str) -> ServiceStatus:
        qdrant_available = self._is_available(f"{self._qdrant_url}/readyz")
        tags = self._get_json(f"{self._ollama_url}/api/tags")
        ollama_available = tags is not None
        model_available = False
        if isinstance(tags, dict):
            models = tags.get("models", [])
            if isinstance(models, list):
                names = {
                    item.get("name", "").split(":", maxsplit=1)[0]
                    for item in models
                    if isinstance(item, dict)
                }
                model_available = embedding_model.split(":", maxsplit=1)[0] in names
        return ServiceStatus(qdrant_available, ollama_available, model_available)

    def pull_model(self, model: str) -> None:
        body = json.dumps({"model": model, "stream": False}).encode()
        request = Request(
            f"{self._ollama_url}/api/pull",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=1800) as response:  # noqa: S310 - configured local URL
            response.read()

    def _is_available(self, url: str) -> bool:
        request = Request(url, method="GET")
        try:
            with urlopen(  # noqa: S310 - configured local URL
                request, timeout=self._timeout_seconds
            ) as response:
                response.read()
        except (HTTPError, URLError, OSError, TimeoutError):
            return False
        return True

    def _get_json(self, url: str) -> object | None:
        request = Request(url, method="GET")
        try:
            with urlopen(  # noqa: S310 - configured local URL
                request, timeout=self._timeout_seconds
            ) as response:
                return cast(object, json.loads(response.read()))
        except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
            return None
