"""FastAPI 应用入口。

启动时构建容器（含建表、播种、重启恢复），关闭时释放连接。
可用 TENANT_GOV_DATABASE_PATH / TENANT_GOV_LOG_PATH 等环境变量覆盖路径。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.container import build_container
from app.api.errors import register_exception_handlers
from app.api.routes import router
from app.config import settings


def create_app(config=settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = build_container(config)
        app.state.container = container
        try:
            yield
        finally:
            container.close()

    app = FastAPI(title="tenant-api-governance", version="0.1.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()
