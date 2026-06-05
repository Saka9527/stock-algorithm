# -*- coding: utf-8 -*-
"""
BaoStock 估值字段 -> factor_data_wide 增量覆盖同步。

  python scripts/sync_baostock_factor_wide.py
  python scripts/sync_baostock_factor_wide.py --dry-run
  python scripts/sync_baostock_factor_wide.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.baostock.factor_wide_sync import BaostockFactorWideSync


def parse_args():
    p = argparse.ArgumentParser(description="BaoStock 估值同步至 factor_data_wide")
    p.add_argument("--config", default="", help="baostock 配置路径")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--max-stocks", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--status", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    sync = BaostockFactorWideSync(
        baostock_config_path=args.config or None,
        ifind_config_path=args.ifind_config or None,
    )
    if args.status:
        print(json.dumps(sync.status(), ensure_ascii=False, indent=2))
        return
    result = sync.run(
        max_stocks=args.max_stocks or None,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
