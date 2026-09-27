"""运行配置：数据库路径、配额窗口等。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class QuotaRule:
    """单个接口的配额规则。"""

    limit: int
    window_seconds: int


@dataclass(frozen=True)
class Settings:
    """服务配置。"""

    db_path: Path
    default_quota: QuotaRule = field(default_factory=lambda: QuotaRule(limit=100, window_seconds=60))
    route_quotas: dict[str, QuotaRule] = field(default_factory=dict)

    def quota_for(self, route: str) -> QuotaRule:
        """返回指定接口的配额规则，未配置时使用默认规则。"""
        return self.route_quotas.get(route, self.default_quota)
