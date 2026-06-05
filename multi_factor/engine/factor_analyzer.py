# -*- coding: utf-8 -*-
"""单因子分析：日度/月度 IC、分层回测、收益曲线。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from multi_factor.engine.data_hub import DataHub
from multi_factor.engine.strategy_config import StrategyConfig
from multi_factor.ifind.factor_metrics import (
    _sort_ascending,
    build_factor_report,
    compute_quantile_returns,
    compute_rank_ic_series,
    cumulative_nav,
    summarize_ic,
)


def monthly_ic(ic_daily: pd.Series) -> pd.Series:
    """按月聚合 IC（日均）。"""
    if ic_daily.empty:
        return ic_daily
    return ic_daily.groupby(ic_daily.index.to_period("M")).mean()


def analyze_single_factor(
    hub: DataHub,
    factor_code: str,
    cfg: StrategyConfig,
    meta: dict | None = None,
) -> dict[str, Any]:
    """单因子完整分析结果。"""
    code = factor_code.upper()
    panel = hub.load_factor(code)
    meta = meta or hub.factor_meta_map().get(code, {"factor_code": code, "sort_type": "desc"})
    ascending = _sort_ascending(meta.get("sort_type"))

    report = build_factor_report(
        panel,
        hub.returns,
        meta,
        period=cfg.ic_period,
        quantiles=cfg.quantile_groups,
    )

    ic_daily = compute_rank_ic_series(
        panel, hub.returns, period=cfg.ic_period, ascending=ascending
    )
    ic_m = monthly_ic(ic_daily)
    ic_m_summary = summarize_ic(ic_m)

    qret = compute_quantile_returns(
        panel,
        hub.returns,
        period=cfg.ic_period,
        ascending=ascending,
        quantiles=cfg.quantile_groups,
    )
    quantile_nav_curves = {}
    if not qret.empty:
        for col in qret.columns:
            nav = cumulative_nav(qret[col])
            quantile_nav_curves[col] = [
                {"date": d.strftime("%Y-%m-%d"), "nav": float(nav.loc[d])}
                for d in nav.index
            ]

    report["ic_daily_series"] = [
        {"date": d.strftime("%Y-%m-%d"), "ic": float(v)} for d, v in ic_daily.dropna().items()
    ]
    report["ic_monthly_series"] = [
        {"date": str(p), "ic": float(v)} for p, v in ic_m.dropna().items()
    ]
    report["ic_monthly_summary"] = ic_m_summary
    report["quantile_nav_curves"] = quantile_nav_curves
    return report


def analyze_all_factors(hub: DataHub, cfg: StrategyConfig) -> dict[str, dict]:
    """对配置中每个因子做分析。"""
    out = {}
    for spec in cfg.factors:
        code = spec.code.upper()
        try:
            out[code] = analyze_single_factor(hub, code, cfg)
        except Exception as ex:
            out[code] = {"error": str(ex), "factor_code": code}
    return out
