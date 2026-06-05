# -*- coding: utf-8 -*-
"""iFinD 数据源完整流程：因子研究 -> 得分 -> 回测。"""

from pathlib import Path

from multi_factor import config as project_config
from multi_factor.ifind.analysis import run_full_factor_research
from multi_factor.ifind.backtest_local import run_local_backtest, save_local_backtest_report
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_engine import precompute_composite_scores
from multi_factor.ifind.provider import IFindDataProvider
from multi_factor.report import merge_factor_and_backtest_report, print_summary


def run_ifind_pipeline(
    start: str,
    end: str,
    config_path: str | Path | None = None,
    top_n: int | None = None,
    skip_factor_analysis: bool = False,
    scores_only: bool = False,
    skip_backtest: bool = False,
    local_backtest: bool = True,
    weights: dict | None = None,
) -> None:
    cfg = load_ifind_config(config_path)
    provider = IFindDataProvider(cfg)
    weights = weights or project_config.FACTOR_WEIGHTS
    top_n = top_n or project_config.TOP_N

    project_config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    project_config.FACTOR_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    project_config.BACKTEST_REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if not skip_factor_analysis:
        print(">>> [iFinD] RQFactor 因子检验 (IC / 分层 / 因子收益) ...")
        run_full_factor_research(provider, start, end, weights, project_config.FACTOR_ANALYSIS_DIR)
        print(f">>> 因子分析: {project_config.FACTOR_ANALYSIS_DIR}")

    print(">>> [iFinD] 预计算合成因子得分 ...")
    scores = precompute_composite_scores(
        provider, start, end, weights, project_config.FACTOR_SCORES_PATH
    )
    print(f">>> 因子得分: {project_config.FACTOR_SCORES_PATH} ({scores.shape})")

    if scores_only or skip_backtest:
        return

    if local_backtest:
        print(">>> [iFinD] 本地回测（不依赖 RQAlpha Bundle）...")
        bt = run_local_backtest(provider, scores, start, end, top_n=top_n)
        print_summary(bt["summary"])
        save_local_backtest_report(bt, project_config.BACKTEST_REPORT_DIR)
    else:
        from multi_factor.strategy import get_backtest_config, handle_bar, init
        from rqalpha import run_func

        if cfg.benchmark_rq:
            project_config.BENCHMARK = cfg.benchmark_rq
        print(">>> [iFinD] RQAlpha 回测（因子来自 iFinD，撮合使用 Bundle）...")
        bt_config = get_backtest_config(start, end)
        bt_config["extra"]["context_vars"]["top_n"] = top_n
        result = run_func(config=bt_config, init=init, handle_bar=handle_bar)
        from multi_factor.report import build_backtest_report

        analyser = result.get("sys_analyser", result)
        print_summary(analyser.get("summary", {}))
        build_backtest_report(result, project_config.BACKTEST_REPORT_DIR)

    merged = project_config.OUTPUT_DIR / "multi_factor_full_report.xlsx"
    if project_config.FACTOR_ANALYSIS_DIR.exists():
        merge_factor_and_backtest_report(
            project_config.FACTOR_ANALYSIS_DIR,
            project_config.BACKTEST_REPORT_DIR,
            merged,
        )
    print(f">>> 回测报告: {project_config.BACKTEST_REPORT_DIR}")
    if merged.exists():
        print(f">>> 合并报告: {merged}")
