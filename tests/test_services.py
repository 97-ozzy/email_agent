from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

from email_agent.config import Settings
from email_agent.services import HttpServiceAdapter


class FastEmbedDiagnosticsTests(TestCase):
    def test_status_checks_qdrant_and_registry_without_loading_weights(self) -> None:
        adapter = HttpServiceAdapter(Settings("key", "model"))
        with (
            patch("email_agent.services.urlopen", return_value=MagicMock()) as request,
            patch("fastembed.TextEmbedding.__init__", side_effect=AssertionError("weights")),
        ):
            status = adapter.status("Qwen/Qwen3-Embedding-0.6B")
        self.assertTrue(status.qdrant_available)
        self.assertTrue(status.fastembed_available)
        self.assertTrue(status.embedding_model_available)
        self.assertEqual(request.call_args.args[0], "http://localhost:6333/readyz")
