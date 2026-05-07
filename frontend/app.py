"""AgentFlow Streamlit 前端：提供极简 Apple 风格的 Agent 对话界面。"""

from __future__ import annotations

import html
import os
import time
import uuid
from typing import Any

import requests
import streamlit as st

API_BASE_URL = os.getenv("AGENTFLOW_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_URL = f"{API_BASE_URL}/api/v1/agent/chat"
SESSIONS_URL = f"{API_BASE_URL}/api/v1/sessions"
AUTH_LOGIN_URL = f"{API_BASE_URL}/api/v1/auth/login"
AUTH_REGISTER_URL = f"{API_BASE_URL}/api/v1/auth/register"
AUTH_ME_URL = f"{API_BASE_URL}/api/v1/auth/me"
API_KEY = os.getenv("AGENTFLOW_API_KEY", "")
REQUEST_TIMEOUT_SECONDS = 60
MAX_REQUEST_ATTEMPTS = 3
REQUEST_RETRY_BACKOFF_SECONDS = 0.6


def init_session_state() -> None:
    """初始化 Streamlit 会话状态，保存前端消息历史和后端线程 session_id。"""
    if "session_id" not in st.session_state:
        st.session_state.session_id = str(uuid.uuid4())
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "auth_token" not in st.session_state:
        st.session_state.auth_token = ""
    if "current_user" not in st.session_state:
        st.session_state.current_user = None


def start_new_chat() -> None:
    """新建对话时清空当前屏幕历史，并生成新的后端会话标识。"""
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())
    st.rerun()


def logout() -> None:
    """Clear auth and local chat state."""
    st.session_state.auth_token = ""
    st.session_state.current_user = None
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())
    st.rerun()


def inject_apple_style() -> None:
    """注入 Apple 风格 CSS，并隐藏 Streamlit 默认菜单与水印。"""
    st.markdown(
        """
        <style>
            #MainMenu, footer {
                visibility: hidden;
                height: 0;
            }

            :root {
                --apple-blue-start: #0A84FF;
                --apple-blue-end: #0066D6;
                --apple-gray: #F2F2F7;
                --apple-text: #1D1D1F;
                --apple-secondary: #6E6E73;
                --apple-border: rgba(60, 60, 67, 0.16);
            }

            html, body, [class*="css"] {
                font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display",
                    "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
                color: var(--apple-text);
            }

            .stApp {
                background:
                    radial-gradient(circle at top left, rgba(10, 132, 255, 0.08), transparent 28rem),
                    linear-gradient(180deg, #FFFFFF 0%, #FAFAFC 100%);
            }

            .block-container {
                max-width: 860px;
                padding-top: 2.5rem;
                padding-bottom: 7rem;
            }

            [data-testid="stSidebar"] {
                background: rgba(255, 255, 255, 0.76);
                border-right: 1px solid var(--apple-border);
                backdrop-filter: blur(28px);
                -webkit-backdrop-filter: blur(28px);
            }

            [data-testid="stSidebar"] > div {
                width: 18rem;
                min-width: 18rem;
            }

            [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
                scrollbar-width: none;
            }

            [data-testid="stSidebar"] [data-testid="stSidebarContent"]::-webkit-scrollbar {
                display: none;
            }

            [data-testid="stSidebar"] .stButton > button {
                width: 100%;
                min-height: 2.25rem;
                border: 0;
                border-radius: 0.85rem;
                color: var(--apple-text);
                font-weight: 450;
                text-align: left;
                background: transparent;
                box-shadow: none;
                transition: background 160ms ease, color 160ms ease, transform 160ms ease;
            }

            [data-testid="stSidebar"] .stButton > button:hover {
                color: var(--apple-text);
                background: rgba(60, 60, 67, 0.08);
                transform: translateY(-1px);
            }

            [data-testid="stSidebar"] .stButton > button[kind="primary"] {
                border-radius: 999px;
                color: white;
                font-weight: 600;
                text-align: center;
                background: linear-gradient(135deg, var(--apple-blue-start), var(--apple-blue-end));
                box-shadow: 0 10px 28px rgba(0, 102, 214, 0.22);
            }

            [data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
                color: white;
                background: linear-gradient(135deg, #2F9BFF, var(--apple-blue-end));
                transform: translateY(-1px);
            }

            .sidebar-section-title {
                margin: 1.25rem 0 0.35rem;
                color: var(--apple-secondary);
                font-size: 0.78rem;
                font-weight: 700;
                letter-spacing: 0.02em;
            }

            .sidebar-session {
                margin: 0.5rem 0 0.75rem;
                color: var(--apple-secondary);
                font-size: 0.72rem;
                word-break: break-all;
            }

            .sidebar-user-card {
                position: fixed;
                bottom: 1.25rem;
                left: 1rem;
                z-index: 999;
                display: flex;
                align-items: center;
                gap: 0.7rem;
                width: calc(18rem - 2rem);
                padding: 0.65rem 0.7rem;
                border-radius: 1rem;
                background: rgba(255, 255, 255, 0.68);
                border: 1px solid rgba(60, 60, 67, 0.08);
                box-sizing: border-box;
            }

            .sidebar-avatar {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 2.35rem;
                height: 2.35rem;
                border-radius: 50%;
                color: white;
                font-size: 0.95rem;
                font-weight: 700;
                background: linear-gradient(135deg, #1D1D1F, #6E6E73);
                box-shadow: 0 10px 24px rgba(0, 0, 0, 0.12);
            }

            .sidebar-user-name {
                margin-top: 0.1rem;
                font-size: 0.9rem;
                font-weight: 650;
                letter-spacing: -0.015em;
            }

            .sidebar-user-plan {
                margin-top: -0.25rem;
                color: var(--apple-secondary);
                font-size: 0.72rem;
            }

            .hero-title {
                margin: 0 0 0.25rem;
                font-size: clamp(2.1rem, 5vw, 3.5rem);
                line-height: 1.05;
                letter-spacing: -0.055em;
                font-weight: 750;
                text-align: center;
            }

            .hero-subtitle {
                margin-bottom: 2rem;
                color: var(--apple-secondary);
                font-size: 1.02rem;
                text-align: center;
            }

            .welcome-panel {
                margin: 2.5rem auto 1.25rem;
                text-align: center;
            }

            .welcome-title {
                margin-bottom: 1rem;
                font-size: 1.75rem;
                font-weight: 700;
                letter-spacing: -0.035em;
            }

            .chat-row {
                display: flex;
                width: 100%;
                margin: 0.25rem 0 0.8rem;
            }

            .chat-row.user {
                justify-content: flex-end;
            }

            .chat-row.assistant {
                justify-content: flex-start;
            }

            .chat-bubble {
                max-width: min(78%, 640px);
                padding: 0.78rem 1rem;
                border-radius: 1.35rem;
                font-size: 1rem;
                line-height: 1.48;
                letter-spacing: -0.01em;
                white-space: pre-wrap;
                word-break: break-word;
            }

            .chat-bubble.user {
                color: #FFFFFF;
                border-bottom-right-radius: 0.42rem;
                background: linear-gradient(180deg, var(--apple-blue-start), var(--apple-blue-end));
                box-shadow: 0 12px 30px rgba(10, 132, 255, 0.22);
            }

            .chat-bubble.assistant {
                color: var(--apple-text);
                border: 1px solid rgba(60, 60, 67, 0.08);
                border-bottom-left-radius: 0.42rem;
                background: var(--apple-gray);
            }

            [data-testid="stChatInput"] {
                border-radius: 999px;
                border: 1px solid var(--apple-border);
                box-shadow: 0 18px 48px rgba(0, 0, 0, 0.08);
            }

            .preset-chip button {
                border-radius: 999px;
                border: 1px solid var(--apple-border);
                background: rgba(255, 255, 255, 0.82);
                box-shadow: 0 10px 28px rgba(0, 0, 0, 0.06);
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_message(role: str, content: str) -> None:
    """使用 st.chat_message 渲染历史消息，Assistant 保留 Markdown 表格能力。"""
    if role == "assistant":
        with st.chat_message(role):
            st.markdown(content)
        return

    safe_content = html.escape(content)

    with st.chat_message(role):
        st.markdown(
            f"""
            <div class="chat-row user">
                <div class="chat-bubble user">{safe_content}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def parse_agent_response(response_payload: dict[str, Any]) -> dict[str, Any]:
    """从后端统一响应体中提取 Agent 回复与真实执行轨迹。"""
    reply = response_payload.get("reply")
    thoughts = response_payload.get("thoughts", [])
    return {
        "reply": reply if isinstance(reply, str) and reply.strip() else "后端返回了空回复，请稍后再试。",
        "thoughts": thoughts if isinstance(thoughts, list) else [],
    }


def _friendly_error_message(status_code: int, detail: Any) -> str:
    """Convert backend error details to user-facing guidance."""
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
        message = str(detail.get("message") or "")
        if code == "LLM_CONFIG_ERROR":
            return "模型密钥或地址配置有误，请检查 `.env` 中 DEEPSEEK_API_KEY 与 DEEPSEEK_BASE_URL。"
        if code == "DB_UNAVAILABLE":
            return "数据库暂时不可用，请确认 Postgres 容器已启动。"
        if code == "REDIS_UNAVAILABLE":
            return "Redis 暂时不可用，请确认 Redis 容器已启动。"
        if code == "TOOL_CALL_FAILED":
            return "工具调用失败，建议重试或把问题拆得更小。"
        if code and message:
            return message
    if status_code == 401:
        return "登录状态失效，请重新登录。"
    if status_code == 403:
        return "权限不足，当前账号无法执行该操作。"
    if status_code == 429:
        return "请求过于频繁，请稍后重试。"
    if status_code >= 500:
        return "服务暂时不可用，请稍后重试。"
    return f"请求失败（HTTP {status_code}）。"


def authenticate(username: str, password: str, *, register: bool, display_name: str = "") -> str | None:
    """Login or register and persist the JWT in Streamlit session state."""
    url = AUTH_REGISTER_URL if register else AUTH_LOGIN_URL
    payload = {"username": username, "password": password, "display_name": display_name or username}
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.HTTPError as exc:
        try:
            detail = exc.response.json().get("detail")
        except ValueError:
            detail = str(exc)
        return str(detail)
    except (requests.RequestException, ValueError) as exc:
        return f"认证服务暂时不可用：{exc}"

    token = data.get("access_token")
    user = data.get("user")
    if not isinstance(token, str) or not isinstance(user, dict):
        return "认证响应格式异常。"
    st.session_state.auth_token = token
    st.session_state.current_user = user
    st.session_state.messages = []
    st.session_state.session_id = str(uuid.uuid4())
    st.rerun()
    return None


def refresh_current_user() -> None:
    """Load current user profile when a token exists."""
    if not st.session_state.auth_token or st.session_state.current_user:
        return
    try:
        response = requests.get(AUTH_ME_URL, headers=_api_headers(), timeout=10)
        response.raise_for_status()
        st.session_state.current_user = response.json()
    except (requests.RequestException, ValueError):
        st.session_state.auth_token = ""
        st.session_state.current_user = None


def render_auth_page() -> None:
    """Render a minimal login/register screen."""
    st.markdown('<h1 class="hero-title">AgentFlow</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hero-subtitle">登录后，你的会话历史会和其他用户完全隔离。</div>',
        unsafe_allow_html=True,
    )
    login_tab, register_tab = st.tabs(["登录", "注册"])
    with login_tab:
        with st.form("login_form"):
            username = st.text_input("用户名", key="login_username")
            password = st.text_input("密码", type="password", key="login_password")
            submitted = st.form_submit_button("登录", use_container_width=True)
        if submitted:
            error = authenticate(username, password, register=False)
            if error:
                st.error(error)

    with register_tab:
        with st.form("register_form"):
            display_name = st.text_input("显示名称", key="register_display_name")
            username = st.text_input("用户名", key="register_username")
            password = st.text_input("密码（至少 8 位）", type="password", key="register_password")
            submitted = st.form_submit_button("创建账号", use_container_width=True)
        if submitted:
            error = authenticate(username, password, register=True, display_name=display_name)
            if error:
                st.error(error)


def request_agent_reply(session_id: str, message: str) -> dict[str, Any]:
    """调用本地 FastAPI Agent 接口，包含超时、异常处理和有限重试。"""
    last_error: Exception | None = None
    payload = {"session_id": session_id, "message": message}
    headers = _api_headers()

    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = requests.post(
                API_URL,
                json=payload,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return parse_agent_response(response.json())
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            detail: Any = ""
            try:
                payload = exc.response.json() if exc.response is not None else {}
                detail = payload.get("detail", payload)
            except ValueError:
                detail = exc.response.text if exc.response is not None else str(exc)
            return {
                "reply": _friendly_error_message(status_code, detail),
                "thoughts": [
                    {"type": "error", "label": f"请求失败: HTTP {status_code}", "status": "failed"},
                ],
            }
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_REQUEST_ATTEMPTS:
                time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * attempt)

    return {
        "reply": f"Agent 服务暂时不可用，请稍后重试。错误信息：{last_error}",
        "thoughts": [],
    }


def fetch_sessions() -> list[str | dict[str, Any]]:
    """从后端读取已持久化的会话 ID 列表。"""
    try:
        response = requests.get(SESSIONS_URL, headers=_api_headers(), timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []

    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [session for session in sessions if isinstance(session, (str, dict))]


def fetch_session_messages(session_id: str) -> list[dict[str, str]]:
    """读取指定会话的历史消息，并转换为前端渲染所需结构。"""
    try:
        response = requests.get(f"{SESSIONS_URL}/{session_id}/messages", headers=_api_headers(), timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return []

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    normalized_messages: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            normalized_messages.append({"role": role, "content": content})
    return normalized_messages


def rename_session(session_id: str, title: str) -> str | None:
    """Rename the current user's session."""
    try:
        response = requests.patch(
            f"{SESSIONS_URL}/{session_id}",
            json={"title": title},
            headers=_api_headers(),
            timeout=10,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def delete_session(session_id: str) -> str | None:
    """Delete the current user's session."""
    try:
        response = requests.delete(f"{SESSIONS_URL}/{session_id}", headers=_api_headers(), timeout=10)
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def switch_to_history_session(session_id: str) -> None:
    """切换到历史会话：加载消息、更新当前 session_id，并刷新页面。"""
    st.session_state.session_id = session_id
    st.session_state.messages = fetch_session_messages(session_id)
    st.rerun()


def build_session_title(session_id: str) -> str:
    """用会话第一条用户消息生成历史标题，避免直接展示 UUID。"""
    messages = fetch_session_messages(session_id)
    for message in messages:
        if message["role"] != "user":
            continue
        title = " ".join(message["content"].split())
        for icon in ("📊", "📦"):
            title = title.replace(icon, "")
        title = title.strip()
        if not title:
            break
        return title if len(title) <= 18 else f"{title[:18]}..."
    return f"未命名会话 {session_id[:6]}"


def session_id_from_summary(session: str | dict[str, Any]) -> str:
    """Normalize legacy string sessions and new structured session summaries."""
    if isinstance(session, dict):
        return str(session.get("session_id", ""))
    return str(session)


def session_title_from_summary(session: str | dict[str, Any]) -> str:
    """Use backend-provided title when available, otherwise fall back to message lookup."""
    if isinstance(session, dict):
        title = str(session.get("title") or "").strip()
        if title:
            return title
        session_id = session_id_from_summary(session)
        return f"未命名会话 {session_id[:6]}"
    return build_session_title(session)


def _api_headers() -> dict[str, str]:
    if st.session_state.get("auth_token"):
        return {"Authorization": f"Bearer {st.session_state.auth_token}"}
    if API_KEY:
        return {"Authorization": f"Bearer {API_KEY}"}
    return {}


def stream_text(text: str):
    """将完整回复拆成字符流，模拟 Agent 正在逐字输出。"""
    for char in text:
        yield char
        time.sleep(0.01)


def render_sidebar() -> None:
    """渲染 ChatGPT 风格侧边栏：新建对话、历史记录与底部用户信息。"""
    if st.sidebar.button(
        "新建对话",
        use_container_width=True,
        type="primary",
    ):
        start_new_chat()
    st.sidebar.markdown(
        f'<div class="sidebar-session">Session ID: {st.session_state.session_id}</div>',
        unsafe_allow_html=True,
    )
    with st.sidebar.expander("当前会话设置", expanded=False):
        new_title = st.text_input("重命名当前会话", value="", placeholder="例如：差旅政策问答")
        if st.button("保存标题", use_container_width=True):
            error = rename_session(st.session_state.session_id, new_title)
            if error:
                st.error(error)
            else:
                st.rerun()
        if st.button("删除当前会话", use_container_width=True):
            error = delete_session(st.session_state.session_id)
            if error:
                st.error(error)
            else:
                start_new_chat()

    st.sidebar.markdown(
        '<div class="sidebar-section-title"><strong>历史记录</strong></div>',
        unsafe_allow_html=True,
    )
    sessions = fetch_sessions()
    if sessions:
        for session in sessions:
            session_id = session_id_from_summary(session)
            if not session_id:
                continue
            label = session_title_from_summary(session)
            if session_id == st.session_state.session_id:
                label = f"当前 · {label}"
            if st.sidebar.button(
                label,
                key=f"session_history_{session_id}",
                use_container_width=True,
            ):
                switch_to_history_session(session_id)
    else:
        st.sidebar.caption("暂无历史记录")

    st.sidebar.markdown(
        f"""
        <div class="sidebar-user-card">
            <div class="sidebar-avatar">{_user_initials()}</div>
            <div>
                <div class="sidebar-user-name">{html.escape(_user_display_name())}</div>
                <div class="sidebar-user-plan">Private Workspace</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.sidebar.button("退出登录", use_container_width=True):
        logout()


def _user_display_name() -> str:
    user = st.session_state.get("current_user") or {}
    return str(user.get("display_name") or user.get("username") or "AgentFlow User")


def _user_initials() -> str:
    name = _user_display_name().strip()
    if not name:
        return "AF"
    return "".join(part[:1].upper() for part in name.split()[:2])[:2]


def render_empty_chat_presets() -> str | None:
    """新对话时展示欢迎语和快捷指令按钮，返回被点击的指令文本。"""
    st.markdown(
        """
        <div class="welcome-panel">
            <div class="welcome-title">请问有什么可以帮您？</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left_spacer, left, right, right_spacer = st.columns([1, 2.2, 2.2, 1])
    preset_prompt: str | None = None

    with left:
        if st.button("📊 查询差旅报销政策", use_container_width=True):
            preset_prompt = "📊 查询差旅报销政策"
    with right:
        if st.button("📦 查询订单 ORD-999 状态", use_container_width=True):
            preset_prompt = "📦 查询订单 ORD-999 状态"

    return preset_prompt


def handle_user_prompt(prompt: str) -> None:
    """统一处理用户输入：立即渲染用户消息、展示思考状态，并流式输出 Agent 回复。"""
    user_message = {"role": "user", "content": prompt}
    st.session_state.messages.append(user_message)
    render_message(user_message["role"], user_message["content"])

    with st.status("Agent 正在深度思考中...", expanded=True) as status:
        st.write("queued · 正在识别意图")
        st.write("queued · 正在查询知识库/工具")
        st.write("queued · 正在生成最终回答")
        response_data = request_agent_reply(st.session_state.session_id, prompt)
        assistant_reply = str(response_data.get("reply", ""))
        thoughts = response_data.get("thoughts", [])
        if thoughts:
            for thought in thoughts:
                if isinstance(thought, dict):
                    label = str(thought.get("label") or thought.get("tool") or thought.get("type") or thought)
                    status_text = str(thought.get("status") or "completed")
                    icon = "✅" if status_text == "completed" else "⚠️" if status_text == "failed" else "⏳"
                    st.write(f"{icon} {status_text} · {label}")
                else:
                    st.write(str(thought))
        else:
            st.write("🧠 综合分析中...")
        status.update(label="思考完成", state="complete", expanded=False)

    with st.chat_message("assistant"):
        streamed_reply = st.write_stream(stream_text(assistant_reply))

    assistant_message = {"role": "assistant", "content": streamed_reply}
    st.session_state.messages.append(assistant_message)


def main() -> None:
    """渲染 Streamlit 前端页面并处理用户输入。"""
    st.set_page_config(
        page_title="AgentFlow",
        page_icon="AF",
        layout="centered",
        initial_sidebar_state="expanded",
    )
    init_session_state()
    inject_apple_style()
    refresh_current_user()

    if not st.session_state.auth_token:
        render_auth_page()
        return

    render_sidebar()

    st.markdown('<h1 class="hero-title">AgentFlow</h1>', unsafe_allow_html=True)
    st.markdown(
        '<div class="hero-subtitle">一个干净、专注的企业智能体对话入口。</div>',
        unsafe_allow_html=True,
    )

    preset_prompt = None
    if not st.session_state.messages:
        preset_prompt = render_empty_chat_presets()

    for message in st.session_state.messages:
        render_message(message["role"], message["content"])

    chat_prompt = st.chat_input("向 AgentFlow 发送消息...")
    active_prompt = preset_prompt or chat_prompt

    if active_prompt:
        handle_user_prompt(active_prompt)


if __name__ == "__main__":
    main()
