from __future__ import annotations

import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from qdrant_client import QdrantClient
from test_search import ControlledEmbedding, archived_message

from email_agent.archive import SQLiteArchive
from email_agent.config import Settings
from email_agent.retrieval import HybridSearchIndex
from email_agent.search import SearchRequest


class EmbeddingMigrationTests(TestCase):
    def test_reindex_command_rebuilds_local_sources_without_gmail(self) -> None:
        from email_agent.cli import main

        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("local", "Invoice local-only proof"))
            source = archive.message_body_path("local")
            original = source.read_bytes()
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            with (
                patch("sys.argv", ["email-agent", "--reindex"]),
                patch.dict(
                    os.environ,
                    {
                        "OPENROUTER_API_KEY": "test-key",
                        "OPENROUTER_MODEL": "model",
                        "EMAIL_AGENT_DATA_DIR": directory,
                    },
                ),
                patch("email_agent.cli.GmailAgentAdapter", side_effect=AssertionError("Gmail")),
                patch("email_agent.cli.run_application", side_effect=AssertionError("startup")),
                patch("email_agent.retrieval.HybridSearchIndex", return_value=index),
                patch("builtins.print"),
            ):
                main()
                main()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(index.client.count(index.collection, exact=True).count, 1)
            self.assertEqual(index.search(SearchRequest("invoice"))[0].gmail_message_id, "local")

    def test_official_adapter_receives_exact_model_cpu_and_persistent_cache(self) -> None:
        with TemporaryDirectory() as directory:
            settings = Settings("key", "model", data_dir=Path(directory))
            with patch(
                "email_agent.retrieval.FastEmbedEmbedding", return_value=ControlledEmbedding()
            ) as factory:
                HybridSearchIndex(
                    settings, SQLiteArchive(Path(directory)), client=QdrantClient(":memory:")
                )
            factory.assert_called_once_with(
                model_name="Qwen/Qwen3-Embedding-0.6B",
                cache_dir=str(Path(directory) / "models"),
                providers=["CPUExecutionProvider"],
            )

    def test_same_dimension_model_change_rebuilds_without_touching_sources(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("source", "Invoice migration proof"))
            source = archive.message_body_path("source")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            client = QdrantClient(":memory:")
            settings = Settings("key", "model", data_dir=Path(directory))
            first = HybridSearchIndex(
                settings, archive, client=client, embed_model=ControlledEmbedding()
            )
            first.refresh()
            changed = Settings(
                "key", "model", data_dir=Path(directory), embedding_model="test-same-dimension"
            )
            second = HybridSearchIndex(
                changed, archive, client=client, embed_model=ControlledEmbedding()
            )
            self.assertNotEqual(first.collection, second.collection)
            self.assertNotEqual(first.docstore_path, second.docstore_path)
            self.assertEqual(second.search(SearchRequest("invoice")), [])
            self.assertEqual(second.refresh(), 1)
            self.assertEqual(second.refresh(), 0)
            self.assertEqual(second.refresh(force=True), 1)
            self.assertEqual(client.count(second.collection, exact=True).count, 1)
            self.assertEqual(second.search(SearchRequest("invoice"))[0].gmail_message_id, "source")
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertTrue(client.collection_exists(first.collection))

    def test_interrupted_force_rebuild_retries_missing_vectors(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("source", "Invoice recovery"))
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            with (
                patch.object(index.index, "insert_nodes", side_effect=RuntimeError("interrupted")),
                self.assertRaises(RuntimeError),
            ):
                index.refresh(force=True)
            self.assertEqual(index.refresh(), 1)
            self.assertEqual(index.search(SearchRequest("invoice"))[0].gmail_message_id, "source")


@skipUnless(
    os.environ.get("EMAIL_AGENT_LIVE_QWEN") == "1", "requires downloaded Qwen and server Qdrant"
)
class LiveQwenIntegrationTests(TestCase):
    def test_downloaded_qwen_cpu_hybrid_retrieval_and_restart(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(
                archived_message("leave", "Your annual leave has been approved for August.")
            )
            archive.store_message(archived_message("invoice", "Invoice ZX-920 totals 42 EUR."))
            settings = Settings(
                "key",
                "model",
                data_dir=Path(directory),
                qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
            )
            from llama_index.embeddings.fastembed import FastEmbedEmbedding

            model = FastEmbedEmbedding(
                model_name=settings.embedding_model,
                cache_dir=str(Path("data/models").resolve()),
                providers=["CPUExecutionProvider"],
            )
            self.assertEqual(len(model.get_text_embedding("test")), 1024)
            client = QdrantClient(url=settings.qdrant_url)
            from uuid import uuid4

            namespace = "email_agent_test_" + uuid4().hex
            index = HybridSearchIndex(
                settings,
                archive,
                client=client,
                embed_model=model,
                collection_namespace=namespace,
            )
            try:
                self.assertEqual(index.refresh(), 2)
                self.assertEqual(index.refresh(), 0)
                self.assertEqual(
                    index.search(SearchRequest("ZX-920", limit=1))[0].gmail_message_id, "invoice"
                )
                self.assertEqual(
                    index.search(SearchRequest("When can I take a holiday?", limit=1))[
                        0
                    ].gmail_message_id,
                    "leave",
                )
                self.assertEqual(index.refresh(force=True), 2)
                self.assertEqual(client.count(index.collection, exact=True).count, 2)
                client.close()
                client = QdrantClient(url=settings.qdrant_url)
                index = HybridSearchIndex(
                    settings,
                    archive,
                    client=client,
                    embed_model=model,
                    collection_namespace=namespace,
                )
                self.assertEqual(index.refresh(), 0)
                self.assertEqual(
                    index.search(SearchRequest("ZX-920", limit=1))[0].gmail_message_id,
                    "invoice",
                )
            finally:
                client.delete_collection(index.collection)
                client.close()
