# -*- coding: utf-8 -*-
"""截面中性化：市值回归残差、行业组内去均值。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    if s.dropna().empty:
        return s
    lo, hi = s.quantile([lower, upper])
    return s.clip(lo, hi)


def neutralize_market_cap(
    factor: pd.Series,
    cap: pd.Series,
    min_stocks: int = 30,
) -> pd.Series:
    """对 log(市值) 做 OLS，取残差作为市值中性化因子。"""
    df = pd.DataFrame({"f": factor, "cap": cap}).dropna()
    df = df[np.isfinite(df["f"]) & np.isfinite(df["cap"]) & (df["cap"] > 0)]
    if len(df) < min_stocks:
        return factor
    y = _winsorize(df["f"])
    x = np.log(df["cap"])
    x = (x - x.mean()) / (x.std() + 1e-12)
    X = np.column_stack([np.ones(len(x)), x.values])
    beta, _, _, _ = np.linalg.lstsq(X, y.values, rcond=None)
    resid = y - (beta[0] + beta[1] * x)
    out = factor.copy()
    out.loc[resid.index] = resid
    return out


def neutralize_industry(
    factor: pd.Series,
    industry: pd.Series,
    min_stocks: int = 10,
) -> pd.Series:
    """行业组内去均值（行业中性）。"""
    df = pd.DataFrame({"f": factor, "ind": industry}).dropna()
    if len(df) < min_stocks:
        return factor
    out = factor.copy()
    for _, grp in df.groupby("ind"):
        if len(grp) < 3:
            continue
        demean = grp["f"] - grp["f"].mean()
        out.loc[demean.index] = demean
    return out


def neutralize_cross_section(
    factor_row: pd.Series,
    cap_row: pd.Series | None,
    industry_row: pd.Series | None,
    cap_neutral: bool,
    industry_neutral: bool,
) -> pd.Series:
    """单日截面中性化流水线。"""
    s = factor_row.copy()
    if cap_neutral and cap_row is not None:
        s = neutralize_market_cap(s, cap_row)
    if industry_neutral and industry_row is not None:
        s = neutralize_industry(s, industry_row)
    return s


def neutralize_panel(
    factor_panel: pd.DataFrame,
    cap_panel: pd.DataFrame | None,
    industry_panel: pd.DataFrame | None,
    cap_neutral: bool,
    industry_neutral: bool,
) -> pd.DataFrame:
    """逐日截面中性化。"""
    out = factor_panel.copy()
    for dt in factor_panel.index:
        cap_r = cap_panel.loc[dt] if cap_panel is not None and dt in cap_panel.index else None
        ind_r = (
            industry_panel.loc[dt]
            if industry_panel is not None and dt in industry_panel.index
            else None
        )
        out.loc[dt] = neutralize_cross_section(
            factor_panel.loc[dt],
            cap_r,
            ind_r,
            cap_neutral,
            industry_neutral,
        )
    return out
