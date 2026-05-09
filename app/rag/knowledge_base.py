"""RAG 知识库：中文向量 + PostgreSQL/pgvector 存储与检索。"""

from __future__ import annotations

import io
import hashlib
import math
import os
import re
from typing import Any

import psycopg2
from pgvector.psycopg2 import register_vector
from psycopg2.extensions import connection as PgConnection
from pypdf import PdfReader

from app.core.config import get_database_url

# 与 compose 中 Postgres 配置一致；可通过环境变量覆盖
DEFAULT_DATABASE_URL = "postgresql://admin:password123@localhost:5432/agentflow_db"
EMBEDDING_MODEL = "shibing624/text2vec-base-chinese"
EMBEDDING_DIM = 768
DEFAULT_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0"))
DEFAULT_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "650"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))

_embeddings_instance: "LocalHashEmbeddings | Any | None" = None


class LocalHashEmbeddings:
    """Small deterministic embedding backend for demos without downloading ML models."""

    def _features(self, text: str) -> list[str]:
        lowered = text.lower()
        words = re.findall(r"[a-z0-9_]+", lowered)
        cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
        cjk_bigrams = ["".join(cjk_chars[idx : idx + 2]) for idx in range(max(0, len(cjk_chars) - 1))]
        return words + cjk_chars + cjk_bigrams

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]


def _keyword_terms(query: str) -> list[str]:
    """Extract short keyword terms for lexical fallback retrieval."""
    lowered = query.lower()
    terms: list[str] = []
    terms.extend(word for word in re.findall(r"[a-z0-9_]+", lowered) if len(word) >= 2)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", lowered)
    terms.extend("".join(cjk_chars[idx : idx + 2]) for idx in range(max(0, len(cjk_chars) - 1)))

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            unique_terms.append(term)
    return unique_terms[:16]


def _keyword_score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _database_url() -> str:
    return os.getenv("DATABASE_URL", get_database_url())


def get_embeddings() -> LocalHashEmbeddings | Any:
    """Return embeddings backend.

    Default to a deterministic local backend so demo deployments do not need
    HuggingFace/Torch downloads. Set RAG_EMBEDDING_BACKEND=huggingface to use
    the heavier sentence-transformers model.
    """
    global _embeddings_instance
    if _embeddings_instance is None:
        if os.getenv("RAG_EMBEDDING_BACKEND", "local").strip().lower() == "huggingface":
            from langchain_huggingface import HuggingFaceEmbeddings

            _embeddings_instance = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL,
                model_kwargs={"device": "cpu"},
                # 归一化后 pgvector 的 <=> 与余弦相似度语义一致，便于按距离排序
                encode_kwargs={"normalize_embeddings": True},
                query_encode_kwargs={"normalize_embeddings": True},
            )
        else:
            _embeddings_instance = LocalHashEmbeddings()
    return _embeddings_instance


def _connect_bare() -> PgConnection:
    """仅建立连接，不注册 vector 适配器。

    首次建库时若先调用 register_vector，会因尚未执行 CREATE EXTENSION vector
    而报「vector type not found in the database」。建扩展/建表阶段必须使用本函数。
    """
    return psycopg2.connect(_database_url())


def _connect() -> PgConnection:
    """建立 psycopg2 连接并注册 vector 类型适配器（扩展已存在后的读写路径使用）。"""
    conn = _connect_bare()
    register_vector(conn)
    return conn


def init_db() -> None:
    """创建 pgvector 扩展、文档表与 knowledge_base 分块表。"""
    ddl_extension = "CREATE EXTENSION IF NOT EXISTS vector;"
    ddl_documents = """
    CREATE TABLE IF NOT EXISTS documents (
        id BIGSERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        filename TEXT NOT NULL,
        content_type TEXT NOT NULL DEFAULT 'text/plain',
        status TEXT NOT NULL DEFAULT 'ready',
        chunk_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    ddl_table = """
    CREATE TABLE IF NOT EXISTS knowledge_base (
        id BIGSERIAL PRIMARY KEY,
        document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        embedding vector(768) NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        page_number INTEGER,
        chunk_index INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    ddl_document_id = "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS document_id BIGINT;"
    ddl_source = "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';"
    ddl_page_number = "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS page_number INTEGER;"
    ddl_chunk_index = "ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS chunk_index INTEGER NOT NULL DEFAULT 0;"
    ddl_created_at = (
        "ALTER TABLE knowledge_base "
        "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
    )
    ddl_vector_index = """
    CREATE INDEX IF NOT EXISTS idx_knowledge_base_embedding
    ON knowledge_base USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
    """
    conn = _connect_bare()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(ddl_extension)
        cur.execute(ddl_documents)
        cur.execute(ddl_table)
        cur.execute(ddl_document_id)
        cur.execute(ddl_source)
        cur.execute(ddl_page_number)
        cur.execute(ddl_chunk_index)
        cur.execute(ddl_created_at)
        cur.execute(ddl_vector_index)
        cur.close()
    finally:
        conn.close()


def add_document(
    title: str,
    content: str,
    *,
    source: str = "manual",
    document_id: int | None = None,
    page_number: int | None = None,
    chunk_index: int = 0,
) -> int:
    """将正文向量化后写入知识库，返回新记录 id。"""
    if not content.strip():
        raise ValueError("content 不能为空")
    emb = get_embeddings()
    vec = emb.embed_documents([content.replace("\n", " ")])[0]
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"向量维度应为 {EMBEDDING_DIM}，实际为 {len(vec)}")

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO knowledge_base (document_id, title, content, embedding, source, page_number, chunk_index)
            VALUES (%s, %s, %s, %s::vector, %s, %s, %s)
            RETURNING id;
            """,
            (document_id, title, content, vec, source, page_number, chunk_index),
        )
        row = cur.fetchone()
        conn.commit()
        if row is None:
            raise RuntimeError("插入失败：未返回 id")
        return int(row[0])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search_documents(query: str, top_k: int = 3) -> list[str]:
    """将 query 向量化，按 pgvector 余弦距离 `<=>` 升序取最相近的 top_k 条，返回正文列表。"""
    return [chunk["content"] for chunk in search_document_chunks(query, top_k=top_k)]


def search_document_chunks(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Return matching chunks with source metadata and similarity scores."""
    if top_k < 1:
        raise ValueError("top_k 必须 >= 1")
    q = query.strip()
    if not q:
        return []

    vec = get_embeddings().embed_query(q.replace("\n", " "))
    if len(vec) != EMBEDDING_DIM:
        raise ValueError(f"查询向量维度应为 {EMBEDDING_DIM}，实际为 {len(vec)}")

    conn = _connect()
    try:
        cur = conn.cursor()
        # `<=>` 为余弦距离：越小越相似；与架构文档中「余弦相似度」一致采用距离排序
        cur.execute(
            """
            SELECT title, content, source, page_number, document_id, 1 - (embedding <=> %s::vector) AS similarity
            FROM knowledge_base
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """,
            (vec, vec, max(top_k, 6)),
        )
        rows: list[tuple[Any, ...]] = cur.fetchall()
        chunks = [
            {
                "title": str(row[0]),
                "content": str(row[1]),
                "source": str(row[2]),
                "page_number": int(row[3]) if row[3] is not None else None,
                "document_id": int(row[4]) if row[4] is not None else None,
                "similarity": float(row[5]),
                "keyword_score": 0,
            }
            for row in rows
            if float(row[5]) >= DEFAULT_SIMILARITY_THRESHOLD
        ]

        terms = _keyword_terms(q)
        if terms:
            clauses = " OR ".join(["(title ILIKE %s OR content ILIKE %s OR source ILIKE %s)" for _ in terms])
            params: list[Any] = []
            for term in terms:
                pattern = f"%{term}%"
                params.extend([pattern, pattern, pattern])
            cur.execute(
                f"""
                SELECT title, content, source, page_number, document_id
                FROM knowledge_base
                WHERE {clauses}
                LIMIT %s;
                """,
                [*params, max(top_k * 4, 12)],
            )
            for row in cur.fetchall():
                content = str(row[1])
                chunks.append(
                    {
                        "title": str(row[0]),
                        "content": content,
                        "source": str(row[2]),
                        "page_number": int(row[3]) if row[3] is not None else None,
                        "document_id": int(row[4]) if row[4] is not None else None,
                        "similarity": 0.0,
                        "keyword_score": _keyword_score(" ".join([str(row[0]), content, str(row[2])]), terms),
                    }
                )

        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for chunk in chunks:
            key = (chunk["source"], chunk["page_number"], chunk["content"])
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = chunk
            else:
                existing["similarity"] = max(float(existing["similarity"]), float(chunk["similarity"]))
                existing["keyword_score"] = max(int(existing["keyword_score"]), int(chunk["keyword_score"]))

        ranked = sorted(
            deduped.values(),
            key=lambda item: (int(item["keyword_score"]), float(item["similarity"])),
            reverse=True,
        )
        return ranked[:top_k]
    finally:
        conn.close()


def ingest_document(title: str, filename: str, content_type: str, raw_bytes: bytes) -> dict[str, Any]:
    """Extract, split, embed, and store an uploaded document."""
    text_pages = extract_text_pages(filename, raw_bytes)
    chunks: list[tuple[str, int | None]] = []
    for page_number, text in text_pages:
        chunks.extend((chunk, page_number) for chunk in split_text(text))
    if not chunks:
        raise ValueError("文档内容为空，无法入库。")

    conn = _connect_bare()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO documents (title, filename, content_type, status, chunk_count)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (title, filename, content_type or "application/octet-stream", "indexing", len(chunks)),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("创建文档失败。")
        document_id = int(row[0])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        for idx, (chunk, page_number) in enumerate(chunks):
            add_document(
                title,
                chunk,
                source=filename,
                document_id=document_id,
                page_number=page_number,
                chunk_index=idx,
            )
        _mark_document_ready(document_id, len(chunks))
    except Exception:
        _mark_document_failed(document_id)
        raise

    return {"id": document_id, "title": title, "filename": filename, "chunk_count": len(chunks)}


def list_documents() -> list[dict[str, Any]]:
    """List ingested documents by newest first."""
    conn = _connect_bare()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, filename, content_type, status, chunk_count, created_at
            FROM documents
            ORDER BY created_at DESC;
            """
        )
        rows = cur.fetchall()
        return [
            {
                "id": int(row[0]),
                "title": str(row[1]),
                "filename": str(row[2]),
                "content_type": str(row[3]),
                "status": str(row[4]),
                "chunk_count": int(row[5]),
                "created_at": row[6],
            }
            for row in rows
        ]
    finally:
        conn.close()


def delete_document(document_id: int) -> None:
    """Delete a document and its vector chunks."""
    conn = _connect_bare()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM documents WHERE id = %s;", (document_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reindex_document(document_id: int) -> dict[str, Any]:
    """Rebuild embeddings for all chunks belonging to one document."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, content
            FROM knowledge_base
            WHERE document_id = %s
            ORDER BY chunk_index ASC, id ASC;
            """,
            (document_id,),
        )
        rows = cur.fetchall()
        if not rows:
            raise ValueError("文档不存在或没有可重建的分块")
        contents = [str(row[1]).replace("\n", " ") for row in rows]
        vectors = get_embeddings().embed_documents(contents)
        for (chunk_id, _content), vec in zip(rows, vectors, strict=True):
            cur.execute(
                "UPDATE knowledge_base SET embedding = %s::vector WHERE id = %s;",
                (vec, int(chunk_id)),
            )
        cur.execute(
            """
            UPDATE documents
            SET status = 'ready', chunk_count = %s
            WHERE id = %s;
            """,
            (len(rows), document_id),
        )
        conn.commit()
        return {"document_id": document_id, "chunk_count": len(rows)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def extract_text_pages(filename: str, raw_bytes: bytes) -> list[tuple[int | None, str]]:
    """Extract text pages from txt/md/pdf uploads."""
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(raw_bytes))
        return [(idx + 1, page.extract_text() or "") for idx, page in enumerate(reader.pages)]
    return [(None, raw_bytes.decode("utf-8", "ignore"))]


def split_text(text: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks suitable for vector search."""
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        chunks.append(normalized[start:end])
        if end == len(normalized):
            break
        start = max(0, end - overlap)
    return chunks


def _mark_document_ready(document_id: int, chunk_count: int) -> None:
    _update_document_status(document_id, "ready", chunk_count)


def _mark_document_failed(document_id: int) -> None:
    _update_document_status(document_id, "failed", 0)


def _update_document_status(document_id: int, status: str, chunk_count: int) -> None:
    conn = _connect_bare()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE documents SET status = %s, chunk_count = %s WHERE id = %s;",
            (status, chunk_count, document_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 启动阶段演示数据（与 main.py 生命周期配合，幂等插入）
_SEED_POLICY_TITLE = "差旅报销政策"
_SEED_POLICY_CONTENT = (
    "公司规定高铁只能报销二等座，飞机只能报销经济舱，住宿上限每天 400 元。"
)


def seed_travel_policy_demo_if_missing() -> None:
    """幂等插入演示政策。

    为避免应用启动阶段加载 sentence-transformers（可能在被 Ctrl+C 中断时触发
    multiprocessing 资源清理告警），这里直接写入一个 768 维零向量作为 mock embedding。
    MVP 场景下可保证示例数据可被检索链路返回；后续可再异步回填真实向量。
    """
    conn = _connect_bare()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM knowledge_base WHERE title = %s LIMIT 1;",
            (_SEED_POLICY_TITLE,),
        )
        exists = cur.fetchone() is not None
        if not exists:
            zero_vec = "[" + ",".join(["0"] * EMBEDDING_DIM) + "]"
            cur.execute(
                """
                INSERT INTO knowledge_base (title, content, embedding)
                VALUES (%s, %s, %s::vector);
                """,
                (_SEED_POLICY_TITLE, _SEED_POLICY_CONTENT, zero_vec),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
