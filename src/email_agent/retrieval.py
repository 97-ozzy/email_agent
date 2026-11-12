"""Persistent LlamaIndex/Qdrant hybrid evidence retrieval."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime, time
from email.utils import parseaddr
from uuid import NAMESPACE_URL, uuid5

import tiktoken
from llama_index.core import Document, QueryBundle, StorageContext, VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import BaseNode, TextNode
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.embeddings.fastembed import FastEmbedEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from email_agent.archive import SQLiteArchive
from email_agent.config import Settings
from email_agent.search import SearchRequest, SearchResult, clean_email_body

QUERY_INSTRUCTION = (
    "Instruct: Given an email search query, retrieve relevant email passages\nQuery:"
)


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
        collection_namespace: str = "email_agent_sources",
    ) -> None:
        self.settings = settings
        self.archive = archive
        self.client = client or QdrantClient(url=settings.qdrant_url)
        self.embedding_contract = {
            "provider": "fastembed",
            "model": settings.embedding_model,
            "dimensions": 1024,
            "query_instruction": QUERY_INSTRUCTION,
            "normalization": "l2-last-token",
            "artifact": "Qdrant/Qwen3-Embedding-0.6B-onnx:fp32",
            "version": 1,
        }
        self.contract_id = hashlib.sha256(
            json.dumps(self.embedding_contract, sort_keys=True).encode()
        ).hexdigest()[:16]
        self.collection = f"{collection_namespace}_{self.contract_id}"
        self.embed_model = embed_model or FastEmbedEmbedding(
            model_name=settings.embedding_model,
            cache_dir=str(settings.data_dir / "models"),
            providers=["CPUExecutionProvider"],
        )
        self.store = QdrantVectorStore(
            collection_name=self.collection,
            client=self.client,
            enable_hybrid=True,
            fastembed_sparse_model="Qdrant/bm25",
        )
        self.directory = settings.data_dir / "index" / self.contract_id
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
            embed_model=self.embed_model,
        )
        self.splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64, id_func=_node_id)
        with closing(sqlite3.connect(archive.database_path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS email_index_state "
                "(document_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)"
            )

    def refresh(self, *, force: bool = False) -> int:
        """Upsert changed documents and replace stale chunks before recording completion."""
        with closing(sqlite3.connect(self.archive.database_path)) as db:
            state = dict(db.execute("SELECT document_id, fingerprint FROM email_index_state"))
        collection_exists = self.client.collection_exists(self.collection)
        rebuild = not collection_exists
        changed = 0
        for document in self._documents():
            document_id = document.id_
            metadata = document.metadata
            text = document.text
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        text,
                        metadata,
                        self.splitter.chunk_size,
                        self.splitter.chunk_overlap,
                        "unified-v1",
                        self.embedding_contract,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if not force and not rebuild and state.get(document_id) == fingerprint:
                continue
            with closing(sqlite3.connect(self.archive.database_path)) as db, db:
                db.execute("DELETE FROM email_index_state WHERE document_id=?", (document_id,))
                self._set_indexing_status(db, document_id, "pending")
            try:
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
            except Exception:
                with closing(sqlite3.connect(self.archive.database_path)) as db, db:
                    self._set_indexing_status(db, document_id, "failed")
                raise
            with closing(sqlite3.connect(self.archive.database_path)) as db, db:
                db.execute(
                    "INSERT INTO email_index_state VALUES (?, ?) ON CONFLICT(document_id) "
                    "DO UPDATE SET fingerprint=excluded.fingerprint",
                    (document_id, fingerprint),
                )
                self._set_indexing_status(db, document_id, "indexed")
            changed += 1
        return changed

    @staticmethod
    def _set_indexing_status(db: sqlite3.Connection, document_id: str, status: str) -> None:
        if document_id.startswith("email:"):
            db.execute(
                "UPDATE messages SET indexing_status=? WHERE gmail_message_id=?",
                (status, document_id.removeprefix("email:")),
            )
        else:
            db.execute(
                "UPDATE message_attachments SET indexing_status=? WHERE processing_status='parsed' "
                "AND content_sha256=(SELECT content_sha256 FROM attachment_documents "
                "WHERE document_id=?)",
                (status, document_id),
            )

    def _documents(self) -> Iterator[Document]:
        with closing(sqlite3.connect(self.archive.database_path)) as db:
            db.row_factory = sqlite3.Row
            messages = db.execute(
                "SELECT * FROM messages WHERE ingestion_status='downloaded'"
            ).fetchall()
            attachments = db.execute(
                "SELECT d.* FROM attachment_documents d JOIN attachment_blobs b "
                "ON b.content_sha256=d.content_sha256 WHERE b.extraction_status='parsed'"
            ).fetchall()
        for row in messages:
            metadata = {
                "document_id": f"email:{row['gmail_message_id']}",
                "gmail_message_id": row["gmail_message_id"],
                "thread_id": row["gmail_thread_id"],
                "source_type": "email",
                "document_type": "email",
                "title": row["subject"],
                "timestamp": row["message_timestamp"],
                "sender": row["sender"],
                "sender_address": parseaddr(row["sender"])[1].lower(),
                "location": row["source_reference"],
            }
            metadata["sources"] = [dict(metadata)]
            body = (self.settings.data_dir / row["body_path"]).read_text(encoding="utf-8")
            yield self._document(clean_email_body(body, row["body_format"]), metadata)
        for row in attachments:
            sources = self.archive.attachment_sources(row["document_id"])
            if not sources:
                continue
            reader_metadata = json.loads(row["metadata"])
            mime = str(reader_metadata.get("source_mime_type", sources[0].mime_type))
            filename = str(reader_metadata.get("source_filename", sources[0].filename))
            kind = (
                "pdf"
                if mime == "application/pdf" or filename.lower().endswith(".pdf")
                else "docx"
                if "wordprocessingml" in mime or filename.lower().endswith(".docx")
                else "text"
            )
            metadata = {
                "document_id": row["document_id"],
                "content_sha256": row["content_sha256"],
                "source_type": "attachment",
                "document_type": kind,
                "sources": [
                    {**asdict(source), "sender_address": parseaddr(source.sender)[1].lower()}
                    for source in sources
                ],
            }
            text = (self.settings.data_dir / row["text_path"]).read_text(encoding="utf-8")
            yield self._document(text, metadata)

    @staticmethod
    def _document(text: str, metadata: dict[str, object]) -> Document:
        return Document(
            text=text,
            id_=str(metadata["document_id"]),
            metadata=metadata,
            excluded_embed_metadata_keys=list(metadata),
            excluded_llm_metadata_keys=list(metadata),
        )

    def search(self, request: SearchRequest) -> list[SearchResult]:
        """Retrieve evidence directly with the integration's built-in fusion."""
        if not request.query.strip() or not 1 <= request.limit <= 20:
            raise ValueError("search requires a query and a limit between 1 and 20")
        if not self.client.collection_exists(self.collection):
            return []
        conditions: list[models.FieldCondition] = []
        start: datetime | None = None
        end: datetime | None = None
        for key, value in (
            ("sender_address", request.sender),
            ("filename", request.filename),
        ):
            if value:
                conditions.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(
                            value=value.lower() if key == "sender_address" else value
                        ),
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
        filters: list[models.FieldCondition | models.NestedCondition] = []
        if conditions:
            filters.append(
                models.NestedCondition(
                    nested=models.Nested(key="sources", filter=models.Filter(must=[*conditions]))
                )
            )
        for key, value in (
            ("source_type", request.source_type),
            ("document_type", request.document_type),
        ):
            if value:
                filters.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        retriever = self.index.as_retriever(
            vector_store_query_mode="hybrid",
            similarity_top_k=request.limit,
            sparse_top_k=request.limit,
            hybrid_top_k=request.limit,
            vector_store_kwargs={"qdrant_filters": models.Filter(must=[*filters])}
            if filters
            else {},
        )
        remaining = self.settings.search_context_chars
        token_budget = self.settings.search_context_tokens
        tokenizer = tiktoken.get_encoding("cl100k_base")
        results: list[SearchResult] = []
        bundle = QueryBundle(
            query_str=request.query,
            embedding=self.embed_model.get_query_embedding(QUERY_INSTRUCTION + request.query),
        )
        for hit in retriever.retrieve(bundle):
            if remaining <= 0 or token_budget <= 0:
                break
            text = hit.node.get_content()[:remaining]
            tokens = tokenizer.encode(text, disallowed_special=())[:token_budget]
            text = tokenizer.decode_bytes(tokens).decode("utf-8", errors="ignore")
            token_budget -= len(tokens)
            remaining -= len(text)
            metadata = hit.node.metadata
            sources = (
                self.archive.attachment_sources(str(metadata["document_id"]))
                if metadata["source_type"] == "attachment"
                else ()
            )
            if metadata["source_type"] == "attachment" and not sources:
                continue
            # Select the occurrence satisfying this request for the singular citation;
            # retain every occurrence in sources, resolved from the authoritative archive.
            matching = [
                source
                for source in sources
                if (not request.filename or source.filename == request.filename)
                and (
                    not request.sender
                    or parseaddr(source.sender)[1].lower() == request.sender.lower()
                )
                and (start is None or datetime.fromisoformat(source.timestamp) >= start)
                and (end is None or datetime.fromisoformat(source.timestamp) <= end)
            ]
            source = matching[0] if matching else None
            if sources and source is None:
                continue
            start_char = hit.node.start_char_idx if isinstance(hit.node, TextNode) else None
            citation = {**asdict(source), "title": source.filename} if source else metadata
            results.append(
                SearchResult(
                    text=text,
                    score=hit.score or 0.0,
                    document_id=str(metadata["document_id"]),
                    gmail_message_id=str(citation["gmail_message_id"]),
                    thread_id=str(citation["thread_id"]),
                    source_type=str(metadata["source_type"]),
                    title=str(citation["title"]),
                    timestamp=str(citation["timestamp"]),
                    sender=str(citation["sender"]),
                    location=str(citation["location"]),
                    attachment_id=source.attachment_id if source else None,
                    filename=source.filename if source else None,
                    mime_type=source.mime_type if source else None,
                    document_type=str(metadata["document_type"]),
                    sources=sources,
                    start_char=start_char,
                    end_char=(start_char + len(text)) if start_char is not None else None,
                )
            )
        return results
