"""Tool registry metadata used by the orchestrator and observability layers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError


@dataclass(frozen=True)
class ToolSpec:
    name: str
    owner: str
    tool: Callable[..., Any]
    args_schema: type[BaseModel] | None
    description: str = ""
    business_domain: str = "general"
    required_role: str = "user"
    timeout_seconds: int = 30
    max_retries: int = 1
    is_write: bool = False
    enabled: bool = True

    def validate_args(self, payload: dict[str, Any]) -> BaseModel | dict[str, Any]:
        """Validate payload before execution when the tool exposes a Pydantic schema."""
        if self.args_schema is None:
            return payload
        try:
            return self.args_schema.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"工具 {self.name} 参数校验失败: {exc}") from exc


def register_tool(
    *,
    name: str,
    owner: str,
    tool: Callable[..., Any],
    args_schema: type[BaseModel] | None,
    description: str = "",
    business_domain: str = "general",
    required_role: str = "user",
    timeout_seconds: int = 30,
    max_retries: int = 1,
    is_write: bool = False,
    enabled: bool = True,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        owner=owner,
        tool=tool,
        args_schema=args_schema,
        description=description,
        business_domain=business_domain,
        required_role=required_role,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        is_write=is_write,
        enabled=enabled,
    )


async def execute_tool_spec(spec: ToolSpec, payload: dict[str, Any]) -> Any:
    """Validate and execute a registered tool with timeout and limited retry.

    The ReAct agent still receives LangChain tool objects directly, but this function
    is the shared execution contract for API-driven tools and future adapters.
    """

    validated = spec.validate_args(payload)
    kwargs = validated.model_dump() if isinstance(validated, BaseModel) else dict(validated)
    attempts = max(1, spec.max_retries + 1)
    last_error: Exception | None = None

    for _attempt in range(attempts):
        try:
            result = spec.tool.ainvoke(kwargs) if hasattr(spec.tool, "ainvoke") else _call_tool(spec.tool, kwargs)
            if inspect.isawaitable(result):
                return await asyncio.wait_for(result, timeout=spec.timeout_seconds)
            return result
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if spec.is_write:
                break

    assert last_error is not None
    raise last_error


def _call_tool(tool: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    if hasattr(tool, "invoke"):
        return tool.invoke(kwargs)  # type: ignore[no-any-return]
    return tool(**kwargs)
