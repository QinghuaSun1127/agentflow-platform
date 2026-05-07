"""Audit log persistence."""

from __future__ import annotations

import os
from typing import Any

from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import get_database_url

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 5
_audit_manager: AuditManager | None = None


def _database_url() -> str:
    return os.getenv("DATABASE_URL", get_database_url())


class AuditManager:
    def __init__(self, database_url: str | None = None) -> None:
        self._pool = ThreadedConnectionPool(
            MIN_POOL_CONNECTIONS,
            MAX_POOL_CONNECTIONS,
            database_url or _database_url(),
        )

    def init_table(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            session_id TEXT NOT NULL,
            route TEXT,
            tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            user_input TEXT NOT NULL,
            raw_output TEXT NOT NULL,
            filtered_output TEXT NOT NULL,
            latency_ms INTEGER,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        idx = "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs (created_at DESC);"
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute(idx)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def record(self, event: dict[str, Any]) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_logs (
                        user_id, session_id, route, tools, user_input, raw_output,
                        filtered_output, latency_ms, error, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()));
                    """,
                    (
                        event.get("user_id"),
                        event.get("session_id"),
                        event.get("route"),
                        Json(event.get("tools") or []),
                        event.get("user_input", ""),
                        event.get("raw_output", ""),
                        event.get("filtered_output", ""),
                        event.get("latency_ms"),
                        event.get("error"),
                        event.get("created_at"),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, user_id, session_id, route, tools, latency_ms, error, created_at
                    FROM audit_logs
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._pool.putconn(conn)


def get_audit_manager() -> AuditManager:
    global _audit_manager
    if _audit_manager is None:
        _audit_manager = AuditManager()
    return _audit_manager

