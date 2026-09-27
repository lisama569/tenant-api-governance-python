"""统一时钟：生产用 time.time；测试可替换为可控时钟。"""
from __future__ import annotations

import time


def now_ts() -> int:
    return int(time.time())
