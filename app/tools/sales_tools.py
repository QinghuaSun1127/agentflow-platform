"""Sales/order domain tools."""

from __future__ import annotations

import asyncio
import json

from langchain_core.tools import tool

from app.database.tools_manager import get_tools_manager
from app.tools.registry import ToolSpec, register_tool
from app.tools.schemas import GetCustomerIdInput, GetRecentOrdersInput, UpdateOrderStatusInput
from app.utils.redis_lock import acquire_lock


@tool(args_schema=GetCustomerIdInput)
def get_customer_id(phone: str) -> str:
    """Get internal user id by phone number."""
    if not get_tools_manager().is_enabled("get_customer_id"):
        return json.dumps(
            {"error": True, "code": "TOOL_DISABLED", "message": "工具已被管理员禁用：get_customer_id"},
            ensure_ascii=False,
        )
    normalized = phone.strip()
    if normalized == "13800138000":
        return json.dumps(
            {"user_id": "U_888", "display_name": "演示用户_张三", "note": "Mock 数据"},
            ensure_ascii=False,
        )
    return "查无此人"


@tool(args_schema=GetRecentOrdersInput)
def get_recent_orders(user_id: str) -> str:
    """Get recent orders by internal user id."""
    if not get_tools_manager().is_enabled("get_recent_orders"):
        return json.dumps(
            {"error": True, "code": "TOOL_DISABLED", "message": "工具已被管理员禁用：get_recent_orders"},
            ensure_ascii=False,
        )
    uid = user_id.strip()
    if uid == "U_888":
        return json.dumps(
            [
                {
                    "order_id": "ORD-20250501-001",
                    "product_name": "MacBook Pro 14",
                    "amount_cny": 18999.0,
                    "status": "已发货",
                },
                {
                    "order_id": "ORD-20250502-010",
                    "product_name": "iPhone 15 Pro",
                    "amount_cny": 8999.0,
                    "status": "待发货",
                },
            ],
            ensure_ascii=False,
        )
    return json.dumps([], ensure_ascii=False)


@tool("update_order_status", args_schema=UpdateOrderStatusInput)
async def update_order_status(order_id: str, status: str) -> str:
    """Update order status with distributed lock."""
    if not get_tools_manager().is_enabled("update_order_status"):
        return json.dumps(
            {"error": True, "code": "TOOL_DISABLED", "message": "工具已被管理员禁用：update_order_status"},
            ensure_ascii=False,
        )
    oid = order_id.strip()
    new_status = status.strip()
    if not oid:
        raise ValueError("order_id 不能为空")
    if not new_status:
        raise ValueError("status 不能为空")
    async with acquire_lock(f"lock:order:{oid}", timeout=5):
        await asyncio.sleep(3)
        return f"修改成功：订单 {oid} 状态已更新为 {new_status}"


@tool
def always_fail_tool() -> str:
    """Always fail to test circuit breakers."""
    if not get_tools_manager().is_enabled("always_fail_tool"):
        return json.dumps(
            {"error": True, "code": "TOOL_DISABLED", "message": "工具已被管理员禁用：always_fail_tool"},
            ensure_ascii=False,
        )
    raise Exception("数据库连接超时")


SALES_TOOL_SPECS: list[ToolSpec] = [
    register_tool(
        name="get_customer_id",
        owner="sales",
        tool=get_customer_id,
        args_schema=GetCustomerIdInput,
        description="根据手机号查询内部用户 ID",
        business_domain="sales",
        required_role="user",
        timeout_seconds=10,
    ),
    register_tool(
        name="get_recent_orders",
        owner="sales",
        tool=get_recent_orders,
        args_schema=GetRecentOrdersInput,
        description="根据 user_id 查询最近订单",
        business_domain="sales",
        required_role="user",
        timeout_seconds=10,
    ),
    register_tool(
        name="update_order_status",
        owner="sales",
        tool=update_order_status,
        args_schema=UpdateOrderStatusInput,
        description="修改订单状态（写操作）",
        business_domain="sales",
        required_role="operator",
        timeout_seconds=10,
        max_retries=0,
        is_write=True,
    ),
    register_tool(
        name="always_fail_tool",
        owner="debug",
        tool=always_fail_tool,
        args_schema=None,
        description="故障注入测试工具",
        business_domain="debug",
        required_role="admin",
        timeout_seconds=5,
        max_retries=0,
    ),
]

