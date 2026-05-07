"""HR domain tools."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from app.database.tools_manager import get_tools_manager
from app.rag.knowledge_base import search_document_chunks
from app.tools.registry import ToolSpec, register_tool
from app.tools.schemas import SearchCompanyPolicyInput


@tool(args_schema=SearchCompanyPolicyInput)
def search_company_policy(query: str) -> str:
    """Search HR/company policy chunks from knowledge base."""
    q = query.strip()
    if not q:
        return json.dumps({"results": [], "count": 0, "note": "query 为空，未执行检索。"}, ensure_ascii=False)
    try:
        if not get_tools_manager().is_enabled("search_company_policy"):
            return json.dumps(
                {"error": True, "code": "TOOL_DISABLED", "message": "工具已被管理员禁用：search_company_policy"},
                ensure_ascii=False,
            )
        chunks = search_document_chunks(q, top_k=3)
        return json.dumps(
            {
                "results": [chunk["content"] for chunk in chunks],
                "sources": [
                    {
                        "title": chunk["title"],
                        "source": chunk["source"],
                        "page_number": chunk.get("page_number"),
                        "similarity": chunk["similarity"],
                    }
                    for chunk in chunks
                ],
                "count": len(chunks),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps(
            {"error": True, "message": "制度知识库检索失败，请稍后重试或联系管理员。", "detail": str(exc)},
            ensure_ascii=False,
        )


HR_TOOL_SPECS: list[ToolSpec] = [
    register_tool(
        name="search_company_policy",
        owner="hr",
        tool=search_company_policy,
        args_schema=SearchCompanyPolicyInput,
        description="检索公司制度知识库并返回引用来源",
        business_domain="hr",
        required_role="user",
        timeout_seconds=20,
    )
]

