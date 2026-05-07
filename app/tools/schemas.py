"""Shared tool input schemas."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


class GetCustomerIdInput(BaseModel):
    phone: Annotated[
        str,
        Field(
            description=(
                "用户在中国大陆使用的手机号码，必须为纯数字字符串，不要包含 +86、空格、横杠或括号。"
            )
        ),
    ]


class GetRecentOrdersInput(BaseModel):
    user_id: Annotated[
        str,
        Field(description="系统内部用户唯一标识（例如 U_888），应来自 get_customer_id 的返回结果。"),
    ]


class SearchCompanyPolicyInput(BaseModel):
    query: Annotated[
        str,
        Field(description="用于知识库检索的自然语言问题或关键词，不能为空。"),
    ]


class UpdateOrderStatusInput(BaseModel):
    order_id: Annotated[
        str,
        Field(description="订单号，例如 ORD-20250501-001。"),
    ]
    status: Annotated[
        str,
        Field(description="目标订单状态，例如 待发货 / 已发货 / 已取消 / 已完成。"),
    ]

