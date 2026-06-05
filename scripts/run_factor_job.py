# -*- coding: utf-8 -*-
"""可复用因子任务入口：生成并落库 factor_data_wide。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_jobs import (
    FactorJobRunner,
    create_factor_calculator,
    create_panel_align_calculator,
    resolve_job_dates,
)
from multi_factor.ifind.provider import IFindDataProvider


def parse_args():
    p = argparse.ArgumentParser(description="因子任务：Python 计算 + DB upsert")
    p.add_argument(
        "--factor",
        default="MOMENTUM_20",
        help="因子代码（计算：MOMENTUM_N / ROE_YOY / MAIN_NET_INFLOW_RATIO；--align 时可为 PE 等同步因子）",
    )
    p.add_argument(
        "--align",
        action="store_true",
        help="按交易日历对 factor_data_wide 历史做前向填充（估值/基本面同步因子）",
    )
    p.add_argument("--start", default="", help="开始日期 YYYYMMDD，默认近90天")
    p.add_argument("--end", default="", help="结束日期 YYYYMMDD，默认今天")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--run-type", default="incr", choices=("incr", "full"))
    p.add_argument("--dry-run", action="store_true", help="只计算不写库")
    return p.parse_args()


def resolve_dates(provider: IFindDataProvider, start: str, end: str, run_type: str) -> tuple[str, str]:
    return resolve_job_dates(provider, start, end, run_type)


def create_calculator(factor_code: str, align: bool = False):
    if align:
        return create_panel_align_calculator(factor_code)
    return create_factor_calculator(factor_code)


def verify_for_backtest(provider: IFindDataProvider, factor_code: str, start: str, end: str) -> dict:
    panel = provider.load_factor_panel_by_code(factor_code, start, end)
    ret = provider.get_daily_returns(start, end)
    common = panel.columns.intersection(ret.columns)
    return {
        "factor_panel_shape": list(panel.shape),
        "returns_shape": list(ret.shape),
        "common_symbol_count": int(len(common)),
        "date_start": panel.index.min().strftime("%Y-%m-%d") if len(panel) else None,
        "date_end": panel.index.max().strftime("%Y-%m-%d") if len(panel) else None,
    }


def main():
    args = parse_args()
    cfg = load_ifind_config(args.ifind_config or None)
    runner = FactorJobRunner(cfg)
    provider = IFindDataProvider(cfg)
    start, end = resolve_dates(provider, args.start, args.end, args.run_type)
    calc = create_calculator(args.factor, align=args.align)
    if args.align:
        meta = provider.get_factor_meta(calc.factor_code) or {}
        if meta.get("factor_name"):
            calc.factor_name = str(meta["factor_name"])
        if meta.get("factor_type"):
            calc.factor_type = str(meta["factor_type"])
        if meta.get("sort_type"):
            calc.sort_type = str(meta["sort_type"])

    print(f">>> run factor job: {calc.factor_code} {start}~{end} run_type={args.run_type}")
    res = runner.run(
        calc=calc,
        start=start,
        end=end,
        run_type=args.run_type,
        dry_run=args.dry_run,
    )
    print(">>> job result")
    print(json.dumps(res, ensure_ascii=False, indent=2))

    # 回测兼容校验：能否从 factor_data_wide 读出并与收益对齐
    check = verify_for_backtest(provider, calc.factor_code, start, end)
    print(">>> backtest compatibility")
    print(json.dumps(check, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

