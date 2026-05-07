"""Usage analytics persistence for the admin dashboard."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import Json, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import get_database_url

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 5
_usage_manager: UsageManager | None = None


def _database_url() -> str:
    return os.getenv("DATABASE_URL", get_database_url())


class UsageManager:
    """Store per-request usage records and aggregate admin metrics."""

    def __init__(self, database_url: str | None = None) -> None:
        self._pool = ThreadedConnectionPool(
            MIN_POOL_CONNECTIONS,
            MAX_POOL_CONNECTIONS,
            database_url or _database_url(),
        )

    def init_table(self) -> None:
        ddl_table = """
        CREATE TABLE IF NOT EXISTS agent_usage (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            session_id TEXT NOT NULL,
            route TEXT,
            tools JSONB NOT NULL DEFAULT '[]'::jsonb,
            tool_names JSONB NOT NULL DEFAULT '[]'::jsonb,
            prompt TEXT NOT NULL,
            prompt_chars INTEGER NOT NULL DEFAULT 0,
            reply_chars INTEGER NOT NULL DEFAULT 0,
            estimated_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'success',
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        ddl_tool_names = (
            "ALTER TABLE agent_usage "
            "ADD COLUMN IF NOT EXISTS tool_names JSONB NOT NULL DEFAULT '[]'::jsonb;"
        )
        ddl_index_created = "CREATE INDEX IF NOT EXISTS idx_agent_usage_created ON agent_usage (created_at DESC);"
        ddl_index_user = (
            "CREATE INDEX IF NOT EXISTS idx_agent_usage_user_created "
            "ON agent_usage (user_id, created_at DESC);"
        )
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl_table)
                cur.execute(ddl_tool_names)
                cur.execute(ddl_index_created)
                cur.execute(ddl_index_user)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def record_interaction(
        self,
        *,
        user_id: int,
        session_id: str,
        prompt: str,
        reply: str,
        route: str | None,
        tools: list[str],
        tool_names: list[str] | None = None,
        latency_ms: int,
        status: str = "success",
        error: str | None = None,
    ) -> None:
        prompt_chars = len(prompt)
        reply_chars = len(reply)
        estimated_tokens = max(1, (prompt_chars + reply_chars) // 4)
        estimated_cost_usd = round(estimated_tokens / 1000 * float(os.getenv("ESTIMATED_COST_USD_PER_1K", "0.002")), 6)
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_usage (
                        user_id, session_id, route, tools, tool_names, prompt, prompt_chars, reply_chars,
                        estimated_tokens, estimated_cost_usd, latency_ms, status, error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """,
                    (
                        user_id,
                        session_id,
                        route,
                        Json(tools),
                        Json(tool_names or tools),
                        prompt,
                        prompt_chars,
                        reply_chars,
                        estimated_tokens,
                        estimated_cost_usd,
                        latency_ms,
                        status,
                        error,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get_admin_stats(self, days: int = 7) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(days=days)
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total_calls,
                        COALESCE(SUM(estimated_tokens), 0) AS total_tokens,
                        COALESCE(SUM(estimated_cost_usd), 0) AS total_cost_usd,
                        COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                        COUNT(*) FILTER (WHERE status != 'success') AS error_count
                    FROM agent_usage
                    WHERE created_at >= %s;
                    """,
                    (since,),
                )
                totals = dict(cur.fetchone() or {})
                cur.execute(
                    """
                    SELECT DATE_TRUNC('day', created_at) AS day, COUNT(*) AS calls
                    FROM agent_usage
                    WHERE created_at >= %s
                    GROUP BY day
                    ORDER BY day ASC;
                    """,
                    (since,),
                )
                daily = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT COALESCE(route, 'unknown') AS route, COUNT(*) AS calls
                    FROM agent_usage
                    WHERE created_at >= %s
                    GROUP BY route
                    ORDER BY calls DESC;
                    """,
                    (since,),
                )
                routes = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT prompt, route, estimated_tokens, estimated_cost_usd, latency_ms, created_at
                    FROM agent_usage
                    WHERE created_at >= %s
                    ORDER BY created_at DESC
                    LIMIT 20;
                    """,
                    (since,),
                )
                recent = [dict(row) for row in cur.fetchall()]
            return {"totals": totals, "daily": daily, "routes": routes, "recent": recent}
        finally:
            self._pool.putconn(conn)

    def get_tool_stats(self, days: int = 7) -> list[dict[str, Any]]:
        since = datetime.now(UTC) - timedelta(days=days)
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    WITH exploded AS (
                        SELECT
                            created_at,
                            latency_ms,
                            status,
                            jsonb_array_elements_text(tool_names) AS tool_name
                        FROM agent_usage
                        WHERE created_at >= %s
                    )
                    SELECT
                        tool_name AS name,
                        COUNT(*) AS calls,
                        COUNT(*) FILTER (WHERE status != 'success') AS error_calls,
                        COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                    FROM exploded
                    GROUP BY tool_name
                    ORDER BY calls DESC;
                    """,
                    (since,),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()


def get_usage_manager() -> UsageManager:
    global _usage_manager
    if _usage_manager is None:
        _usage_manager = UsageManager()
    return _usage_manager
