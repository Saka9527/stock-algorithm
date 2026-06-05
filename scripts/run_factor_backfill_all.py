# -*- coding: utf-8 -*-
"""批量补全 factor_base_info 因子：calc 落库 + performance 预热。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_jobs import (
    FactorJobRunner,
    create_factor_calculator,
    create_panel_align_calculator,
    list_computable_factor_codes,
    list_factors_ready_for_backtest,
    list_panel_backfill_factor_codes,
    resolve_daily_available_range,
    resolve_job_dates,
)
from multi_factor.ifind.factor_performance_jobs import FactorPerformanceJobRunner
from multi_factor.ifind.provider import IFindDataProvider


def parse_args():
    p = argparse.ArgumentParser(
        description="批量补全可回测因子：先 calc（可计算因子）再 performance（全部可用因子）"
    )
    p.add_argument("--start", default="", help="开始日期 YYYYMMDD，默认日 K 最早日期")
    p.add_argument("--end", default="", help="结束日期 YYYYMMDD，默认日 K 最晚日期")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--run-type", default="full", choices=("incr", "full"), help="calc 任务类型")
    p.add_argument("--skip-calc", action="store_true", help="跳过 factor_data_wide 补全")
    p.add_argument(
        "--skip-align",
        action="store_true",
        help="跳过同步因子按交易日历前向填充（估值/基本面等）",
    )
    p.add_argument("--skip-performance", action="store_true", help="跳过 factor_performance 预热")
    p.add_argument(
        "--calc-factors",
        default="",
        help="仅补算指定因子（逗号分隔），默认 factor_base_info 中全部可计算因子",
    )
    p.add_argument(
        "--align-factors",
        default="",
        help="仅对齐填充指定同步因子（逗号分隔），默认全部需补全的同步因子",
    )
    p.add_argument("--dry-run-calc", action="store_true", help="calc 阶段只试算不落库")
    p.add_argument("--period", type=int, default=1, help="绩效 IC/收益前瞻期")
    p.add_argument("--quantiles", type=int, default=5, help="分层组数")
    p.add_argument("--top-pct", type=float, default=0.2, help="Top/Bottom 比例")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_ifind_config(args.ifind_config or None)
    provider = IFindDataProvider(cfg)
    meta_list = provider.list_factor_base_info()

    if args.start and args.end:
        start, end = args.start.replace("-", "")[:8], args.end.replace("-", "")[:8]
    else:
        start, end = resolve_daily_available_range(provider)

    print(f">>> backfill window: {start} ~ {end}")

    align_results: list[dict] = []
    if not args.skip_align:
        align_codes = list_panel_backfill_factor_codes(provider, start, end, meta_list)
        if args.align_factors.strip():
            allow = {c.strip().upper() for c in args.align_factors.split(",") if c.strip()}
            align_codes = [c for c in align_codes if c in allow]
        print(f">>> align phase: {len(align_codes)} factors need panel fill -> {align_codes}")
        runner = FactorJobRunner(cfg)
        for code in align_codes:
            calc = create_panel_align_calculator(code)
            meta = provider.get_factor_meta(code) or {}
            if meta.get("factor_name"):
                calc.factor_name = str(meta["factor_name"])
            if meta.get("factor_type"):
                calc.factor_type = str(meta["factor_type"])
            if meta.get("sort_type"):
                calc.sort_type = str(meta["sort_type"])
            print(f">>> align job {code}: {start}~{end}")
            try:
                res = runner.run(
                    calc=calc,
                    start=start,
                    end=end,
                    run_type="full",
                    dry_run=args.dry_run_calc,
                )
                align_results.append({"factor_code": code, "status": "success", **res})
            except Exception as ex:
                align_results.append({"factor_code": code, "status": "failed", "error": str(ex)})

    calc_results: list[dict] = []
    if not args.skip_calc:
        computable = list_computable_factor_codes(meta_list)
        if args.calc_factors.strip():
            allow = {c.strip().upper() for c in args.calc_factors.split(",") if c.strip()}
            computable = [c for c in computable if c in allow]
        print(f">>> calc phase: {len(computable)} computable factors -> {computable}")
        runner = FactorJobRunner(cfg)
        for code in computable:
            calc = create_factor_calculator(code)
            job_start, job_end = resolve_job_dates(provider, start, end, args.run_type)
            print(f">>> calc job {code}: {job_start}~{job_end}")
            try:
                res = runner.run(
                    calc=calc,
                    start=job_start,
                    end=job_end,
                    run_type=args.run_type,
                    dry_run=args.dry_run_calc,
                )
                calc_results.append({"factor_code": code, "status": "success", **res})
            except Exception as ex:
                calc_results.append({"factor_code": code, "status": "failed", "error": str(ex)})

    perf_result: dict | None = None
    if not args.skip_performance:
        ready = list_factors_ready_for_backtest(provider, start, end, meta_list)
        print(f">>> performance phase: {len(ready)} factors ready -> {ready}")
        perf_runner = FactorPerformanceJobRunner(cfg)
        perf_result = perf_runner.run_batch(
            factor_codes=ready,
            start=start,
            end=end,
            period=args.period,
            quantiles=args.quantiles,
            top_pct=args.top_pct,
        )

    out = {
        "window": {"start": start, "end": end},
        "align": align_results,
        "calc": calc_results,
        "performance": perf_result,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
