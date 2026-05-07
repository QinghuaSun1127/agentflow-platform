"""Hidden Streamlit admin dashboard for AgentFlow usage analytics."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import requests
import streamlit as st

API_BASE_URL = os.getenv("AGENTFLOW_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_STATS_URL = f"{API_BASE_URL}/api/v1/admin/stats"
ADMIN_DOCUMENTS_URL = f"{API_BASE_URL}/api/v1/admin/documents"
ADMIN_TOOLS_URL = f"{API_BASE_URL}/api/v1/admin/tools"
ADMIN_TOOLS_CONFIG_URL = f"{API_BASE_URL}/api/v1/admin/tools/config"
ADMIN_TOOLS_STATS_URL = f"{API_BASE_URL}/api/v1/admin/tools/stats"
ADMIN_USERS_URL = f"{API_BASE_URL}/api/v1/admin/users"
AUTH_ME_URL = f"{API_BASE_URL}/api/v1/auth/me"


def main() -> None:
    st.set_page_config(page_title="AgentFlow Admin", page_icon="AF", layout="wide")
    st.title("AgentFlow Admin Dashboard")

    token = st.session_state.get("auth_token", "")
    if not token:
        st.warning("请先在主页面登录管理员账号，再打开 Admin 页面。")
        st.stop()

    profile = fetch_current_profile(token)
    if profile is None:
        st.error("无法校验账号权限，请重新登录。")
        st.stop()
    if not profile.get("is_admin"):
        st.error("仅管理员可访问本页面。运营人员请通过 API 或后续专用入口维护知识库。")
        st.stop()

    overview_tab, docs_tab, tools_tab, users_tab = st.tabs(["运营大屏", "知识库管理", "工具中心", "用户权限"])
    with overview_tab:
        render_overview(token)
    with docs_tab:
        render_documents(token)
    with tools_tab:
        render_tools(token)
    with users_tab:
        render_users(token)


def render_overview(token: str) -> None:
    days = st.sidebar.slider("统计窗口（天）", min_value=1, max_value=30, value=7)
    stats = fetch_admin_stats(token, days)
    if not stats:
        return
    totals = stats.get("totals", {})
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("调用次数", int(totals.get("total_calls") or 0))
    col2.metric("Token 估算", int(totals.get("total_tokens") or 0))
    col3.metric("费用估算", f"${float(totals.get('total_cost_usd') or 0):.4f}")
    col4.metric("平均耗时", f"{float(totals.get('avg_latency_ms') or 0):.0f} ms")
    col5.metric("错误数", int(totals.get("error_count") or 0))

    left, right = st.columns([1.3, 1])
    with left:
        st.subheader("每日调用趋势")
        daily_df = pd.DataFrame(stats.get("daily", []))
        if daily_df.empty:
            st.info("暂无调用数据。")
        else:
            daily_df["day"] = pd.to_datetime(daily_df["day"]).dt.date
            st.line_chart(daily_df.set_index("day")["calls"])

    with right:
        st.subheader("意图路由分布")
        route_df = pd.DataFrame(stats.get("routes", []))
        if route_df.empty:
            st.info("暂无路由数据。")
        else:
            st.bar_chart(route_df.set_index("route")["calls"])

    st.subheader("最近问题")
    recent_df = pd.DataFrame(stats.get("recent", []))
    if recent_df.empty:
        st.info("暂无最近调用。")
    else:
        st.dataframe(
            recent_df[["created_at", "route", "prompt", "estimated_tokens", "estimated_cost_usd", "latency_ms"]],
            use_container_width=True,
            hide_index=True,
        )


def render_documents(token: str) -> None:
    st.subheader("知识库文档")
    uploaded_file = st.file_uploader("上传制度文档（txt/md/pdf）", type=["txt", "md", "pdf"])
    title = st.text_input("文档标题", placeholder="例如：差旅报销制度 2026")
    if st.button("上传并入库", use_container_width=True, disabled=uploaded_file is None):
        assert uploaded_file is not None
        error = upload_document(token, uploaded_file, title or uploaded_file.name)
        if error:
            st.error(error)
        else:
            st.success("文档已入库。")
            st.rerun()

    docs = fetch_documents(token)
    if not docs:
        st.info("暂无知识库文档。")
        return
    docs_df = pd.DataFrame(docs)
    st.dataframe(docs_df, use_container_width=True, hide_index=True)
    delete_id = st.number_input("删除文档 ID", min_value=0, step=1)
    reindex_id = st.number_input("重建向量文档 ID", min_value=0, step=1)
    if st.button("重建文档向量", use_container_width=True, disabled=reindex_id <= 0):
        error = reindex_document(token, int(reindex_id))
        if error:
            st.error(error)
        else:
            st.success("文档向量已重建。")
            st.rerun()
    if st.button("删除文档", use_container_width=True, disabled=delete_id <= 0):
        error = delete_document(token, int(delete_id))
        if error:
            st.error(error)
        else:
            st.success("文档已删除。")
            st.rerun()


def render_tools(token: str) -> None:
    st.subheader("工具注册中心")
    days = st.slider("工具统计窗口（天）", min_value=1, max_value=30, value=7, key="tools_days")
    configs = fetch_tools_config(token)
    if not configs:
        st.info("暂无工具配置（请确认已完成数据库初始化）。")
        return

    left, right = st.columns([1.1, 1])
    with left:
        st.markdown("#### 工具配置（启停/参数）")
        cfg_df = pd.DataFrame(configs)
        st.dataframe(
            cfg_df[["owner", "name", "enabled", "is_write", "timeout_seconds", "max_retries"]],
            use_container_width=True,
            hide_index=True,
        )
        st.markdown("#### 编辑单个工具")
        tool_name = st.selectbox("工具", options=[row["name"] for row in configs])
        current = next((row for row in configs if row["name"] == tool_name), None) or {}
        enabled = st.checkbox("启用", value=bool(current.get("enabled", True)))
        timeout_seconds = st.number_input(
            "超时（秒）",
            min_value=1,
            max_value=600,
            value=int(current.get("timeout_seconds") or 30),
            step=1,
        )
        max_retries = st.number_input(
            "重试次数",
            min_value=0,
            max_value=10,
            value=int(current.get("max_retries") or 1),
            step=1,
        )
        if st.button("保存工具配置", use_container_width=True):
            error = update_tool_config(token, tool_name, enabled, int(timeout_seconds), int(max_retries))
            if error:
                st.error(error)
            else:
                st.success("已更新。")
                st.rerun()

    with right:
        st.markdown("#### 工具调用统计")
        stats = fetch_tools_stats(token, days)
        stats_df = pd.DataFrame(stats)
        if stats_df.empty:
            st.info("统计窗口内暂无工具调用。")
        else:
            st.dataframe(stats_df, use_container_width=True, hide_index=True)


def render_users(token: str) -> None:
    st.subheader("用户权限")
    users = fetch_users(token)
    if not users:
        st.info("暂无用户。")
        return
    st.dataframe(pd.DataFrame(users), use_container_width=True, hide_index=True)
    user_id = st.number_input("用户 ID", min_value=0, step=1)
    role = st.selectbox("角色", ["user", "operator", "admin"])
    is_active = st.checkbox("启用账号", value=True)
    if st.button("更新用户", use_container_width=True, disabled=user_id <= 0):
        error = update_user(token, int(user_id), role, is_active)
        if error:
            st.error(error)
        else:
            st.success("用户已更新。")
            st.rerun()


def fetch_current_profile(token: str) -> dict[str, Any] | None:
    try:
        response = requests.get(AUTH_ME_URL, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except (requests.RequestException, ValueError):
        return None


def fetch_admin_stats(token: str, days: int) -> dict[str, Any] | None:
    try:
        response = requests.get(
            ADMIN_STATS_URL,
            params={"days": days},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 403:
            st.error("当前账号不是管理员。第一个注册的账号会自动成为管理员。")
        else:
            st.error(f"Admin API 调用失败：{exc.response.text}")
    except (requests.RequestException, ValueError) as exc:
        st.error(f"Admin API 暂时不可用：{exc}")
    return None


def fetch_documents(token: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(ADMIN_DOCUMENTS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError) as exc:
        st.error(f"读取文档列表失败：{exc}")
        return []


def upload_document(token: str, uploaded_file: Any, title: str) -> str | None:
    try:
        response = requests.post(
            ADMIN_DOCUMENTS_URL + "/upload",
            params={"title": title},
            files={"file": (uploaded_file.name, uploaded_file.getvalue(), uploaded_file.type)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def delete_document(token: str, document_id: int) -> str | None:
    try:
        response = requests.delete(
            f"{ADMIN_DOCUMENTS_URL}/{document_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def reindex_document(token: str, document_id: int) -> str | None:
    try:
        response = requests.post(
            f"{ADMIN_DOCUMENTS_URL}/{document_id}/reindex",
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def fetch_tools(token: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(ADMIN_TOOLS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError) as exc:
        st.error(f"读取工具列表失败：{exc}")
        return []


def fetch_tools_config(token: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(ADMIN_TOOLS_CONFIG_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError) as exc:
        st.error(f"读取工具配置失败：{exc}")
        return []


def fetch_tools_stats(token: str, days: int) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            ADMIN_TOOLS_STATS_URL,
            params={"days": days},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError) as exc:
        st.error(f"读取工具统计失败：{exc}")
        return []


def update_tool_config(token: str, tool_name: str, enabled: bool, timeout_seconds: int, max_retries: int) -> str | None:
    try:
        response = requests.patch(
            f"{API_BASE_URL}/api/v1/admin/tools/{tool_name}",
            json={"enabled": enabled, "timeout_seconds": timeout_seconds, "max_retries": max_retries},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


def fetch_users(token: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(ADMIN_USERS_URL, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (requests.RequestException, ValueError) as exc:
        st.error(f"读取用户列表失败：{exc}")
        return []


def update_user(token: str, user_id: int, role: str, is_active: bool) -> str | None:
    try:
        response = requests.patch(
            f"{ADMIN_USERS_URL}/{user_id}",
            json={"role": role, "is_active": is_active},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc.response.text
    except requests.RequestException as exc:
        return str(exc)
    return None


if __name__ == "__main__":
    main()
