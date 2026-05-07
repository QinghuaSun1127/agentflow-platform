"""DeepSeek 聊天模型封装：通过 OpenAI 兼容接口使用 LangChain ChatOpenAI。"""

from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

from app.core.config import ROOT_DIR  # noqa: F401  # 导入配置模块时会集中加载项目 .env

# 模块级缓存：避免重复构造客户端
_llm: ChatOpenAI | None = None


def get_llm() -> ChatOpenAI:
    """返回配置好的 DeepSeek ChatOpenAI 实例（deepseek-chat，支持 Function Calling）。

    使用较低 temperature（0.1）以降低随机性，便于 Agent 推理稳定。
    """
    global _llm
    if _llm is not None:
        return _llm

    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL")
    if not api_key:
        raise ValueError("未设置环境变量 DEEPSEEK_API_KEY，请在 .env 中配置。")
    if not base_url:
        raise ValueError("未设置环境变量 DEEPSEEK_BASE_URL，请在 .env 中配置。")

    _llm = ChatOpenAI(
        model="deepseek-chat",
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
    )
    return _llm
