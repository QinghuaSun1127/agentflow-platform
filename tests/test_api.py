from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import main
from app.core.config import get_api_keys
from app.database.user_manager import UserRecord


@pytest.fixture(autouse=True)
def clear_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", "")
    get_api_keys.cache_clear()
    main.app.dependency_overrides.clear()
    yield
    main.app.dependency_overrides.clear()


def fake_user() -> UserRecord:
    return UserRecord(id=42, username="demo", display_name="Demo User", is_admin=True)


def test_healthz() -> None:
    client = TestClient(main.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_agent_chat_response_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_save_message(user_id: int, session_id: str, role: str, content: str) -> None:
        return None

    async def fake_invoke(session_id: str, message: str) -> dict:
        return {
            "reply": "联系 13812345678",
            "thoughts": [{"type": "route", "route": "general", "label": "路由主管识别意图为: 通用问答"}],
        }

    async def fake_rate_limit(identity: str) -> None:
        return None

    monkeypatch.setattr(main, "_save_chat_message", fake_save_message)
    monkeypatch.setattr(main, "_invoke_agent_with_retry", fake_invoke)
    monkeypatch.setattr(main, "enforce_rate_limit", fake_rate_limit)
    main.app.dependency_overrides[main.get_current_user] = fake_user

    class FakeUsageManager:
        def record_interaction(self, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(main, "get_usage_manager", lambda: FakeUsageManager())

    client = TestClient(main.app)
    response = client.post("/api/v1/agent/chat", json={"session_id": "s1", "message": "hello"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "联系 138****5678"
    assert payload["thoughts"][0]["type"] == "route"


def test_sessions_return_structured_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeHistoryManager:
        def get_session_summaries(self, _user_id: int) -> list[dict]:
            return [
                {
                    "session_id": "s1",
                    "title": "demo",
                    "last_updated": datetime(2026, 5, 5, tzinfo=UTC),
                }
            ]

    monkeypatch.setattr(main, "get_chat_history_manager", lambda: FakeHistoryManager())
    main.app.dependency_overrides[main.get_current_user] = fake_user

    client = TestClient(main.app)
    response = client.get("/api/v1/sessions")

    assert response.status_code == 200
    assert response.json()["sessions"][0]["title"] == "demo"


def test_auth_required_for_sessions() -> None:
    client = TestClient(main.app)

    assert client.get("/api/v1/sessions").status_code == 401


def test_admin_stats_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeUsageManager:
        def get_admin_stats(self, _days: int = 7) -> dict:
            return {"totals": {"total_calls": 1}, "daily": [], "routes": [], "recent": []}

    monkeypatch.setattr(main, "get_usage_manager", lambda: FakeUsageManager())
    main.app.dependency_overrides[main.get_current_user] = fake_user

    client = TestClient(main.app)
    response = client.get("/api/v1/admin/stats")

    assert response.status_code == 200
    assert response.json()["totals"]["total_calls"] == 1


def test_admin_tools_config_and_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeToolsManager:
        def list_configs(self) -> list[object]:
            return [
                type(
                    "Cfg",
                    (),
                    {
                        "name": "get_customer_id",
                        "owner": "sales",
                        "enabled": True,
                        "is_write": False,
                        "timeout_seconds": 10,
                        "max_retries": 1,
                    },
                )()
            ]

        def update_config(self, name: str, **_kwargs: object) -> object:
            assert name == "get_customer_id"
            return type(
                "Cfg",
                (),
                {
                    "name": "get_customer_id",
                    "owner": "sales",
                    "enabled": False,
                    "is_write": False,
                    "timeout_seconds": 10,
                    "max_retries": 1,
                },
            )()

    monkeypatch.setattr(main, "get_tools_manager", lambda: FakeToolsManager())
    main.app.dependency_overrides[main.get_current_user] = fake_user

    client = TestClient(main.app)
    resp = client.get("/api/v1/admin/tools/config")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "get_customer_id"
    assert resp.json()[0]["enabled"] is True

    resp2 = client.patch("/api/v1/admin/tools/get_customer_id", json={"enabled": False})
    assert resp2.status_code == 200
    assert resp2.json()["enabled"] is False


def test_admin_tools_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeUsageManager:
        def get_tool_stats(self, _days: int = 7) -> list[dict]:
            return [{"name": "get_customer_id", "calls": 3, "error_calls": 1, "avg_latency_ms": 12.5}]

    monkeypatch.setattr(main, "get_usage_manager", lambda: FakeUsageManager())
    main.app.dependency_overrides[main.get_current_user] = fake_user

    client = TestClient(main.app)
    resp = client.get("/api/v1/admin/tools/stats")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "get_customer_id"


def test_auth_refresh_returns_new_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeUserManager:
        def get_user_by_id(self, user_id: int) -> UserRecord | None:
            if user_id == 42:
                return fake_user()
            return None

    monkeypatch.setattr(main, "get_user_manager", lambda: FakeUserManager())
    monkeypatch.setattr(main, "decode_refresh_token", lambda _token: {"sub": "42"})

    client = TestClient(main.app)
    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": "dummy-refresh-token-12345"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
