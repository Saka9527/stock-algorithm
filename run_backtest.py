# -*- coding: utf-8 -*-
"""
多因子策略一键回测入口。

数据源 (--source):
  rqdatac  - 米筐 RQDatac + RQFactor execute_factor（默认）
  ifind    - 同花顺 iFinD 自建同步表（SQL/CSV），不依赖 RQDatac
  demo     - RQAlpha Bundle 演示动量

iFinD 配置: config/ifind_config.yaml（参考 config/ifind_config.example.yaml）

RQAlpha Bundle（ifind + RQAlpha 撮合时）:
  rqalpha download-bundle --confirm
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor import config
from multi_factor.data_utils import init_rqdatac, verify_rqdatac_connection
from multi_factor.factor_analysis import (
    precompute_composite_scores,
    run_full_factor_research,
)
from multi_factor.report import (
    build_backtest_report,
    merge_factor_and_backtest_report,
    print_summary,
)
from multi_factor.strategy import get_backtest_config, handle_bar, init


def parse_args():
    p = argparse.ArgumentParser(description="RQAlpha + RQFactor 多因子回测")
    p.add_argument(
        "--source",
        choices=("rqdatac", "ifind", "demo"),
        default="rqdatac",
        help="数据来源: rqdatac | ifind | demo",
    )
    p.add_argument(
        "--ifind-config",
        default=str(config.IFIND_CONFIG_PATH),
        help="iFinD 映射配置 YAML 路径",
    )
    p.add_argument(
        "--local-backtest",
        action="store_true",
        help="[ifind] 使用本地撮合回测，不依赖 RQAlpha Bundle",
    )
    p.add_argument(
        "--rqalpha-backtest",
        action="store_true",
        help="[ifind] 因子来自 iFinD 表，撮合仍用 RQAlpha Bundle",
    )
    p.add_argument("--start", default=config.START_DATE, help="开始日期 YYYYMMDD")
    p.add_argument("--end", default=config.END_DATE, help="结束日期 YYYYMMDD")
    p.add_argument("--index", default=config.UNIVERSE_INDEX, help="股票池指数（rqdatac）")
    p.add_argument("--top-n", type=int, default=config.TOP_N, help="持仓数量")
    p.add_argument("--skip-factor-analysis", action="store_true", help="跳过因子检验")
    p.add_argument("--skip-backtest", action="store_true", help="仅做因子研究")
    p.add_argument("--scores-only", action="store_true", help="仅预计算因子得分")
    p.add_argument(
        "--demo",
        action="store_true",
        help="同 --source demo",
    )
    p.add_argument(
        "--engine",
        action="store_true",
        help="[ifind] 使用新版多因子引擎（T+1/完整绩效/报告）",
    )
    p.add_argument(
        "--strategy-config",
        default="",
        help="策略 YAML 路径（配合 --engine）",
    )
    p.add_argument(
        "--rebalance",
        choices=("daily", "weekly", "monthly"),
        default="daily",
        help="[--engine] 调仓频率",
    )
    p.add_argument(
        "--universe",
        choices=("all_a", "csi300", "csi500", "csi1000"),
        default="all_a",
        help="[--engine] 股票池",
    )
    p.add_argument(
        "--cap-neutral",
        action="store_true",
        help="[--engine] 市值中性化",
    )
    p.add_argument(
        "--factors",
        default="",
        help="[--engine] 逗号分隔因子代码，如 PE_TTM,PB,ROE_TTM,MOMENTUM_20",
    )
    return p.parse_args()


def main():
    args = parse_args()
    if args.demo:
        args.source = "demo"

    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    config.FACTOR_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    config.BACKTEST_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if args.source == "ifind" and args.engine:
        from pathlib import Path

        import yaml

        from multi_factor.engine.pipeline import run_engine_pipeline, strategy_from_dict
        from multi_factor.engine.strategy_config import FactorSpec, StrategyConfig

        if args.strategy_config and Path(args.strategy_config).exists():
            with open(args.strategy_config, encoding="utf-8") as f:
                cfg = strategy_from_dict(yaml.safe_load(f))
        else:
            factors = []
            if args.factors:
                for c in args.factors.split(","):
                    factors.append(FactorSpec(c.strip()))
            cfg = StrategyConfig(
                start=args.start,
                end=args.end,
                ifind_config_path=args.ifind_config,
                factors=factors,
                top_n=args.top_n,
                rebalance_freq=args.rebalance,
                universe=args.universe,
                cap_neutral=args.cap_neutral,
                output_dir=config.OUTPUT_DIR / "engine_report",
            )
        run_engine_pipeline(cfg)
        return

    if args.source == "ifind":
        from multi_factor.ifind.pipeline import run_ifind_pipeline

        local_bt = args.local_backtest or not args.rqalpha_backtest
        run_ifind_pipeline(
            start=args.start,
            end=args.end,
            config_path=args.ifind_config,
            top_n=args.top_n,
            skip_factor_analysis=args.skip_factor_analysis,
            scores_only=args.scores_only,
            skip_backtest=args.skip_backtest,
            local_backtest=local_bt,
            weights=config.FACTOR_WEIGHTS,
        )
        return

    if args.source == "demo":
        from multi_factor.demo_scores import generate_demo_scores

        print(">>> 演示模式：使用 RQAlpha Bundle 生成动量得分（跳过 RQDatac）...")
        generate_demo_scores(args.start, args.end, config.FACTOR_SCORES_PATH)
        if args.scores_only:
            return
        _run_rqalpha_backtest(args)
        return

    print(">>> 初始化 RQDatac ...")
    init_rqdatac()
    verify_rqdatac_connection()
    print(">>> RQDatac 已连接且接口可用")

    if not args.skip_factor_analysis:
        print(">>> RQFactor 因子检验 (IC / 分层 / 因子收益) ...")
        run_full_factor_research(
            start_date=args.start,
            end_date=args.end,
            universe_index=args.index,
            weights=config.FACTOR_WEIGHTS,
            output_dir=config.FACTOR_ANALYSIS_DIR,
        )
        print(f">>> 因子分析结果已保存至 {config.FACTOR_ANALYSIS_DIR}")

    print(">>> 预计算合成因子得分 ...")
    precompute_composite_scores(
        args.start,
        args.end,
        args.index,
        config.FACTOR_WEIGHTS,
        config.FACTOR_SCORES_PATH,
    )
    print(f">>> 因子得分: {config.FACTOR_SCORES_PATH}")

    if args.scores_only or args.skip_backtest:
        return

    _run_rqalpha_backtest(args)


def _run_rqalpha_backtest(args):
    from rqalpha import run_func

    print(">>> RQAlpha 策略回测 ...")
    bt_config = get_backtest_config(args.start, args.end)
    bt_config["extra"]["context_vars"]["top_n"] = args.top_n

    result = run_func(config=bt_config, init=init, handle_bar=handle_bar)

    analyser = result.get("sys_analyser", result)
    summary = analyser.get("summary", {})
    print_summary(summary)

    build_backtest_report(result, config.BACKTEST_REPORT_DIR)

    merged = config.OUTPUT_DIR / "multi_factor_full_report.xlsx"
    if config.FACTOR_ANALYSIS_DIR.exists():
        merge_factor_and_backtest_report(
            config.FACTOR_ANALYSIS_DIR,
            config.BACKTEST_REPORT_DIR,
            merged,
        )

    print(f">>> 回测报告目录: {config.BACKTEST_REPORT_DIR}")
    if merged.exists():
        print(f">>> 合并完整报告: {merged}")
    print(">>> 全部流程完成")


if __name__ == "__main__":
    main()
