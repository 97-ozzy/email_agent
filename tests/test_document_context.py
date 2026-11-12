from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import TestCase

from qdrant_client import QdrantClient
from test_search import ControlledEmbedding, archived_message

from email_agent.archive import SQLiteArchive
from email_agent.config import Settings
from email_agent.retrieval import HybridSearchIndex
from email_agent.search import SearchRequest


class DocumentContextTests(TestCase):
    def test_search_passage_expands_in_order_with_citations_after_reload(self) -> None:
        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("long", "Invoice numbered clause. " * 1600))
            client = QdrantClient(":memory:")
            settings = Settings("key", "model", data_dir=Path(directory))
            index = HybridSearchIndex(
                settings, archive, client=client, embed_model=ControlledEmbedding()
            )
            index.refresh()
            hit = index.search(SearchRequest("invoice", limit=1))[0]
            self.assertTrue(hit.chunk_id)
            reloaded = HybridSearchIndex(
                settings, archive, client=client, embed_model=ControlledEmbedding()
            )
            result = reloaded.get_local_document(hit.document_id, chunk_index=2)
            self.assertIsNone(result.error)
            self.assertEqual([c.chunk_index for c in result.chunks], [1, 2, 3])
            for chunk in result.chunks:
                self.assertEqual(chunk.document_id, "email:long")
                self.assertEqual(chunk.metadata["gmail_message_id"], "long")
                self.assertTrue(chunk.metadata["location"])
            exact = reloaded.get_local_document(hit.document_id, chunk_id=hit.chunk_id, num_nodes=0)
            self.assertEqual(exact.chunks[0].chunk_index, hit.chunk_index)
            self.assertEqual(exact.chunks[0].text, hit.text)

    def test_boundaries_errors_and_source_isolation_without_network_or_embeddings(self) -> None:
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            for name in ("first", "other"):
                archive.store_message(archived_message(name, f"Invoice {name} fact. " * 1600))
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            other = index.get_local_document("email:other", chunk_index=0, num_nodes=0).chunks[0]
            with (
                patch.object(
                    index.client, "query_points", side_effect=AssertionError("vector search")
                ),
                patch.object(
                    ControlledEmbedding,
                    "_get_query_embedding",
                    side_effect=AssertionError("embedding"),
                ),
            ):
                start = index.get_local_document("email:first", chunk_index=0, num_nodes=2)
                self.assertEqual([c.chunk_index for c in start.chunks], [0, 1, 2])
                self.assertTrue(all("other" not in c.text for c in start.chunks))
                last = 0
                while (
                    index.get_local_document("email:first", chunk_index=last + 1, num_nodes=0).error
                    is None
                ):
                    last += 1
                end = index.get_local_document("email:first", chunk_index=last, num_nodes=2)
                self.assertEqual([c.chunk_index for c in end.chunks], [last - 2, last - 1, last])
                cases: list[tuple[dict[str, Any], str]] = [
                    ({"chunk_index": 10000}, "unknown_chunk"),
                    ({"chunk_id": "missing"}, "unknown_chunk"),
                    ({"chunk_id": other.chunk_id}, "unknown_chunk"),
                    ({"chunk_index": 0, "num_nodes": 3}, "invalid_request"),
                    ({"chunk_index": 0, "num_nodes": -1}, "invalid_request"),
                    ({"chunk_index": -1}, "invalid_request"),
                    ({}, "invalid_request"),
                    ({"chunk_index": 0, "chunk_id": other.chunk_id}, "invalid_request"),
                ]
                for kwargs, code in cases:
                    result = index.get_local_document("email:first", **kwargs)
                    self.assertIsNotNone(result.error)
                    assert result.error is not None
                    self.assertEqual(result.error.code, code)
                    self.assertEqual(result.chunks, ())
                missing = index.get_local_document("missing", chunk_index=0)
                assert missing.error is not None
                self.assertEqual(missing.error.code, "unknown_document")

    def test_configured_token_limit_truncates_only_an_oversized_target(self) -> None:
        import tiktoken

        from email_agent.config import ConfigurationError

        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(
                archived_message("long", "Invoice clause with unicode \U0001f680. " * 1600)
            )
            settings = Settings.from_environ(
                {
                    "OPENROUTER_API_KEY": "key",
                    "OPENROUTER_MODEL": "model",
                    "EMAIL_AGENT_DATA_DIR": directory,
                    "EMAIL_AGENT_DOCUMENT_CONTEXT_TOKENS": "600",
                }
            )
            self.assertEqual(settings.document_context_tokens, 600)
            index = HybridSearchIndex(
                settings,
                archive,
                client=QdrantClient(":memory:"),
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            result = index.get_local_document("email:long", chunk_index=2, num_nodes=2)
            self.assertEqual([c.chunk_index for c in result.chunks], [2])
            self.assertFalse(result.chunks[0].truncated)
            tiny = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory), document_context_tokens=7),
                archive,
                client=index.client,
                embed_model=ControlledEmbedding(),
            )
            result = tiny.get_local_document("email:long", chunk_index=2)
            self.assertEqual([c.chunk_index for c in result.chunks], [2])
            self.assertTrue(result.chunks[0].truncated)
            self.assertLessEqual(
                len(tiktoken.get_encoding("cl100k_base").encode(result.chunks[0].text)), 7
            )
            for value in ("0", "-1", "garbage"):
                with self.assertRaises(ConfigurationError):
                    Settings.from_environ(
                        {
                            "OPENROUTER_API_KEY": "key",
                            "OPENROUTER_MODEL": "model",
                            "EMAIL_AGENT_DOCUMENT_CONTEXT_TOKENS": value,
                        }
                    )

    def test_attachment_context_preserves_sources_and_excludes_other_contracts(self) -> None:
        from test_attachments import AttachmentGmail, attachment, run_import

        with TemporaryDirectory() as directory:
            data = b"Invoice attachment clause. " * 1600
            run_import(
                directory,
                AttachmentGmail([attachment("a", "contract.txt", "text/plain", data)], {"a": data}),
            )
            archive = SQLiteArchive(Path(directory))
            client = QdrantClient(":memory:")
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            hit = index.search(SearchRequest("invoice", source_type="attachment", limit=1))[0]
            expanded = index.get_local_document(hit.document_id, chunk_index=2)
            self.assertEqual([c.chunk_index for c in expanded.chunks], [1, 2, 3])
            for chunk in expanded.chunks:
                self.assertEqual(chunk.sources, hit.sources)
                self.assertEqual(chunk.sources[0].filename, "contract.txt")
                self.assertEqual(chunk.metadata["document_id"], hit.document_id)
            self.assertEqual(index.refresh(force=True), 2)
            rebuilt = index.get_local_document(hit.document_id, chunk_id=hit.chunk_id, num_nodes=0)
            self.assertEqual(rebuilt.chunks[0].chunk_id, hit.chunk_id)
            migrated = HybridSearchIndex(
                Settings(
                    "key", "model", data_dir=Path(directory), embedding_model="changed-contract"
                ),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            result = migrated.get_local_document(hit.document_id, chunk_id=hit.chunk_id)
            assert result.error is not None
            self.assertEqual(result.error.code, "unknown_document")
            self.assertNotEqual(migrated.docstore_path, index.docstore_path)
            self.assertEqual(migrated.refresh(), 2)
            self.assertIsNone(
                migrated.get_local_document(hit.document_id, chunk_id=hit.chunk_id).error
            )
            self.assertIsNone(
                index.get_local_document(hit.document_id, chunk_id=hit.chunk_id).error
            )

    def test_context_survives_a_separate_python_process(self) -> None:
        import json
        import subprocess
        import sys

        with TemporaryDirectory() as directory:
            archive = SQLiteArchive(Path(directory))
            archive.store_message(archived_message("restart", "Invoice stable clause. " * 1600))
            path = str(Path(directory) / "qdrant")
            client = QdrantClient(path=path)
            index = HybridSearchIndex(
                Settings("key", "model", data_dir=Path(directory)),
                archive,
                client=client,
                embed_model=ControlledEmbedding(),
            )
            index.refresh()
            before = index.get_local_document("email:restart", chunk_index=2)
            client.close()
            script = """
import json, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_search import ControlledEmbedding
from email_agent.archive import SQLiteArchive
from email_agent.config import Settings
from email_agent.retrieval import HybridSearchIndex
from qdrant_client import QdrantClient
p = Path(sys.argv[1])
client = QdrantClient(path=str(p / 'qdrant'))
index = HybridSearchIndex(Settings('key', 'model', data_dir=p), SQLiteArchive(p),
                          client=client, embed_model=ControlledEmbedding())
result = index.get_local_document('email:restart', chunk_index=2)
assert result.error is None
print(json.dumps([[c.chunk_id, c.chunk_index, c.text] for c in result.chunks]))
client.close()
"""
            process = subprocess.run(
                [sys.executable, "-c", script, directory],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                json.loads(process.stdout),
                [[c.chunk_id, c.chunk_index, c.text] for c in before.chunks],
            )
