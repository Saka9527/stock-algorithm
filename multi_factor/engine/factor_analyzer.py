# -*- coding: utf-8 -*-
"""单因子分析：日度/月度 IC、分层回测、收益曲线。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from multi_factor.engine.data_hub import DataHub
from multi_factor.engine.strategy_config import StrategyConfig
from multi_factor.ifind.factor_metrics import (
    build_factor_report,
    cumulative_nav,
    summarize_ic,
)
from multi_factor.ifind.factor_performance_jobs import FactorPerformanceReader


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

    report = build_factor_report(
        panel,
        hub.returns,
        meta,
        period=cfg.ic_period,
        quantiles=cfg.quantile_groups,
    )

    ic_daily = pd.Series(
        {
            pd.Timestamp(item["date"]): float(item["ic"])
            for item in report.get("ic_trend", [])
            if item.get("ic") is not None
        },
        dtype=float,
    ).sort_index()
    ic_m = monthly_ic(ic_daily)
    ic_m_summary = summarize_ic(ic_m)

    quantile_nav_curves: dict[str, list[dict]] = {}
    for qname, series in (report.get("quantile_returns") or {}).items():
        nav_vals = [x.get("nav") for x in series if x.get("nav") is not None]
        if nav_vals:
            quantile_nav_curves[qname] = series
        else:
            rets = pd.Series(
                {pd.Timestamp(x["date"]): float(x["return"]) for x in series if x.get("return") is not None}
            ).sort_index()
            if not rets.empty:
                nav = cumulative_nav(rets)
                quantile_nav_curves[qname] = [
                    {"date": d.strftime("%Y-%m-%d"), "nav": float(nav.loc[d])} for d in nav.index
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


def _report_from_db(hub: DataHub, code: str, cfg: StrategyConfig) -> dict | None:
    if not hub.ifind.db_url:
        return None
    try:
        reader = FactorPerformanceReader(hub.ifind)
        meta = hub.factor_meta_map().get(code, {"factor_code": code})
        rep = reader.build_report(
            meta,
            hub.cfg.start,
            hub.cfg.end,
            period=cfg.ic_period,
            quantiles=cfg.quantile_groups,
        )
        if not rep:
            return None
        rep["data_source"] = "db"
        if "ic_daily_series" not in rep:
            rep["ic_daily_series"] = rep.get("ic_trend", [])
        return rep
    except Exception:
        return None


def analyze_all_factors(
    hub: DataHub,
    cfg: StrategyConfig,
    prefer_db: bool = True,
) -> dict[str, dict]:
    """对配置中每个因子做分析；优先读 factor_performance_* 落库结果。"""
    out: dict[str, dict] = {}
    for spec in cfg.factors:
        code = spec.code.upper()
        try:
            if prefer_db:
                cached = _report_from_db(hub, code, cfg)
                if cached:
                    out[code] = cached
                    continue
            out[code] = analyze_single_factor(hub, code, cfg)
        except Exception as ex:
            out[code] = {"error": str(ex), "factor_code": code}
    return out
