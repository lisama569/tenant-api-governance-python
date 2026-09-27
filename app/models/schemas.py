"""Pydantic 数据模型。"""
from __future__ import annotations

from pydantic import BaseModel


class Tenant(BaseModel):
    id: str
    name: str
    scopes: list[str]


class Principal(BaseModel):
    tenant_id: str
    scopes: list[str]
    expires_at: int


class OrderCreateRequest(BaseModel):
    item: str
    quantity: int


class OrderCreateResponse(BaseModel):
    order_id: str
    tenant_id: str
    item: str
    quantity: int


class ErrorBody(BaseModel):
    error_code: str
    message: str
    retry_after: int | None = None
