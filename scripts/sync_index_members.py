# -*- coding: utf-8 -*-
"""
指数成分股同步（沪深300 / 中证500 / 中证1000）。

  python scripts/sync_index_members.py --pool csi300 --use-full-range
  python scripts/sync_index_members.py --all --use-full-range --monthly
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.baostock.index_members_sync import IndexMembersSync
from multi_factor.service.backtest_validation import query_db_data_range


def parse_args():
    p = argparse.ArgumentParser(description="同步指数成分股至 index_members")
    p.add_argument("--ifind-config", default="")
    p.add_argument("--pool", default="", choices=["", "csi300", "csi500", "csi1000"])
    p.add_argument("--all", action="store_true", help="同步全部支持的指数池")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--use-full-range", action="store_true")
    p.add_argument("--data-years", type=float, default=3.0)
    p.add_argument("--monthly", action="store_true", default=True, help="按月末交易日快照")
    p.add_argument("--ensure-table", action="store_true", default=True)
    return p.parse_args()


def main():
    args = parse_args()
    sync = IndexMembersSync(args.ifind_config or None)
    if args.ensure_table:
        sync.ensure_table()

    if args.use_full_range:
        span = query_db_data_range(sync.cfg, years=args.data_years)
        start, end = span["start"], span["end"]
    else:
        start, end = args.start, args.end

    pools = None
    if args.pool:
        pools = [args.pool]
    elif not args.all:
        pools = ["csi300", "csi500", "csi1000"]

    result = sync.sync_pools(pools=pools, start=start, end=end, monthly=args.monthly)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
