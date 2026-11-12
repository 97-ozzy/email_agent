"""Persistent LlamaIndex/Qdrant hybrid evidence retrieval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time
from email.utils import parseaddr
from uuid import NAMESPACE_URL, uuid5

import tiktoken
from llama_index.core import Document, StorageContext, VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import BaseNode
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from email_agent.archive import SQLiteArchive
from email_agent.config import Settings
from email_agent.search import SearchRequest, SearchResult, clean_email_body


def _node_id(index: int, document: BaseNode) -> str:
    return str(uuid5(NAMESPACE_URL, f"{document.id_}:{index}"))


class HybridSearchIndex:
    """A rebuildable derived index; archived sources remain authoritative."""

    def __init__(
        self,
        settings: Settings,
        archive: SQLiteArchive,
        *,
        client: QdrantClient | None = None,
        embed_model: BaseEmbedding | None = None,
    ) -> None:
        self.settings = settings
        self.archive = archive
        self.client = client or QdrantClient(url=settings.qdrant_url)
        self.collection = "email_agent_sources"
        self.store = QdrantVectorStore(
            collection_name=self.collection,
            client=self.client,
            enable_hybrid=True,
            fastembed_sparse_model="Qdrant/bm25",
        )
        self.directory = settings.data_dir / "index"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.docstore_path = self.directory / "docstore.json"
        self.docstore = (
            SimpleDocumentStore.from_persist_path(str(self.docstore_path))
            if self.docstore_path.exists()
            else SimpleDocumentStore()
        )
        self.index = VectorStoreIndex(
            nodes=[],
            storage_context=StorageContext.from_defaults(
                vector_store=self.store,
                docstore=self.docstore,
            ),
            embed_model=embed_model
            or OllamaEmbedding(
                model_name=settings.embedding_model,
                base_url=settings.ollama_url,
            ),
        )
        self.splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64, id_func=_node_id)
        with closing(sqlite3.connect(archive.database_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS email_index_state "
                "(document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )

    def refresh(self) -> int:
        """Upsert changed documents and replace stale chunks before recording completion."""
        with closing(sqlite3.connect(self.archive.database_path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM messages WHERE ingestion_status='downloaded'"
            ).fetchall()
            state = dict(db.execute("SELECT document_id, fingerprint FROM email_index_state"))
        collection_exists = self.client.collection_exists(self.collection)
        rebuild = not collection_exists
        changed = 0
        for row in rows:
            document_id = f"email:{row['gmail_message_id']}"
            body = (self.settings.data_dir / row["body_path"]).read_text(encoding="utf-8")
            metadata = {
                "document_id": document_id,
                "gmail_message_id": row["gmail_message_id"],
                "thread_id": row["gmail_thread_id"],
                "source_type": "email",
                "title": row["subject"],
                "timestamp": row["message_timestamp"],
                "sender": row["sender"],
                "sender_address": parseaddr(row["sender"])[1].lower(),
                "location": row["source_reference"],
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        body,
                        row["body_format"],
                        metadata,
                        self.splitter.chunk_size,
                        self.splitter.chunk_overlap,
                        "clean-v1",
                        self.settings.embedding_model,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not rebuild and state.get(document_id) == fingerprint:
                continue
            text = clean_email_body(body, row["body_format"])
            document = Document(
                text=text,
                id_=document_id,
                metadata=metadata,
                excluded_embed_metadata_keys=list(metadata),
                excluded_llm_metadata_keys=list(metadata),
            )
            nodes = self.splitter.get_nodes_from_documents([document]) if text else []
            if collection_exists:
                self.store.delete(document_id)
            self.docstore.delete_ref_doc(document_id, raise_error=False)
            if nodes:
                self.index.insert_nodes(nodes)
                self.docstore.add_documents(nodes)
                collection_exists = True
            temporary = self.docstore_path.with_suffix(".tmp")
            self.docstore.persist(str(temporary))
            temporary.replace(self.docstore_path)
            with closing(sqlite3.connect(self.archive.database_path)) as db, db:
                db.execute(
                    "INSERT INTO email_index_state VALUES (?, ?) ON CONFLICT(document_id) "
                    "DO UPDATE SET fingerprint=excluded.fingerprint",
                    (document_id, fingerprint),
                )
                db.execute(
                    "UPDATE messages SET indexing_status='indexed' WHERE gmail_message_id=?",
                    (row["gmail_message_id"],),
                )
            changed += 1
        return changed

    def search(self, request: SearchRequest) -> list[SearchResult]:
        """Retrieve evidence directly with the integration's built-in fusion."""
        if not request.query.strip() or not 1 <= request.limit <= 20:
            raise ValueError("search requires a query and a limit between 1 and 20")
        if not self.client.collection_exists(self.collection):
            return []
        conditions: list[models.FieldCondition] = []
        for key, value in (
            ("sender_address", request.sender),
            ("source_type", request.source_type),
        ):
            if value:
                conditions.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value.lower()),
                    )
                )
        if request.date_from or request.date_to:
            start = (
                datetime.fromisoformat(request.date_from).replace(tzinfo=UTC)
                if request.date_from
                else None
            )
            end = (
                datetime.combine(datetime.fromisoformat(request.date_to).date(), time.max, UTC)
                if request.date_to
                else None
            )
            conditions.append(
                models.FieldCondition(
                    key="timestamp",
                    range=models.DatetimeRange(gte=start, lte=end),
                )
            )
        retriever = self.index.as_retriever(
            vector_store_query_mode="hybrid",
            similarity_top_k=request.limit,
            sparse_top_k=request.limit,
            hybrid_top_k=request.limit,
            vector_store_kwargs={"qdrant_filters": models.Filter(must=[*conditions])}
            if conditions
            else {},
        )
        remaining = self.settings.search_context_chars
        token_budget = self.settings.search_context_tokens
        tokenizer = tiktoken.get_encoding("cl100k_base")
        results: list[SearchResult] = []
        for hit in retriever.retrieve(request.query):
            if remaining <= 0 or token_budget <= 0:
                break
            text = hit.node.get_content()[:remaining]
            tokens = tokenizer.encode(text, disallowed_special=())[:token_budget]
            text = tokenizer.decode(tokens)
            token_budget -= len(tokens)
            remaining -= len(text)
            metadata = hit.node.metadata
            results.append(
                SearchResult(
                    text=text,
                    score=hit.score or 0.0,
                    document_id=str(metadata["document_id"]),
                    gmail_message_id=str(metadata["gmail_message_id"]),
                    thread_id=str(metadata["thread_id"]),
                    source_type=str(metadata["source_type"]),
                    title=str(metadata["title"]),
                    timestamp=str(metadata["timestamp"]),
                    sender=str(metadata["sender"]),
                    location=str(metadata["location"]),
                )
            )
        return results
