"""把 GovernanceError 映射为统一 JSON 错误体，并归一化请求校验错误。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import GovernanceError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GovernanceError)
    async def _on_governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error_code": exc.error_code,
                "message": exc.message,
                "retry_after": exc.retry_after,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error_code": "invalid_request",
                "message": "request payload failed validation",
                "details": exc.errors(),
                "retry_after": None,
            },
        )
