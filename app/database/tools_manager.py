"""Tool configuration persistence and synchronization."""

from __future__ import annotations

import os
from dataclasses import dataclass

from psycopg2 import OperationalError
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import get_database_url
from app.tools.registry import ToolSpec

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 5
_tools_manager: ToolsManager | None = None


@dataclass(frozen=True)
class ToolConfig:
    name: str
    owner: str
    enabled: bool
    is_write: bool
    timeout_seconds: int
    max_retries: int


def _database_url() -> str:
    return os.getenv("DATABASE_URL", get_database_url())


class ToolsManager:
    """Persist and manage tool configuration."""

    def __init__(self, database_url: str | None = None) -> None:
        self._pool = ThreadedConnectionPool(
            MIN_POOL_CONNECTIONS,
            MAX_POOL_CONNECTIONS,
            database_url or _database_url(),
        )

    def init_table(self) -> None:
        ddl_table = """
        CREATE TABLE IF NOT EXISTS tools (
            name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            is_write BOOLEAN NOT NULL DEFAULT FALSE,
            timeout_seconds INTEGER NOT NULL DEFAULT 30,
            max_retries INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl_table)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def sync_from_registry(self) -> None:
        """Upsert any missing tools from the in-code registry."""
        from app.tools.mock_tools import TOOL_REGISTRY

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                for spec in TOOL_REGISTRY.values():
                    cur.execute(
                        """
                        INSERT INTO tools (name, owner, enabled, is_write, timeout_seconds, max_retries)
                        VALUES (%s, %s, TRUE, %s, %s, %s)
                        ON CONFLICT (name)
                        DO UPDATE SET owner = EXCLUDED.owner, is_write = EXCLUDED.is_write, updated_at = NOW();
                        """,
                        (
                            spec.name,
                            spec.owner,
                            spec.is_write,
                            spec.timeout_seconds,
                            spec.max_retries,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def list_configs(self) -> list[ToolConfig]:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT name, owner, enabled, is_write, timeout_seconds, max_retries
                    FROM tools
                    ORDER BY owner ASC, name ASC;
                    """
                )
                return [ToolConfig(**row) for row in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def update_config(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
    ) -> ToolConfig:
        if timeout_seconds is not None and timeout_seconds < 1:
            raise ValueError("timeout_seconds 必须 >= 1")
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries 必须 >= 0")

        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE tools
                    SET
                        enabled = COALESCE(%s, enabled),
                        timeout_seconds = COALESCE(%s, timeout_seconds),
                        max_retries = COALESCE(%s, max_retries),
                        updated_at = NOW()
                    WHERE name = %s
                    RETURNING name, owner, enabled, is_write, timeout_seconds, max_retries;
                    """,
                    (enabled, timeout_seconds, max_retries, name),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:
                raise ValueError("工具不存在")
            return ToolConfig(**row)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def enabled_tool_specs(self, owner: str) -> list[ToolSpec]:
        """Return enabled tools for an owner, with config overrides applied."""
        from app.tools.mock_tools import TOOL_REGISTRY

        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT name, enabled, timeout_seconds, max_retries
                    FROM tools
                    WHERE owner = %s AND enabled = TRUE;
                    """,
                    (owner,),
                )
                configs = {row["name"]: row for row in cur.fetchall()}
        finally:
            self._pool.putconn(conn)

        specs: list[ToolSpec] = []
        for name, spec in TOOL_REGISTRY.items():
            if spec.owner != owner:
                continue
            if name not in configs:
                continue
            row = configs[name]
            specs.append(
                ToolSpec(
                    name=spec.name,
                    owner=spec.owner,
                    tool=spec.tool,
                    args_schema=spec.args_schema,
                    timeout_seconds=int(row["timeout_seconds"]),
                    max_retries=int(row["max_retries"]),
                    is_write=spec.is_write,
                )
            )
        return specs

    def is_enabled(self, tool_name: str) -> bool:
        """Return whether tool is enabled.

        If the tools table isn't initialized yet (or tool row missing), default to enabled
        to avoid surprising behavior during local development / early bootstrap.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT enabled FROM tools WHERE name = %s;", (tool_name,))
                row = cur.fetchone()
            return bool(row[0]) if row else True
        except Exception:
            return True
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()


def get_tools_manager() -> ToolsManager:
    global _tools_manager
    if _tools_manager is None:
        try:
            _tools_manager = ToolsManager()
        except OperationalError:
            class _NoopToolsManager(ToolsManager):  # type: ignore[misc]
                def __init__(self) -> None:  # noqa: D401
                    """No-op manager when Postgres is unavailable."""

                def init_table(self) -> None:  # noqa: D401
                    """Skip."""

                def sync_from_registry(self) -> None:  # noqa: D401
                    """Skip."""

                def list_configs(self) -> list[ToolConfig]:  # noqa: D401
                    """Return empty configs when DB down."""
                    return []

                def update_config(self, name: str, **_kwargs: object) -> ToolConfig:  # noqa: D401
                    """Always error when DB down."""
                    raise ValueError(f"工具配置不可用（数据库不可用）：{name}")

                def enabled_tool_specs(self, _owner: str) -> list[ToolSpec]:  # noqa: D401
                    """Fall back to registry in callers."""
                    raise OperationalError("tools_config_db_unavailable")

                def is_enabled(self, _tool_name: str) -> bool:  # noqa: D401
                    """Default allow when DB down."""
                    return True

                def close(self) -> None:  # noqa: D401
                    """Skip."""

            _tools_manager = _NoopToolsManager()
    return _tools_manager

