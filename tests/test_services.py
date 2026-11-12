from __future__ import annotations

import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

from email_agent.config import Settings
from email_agent.services import HttpServiceAdapter


class OllamaAdapterTests(TestCase):
    def test_pull_model_uses_ollama_model_field(self) -> None:
        settings = Settings("test-key", "test/model")
        adapter = HttpServiceAdapter(settings)
        response_context = MagicMock()

        with patch("email_agent.services.urlopen", return_value=response_context) as open_url:
            adapter.pull_model("bge-m3")

        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload, {"model": "bge-m3", "stream": False})
