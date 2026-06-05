# -*- coding: utf-8 -*-
"""RQFactor 因子检验：IC 分析、分层回测、因子收益。"""

from pathlib import Path

import pandas as pd
from rqfactor import (
    FactorAnalysisEngine,
    FactorReturnAnalysis,
    ICAnalysis,
    QuantileReturnAnalysis,
    Winzorization,
)
from rqfactor.engine_v2 import execute_factor

from multi_factor import config
from multi_factor.data_utils import get_universe, init_rqdatac
from multi_factor.factors import SINGLE_FACTORS, build_composite_factor


def _build_engine(benchmark: str, quantile: int) -> FactorAnalysisEngine:
    engine = FactorAnalysisEngine()
    engine.append(("winzorization", Winzorization(method="mad")))
    engine.append(
        (
            "ic",
            ICAnalysis(rank_ic=True, industry_classification="sws", max_decay=20),
        )
    )
    engine.append(
        (
            "quantile",
            QuantileReturnAnalysis(quantile=quantile, benchmark=benchmark),
        )
    )
    engine.append(("return", FactorReturnAnalysis()))
    return engine


def run_single_factor_analysis(
    factor_name: str,
    start_date: str,
    end_date: str,
    universe_index: str,
    output_dir: Path,
) -> dict:
    """对单个因子执行完整检验管道。"""
    init_rqdatac()
    factor = SINGLE_FACTORS[factor_name]
    ref_date = start_date
    universe = get_universe(universe_index, ref_date)
    df = execute_factor(factor, universe, start_date, end_date)

    engine = _build_engine(config.BENCHMARK, config.QUANTILE_GROUPS)
    ascending = config.FACTOR_ASCENDING.get(factor_name, True)
    result = engine.analysis(
        df,
        "daily",
        ascending=ascending,
        periods=config.IC_PERIODS,
        keep_preprocess_result=True,
    )
    _export_factor_result(result, output_dir / factor_name)
    return result


def run_composite_factor_analysis(
    start_date: str,
    end_date: str,
    universe_index: str,
    weights: dict,
    output_dir: Path,
) -> dict:
    """对合成因子执行检验。"""
    init_rqdatac()
    composite = build_composite_factor(weights)
    universe = get_universe(universe_index, start_date)
    df = execute_factor(composite, universe, start_date, end_date)

    engine = _build_engine(config.BENCHMARK, config.QUANTILE_GROUPS)
    result = engine.analysis(
        df,
        "daily",
        ascending=config.FACTOR_ASCENDING["composite"],
        periods=config.IC_PERIODS,
        keep_preprocess_result=True,
    )
    _export_factor_result(result, output_dir / "composite")
    return result


def _export_factor_result(result: dict, out_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    writer = pd.ExcelWriter(out_dir / "factor_analysis.xlsx", engine="openpyxl")
    try:
        result["ic"].summary().to_excel(writer, sheet_name="IC汇总")
        result["quantile"].quantile_returns.to_excel(
            writer, sheet_name="分层累计收益"
        )
        result["quantile"].quantile_turnover.to_excel(
            writer, sheet_name="分层换手率"
        )
        result["return"].factor_returns.to_excel(writer, sheet_name="因子收益")
    finally:
        writer.close()

    for key, fname in (("ic", "ic_analysis.png"), ("quantile", "quantile_returns.png")):
        try:
            fig = result[key].show()
            if fig is not None:
                fig.savefig(out_dir / fname, dpi=120, bbox_inches="tight")
                plt.close(fig)
        except Exception:
            try:
                result[key].plot()
                plt.savefig(out_dir / fname, dpi=120, bbox_inches="tight")
                plt.close("all")
            except Exception:
                pass


def run_full_factor_research(
    start_date: str | None = None,
    end_date: str | None = None,
    universe_index: str | None = None,
    weights: dict | None = None,
    output_dir: Path | None = None,
) -> dict:
    """运行全部因子（单因子 + 合成）检验。"""
    start_date = start_date or config.START_DATE
    end_date = end_date or config.END_DATE
    universe_index = universe_index or config.UNIVERSE_INDEX
    weights = weights or config.FACTOR_WEIGHTS
    output_dir = output_dir or config.FACTOR_ANALYSIS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for name in SINGLE_FACTORS:
        results[name] = run_single_factor_analysis(
            name, start_date, end_date, universe_index, output_dir
        )

    results["composite"] = run_composite_factor_analysis(
        start_date, end_date, universe_index, weights, output_dir
    )
    return results


def precompute_composite_scores(
    start_date: str,
    end_date: str,
    universe_index: str,
    weights: dict,
    save_path: Path,
) -> pd.DataFrame:
    """预计算合成因子截面得分，供 RQAlpha 策略调仓使用。"""
    init_rqdatac()
    composite = build_composite_factor(weights)
    universe = get_universe(universe_index, start_date)
    df = execute_factor(composite, universe, start_date, end_date)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(save_path)
    return df
