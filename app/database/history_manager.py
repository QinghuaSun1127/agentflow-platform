"""聊天历史持久化：使用 PostgreSQL 连接池保存与读取会话消息。"""

from __future__ import annotations

import os
from typing import Any

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import get_database_url

# 与 compose 中 Postgres 配置一致；可通过环境变量覆盖
DEFAULT_DATABASE_URL = "postgresql://admin:password123@localhost:5432/agentflow_db"
MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 5

_history_manager: ChatHistoryManager | None = None


def _database_url() -> str:
    """读取 PostgreSQL 连接串，默认与本地 compose.yaml 保持一致。"""
    return os.getenv("DATABASE_URL", get_database_url())


class ChatHistoryManager:
    """聊天记录数据库操作类。

    使用 ThreadedConnectionPool 复用 PostgreSQL 连接，避免每次保存/查询都重新建连。
    """

    def __init__(self, database_url: str | None = None) -> None:
        self._pool = ThreadedConnectionPool(
            MIN_POOL_CONNECTIONS,
            MAX_POOL_CONNECTIONS,
            database_url or _database_url(),
        )

    def init_table(self) -> None:
        """创建 chat_history 表与会话查询所需索引。"""
        ddl_table = """
        CREATE TABLE IF NOT EXISTS chat_history (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL DEFAULT 0,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        ddl_sessions = """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            user_id BIGINT NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, session_id)
        );
        """
        ddl_user_id = "ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS user_id BIGINT NOT NULL DEFAULT 0;"
        ddl_session_index = """
        CREATE INDEX IF NOT EXISTS idx_chat_history_session_created
        ON chat_history (user_id, session_id, created_at);
        """
        ddl_recent_index = """
        CREATE INDEX IF NOT EXISTS idx_chat_history_recent_session
        ON chat_history (user_id, session_id, created_at DESC);
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl_table)
                cur.execute(ddl_sessions)
                cur.execute(ddl_user_id)
                cur.execute(ddl_session_index)
                cur.execute(ddl_recent_index)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def save_message(self, user_id: int, session_id: str, role: str, content: str) -> None:
        """向 chat_history 表插入一条聊天消息。"""
        if user_id < 0:
            raise ValueError("user_id 不能为负数")
        if not session_id.strip():
            raise ValueError("session_id 不能为空")
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role 必须是 user、assistant 或 system")
        if not content.strip():
            raise ValueError("content 不能为空")

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_sessions (user_id, session_id, title)
                    VALUES (%s, %s, NULL)
                    ON CONFLICT (user_id, session_id)
                    DO UPDATE SET updated_at = NOW();
                    """,
                    (user_id, session_id),
                )
                if role == "user":
                    cur.execute(
                        """
                        UPDATE chat_sessions
                        SET title = COALESCE(title, %s), updated_at = NOW()
                        WHERE user_id = %s AND session_id = %s;
                        """,
                        (_build_session_title(content, session_id), user_id, session_id),
                    )
                cur.execute(
                    """
                    INSERT INTO chat_history (user_id, session_id, role, content)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (user_id, session_id, role, content),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def rename_session(self, user_id: int, session_id: str, title: str) -> None:
        """Rename a user's session."""
        clean_title = " ".join(title.split()).strip()
        if not clean_title:
            raise ValueError("会话标题不能为空")
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_sessions (user_id, session_id, title)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, session_id)
                    DO UPDATE SET title = EXCLUDED.title, updated_at = NOW();
                    """,
                    (user_id, session_id, clean_title[:80]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def delete_session(self, user_id: int, session_id: str) -> None:
        """Delete a user's session and all messages."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chat_history WHERE user_id = %s AND session_id = %s;", (user_id, session_id))
                cur.execute("DELETE FROM chat_sessions WHERE user_id = %s AND session_id = %s;", (user_id, session_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get_session_list(self, user_id: int) -> list[str]:
        """查询所有唯一 session_id，并按最近更新时间倒序返回。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id
                    FROM chat_history
                    WHERE user_id = %s
                    GROUP BY session_id
                    ORDER BY MAX(created_at) DESC;
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
            return [str(row[0]) for row in rows]
        finally:
            self._pool.putconn(conn)

    def get_session_summaries(self, user_id: int) -> list[dict[str, Any]]:
        """查询会话摘要，包含标题与最近更新时间，避免前端为标题逐个拉取消息。"""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH ranked AS (
                        SELECT
                            session_id,
                            role,
                            content,
                            created_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY session_id
                                ORDER BY CASE WHEN role = 'user' THEN 0 ELSE 1 END, created_at ASC, id ASC
                            ) AS title_rank,
                            MAX(created_at) OVER (PARTITION BY session_id) AS last_updated
                        FROM chat_history
                        WHERE user_id = %s
                    ), titled AS (
                        SELECT session_id, title
                        FROM chat_sessions
                        WHERE user_id = %s
                    )
                    SELECT ranked.session_id, COALESCE(titled.title, ranked.content) AS title, last_updated
                    FROM ranked
                    LEFT JOIN titled ON titled.session_id = ranked.session_id
                    WHERE title_rank = 1
                    ORDER BY last_updated DESC;
                    """,
                    (user_id, user_id),
                )
                rows = cur.fetchall()
            return [
                {
                    "session_id": str(row["session_id"]),
                    "title": _build_session_title(str(row["title"]), str(row["session_id"])),
                    "last_updated": row["last_updated"],
                }
                for row in rows
            ]
        finally:
            self._pool.putconn(conn)

    def get_messages_by_session(self, user_id: int, session_id: str) -> list[dict[str, Any]]:
        """获取某个会话的完整消息列表，按创建时间正序排列。"""
        if user_id < 0:
            raise ValueError("user_id 不能为负数")
        if not session_id.strip():
            raise ValueError("session_id 不能为空")

        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, session_id, role, content, created_at
                    FROM chat_history
                    WHERE user_id = %s AND session_id = %s
                    ORDER BY created_at ASC, id ASC;
                    """,
                    (user_id, session_id),
                )
                rows = cur.fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "session_id": str(row["session_id"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        """关闭连接池，供测试或应用退出时释放资源。"""
        self._pool.closeall()


def get_chat_history_manager() -> ChatHistoryManager:
    """返回全局 ChatHistoryManager 单例。"""
    global _history_manager
    if _history_manager is None:
        _history_manager = ChatHistoryManager()
    return _history_manager


def _build_session_title(content: str, session_id: str) -> str:
    """Build a compact title from the first user-facing message."""
    title = " ".join(content.split()).strip()
    for icon in ("📊", "📦"):
        title = title.replace(icon, "").strip()
    if not title:
        return f"未命名会话 {session_id[:6]}"
    return title if len(title) <= 24 else f"{title[:24]}..."
