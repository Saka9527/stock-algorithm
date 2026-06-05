# -*- coding: utf-8 -*-
"""因子维度统计任务入口：写入 factor_performance_summary / factor_performance_series。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_performance_jobs import FactorPerformanceJobRunner


def parse_args():
    p = argparse.ArgumentParser(description="计算并落库因子维度统计（IC/夏普/收益）")
    p.add_argument("--factor-code", default="", help="单个因子代码，不传则批量跑 factor_base_info 全部因子")
    p.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    p.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    p.add_argument("--period", type=int, default=1, help="IC/收益前瞻期（交易日）")
    p.add_argument("--quantiles", type=int, default=5, help="分层组数")
    p.add_argument("--top-pct", type=float, default=0.2, help="Top/Bottom 分组比例")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_ifind_config(args.ifind_config or None)
    runner = FactorPerformanceJobRunner(cfg)
    factor_codes = [args.factor_code] if args.factor_code else None
    result = runner.run_batch(
        factor_codes=factor_codes,
        start=args.start,
        end=args.end,
        period=args.period,
        quantiles=args.quantiles,
        top_pct=args.top_pct,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()

