"""Audit logging for agent requests and responses."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    """Structured audit event for one agent interaction."""

    session_id: str
    user_input: str
    raw_output: str
    filtered_output: str
    user_id: int | None = None
    route: str | None = None
    tools: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class AuditLogService:
    """Emit audit events through structured logs.

    This keeps the first implementation lightweight while preserving the shape needed
    to swap in database persistence later.
    """

    @staticmethod
    def record(event: AuditEvent) -> None:
        payload = asdict(event)
        logger.info("agent_audit_event=%s", json.dumps(payload, ensure_ascii=False, default=_json_default))
        try:
            from app.database.audit_manager import get_audit_manager

            get_audit_manager().record(payload)
        except Exception:  # noqa: BLE001
            logger.exception("audit_log_persist_failed")


def extract_route_and_tools(thoughts: list[Any]) -> tuple[str | None, list[str]]:
    """Extract route and tool names from structured or legacy thought payloads."""
    route: str | None = None
    tools: list[str] = []
    for item in thoughts:
        if isinstance(item, dict):
            if item.get("type") == "route":
                route = str(item.get("route") or item.get("label") or "")
            if item.get("type") == "tool" and item.get("tool"):
                tools.append(str(item["tool"]))
            continue
        text = str(item)
        if "工具" in text and ":" in text:
            tools.append(text.rsplit(":", 1)[-1].strip())
        elif "路由" in text:
            route = text
    return route, tools


def _json_default(value: object) -> str:
    return str(value)
