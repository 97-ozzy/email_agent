"""Qdrant availability and local FastEmbed capability diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.error import URLError
from urllib.request import urlopen

from email_agent.config import Settings


@dataclass(frozen=True)
class ServiceStatus:
    qdrant_available: bool
    fastembed_available: bool
    embedding_model_available: bool


class ServiceAdapter(Protocol):
    def status(self, embedding_model: str) -> ServiceStatus: ...


class HttpServiceAdapter:
    def __init__(self, settings: Settings, *, timeout_seconds: float = 2.0) -> None:
        self._qdrant_url = settings.qdrant_url
        self._timeout_seconds = timeout_seconds

    def status(self, embedding_model: str) -> ServiceStatus:
        try:
            with urlopen(f"{self._qdrant_url}/readyz", timeout=self._timeout_seconds) as response:
                response.read()
            available = True
        except (URLError, OSError, TimeoutError):
            available = False
        try:
            import onnxruntime  # type: ignore[import-untyped]
            from fastembed import TextEmbedding

            runtime = "CPUExecutionProvider" in onnxruntime.get_available_providers()
            supported = any(
                m["model"] == embedding_model for m in TextEmbedding.list_supported_models()
            )
        except ImportError:
            runtime = supported = False
        return ServiceStatus(available, runtime, supported)
