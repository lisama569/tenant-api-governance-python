"""应用配置：本地零外部依赖，所有路径/密钥均可通过环境变量覆盖。"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TENANT_GOV_", env_file=".env", extra="ignore")

    database_path: Path = PROJECT_ROOT / "data" / "governance.db"
    log_path: Path = PROJECT_ROOT / "logs" / "governance.log"
    token_secret: str = "local-dev-shared-secret"
    token_ttl_seconds: int = 3600

    # 每租户每接口每窗口配额：endpoint -> limit
    order_create_limit: int = 3
    order_list_limit: int = 10
    window_seconds: int = 60


settings = Settings()
