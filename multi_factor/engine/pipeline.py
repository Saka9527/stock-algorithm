# -*- coding: utf-8 -*-
"""多因子引擎一站式流水线。"""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Any

import pandas as pd

from multi_factor import config as project_config
from multi_factor.engine.backtest import run_backtest
from multi_factor.engine.composite import build_composite_scores
from multi_factor.engine.data_hub import DataHub
from multi_factor.engine.factor_analyzer import analyze_all_factors
from multi_factor.engine.performance import compute_performance
from multi_factor.engine.report import (
    save_backtest_outputs,
    save_factor_analysis_outputs,
    write_html_report,
)
from multi_factor.engine.strategy_config import DEFAULT_FACTORS, FactorSpec, StrategyConfig


def run_engine_pipeline(cfg: StrategyConfig) -> dict[str, Any]:
    """
    完整流程：加载数据 -> 单因子分析 -> 合成得分 -> 回测 -> 绩效 -> 报告。

    Returns
    -------
    包含 composite_scores, backtest, performance, factor_analyses 的字典。
    """
    if not cfg.factors:
        cfg.factors = list(DEFAULT_FACTORS)

    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(">>> [1/5] 加载行情与交易日 ...")
    t_step = time.perf_counter()
    hub = DataHub(cfg)
    hub.load_base()
    timings["load_base_sec"] = round(time.perf_counter() - t_step, 2)
    print(f"    交易日 {len(hub.trading_dates)} 天, 股票 {hub.close.shape[1]} 只")

    factor_analyses: dict = {}
    if cfg.run_single_factor_analysis:
        print(">>> [2/5] 单因子分析 (IC / 分层) ...")
        t_step = time.perf_counter()
        factor_analyses = analyze_all_factors(hub, cfg, prefer_db=True)
        timings["factor_analysis_sec"] = round(time.perf_counter() - t_step, 2)
        for code, rep in factor_analyses.items():
            if "error" in rep:
                print(f"    {code}: 跳过 ({rep['error']})")
                continue
            save_factor_analysis_outputs(
                code,
                rep,
                hub.returns,
                hub.load_factor(code),
                out_dir,
            )
            print(f"    {code}: IC均值={rep.get('summary', {}).get('ic_mean')}")

    print(">>> [3/5] 多因子合成 ...")
    t_step = time.perf_counter()
    composite = build_composite_scores(hub, cfg)
    timings["composite_sec"] = round(time.perf_counter() - t_step, 2)
    composite_path = out_dir / "composite_scores.pkl"
    with open(composite_path, "wb") as f:
        pickle.dump(composite, f)
    # 兼容旧 API 路径
    with open(project_config.FACTOR_SCORES_PATH, "wb") as f:
        pickle.dump(composite, f)
    print(f"    得分矩阵: {composite.shape}, 已保存 {composite_path}")

    print(">>> [4/5] 回测 (T+1 / 手续费 / 滑点) ...")
    t_step = time.perf_counter()
    bt = run_backtest(composite, hub.returns, cfg, close=hub.close)
    timings["backtest_sec"] = round(time.perf_counter() - t_step, 2)
    bt["benchmark_returns"] = hub.benchmark_returns
    perf = compute_performance(bt["strategy_returns"], hub.benchmark_returns)
    print(
        f"    年化收益={perf['annualized_return']:.2%}, "
        f"最大回撤={perf['max_drawdown']:.2%}, "
        f"夏普={perf.get('sharpe_ratio')}"
    )

    print(">>> [5/5] 导出报告与图表 ...")
    t_step = time.perf_counter()
    save_backtest_outputs(bt, perf, out_dir, hub.benchmark_returns, cfg.start, cfg.end)
    cfg_summary = {
        "start": cfg.start,
        "end": cfg.end,
        "universe": cfg.universe,
        "top_n": cfg.top_n,
        "rebalance": cfg.rebalance_freq,
        "factors": [f.code for f in cfg.factors],
        "weight_mode": cfg.weight_mode,
        "cap_neutral": cfg.cap_neutral,
        "industry_neutral": cfg.industry_neutral,
    }
    write_html_report(perf, cfg_summary, out_dir)

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {"performance": perf, "config": cfg_summary, "factor_analyses_summary": {
                k: v.get("summary") if isinstance(v, dict) else v
                for k, v in factor_analyses.items()
            }},
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    run_id = None
    try:
        from multi_factor.engine.backtest_storage import BacktestStorage
        from multi_factor.ifind.config_loader import load_ifind_config

        ifind_cfg = load_ifind_config(cfg.ifind_config_path)
        if ifind_cfg.db_url:
            storage = BacktestStorage(ifind_cfg)
            run_id = storage.save_run(
                cfg, bt, perf, factor_analyses, hub.returns, out_dir
            )
            print(f"    回测结果已落库 run_id={run_id}")
    except Exception as ex:
        print(f"    回测落库跳过: {ex}")
    timings["report_persist_sec"] = round(time.perf_counter() - t_step, 2)
    timings["total_sec"] = round(time.perf_counter() - t0, 2)

    print(f">>> 完成。报告目录: {out_dir.resolve()}，耗时 {timings['total_sec']}s")
    return {
        "composite_scores": composite,
        "backtest": bt,
        "performance": perf,
        "factor_analyses": factor_analyses,
        "output_dir": out_dir,
        "run_id": run_id,
        "timings": timings,
    }


def strategy_from_dict(d: dict) -> StrategyConfig:
    """从 YAML/JSON 字典构建 StrategyConfig。"""
    factors = [
        FactorSpec(
            code=f["code"],
            weight=float(f.get("weight", 1.0)),
            ascending=f.get("ascending"),
        )
        for f in d.get("factors", [])
    ]
    return StrategyConfig(
        start=str(d.get("start", StrategyConfig.start)),
        end=str(d.get("end", StrategyConfig.end)),
        ifind_config_path=str(d.get("ifind_config_path", StrategyConfig.ifind_config_path)),
        factors=factors,
        weight_mode=d.get("weight_mode", "equal"),
        industry_neutral=bool(d.get("industry_neutral", False)),
        cap_neutral=bool(d.get("cap_neutral", False)),
        universe=d.get("universe", "all_a"),
        top_n=int(d.get("top_n", 30)),
        rebalance_freq=d.get("rebalance_freq", "daily"),
        exclude_st=bool(d.get("exclude_st", True)),
        exclude_suspended=bool(d.get("exclude_suspended", True)),
        exclude_new_days=int(d.get("exclude_new_days", 60)),
        exclude_limit=bool(d.get("exclude_limit", True)),
        initial_cash=float(d.get("initial_cash", 1_000_000)),
        buy_commission=float(d.get("buy_commission", 0.0003)),
        sell_commission=float(d.get("sell_commission", 0.0013)),
        slippage=float(d.get("slippage", 0.001)),
        run_single_factor_analysis=bool(d.get("run_single_factor_analysis", True)),
        output_dir=Path(d.get("output_dir", project_config.OUTPUT_DIR / "engine_report")),
    )
