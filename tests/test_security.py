from app.security.audit_log import extract_route_and_tools
from app.security.rate_limiter import RateLimitExceeded, enforce_rate_limit


def test_extract_route_and_tools_from_structured_steps() -> None:
    route, tools = extract_route_and_tools(
        [
            {"type": "route", "route": "sales", "label": "销售订单业务"},
            {"type": "tool", "tool": "get_customer_id", "label": "正在调用工具"},
        ]
    )

    assert route == "sales"
    assert tools == ["get_customer_id"]


async def test_enforce_rate_limit_raises_when_bucket_exceeds(monkeypatch) -> None:
    class FakeRedis:
        async def incr(self, _key: str) -> int:
            return 2

        async def expire(self, _key: str, _window: int) -> None:
            return None

    monkeypatch.setattr("app.security.rate_limiter._get_redis_client", lambda: FakeRedis())

    try:
        await enforce_rate_limit("user", limit=1, window_seconds=60)
    except RateLimitExceeded:
        return

    raise AssertionError("RateLimitExceeded was not raised")
