import json

import pytest

from app.tools import hr_tools, mock_tools, sales_tools
from app.tools.mock_tools import TOOL_REGISTRY
from app.tools.registry import execute_tool_spec


def test_get_customer_id_returns_demo_user() -> None:
    payload = json.loads(mock_tools.get_customer_id.invoke({"phone": "13800138000"}))

    assert payload["user_id"] == "U_888"


def test_disabled_tool_returns_tool_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeToolsManager:
        def is_enabled(self, _name: str) -> bool:
            return False

    monkeypatch.setattr(sales_tools, "get_tools_manager", lambda: FakeToolsManager())

    payload = json.loads(mock_tools.get_customer_id.invoke({"phone": "13800138000"}))
    assert payload["code"] == "TOOL_DISABLED"


def test_get_recent_orders_returns_demo_orders() -> None:
    orders = json.loads(mock_tools.get_recent_orders.invoke({"user_id": "U_888"}))

    assert orders[0]["order_id"] == "ORD-20250501-001"


def test_search_company_policy_returns_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_search(query: str, top_k: int = 3) -> list[dict]:
        raise RuntimeError("db down")

    monkeypatch.setattr(hr_tools, "search_document_chunks", fail_search)

    payload = json.loads(mock_tools.search_company_policy.invoke({"query": "差旅报销"}))

    assert payload["error"] is True
    assert "制度知识库检索失败" in payload["message"]


def test_tool_registry_validates_arguments() -> None:
    spec = TOOL_REGISTRY["get_customer_id"]

    validated = spec.validate_args({"phone": "13800138000"})

    assert validated.phone == "13800138000"


@pytest.mark.asyncio
async def test_tool_registry_executes_registered_tool() -> None:
    result = await execute_tool_spec(TOOL_REGISTRY["get_customer_id"], {"phone": "13800138000"})

    assert json.loads(result)["user_id"] == "U_888"
