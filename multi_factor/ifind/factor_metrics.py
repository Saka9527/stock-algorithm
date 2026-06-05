# -*- coding: utf-8 -*-
"""基于 factor_data_wide 计算 IC、分组收益、夏普等指标（不依赖 RQDatac）。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from multi_factor.service.serializers import _sanitize_scalar


def _sort_ascending(sort_type: str | None) -> bool:
    """sort_type=desc 表示因子值越大越好，与 RQFactor ascending 一致。"""
    return (sort_type or "desc").lower() != "asc"


def compute_rank_ic_series(
    factor: pd.DataFrame,
    returns: pd.DataFrame,
    period: int = 1,
    ascending: bool = True,
    min_stocks: int = 30,
) -> pd.Series:
    """逐日截面 Spearman IC（秩相关）。"""
    fwd = returns.shift(-period)
    ic_vals = {}
    for dt in factor.index:
        if dt not in fwd.index:
            continue
        fv = factor.loc[dt].dropna()
        rv = fwd.loc[dt].reindex(fv.index)
        mask = fv.notna() & rv.notna() & np.isfinite(fv) & np.isfinite(rv)
        if mask.sum() < min_stocks:
            continue
        f_rank = fv[mask].rank(ascending=ascending)
        ic_vals[dt] = f_rank.corr(rv[mask], method="spearman")
    return pd.Series(ic_vals).sort_index()


def summarize_ic(ic: pd.Series) -> dict[str, Any]:
    ic = ic.dropna()
    if ic.empty:
        return {
            "ic_mean": None,
            "ic_std": None,
            "ic_ir": None,
            "win_rate": None,
            "positive_count": 0,
            "negative_count": 0,
            "total_count": 0,
        }
    pos = (ic > 0).sum()
    neg = (ic < 0).sum()
    mean = float(ic.mean())
    std = float(ic.std())
    return {
        "ic_mean": _sanitize_scalar(mean),
        "ic_std": _sanitize_scalar(std),
        "ic_ir": _sanitize_scalar(mean / std) if std and std > 0 else None,
        "win_rate": _sanitize_scalar(pos / len(ic)),
        "positive_count": int(pos),
        "negative_count": int(neg),
        "total_count": int(len(ic)),
    }


def compute_quantile_returns(
    factor: pd.DataFrame,
    returns: pd.DataFrame,
    period: int = 1,
    ascending: bool = True,
    quantiles: int = 5,
    min_stocks: int = 50,
) -> pd.DataFrame:
    """各分位组平均前瞻收益（日频），列 q1..qN。"""
    fwd = returns.shift(-period)
    rows = []
    for dt in factor.index:
        if dt not in fwd.index:
            continue
        fv = factor.loc[dt].dropna()
        rv = fwd.loc[dt].reindex(fv.index)
        valid = fv.notna() & rv.notna() & np.isfinite(fv) & np.isfinite(rv)
        fv, rv = fv[valid], rv[valid]
        if len(fv) < min_stocks:
            continue
        ranks = fv.rank(ascending=ascending, method="first")
        try:
            groups = pd.qcut(ranks, quantiles, labels=False, duplicates="drop")
        except ValueError:
            continue
        row = {"date": dt}
        for g in range(quantiles):
            row[f"q{g + 1}"] = float(rv[groups == g].mean())
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df


def compute_top_bottom_returns(
    factor: pd.DataFrame,
    returns: pd.DataFrame,
    period: int = 1,
    ascending: bool = True,
    top_pct: float = 0.2,
    bottom_pct: float = 0.2,
    min_stocks: int = 50,
) -> pd.DataFrame:
    """Top / Bottom 分组日收益（用于 UI 分组收益曲线）。"""
    fwd = returns.shift(-period)
    rows = []
    for dt in factor.index:
        if dt not in fwd.index:
            continue
        fv = factor.loc[dt].dropna()
        rv = fwd.loc[dt].reindex(fv.index)
        valid = fv.notna() & rv.notna() & np.isfinite(fv) & np.isfinite(rv)
        fv, rv = fv[valid], rv[valid]
        if len(fv) < min_stocks:
            continue
        ranks = fv.rank(ascending=ascending, pct=True)
        top_ret = float(rv[ranks >= (1 - top_pct)].mean())
        bottom_ret = float(rv[ranks <= bottom_pct].mean())
        rows.append(
            {
                "date": dt,
                "top_group": top_ret,
                "bottom_group": bottom_ret,
                "long_short": top_ret - bottom_ret,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date").sort_index()


def cumulative_nav(daily_returns: pd.Series) -> pd.Series:
    return (1 + daily_returns.fillna(0)).cumprod()


def sharpe_ratio(daily_returns: pd.Series, ann: int = 252) -> float | None:
    r = daily_returns.dropna()
    if len(r) < 2 or r.std() == 0:
        return None
    return float(r.mean() / r.std() * math.sqrt(ann))


def build_factor_report(
    factor_panel: pd.DataFrame,
    returns: pd.DataFrame,
    meta: dict,
    period: int = 1,
    quantiles: int = 5,
    top_pct: float = 0.2,
) -> dict[str, Any]:
    ascending = _sort_ascending(meta.get("sort_type"))
    ic_series = compute_rank_ic_series(
        factor_panel, returns, period=period, ascending=ascending
    )
    ic_summary = summarize_ic(ic_series)
    tb = compute_top_bottom_returns(
        factor_panel, returns, period=period, ascending=ascending, top_pct=top_pct
    )
    qret = compute_quantile_returns(
        factor_panel, returns, period=period, ascending=ascending, quantiles=quantiles
    )

    top_nav = cumulative_nav(tb["top_group"]) if not tb.empty else pd.Series(dtype=float)
    bottom_nav = cumulative_nav(tb["bottom_group"]) if not tb.empty else pd.Series(dtype=float)
    ls_nav = cumulative_nav(tb["long_short"]) if not tb.empty else pd.Series(dtype=float)

    ic_trend = [
        {"date": d.strftime("%Y-%m-%d"), "ic": _sanitize_scalar(v)}
        for d, v in ic_series.items()
    ]
    group_returns = [
        {
            "date": d.strftime("%Y-%m-%d"),
            "top_group": _sanitize_scalar(row["top_group"]),
            "bottom_group": _sanitize_scalar(row["bottom_group"]),
            "top_group_nav": _sanitize_scalar(top_nav.get(d)),
            "bottom_group_nav": _sanitize_scalar(bottom_nav.get(d)),
        }
        for d, row in tb.iterrows()
    ]
    quantile_nav = {}
    if not qret.empty:
        for col in qret.columns:
            quantile_nav[col] = [
                {
                    "date": d.strftime("%Y-%m-%d"),
                    "return": _sanitize_scalar(qret.loc[d, col]),
                    "nav": _sanitize_scalar(cumulative_nav(qret[col]).get(d)),
                }
                for d in qret.index
            ]

    return {
        "meta": meta,
        "period": period,
        "ascending": ascending,
        "summary": {
            **ic_summary,
            "sharpe_top_group": _sanitize_scalar(sharpe_ratio(tb["top_group"]))
            if not tb.empty
            else None,
            "sharpe_bottom_group": _sanitize_scalar(sharpe_ratio(tb["bottom_group"]))
            if not tb.empty
            else None,
            "sharpe_long_short": _sanitize_scalar(sharpe_ratio(tb["long_short"]))
            if not tb.empty
            else None,
            "data_start": factor_panel.index.min().strftime("%Y-%m-%d")
            if len(factor_panel)
            else None,
            "data_end": factor_panel.index.max().strftime("%Y-%m-%d")
            if len(factor_panel)
            else None,
            "stock_count_avg": int(factor_panel.notna().sum(axis=1).mean())
            if len(factor_panel)
            else 0,
        },
        "ic_trend": ic_trend,
        "group_returns": group_returns,
        "quantile_returns": quantile_nav,
    }
