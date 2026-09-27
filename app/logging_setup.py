"""结构化审计日志：文件 + 控制台双通道，供测试与人工核对。

每条日志输出一行 JSON，字段稳定，测试可逐行解析核对：
event、tenant_id、endpoint、scope、key、status、count、limit、window_start 等。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        extra = getattr(record, "audit", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tenant_governance")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 幂等：重复构建容器（测试）时不叠加 handler
    logger.handlers.clear()

    formatter = _JsonLineFormatter()

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger
