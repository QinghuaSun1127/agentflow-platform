"""Multi-Agent 编排：Router + 专业子 Agent + Redis 短期记忆 checkpoint。"""

from __future__ import annotations

import asyncio
import logging
import os
from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph, add_messages
from langgraph.graph.message import REMOVE_ALL_MESSAGES, RemoveMessage
from langgraph.prebuilt import create_react_agent

from app.core.llm import get_llm
from app.database.tools_manager import get_tools_manager
from app.memory.redis_saver import create_redis_checkpointer
from app.tools.mock_tools import (
    HR_TOOL_SPECS,
    SALES_TOOL_SPECS,
)

logger = logging.getLogger(__name__)

RouteName = Literal["hr", "sales", "general"]


class AgentState(TypedDict):
    """Multi-Agent 状态：messages 由 LangGraph 自动追加，route 保存路由结果。"""

    messages: Annotated[list[BaseMessage], add_messages]
    route: RouteName
    thoughts: Annotated[list[dict], add]


class ChatResult(TypedDict):
    """对外返回的聊天结果：最终回复 + 真实执行轨迹。"""

    reply: str
    thoughts: list[dict]


# 供其它模块显式引用的工具列表（运行时会按工具开关重建）
TOOLS: list[object] = []

_OUTPUT_PROTOCOL = (
    "\n\n《输出排版协议》："
    "\n- 极简原则：禁止在回复中使用任何非必要的 Emoji 表情符号。"
    "\n- 结构清晰：优先使用标准 Markdown 的无序列表（-）进行要点说明，文字必须精炼，拒绝长篇大论。"
    "\n- 表格规范：当需要展示多维数据（如客户信息、订单详情）时，"
    "必须使用最基础的 Markdown 原生表格（| 字段 | 内容 |），"
    "绝对禁止将表格嵌套在引用块（>）或其他复杂结构中。"
    "\n- 客观中立：保持专业、理性的 AI 助手口吻，不使用任何夸张的语气词。"
)

_HR_AGENT_PROMPT = (
    "你是 AgentFlow 平台的专业 HR 助手，只负责公司制度、差旅报销、休假考勤、员工手册等内部政策问题。"
    "遇到制度、报销、差旅、休假、考勤等问题时，必须先调用 search_company_policy 检索知识库，再基于检索结果回答。"
    "如果检索结果为空，应说明当前知识库未找到明确条款，不得编造制度。"
    "回答必须明确提示演示环境数据仅供联调，以正式书面制度为准。"
    "当工具返回 sources 时，答案末尾必须新增“参考来源”小节，按列表列出 source/title/page_number。"
    + _OUTPUT_PROTOCOL
)

_SALES_AGENT_PROMPT = (
    "你是 AgentFlow 平台的严谨销售与订单助手，只负责客户识别、订单查询和订单状态修改。"
    "用户提供手机号时，必须先调用 get_customer_id 获取 user_id；需要订单详情时，再调用 get_recent_orders。"
    "用户明确要求修改订单状态时，才可以调用 update_order_status。"
    "不得猜测客户、user_id、订单号或订单状态；演示数据必须和真实生产数据明确区分。"
    + _OUTPUT_PROTOCOL
)

_GENERAL_PROMPT = (
    "你是 AgentFlow 平台的通用业务助理。用户问题不属于 HR 制度政策，也不属于销售订单工具范围时，由你直接回答。"
    "如果用户需要制度查询或订单处理，应建议其补充必要信息并说明系统会路由到对应专业助手。"
    + _OUTPUT_PROTOCOL
)

_ROUTER_PROMPT = (
    "你是 AgentFlow 的意图路由器。请只根据用户最新消息判断应该交给哪个专业助手处理。"
    "\n分类规则："
    "\n- hr：公司制度、报销政策、差旅标准、休假、考勤、员工手册、福利、合规等内部政策问题。"
    "\n- sales：客户、手机号、user_id、订单、订单状态、发货、取消订单、修改订单等业务操作问题。"
    "\n- general：闲聊、能力介绍、无法明确归类的问题。"
    "\n你必须只返回一个小写英文单词：hr、sales 或 general。不要解释。"
)

# 非系统消息超过该数量时，保留尾部 N 条，系统提示（SystemMessage）一律保留
_MAX_NON_SYSTEM_MESSAGES = 20
# 一次正常 ReAct 流程通常包含「模型判断 -> 工具调用 -> 工具结果 -> 模型总结」；
# 多工具任务还会继续往返。5 步容易在拿到工具结果前误触发熔断，MVP 演示采用 12 步。
_AGENT_RECURSION_LIMIT = 12
_AGENT_TIMEOUT_SECONDS = 60

# Agent 与 Redis checkpointer 在事件循环内懒加载（Redis 异步客户端需运行中的 loop）
_init_lock = asyncio.Lock()
_graph = None
_hr_agent = None
_sales_agent = None
_redis_checkpointer = None


def _trim_messages_pre_model_hook(state: dict) -> dict:
    """在调用 LLM 前裁剪状态中的 messages：保留全部 SystemMessage + 最近 N 条其它消息。

    使用 RemoveMessage 全量替换，避免与 LangGraph 消息归并语义冲突；不触碰系统提示。
    """
    messages = list(state.get("messages") or [])
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    others = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(others) <= _MAX_NON_SYSTEM_MESSAGES:
        return {}
    trimmed = system_msgs + others[-_MAX_NON_SYSTEM_MESSAGES :]
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *trimmed]}


def _latest_human_text(messages: list[BaseMessage]) -> str:
    """提取最近一条用户消息，供 router 判断意图。"""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _message_content_to_str(msg.content)
    return ""


def _normalize_route(raw_text: str) -> RouteName:
    """将 LLM 路由输出规范为 hr/sales/general。"""
    route = raw_text.strip().lower()
    if route in {"hr", "sales", "general"}:
        return route  # type: ignore[return-value]
    return "general"


async def router_node(state: AgentState) -> dict:
    """意图路由节点：使用 LLM 将最新用户消息分类到专业子 Agent。"""
    user_text = _latest_human_text(state["messages"])
    response = await get_llm().ainvoke(
        [
            SystemMessage(content=_ROUTER_PROMPT),
            HumanMessage(content=user_text),
        ]
    )
    route = _normalize_route(_message_content_to_str(response.content))
    logger.info("agent_route_selected route=%s", route)
    return {"route": route, "thoughts": [_route_thought(route)]}


def _select_route(state: AgentState) -> RouteName:
    """条件边选择器：根据 router_node 写入的 route 决定下一跳。"""
    return state.get("route", "general")


def _last_ai_message(messages: list) -> AIMessage:
    """从子图结果中提取最后一条 AIMessage，作为父图新增消息。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return AIMessage(content="")


def _extract_tool_thoughts(messages: list) -> list[dict]:
    """从子 Agent 消息中提取工具调用轨迹，不把中间 tool_calls 写入父图记忆。"""
    thoughts: list[dict] = []
    seen_tools: set[str] = set()
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tool_call in getattr(msg, "tool_calls", []) or []:
            tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
            if not tool_name or tool_name in seen_tools:
                continue
            seen_tools.add(tool_name)
            thoughts.append(
                {"type": "tool", "tool": tool_name, "label": f"正在调用工具: {tool_name}", "status": "completed"}
            )
    return thoughts


async def hr_agent_node(state: AgentState) -> dict:
    """HR 子智能体节点：只处理制度知识库相关问题。"""
    logger.info("agent_node_started node=hr_agent")
    result = await _hr_agent.ainvoke({"messages": state["messages"]})
    messages = result.get("messages", [])
    return {
        "messages": [_last_ai_message(messages)],
        "thoughts": _extract_tool_thoughts(messages),
    }


async def sales_agent_node(state: AgentState) -> dict:
    """销售/订单子智能体节点：只处理客户与订单相关问题。"""
    logger.info("agent_node_started node=sales_agent")
    result = await _sales_agent.ainvoke({"messages": state["messages"]})
    messages = result.get("messages", [])
    return {
        "messages": [_last_ai_message(messages)],
        "thoughts": _extract_tool_thoughts(messages),
    }


async def general_node(state: AgentState) -> dict[str, list[AIMessage]]:
    """通用节点：无需工具的普通问答直接由 LLM 回复。"""
    response = await get_llm().ainvoke(
        [
            SystemMessage(content=_GENERAL_PROMPT),
            *state["messages"],
        ]
    )
    return {"messages": [AIMessage(content=_message_content_to_str(response.content))]}


async def _get_graph():
    """在事件循环内单例编译 Multi-Agent StateGraph（父图持有 Redis checkpoint）。"""
    global _graph, _hr_agent, _sales_agent, _redis_checkpointer, TOOLS
    async with _init_lock:
        if _graph is None:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
            _redis_checkpointer = await create_redis_checkpointer(redis_url)

            # 从数据库读取工具开关（若数据库不可用则退化为全量工具）
            try:
                hr_specs = await asyncio.to_thread(get_tools_manager().enabled_tool_specs, "hr")
                sales_specs = await asyncio.to_thread(get_tools_manager().enabled_tool_specs, "sales")
            except Exception as exc:  # noqa: BLE001
                logger.warning("tools_config_unavailable fallback_to_registry error=%s", exc)
                hr_specs = list(HR_TOOL_SPECS)
                sales_specs = list(SALES_TOOL_SPECS)

            hr_tools = [spec.tool for spec in hr_specs]
            sales_tools = [spec.tool for spec in sales_specs]
            TOOLS = [*hr_tools, *sales_tools]

            _hr_agent = create_react_agent(
                get_llm(),
                tools=hr_tools,
                prompt=_HR_AGENT_PROMPT,
                pre_model_hook=_trim_messages_pre_model_hook,
                version="v2",
            )
            _sales_agent = create_react_agent(
                get_llm(),
                tools=sales_tools,
                prompt=_SALES_AGENT_PROMPT,
                pre_model_hook=_trim_messages_pre_model_hook,
                version="v2",
            )

            workflow = StateGraph(AgentState)
            workflow.add_node("router", router_node)
            workflow.add_node("hr_agent", hr_agent_node)
            workflow.add_node("sales_agent", sales_agent_node)
            workflow.add_node("general", general_node)
            workflow.set_entry_point("router")
            workflow.add_conditional_edges(
                "router",
                _select_route,
                {
                    "hr": "hr_agent",
                    "sales": "sales_agent",
                    "general": "general",
                },
            )
            workflow.add_edge("hr_agent", END)
            workflow.add_edge("sales_agent", END)
            workflow.add_edge("general", END)
            _graph = workflow.compile(checkpointer=_redis_checkpointer)
        return _graph


def _message_content_to_str(content: object) -> str:
    """将单条消息的 content 规范为纯文本（兼容部分模型的多段/多模态结构）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and "text" in block or "text" in block:
                    chunks.append(str(block["text"]))
            else:
                chunks.append(str(block))
        return "".join(chunks)
    return str(content)


def _final_assistant_text(messages: list) -> str:
    """从完整消息列表中取最后一条 AI 回复的正文；若无则回退为最后一条任意消息的文本。"""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = _message_content_to_str(msg.content)
            if text.strip():
                return text
    if messages:
        last = messages[-1]
        if isinstance(last, BaseMessage):
            return _message_content_to_str(last.content)
    return ""


def _route_thought(route: object) -> dict:
    """将 router 分类结果转为前端可展示的中文轨迹。"""
    route_name = str(route)
    route_labels = {
        "hr": "HR 制度知识库",
        "sales": "销售订单业务",
        "general": "通用问答",
    }
    return {
        "type": "route",
        "route": route_name,
        "label": f"路由主管识别意图为: {route_labels.get(route_name, route_name)}",
        "status": "completed",
    }


def _extract_thoughts(result: dict) -> list[dict]:
    """从 LangGraph 最终 state 中提取路由与工具调用轨迹。"""
    thoughts = list(result.get("thoughts") or [])
    if not thoughts:
        thoughts.append(_route_thought(result.get("route", "general")))
    seen_tools: set[str] = set()

    for msg in result.get("messages", []):
        if not isinstance(msg, AIMessage):
            continue
        for tool_call in getattr(msg, "tool_calls", []) or []:
            tool_name = tool_call.get("name") if isinstance(tool_call, dict) else None
            if not tool_name or tool_name in seen_tools:
                continue
            seen_tools.add(tool_name)
            tool_thought = {
                "type": "tool",
                "tool": tool_name,
                "label": f"正在调用工具: {tool_name}",
                "status": "completed",
            }
            if tool_thought not in thoughts:
                thoughts.append(tool_thought)

    return thoughts


async def process_chat(session_id: str, user_message: str) -> ChatResult:
    """按会话 ID 调用 Multi-Agent 图，并返回最终答复与真实执行轨迹。

    Args:
        session_id: 会话线程标识，写入 LangGraph configurable.thread_id，
            与 Redis checkpointer 配合实现跨请求短期记忆（24h TTL）。
        user_message: 当前轮用户输入的纯文本。

    Returns:
        reply 为最终 AI 答复，thoughts 为 router 与工具调用轨迹。
    """
    graph = await _get_graph()
    config = {
        "configurable": {"thread_id": session_id},
        # MVP 阶段限制 Multi-Agent 图推进步数，防止工具失败/意图不清导致死循环。
        "recursion_limit": _AGENT_RECURSION_LIMIT,
    }
    state = {"messages": [HumanMessage(content=user_message)], "thoughts": []}
    try:
        result = await asyncio.wait_for(
            graph.ainvoke(state, config),
            timeout=_AGENT_TIMEOUT_SECONDS,
        )
    except GraphRecursionError:
        return {
            "reply": "任务执行步骤过多，已触发最大轮次熔断保护。请把问题拆小一点，或稍后重试。",
            "thoughts": [
                {"type": "circuit_breaker", "label": "执行步骤过多，触发最大轮次熔断保护。", "status": "failed"}
            ],
        }
    except TimeoutError:
        return {
            "reply": "思考时间过长，已触发超时熔断机制，请缩小查询范围。",
            "thoughts": [
                {"type": "timeout", "label": "执行耗时过长，触发超时熔断机制。", "status": "failed"}
            ],
        }
    messages = result.get("messages", [])
    return {
        "reply": _final_assistant_text(messages),
        "thoughts": _extract_thoughts(result),
    }
