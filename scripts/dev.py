"""本地开发辅助：为播种租户签发 token（无外部依赖，仅用于本地验证）。

用法:
    uv run python scripts_dev.py token tenant-acme
    uv run python scripts_dev.py token tenant-acme --ttl 2
    uv run python scripts_dev.py reset          # 清空本地 DB 与日志
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.container import SEED_TENANTS, build_container
from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="tenant governance dev helpers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("token", help="issue a local token for a seeded tenant")
    t.add_argument("tenant_id", choices=[x["tenant_id"] for x in SEED_TENANTS])
    t.add_argument("--ttl", type=int, default=settings.token_ttl_seconds)

    sub.add_parser("reset", help="delete local sqlite db and log file")

    args = parser.parse_args()
    if args.cmd == "token":
        container = build_container(settings)
        print(container.issue_token(args.tenant_id, ttl_seconds=args.ttl))
        container.close()
    elif args.cmd == "reset":
        for path in (settings.database_path,
                     __import__("pathlib").Path(str(settings.database_path) + "-wal"),
                     __import__("pathlib").Path(str(settings.database_path) + "-shm"),
                     settings.log_path):
            if path.exists():
                path.unlink()
                print(f"removed {path}")


if __name__ == "__main__":
    main()
