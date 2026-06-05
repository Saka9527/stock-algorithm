# -*- coding: utf-8 -*-
"""基于 iFinD 本地因子面板 + 本地收益率，调用 RQFactor 做 IC / 分层检验。"""

from pathlib import Path

import pandas as pd
from rqfactor import (
    FactorAnalysisEngine,
    FactorReturnAnalysis,
    ICAnalysis,
    QuantileReturnAnalysis,
    Winzorization,
)

from multi_factor import config as project_config
from multi_factor.factor_analysis import _export_factor_result  # noqa: F401 — 复用无 GUI 导出
from multi_factor.ifind.factor_engine import compute_ranked_factors
from multi_factor.ifind.provider import IFindDataProvider


def _build_engine() -> FactorAnalysisEngine:
    engine = FactorAnalysisEngine()
    engine.append(("winzorization", Winzorization(method="mad")))
    engine.append(("ic", ICAnalysis(rank_ic=True, industry_classification=None, max_decay=20)))
    engine.append(
        (
            "quantile",
            QuantileReturnAnalysis(
                quantile=project_config.QUANTILE_GROUPS, benchmark=None
            ),
        )
    )
    engine.append(("return", FactorReturnAnalysis()))
    return engine


def run_factor_analysis(
    provider: IFindDataProvider,
    factor_panel: pd.DataFrame,
    returns: pd.DataFrame,
    factor_name: str,
    output_dir: Path,
) -> dict:
    engine = _build_engine()
    ascending = project_config.FACTOR_ASCENDING.get(factor_name, True)
    aligned_ret = returns.reindex(index=factor_panel.index, columns=factor_panel.columns)
    result = engine.analysis(
        factor_panel,
        aligned_ret,
        ascending=ascending,
        periods=project_config.IC_PERIODS,
        keep_preprocess_result=True,
    )
    _export_factor_result(result, output_dir)
    return result


def run_full_factor_research(
    provider: IFindDataProvider,
    start: str,
    end: str,
    weights: dict | None,
    output_dir: Path,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    universe = provider.get_universe(start, end)
    ranked = compute_ranked_factors(provider, universe, start, end, weights)
    returns = provider.get_daily_returns(start, end)

    results = {}
    for name in ("pe", "pb", "roe", "momentum", "composite"):
        sub_dir = output_dir / name
        results[name] = run_factor_analysis(
            provider, ranked[name], returns, name, sub_dir
        )
    return results
