"""AgentFlow Platform：FastAPI 入口，暴露 Agent 对话 HTTP 接口。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg2
import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from psycopg2 import OperationalError
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.agent.orchestrator import process_chat
from app.core.config import (
    get_api_keys,
    get_app_env,
    get_cors_origins,
    get_database_url,
    get_redis_url,
    validate_production_security,
)
from app.database.audit_manager import get_audit_manager
from app.database.history_manager import get_chat_history_manager
from app.database.tools_manager import get_tools_manager
from app.database.usage_manager import get_usage_manager
from app.database.user_manager import (
    DuplicateUserError,
    InvalidCredentialsError,
    UserRecord,
    get_user_manager,
)
from app.security.audit_log import AuditEvent, AuditLogService, extract_route_and_tools
from app.security.auth import (
    AuthError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from app.security.dlp_filter import SensitiveDataFilter
from app.security.rate_limiter import RateLimitExceeded, enforce_rate_limit
from app.tools.mock_tools import TOOL_REGISTRY

logger = logging.getLogger(__name__)
_METRICS: dict[str, int] = {
    "agentflow_chat_requests_total": 0,
    "agentflow_chat_errors_total": 0,
    "agentflow_chat_latency_ms_total": 0,
    "agentflow_tool_calls_total": 0,
}

# PostgreSQL 容器刚启动或 Docker Desktop 恢复时，5432 端口可能短暂不可用；
# 启动阶段做有限等待，避免一次 connection refused 就跳过知识库初始化。
_KNOWLEDGE_BASE_INIT_MAX_ATTEMPTS = 10
_KNOWLEDGE_BASE_INIT_BACKOFF_SEC = 1.0


def _bootstrap_datastores() -> None:
    """同步执行：初始化知识库与聊天历史表（供 lifespan 在线程中调用）。"""
    from app.rag.knowledge_base import init_db, seed_travel_policy_demo_if_missing

    init_db()
    seed_travel_policy_demo_if_missing()
    get_user_manager().init_table()
    get_chat_history_manager().init_table()
    get_tools_manager().init_table()
    get_tools_manager().sync_from_registry()
    get_usage_manager().init_table()
    get_audit_manager().init_table()


async def _bootstrap_datastores_with_retry() -> None:
    """启动阶段初始化数据库表，等待 PostgreSQL/pgvector 就绪后再继续。"""
    last_error: Exception | None = None
    for attempt in range(1, _KNOWLEDGE_BASE_INIT_MAX_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_bootstrap_datastores)
            logger.info("知识库与聊天历史表已就绪。")
            return
        except Exception as exc:  # noqa: BLE001 — 数据库连接/扩展初始化可能抛出多种异常
            last_error = exc
            logger.warning(
                "数据库初始化失败，准备第 %s/%s 次重试：%s",
                attempt,
                _KNOWLEDGE_BASE_INIT_MAX_ATTEMPTS,
                exc,
                exc_info=attempt == _KNOWLEDGE_BASE_INIT_MAX_ATTEMPTS,
            )
            if attempt < _KNOWLEDGE_BASE_INIT_MAX_ATTEMPTS:
                await asyncio.sleep(_KNOWLEDGE_BASE_INIT_BACKOFF_SEC * attempt)

    assert last_error is not None
    raise last_error


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用启动时初始化数据库资源；失败则记录日志但不阻断服务启动。"""
    try:
        validate_production_security()
    except Exception:
        logger.exception("生产安全校验未通过，服务停止启动。")
        raise
    try:
        await _bootstrap_datastores_with_retry()
    except Exception:
        logger.exception(
            "数据库启动初始化失败（请检查 PostgreSQL/pgvector 与 DATABASE_URL；"
            "服务仍启动，但知识库和聊天历史可能不可用）。"
        )
    yield

# LLM / Agent 调用在网络抖动或服务端限流时可能短暂失败，做有限次重试以降低误报
_AGENT_CHAT_MAX_ATTEMPTS = 3
_AGENT_CHAT_BACKOFF_SEC = 0.4


class ChatRequest(BaseModel):
    """客户端发起一轮对话时的请求体。"""

    session_id: str = Field(
        ...,
        min_length=1,
        description="会话标识，用于与 LangGraph checkpoint 的 thread_id 对齐，实现同一会话多轮记忆。",
    )
    message: str = Field(
        ...,
        min_length=1,
        description="用户本轮输入的自然语言内容。",
    )


class AuthRequest(BaseModel):
    """登录/注册请求体。"""

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=80)


class UserProfile(BaseModel):
    """当前登录用户信息。"""

    id: int
    username: str
    display_name: str
    is_admin: bool = False
    role: str = "user"
    is_active: bool = True


class AuthResponse(BaseModel):
    """认证成功响应。"""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    user: UserProfile


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=20)


class AgentStep(BaseModel):
    """结构化 Agent 执行轨迹，供前端渲染 timeline。"""

    type: str = Field(..., description="步骤类型，如 route、tool、timeout、circuit_breaker。")
    label: str = Field(..., description="用户可读的步骤描述。")
    status: str = Field(default="completed", description="步骤状态。")
    route: str | None = Field(default=None, description="路由名称。")
    tool: str | None = Field(default=None, description="工具名称。")
    duration_ms: int | None = Field(default=None, description="步骤耗时。")


class ChatResponse(BaseModel):
    """Agent 处理完成后的统一响应体。"""

    reply: str = Field(..., description="模型最终返回给用户可见的答复正文。")
    thoughts: list[AgentStep] = Field(default_factory=list, description="LangGraph 真实执行轨迹。")


class SessionSummary(BaseModel):
    """会话摘要，供前端一次性渲染历史列表。"""

    session_id: str = Field(..., description="会话 ID。")
    title: str = Field(..., description="会话标题。")
    last_updated: datetime = Field(..., description="最近更新时间。")


class SessionListResponse(BaseModel):
    """会话列表响应体。"""

    sessions: list[SessionSummary] = Field(default_factory=list, description="按最近更新时间倒序排列的会话摘要。")


class ChatHistoryMessage(BaseModel):
    """单条聊天历史消息。"""

    id: int = Field(..., description="聊天历史自增主键。")
    session_id: str = Field(..., description="会话 ID。")
    role: str = Field(..., description="消息角色：user、assistant 或 system。")
    content: str = Field(..., description="消息正文。")
    created_at: datetime = Field(..., description="消息创建时间。")


class ChatHistoryResponse(BaseModel):
    """某个会话的聊天历史响应体。"""

    session_id: str = Field(..., description="会话 ID。")
    messages: list[ChatHistoryMessage] = Field(default_factory=list, description="会话消息列表。")


class RenameSessionRequest(BaseModel):
    """Request body for renaming a chat session."""

    title: str = Field(..., min_length=1, max_length=80)


class AdminStatsResponse(BaseModel):
    """Admin dashboard aggregated statistics."""

    totals: dict[str, Any] = Field(default_factory=dict)
    daily: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    recent: list[dict[str, Any]] = Field(default_factory=list)


class DocumentResponse(BaseModel):
    """Knowledge base document metadata."""

    id: int
    title: str
    filename: str
    content_type: str
    status: str = "ready"
    chunk_count: int = 0
    created_at: datetime | None = None


class DocumentUploadResponse(BaseModel):
    """Document upload result."""

    id: int
    title: str
    filename: str
    chunk_count: int


class DocumentReindexResponse(BaseModel):
    """Document reindex result."""

    document_id: int
    chunk_count: int


class ToolInfoResponse(BaseModel):
    """Tool registry metadata for admin display."""

    name: str
    owner: str
    description: str = ""
    business_domain: str = "general"
    required_role: str = "user"
    timeout_seconds: int
    max_retries: int
    is_write: bool
    enabled: bool = True


class ToolConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    max_retries: int | None = Field(default=None, ge=0, le=10)


class ToolStatsResponse(BaseModel):
    name: str
    calls: int
    error_calls: int
    avg_latency_ms: float


class UpdateUserRequest(BaseModel):
    """Admin user management request."""

    role: str | None = Field(default=None, pattern="^(admin|operator|user)$")
    is_active: bool | None = None


app = FastAPI(title="AgentFlow Platform", version="0.1.0", lifespan=lifespan)

_cors_origins = get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def require_api_key(request: Request) -> None:
    """Protect API endpoints when API_KEYS is configured."""
    allowed_keys = get_api_keys()
    if not allowed_keys:
        return

    auth_header = request.headers.get("Authorization", "")
    bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    api_key = request.headers.get("X-API-Key", "").strip()
    if bearer not in allowed_keys and api_key not in allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或无效的 API key。",
        )


async def get_current_user(request: Request) -> UserRecord:
    """Resolve current user from a Bearer JWT."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录。")
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
    except (AuthError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录。") from exc
    user = await asyncio.to_thread(get_user_manager().get_user_by_id, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在。")
    return user


async def require_admin(user: UserRecord = Depends(get_current_user)) -> UserRecord:
    """Require an authenticated admin user."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限。")
    return user


async def require_operator(user: UserRecord = Depends(get_current_user)) -> UserRecord:
    """Require operator or admin permissions."""
    if user.role not in {"admin", "operator"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要运营或管理员权限。")
    return user


def _auth_response_for_user(user: UserRecord) -> AuthResponse:
    access_token = create_access_token(
        {"sub": str(user.id), "username": user.username, "is_admin": user.is_admin, "role": user.role}
    )
    refresh_token = create_refresh_token({"sub": str(user.id), "username": user.username})
    return AuthResponse(access_token=access_token, refresh_token=refresh_token, user=_profile_for_user(user))


def _profile_for_user(user: UserRecord) -> UserProfile:
    return UserProfile(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
        role=user.role,
        is_active=user.is_active,
    )


async def _invoke_agent_with_retry(session_id: str, message: str) -> dict:
    """调用编排层 process_chat，带有限重试与异常记录（满足 LLM 调用的健壮性要求）。"""
    last_error: Exception | None = None
    for attempt in range(1, _AGENT_CHAT_MAX_ATTEMPTS + 1):
        try:
            return await process_chat(session_id, message)
        except Exception as exc:  # noqa: BLE001 — 编排层可能抛出多种供应商相关异常
            last_error = exc
            logger.warning(
                "Agent 调用失败，准备第 %s/%s 次重试：%s",
                attempt,
                _AGENT_CHAT_MAX_ATTEMPTS,
                exc,
                exc_info=attempt == _AGENT_CHAT_MAX_ATTEMPTS,
            )
            if attempt < _AGENT_CHAT_MAX_ATTEMPTS:
                await asyncio.sleep(_AGENT_CHAT_BACKOFF_SEC * attempt)
    assert last_error is not None
    raise _map_runtime_error_to_http(last_error) from last_error


async def _save_chat_message(user_id: int, session_id: str, role: str, content: str) -> None:
    """将聊天消息写入 PostgreSQL，失败时返回明确的服务端错误。"""
    try:
        await asyncio.to_thread(
            get_chat_history_manager().save_message,
            user_id,
            session_id,
            role,
            content,
        )
    except Exception as exc:  # noqa: BLE001 — 持久化层可能抛出连接池/SQL/校验异常
        logger.exception("聊天消息持久化失败：session_id=%s role=%s", session_id, role)
        raise HTTPException(
            status_code=500,
            detail={"code": "DB_UNAVAILABLE", "message": "聊天记录保存失败，请检查数据库连接后重试。"},
        ) from exc


@app.post("/api/v1/auth/register", response_model=AuthResponse)
async def register(body: AuthRequest) -> AuthResponse:
    """Register a local account. The first registered user becomes admin."""
    try:
        user = await asyncio.to_thread(
            get_user_manager().create_user,
            body.username,
            body.password,
            body.display_name,
        )
    except DuplicateUserError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _auth_response_for_user(user)


@app.post("/api/v1/auth/login", response_model=AuthResponse)
async def login(body: AuthRequest) -> AuthResponse:
    """Login with a local account."""
    try:
        await enforce_rate_limit(f"login:{body.username.strip().lower()}", limit=5, window_seconds=300)
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过多，请稍后重试。") from exc
    try:
        user = await asyncio.to_thread(get_user_manager().authenticate, body.username, body.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误。") from exc
    return _auth_response_for_user(user)


@app.get("/api/v1/auth/me", response_model=UserProfile)
async def me(user: UserRecord = Depends(get_current_user)) -> UserProfile:
    """Return current user profile."""
    return _profile_for_user(user)


@app.post("/api/v1/auth/refresh", response_model=AuthResponse)
async def refresh_token(body: RefreshTokenRequest) -> AuthResponse:
    """Refresh access token using a valid refresh token."""
    try:
        claims = decode_refresh_token(body.refresh_token)
        user_id = int(claims["sub"])
    except (AuthError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效或已过期。") from exc
    user = await asyncio.to_thread(get_user_manager().get_user_by_id, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已被禁用。")
    return _auth_response_for_user(user)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> dict[str, Any]:
    """Readiness probe for required external dependencies."""
    checks: dict[str, Any] = {
        "llm_config": bool(os.getenv("DEEPSEEK_API_KEY") and os.getenv("DEEPSEEK_BASE_URL")),
        "postgres": False,
        "redis": False,
    }

    try:
        conn = await asyncio.to_thread(psycopg2.connect, get_database_url())
        conn.close()
        checks["postgres"] = True
    except Exception as exc:  # noqa: BLE001
        checks["postgres_error"] = str(exc)

    redis_client: Redis | None = None
    try:
        redis_client = Redis.from_url(get_redis_url(), decode_responses=True)
        await redis_client.ping()
        checks["redis"] = True
    except Exception as exc:  # noqa: BLE001
        checks["redis_error"] = str(exc)
    finally:
        if redis_client is not None:
            await redis_client.aclose()

    ready = all(bool(checks[name]) for name in ("llm_config", "postgres", "redis"))
    if not ready:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=checks)
    return {"status": "ready", "checks": checks}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    """Minimal Prometheus-compatible metrics without adding another dependency."""
    lines = []
    for name, value in sorted(_METRICS.items()):
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


@app.get("/api/v1/sessions", response_model=SessionListResponse)
async def get_sessions(user: UserRecord = Depends(get_current_user)) -> SessionListResponse:
    """返回已持久化的会话摘要，按最近更新时间倒序排列。"""
    try:
        sessions = await asyncio.to_thread(get_chat_history_manager().get_session_summaries, user.id)
    except Exception as exc:  # noqa: BLE001 — 数据库查询异常统一转为 HTTP 错误
        logger.exception("查询会话列表失败。")
        raise HTTPException(status_code=500, detail="会话列表查询失败，请稍后重试。") from exc
    return SessionListResponse(sessions=sessions)


@app.get(
    "/api/v1/sessions/{session_id}/messages",
    response_model=ChatHistoryResponse,
)
async def get_session_messages(session_id: str, user: UserRecord = Depends(get_current_user)) -> ChatHistoryResponse:
    """返回指定会话的历史消息详情。"""
    try:
        messages = await asyncio.to_thread(
            get_chat_history_manager().get_messages_by_session,
            user.id,
            session_id,
        )
    except Exception as exc:  # noqa: BLE001 — 数据库查询异常统一转为 HTTP 错误
        logger.exception("查询会话消息失败：session_id=%s", session_id)
        raise HTTPException(status_code=500, detail="会话消息查询失败，请稍后重试。") from exc
    return ChatHistoryResponse(session_id=session_id, messages=messages)


@app.patch("/api/v1/sessions/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    user: UserRecord = Depends(get_current_user),
) -> dict[str, str]:
    """Rename a user's chat session."""
    try:
        await asyncio.to_thread(get_chat_history_manager().rename_session, user.id, session_id, body.title)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "ok"}


@app.delete("/api/v1/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, user: UserRecord = Depends(get_current_user)) -> None:
    """Delete a user's chat session."""
    await asyncio.to_thread(get_chat_history_manager().delete_session, user.id, session_id)


@app.get("/api/v1/admin/stats", response_model=AdminStatsResponse)
async def admin_stats(days: int = 7, _user: UserRecord = Depends(require_admin)) -> AdminStatsResponse:
    """Return aggregated usage stats for the admin dashboard."""
    stats = await asyncio.to_thread(get_usage_manager().get_admin_stats, days)
    return AdminStatsResponse(**stats)


@app.get("/api/v1/admin/documents", response_model=list[DocumentResponse])
async def admin_documents(_user: UserRecord = Depends(require_operator)) -> list[DocumentResponse]:
    """List knowledge base documents."""
    from app.rag.knowledge_base import list_documents

    docs = await asyncio.to_thread(list_documents)
    return [DocumentResponse(**doc) for doc in docs]


@app.post("/api/v1/admin/documents/upload", response_model=DocumentUploadResponse)
async def admin_upload_document(
    file: UploadFile = File(...),
    title: str | None = None,
    _user: UserRecord = Depends(require_operator),
) -> DocumentUploadResponse:
    """Upload a txt/md/pdf document and index it into pgvector."""
    from app.rag.knowledge_base import ingest_document

    filename = file.filename or "uploaded.txt"
    if not filename.lower().endswith((".txt", ".md", ".pdf")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="仅支持 .txt、.md、.pdf 文件。")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传文件为空。")
    try:
        result = await asyncio.to_thread(
            ingest_document,
            title or filename,
            filename,
            file.content_type or "application/octet-stream",
            raw,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return DocumentUploadResponse(**result)


@app.delete("/api/v1/admin/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_document(document_id: int, _user: UserRecord = Depends(require_operator)) -> None:
    """Delete a document and its chunks."""
    from app.rag.knowledge_base import delete_document

    await asyncio.to_thread(delete_document, document_id)


@app.post("/api/v1/admin/documents/{document_id}/reindex", response_model=DocumentReindexResponse)
async def admin_reindex_document(
    document_id: int,
    _user: UserRecord = Depends(require_operator),
) -> DocumentReindexResponse:
    """Rebuild embeddings for an uploaded document."""
    from app.rag.knowledge_base import reindex_document

    try:
        result = await asyncio.to_thread(reindex_document, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DocumentReindexResponse(**result)


@app.get("/api/v1/admin/tools", response_model=list[ToolInfoResponse])
async def admin_tools(_user: UserRecord = Depends(require_admin)) -> list[ToolInfoResponse]:
    """Expose registered tool metadata for the admin dashboard."""
    configs = {cfg.name: cfg for cfg in await asyncio.to_thread(get_tools_manager().list_configs)}
    out: list[ToolInfoResponse] = []
    for spec in TOOL_REGISTRY.values():
        cfg = configs.get(spec.name)
        out.append(
            ToolInfoResponse(
                name=spec.name,
                owner=spec.owner,
                description=spec.description,
                business_domain=spec.business_domain,
                required_role=spec.required_role,
                timeout_seconds=cfg.timeout_seconds if cfg else spec.timeout_seconds,
                max_retries=cfg.max_retries if cfg else spec.max_retries,
                is_write=spec.is_write,
                enabled=cfg.enabled if cfg else True,
            )
        )
    return out


@app.get("/api/v1/admin/tools/config", response_model=list[ToolInfoResponse])
async def admin_tools_config(_user: UserRecord = Depends(require_admin)) -> list[ToolInfoResponse]:
    """Return persisted tool configs (source of truth)."""
    configs = await asyncio.to_thread(get_tools_manager().list_configs)
    return [
        ToolInfoResponse(
            name=cfg.name,
            owner=cfg.owner,
            timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
            is_write=cfg.is_write,
            enabled=cfg.enabled,
        )
        for cfg in configs
    ]


@app.patch("/api/v1/admin/tools/{tool_name}", response_model=ToolInfoResponse)
async def admin_update_tool(
    tool_name: str,
    body: ToolConfigUpdateRequest,
    _user: UserRecord = Depends(require_admin),
) -> ToolInfoResponse:
    """Update tool config such as enabled/timeout/retries."""
    try:
        cfg = await asyncio.to_thread(
            get_tools_manager().update_config,
            tool_name,
            enabled=body.enabled,
            timeout_seconds=body.timeout_seconds,
            max_retries=body.max_retries,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ToolInfoResponse(
        name=cfg.name,
        owner=cfg.owner,
        description=TOOL_REGISTRY.get(cfg.name).description if TOOL_REGISTRY.get(cfg.name) else "",
        business_domain=TOOL_REGISTRY.get(cfg.name).business_domain if TOOL_REGISTRY.get(cfg.name) else "general",
        required_role=TOOL_REGISTRY.get(cfg.name).required_role if TOOL_REGISTRY.get(cfg.name) else "user",
        timeout_seconds=cfg.timeout_seconds,
        max_retries=cfg.max_retries,
        is_write=cfg.is_write,
        enabled=cfg.enabled,
    )


@app.get("/api/v1/admin/tools/stats", response_model=list[ToolStatsResponse])
async def admin_tool_stats(days: int = 7, _user: UserRecord = Depends(require_admin)) -> list[ToolStatsResponse]:
    stats = await asyncio.to_thread(get_usage_manager().get_tool_stats, days)
    return [ToolStatsResponse(**row) for row in stats]


@app.get("/api/v1/admin/users", response_model=list[UserProfile])
async def admin_users(_user: UserRecord = Depends(require_admin)) -> list[UserProfile]:
    """List users for admin management."""
    users = await asyncio.to_thread(get_user_manager().list_users)
    return [_profile_for_user(user) for user in users]


@app.patch("/api/v1/admin/users/{user_id}", response_model=UserProfile)
async def admin_update_user(
    user_id: int,
    body: UpdateUserRequest,
    current_user: UserRecord = Depends(require_admin),
) -> UserProfile:
    """Update role or active state for a user."""
    if user_id == current_user.id and body.is_active is False:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能禁用当前登录管理员。")
    manager = get_user_manager()
    try:
        updated = await asyncio.to_thread(manager.get_user_by_id, user_id)
        if updated is None:
            raise ValueError("用户不存在")
        if body.role is not None:
            updated = await asyncio.to_thread(manager.update_user_role, user_id, body.role)
        if body.is_active is not None:
            updated = await asyncio.to_thread(manager.set_user_active, user_id, body.is_active)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _profile_for_user(updated)


@app.post("/api/v1/agent/chat", response_model=ChatResponse)
async def agent_chat(body: ChatRequest, user: UserRecord = Depends(get_current_user)) -> ChatResponse:
    """接收用户消息，经 ReAct Agent 处理后返回自然语言答复。"""
    identity = f"user:{user.id}"
    try:
        await enforce_rate_limit(identity)
    except RateLimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="请求过于频繁，请稍后重试。") from exc

    start = time.perf_counter()
    _METRICS["agentflow_chat_requests_total"] += 1
    safe_user_message = SensitiveDataFilter.mask_sensitive_data(body.message)
    status_value = "success"
    error_text: str | None = None
    try:
        await _save_chat_message(user.id, body.session_id, "user", safe_user_message)
        chat_result = await _invoke_agent_with_retry(body.session_id, body.message)
    except HTTPException as exc:
        status_value = "error"
        try:
            detail = exc.detail
            error_text = (
                f"{detail.get('code')}: {detail.get('message')}" if isinstance(detail, dict) else str(detail)
            )
        except Exception:
            error_text = "agent_http_exception"
        _METRICS["agentflow_chat_errors_total"] += 1
        duration_ms = int((time.perf_counter() - start) * 1000)
        await asyncio.to_thread(
            get_usage_manager().record_interaction,
            user_id=user.id,
            session_id=body.session_id,
            prompt=body.message,
            reply="",
            route=None,
            tools=[],
            tool_names=[],
            latency_ms=duration_ms,
            status=status_value,
            error=error_text,
        )
        raise
    except Exception as exc:
        status_value = "error"
        error_text = str(exc)
        _METRICS["agentflow_chat_errors_total"] += 1
        duration_ms = int((time.perf_counter() - start) * 1000)
        await asyncio.to_thread(
            get_usage_manager().record_interaction,
            user_id=user.id,
            session_id=body.session_id,
            prompt=body.message,
            reply="",
            route=None,
            tools=[],
            tool_names=[],
            latency_ms=duration_ms,
            status=status_value,
            error=error_text,
        )
        raise
    reply = str(chat_result.get("reply", ""))
    thoughts = chat_result.get("thoughts", [])
    safe_reply = SensitiveDataFilter.mask_sensitive_data(reply)
    await _save_chat_message(user.id, body.session_id, "assistant", safe_reply)
    duration_ms = int((time.perf_counter() - start) * 1000)
    route, tools = extract_route_and_tools(thoughts if isinstance(thoughts, list) else [])
    _METRICS["agentflow_chat_latency_ms_total"] += duration_ms
    _METRICS["agentflow_tool_calls_total"] += len(tools)
    AuditLogService.record(
        AuditEvent(
            user_id=user.id,
            session_id=body.session_id,
            user_input=body.message,
            raw_output=reply,
            filtered_output=safe_reply,
            route=route,
            tools=tools,
            latency_ms=duration_ms,
        )
    )
    await asyncio.to_thread(
        get_usage_manager().record_interaction,
        user_id=user.id,
        session_id=body.session_id,
        prompt=body.message,
        reply=safe_reply,
        route=route,
        tools=tools,
        tool_names=tools,
        latency_ms=duration_ms,
        status=status_value,
        error=error_text,
    )
    return ChatResponse(
        reply=safe_reply,
        thoughts=_normalize_steps(thoughts),
    )


def _normalize_steps(raw_steps: object) -> list[AgentStep]:
    """Normalize legacy string thoughts and structured dict steps into response models."""
    if not isinstance(raw_steps, list):
        return []
    steps: list[AgentStep] = []
    for item in raw_steps:
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("tool") or item.get("route") or item.get("type") or "")
            if label:
                steps.append(
                    AgentStep(
                        type=str(item.get("type") or "step"),
                        label=label,
                        status=str(item.get("status") or "completed"),
                        route=str(item["route"]) if item.get("route") is not None else None,
                        tool=str(item["tool"]) if item.get("tool") is not None else None,
                        duration_ms=int(item["duration_ms"]) if item.get("duration_ms") is not None else None,
                    )
                )
            continue
        text = str(item)
        if text:
            steps.append(AgentStep(type="step", label=text))
    if steps:
        steps.append(AgentStep(type="complete", label="生成最终回答", status="completed"))
    return steps


def _map_runtime_error_to_http(error: Exception | None) -> HTTPException:
    """Map runtime failures to user-facing typed errors for frontend guidance."""
    if error is None:
        return HTTPException(status_code=502, detail={"code": "AGENT_UNKNOWN", "message": "智能体暂时不可用。"})

    text = str(error).lower()
    if isinstance(error, OperationalError) or "connection refused" in text or "could not connect" in text:
        return HTTPException(
            status_code=503,
            detail={"code": "DB_UNAVAILABLE", "message": "数据库不可用，请检查 PostgreSQL 后重试。"},
        )
    if "redis" in text:
        return HTTPException(
            status_code=503,
            detail={"code": "REDIS_UNAVAILABLE", "message": "Redis 不可用，请检查缓存服务后重试。"},
        )
    if "deepseek_api_key" in text or "deepseek_base_url" in text or "api key" in text:
        return HTTPException(
            status_code=500,
            detail={"code": "LLM_CONFIG_ERROR", "message": "LLM 配置异常，请检查 DEEPSEEK_API_KEY / BASE_URL。"},
        )
    if "tool" in text:
        return HTTPException(
            status_code=502,
            detail={"code": "TOOL_CALL_FAILED", "message": "工具调用失败，请稍后重试或缩小问题范围。"},
        )
    return HTTPException(
        status_code=502,
        detail={"code": "AGENT_RUNTIME_ERROR", "message": "智能体运行失败，请稍后重试。"},
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=os.getenv("UVICORN_RELOAD", "false").strip().lower() == "true" and get_app_env() != "production",
    )
