# -*- coding: utf-8 -*-
"""
从 BaoStock 同步近三年前复权日 K 至 MySQL。

  python scripts/sync_baostock_daily.py
  python scripts/sync_baostock_daily.py --dry-run
  python scripts/sync_baostock_daily.py --quota
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.baostock.config_loader import load_baostock_config
from multi_factor.baostock.sync import BaostockDailySync


def parse_args():
    p = argparse.ArgumentParser(description="BaoStock 前复权日 K 同步至 MySQL（含 API 配额控制）")
    p.add_argument("--config", default="", help="配置文件路径，默认 config/baostock_config.yaml")
    p.add_argument("--years", type=float, default=0, help="回溯年数，0 表示读配置")
    p.add_argument("--start", default="", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", default="", help="结束日期 YYYY-MM-DD")
    p.add_argument(
        "--daily-limit",
        type=int,
        default=0,
        help="每日 API 请求上限，0 表示读配置",
    )
    p.add_argument(
        "--listed-only",
        action="store_true",
        help="仅用 query_all_stock 取最新在市股票",
    )
    p.add_argument("--codes", default="", help="逗号分隔股票，支持 sh.600000 或 600000.SH")
    p.add_argument("--max-stocks", type=int, default=0, help="最多同步股票数，0 表示不限制")
    p.add_argument("--batch-size", type=int, default=100, help="每批独立会话的股票数")
    p.add_argument("--dry-run", action="store_true", help="仅估算请求次数，不拉数")
    p.add_argument("--quota", action="store_true", help="查看今日配额与库内统计")
    return p.parse_args()


def _normalize_codes(raw: str) -> list[str] | None:
    if not raw.strip():
        return None
    from multi_factor.baostock.code_convert import normalize_bs, ths_to_bs

    out = []
    for part in raw.split(","):
        c = part.strip()
        if not c:
            continue
        if "." in c and c[0].isdigit():
            c = ths_to_bs(c)
        else:
            c = normalize_bs(c)
        out.append(c)
    return out or None


def main():
    args = parse_args()
    cfg = load_baostock_config(args.config or None)
    if args.years > 0:
        cfg.years = args.years
    if args.daily_limit > 0:
        cfg.daily_api_limit = args.daily_limit
    if args.listed_only:
        cfg.use_full_universe = False

    sync = BaostockDailySync(cfg)

    if args.quota:
        print(json.dumps(sync.quota_status(), ensure_ascii=False, indent=2))
        return

    result = sync.run(
        start_date=args.start or None,
        end_date=args.end or None,
        codes=_normalize_codes(args.codes),
        max_stocks=args.max_stocks or None,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
