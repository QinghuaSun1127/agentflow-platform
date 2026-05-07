"""User persistence for local multi-tenant accounts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from psycopg2 import errors
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import get_database_url
from app.security.auth import hash_password, verify_password

MIN_POOL_CONNECTIONS = 1
MAX_POOL_CONNECTIONS = 5
_user_manager: UserManager | None = None


@dataclass(frozen=True)
class UserRecord:
    id: int
    username: str
    display_name: str
    is_admin: bool
    role: str = "user"
    is_active: bool = True

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> UserRecord:
        return cls(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            is_admin=bool(row["is_admin"]),
            role=str(row.get("role") or ("admin" if row["is_admin"] else "user")),
            is_active=bool(row.get("is_active", True)),
        )


class DuplicateUserError(Exception):
    """Raised when username is already taken."""


class InvalidCredentialsError(Exception):
    """Raised when login credentials are invalid."""


def _database_url() -> str:
    return os.getenv("DATABASE_URL", get_database_url())


class UserManager:
    """CRUD and authentication for local users."""

    def __init__(self, database_url: str | None = None) -> None:
        self._pool = ThreadedConnectionPool(
            MIN_POOL_CONNECTIONS,
            MAX_POOL_CONNECTIONS,
            database_url or _database_url(),
        )

    def init_table(self) -> None:
        ddl_table = """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN NOT NULL DEFAULT FALSE,
            role TEXT NOT NULL DEFAULT 'user',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
        ddl_role = "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user';"
        ddl_is_active = "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;"
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl_table)
                cur.execute(ddl_role)
                cur.execute(ddl_is_active)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def create_user(self, username: str, password: str, display_name: str | None = None) -> UserRecord:
        username_norm = _normalize_username(username)
        _validate_password_strength(password)

        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT COUNT(*) FROM users;")
                is_first_user = int(cur.fetchone()["count"]) == 0
                cur.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash, is_admin, role)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, username, display_name, is_admin, role, is_active;
                    """,
                    (
                        username_norm,
                        (display_name or username_norm).strip(),
                        hash_password(password),
                        is_first_user,
                        "admin" if is_first_user else "user",
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return UserRecord.from_row(row)
        except errors.UniqueViolation as exc:
            conn.rollback()
            raise DuplicateUserError("用户名已存在") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def authenticate(self, username: str, password: str) -> UserRecord:
        row = self._get_user_with_password(_normalize_username(username))
        if row is None or not verify_password(password, str(row["password_hash"])):
            raise InvalidCredentialsError("用户名或密码错误")
        if not bool(row.get("is_active", True)):
            raise InvalidCredentialsError("账号已被禁用")
        return UserRecord.from_row(row)

    def get_user_by_id(self, user_id: int) -> UserRecord | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, display_name, is_admin, role, is_active
                    FROM users
                    WHERE id = %s;
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
            return UserRecord.from_row(row) if row else None
        finally:
            self._pool.putconn(conn)

    def list_users(self) -> list[UserRecord]:
        """List users for admin management."""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, display_name, is_admin, role, is_active
                    FROM users
                    ORDER BY created_at DESC, id DESC;
                    """
                )
                return [UserRecord.from_row(row) for row in cur.fetchall()]
        finally:
            self._pool.putconn(conn)

    def update_user_role(self, user_id: int, role: str) -> UserRecord:
        """Update a user's role."""
        if role not in {"admin", "operator", "user"}:
            raise ValueError("role 必须是 admin、operator 或 user")
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET role = %s, is_admin = %s
                    WHERE id = %s
                    RETURNING id, username, display_name, is_admin, role, is_active;
                    """,
                    (role, role == "admin", user_id),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:
                raise ValueError("用户不存在")
            return UserRecord.from_row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def set_user_active(self, user_id: int, is_active: bool) -> UserRecord:
        """Enable or disable a user."""
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET is_active = %s
                    WHERE id = %s
                    RETURNING id, username, display_name, is_admin, role, is_active;
                    """,
                    (is_active, user_id),
                )
                row = cur.fetchone()
            conn.commit()
            if row is None:
                raise ValueError("用户不存在")
            return UserRecord.from_row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()

    def _get_user_with_password(self, username: str) -> dict[str, Any] | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, username, display_name, password_hash, is_admin, role, is_active
                    FROM users
                    WHERE username = %s;
                    """,
                    (username,),
                )
                return cur.fetchone()
        finally:
            self._pool.putconn(conn)


def get_user_manager() -> UserManager:
    global _user_manager
    if _user_manager is None:
        _user_manager = UserManager()
    return _user_manager


def _normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if len(normalized) < 3:
        raise ValueError("用户名至少需要 3 个字符")
    return normalized


def _validate_password_strength(password: str) -> None:
    """Reject weak passwords before hashing."""
    if len(password) < 10:
        raise ValueError("密码至少需要 10 位")
    if password.lower() in {"password123", "1234567890", "qwerty12345"}:
        raise ValueError("密码过于常见，请更换更强的密码")
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        raise ValueError("密码需要同时包含字母和数字")
