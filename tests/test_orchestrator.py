import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent import orchestrator


def test_normalize_route_defaults_to_general() -> None:
    assert orchestrator._normalize_route("sales") == "sales"
    assert orchestrator._normalize_route("unknown") == "general"


def test_latest_human_text_reads_last_user_message() -> None:
    messages = [HumanMessage(content="first"), AIMessage(content="reply"), HumanMessage(content="last")]

    assert orchestrator._latest_human_text(messages) == "last"


def test_trim_messages_keeps_system_and_recent_non_system_messages() -> None:
    messages = [SystemMessage(content="system"), *[HumanMessage(content=str(i)) for i in range(25)]]

    result = orchestrator._trim_messages_pre_model_hook({"messages": messages})

    trimmed = result["messages"]
    assert trimmed[0].id == orchestrator.REMOVE_ALL_MESSAGES
    assert isinstance(trimmed[1], SystemMessage)
    assert [msg.content for msg in trimmed[2:]] == [str(i) for i in range(5, 25)]


@pytest.mark.asyncio
async def test_process_chat_timeout_returns_structured_step(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowGraph:
        async def ainvoke(self, state: dict, _config: dict) -> dict:
            await orchestrator.asyncio.sleep(0.01)
            return state

    async def fake_get_graph() -> SlowGraph:
        return SlowGraph()

    monkeypatch.setattr(orchestrator, "_get_graph", fake_get_graph)
    monkeypatch.setattr(orchestrator, "_AGENT_TIMEOUT_SECONDS", 0.001)

    result = await orchestrator.process_chat("session", "hello")

    assert result["thoughts"][0]["type"] == "timeout"
