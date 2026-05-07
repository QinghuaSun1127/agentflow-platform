"""Compatibility exports for existing imports."""

from __future__ import annotations

from app.database.tools_manager import get_tools_manager
from app.rag.knowledge_base import search_document_chunks
from app.tools import hr_tools, sales_tools

HR_TOOL_SPECS = hr_tools.HR_TOOL_SPECS
SALES_TOOL_SPECS = sales_tools.SALES_TOOL_SPECS
search_company_policy = hr_tools.search_company_policy
get_customer_id = sales_tools.get_customer_id
get_recent_orders = sales_tools.get_recent_orders
update_order_status = sales_tools.update_order_status
always_fail_tool = sales_tools.always_fail_tool

TOOL_REGISTRY = {spec.name: spec for spec in [*HR_TOOL_SPECS, *SALES_TOOL_SPECS]}

__all__ = [
    "HR_TOOL_SPECS",
    "SALES_TOOL_SPECS",
    "TOOL_REGISTRY",
    "search_company_policy",
    "get_customer_id",
    "get_recent_orders",
    "update_order_status",
    "always_fail_tool",
    "get_tools_manager",
    "search_document_chunks",
]

