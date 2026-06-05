# -*- coding: utf-8 -*-
"""策略绩效指标：收益、风险、超额、Alpha/Beta。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def max_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    dd = nav / nav.cummax() - 1.0
    return float(dd.min())


def win_rate(returns: pd.Series) -> float | None:
    r = returns.dropna()
    if r.empty:
        return None
    return float((r > 0).sum() / len(r))


def profit_loss_ratio(returns: pd.Series) -> float | None:
    r = returns.dropna()
    gains = r[r > 0]
    losses = r[r < 0]
    if losses.empty or gains.empty:
        return None
    return float(gains.mean() / abs(losses.mean()))


def compute_performance(
    strategy_returns: pd.Series,
    benchmark_returns: pd.Series,
    ann_days: int = 252,
) -> dict[str, Any]:
    """计算完整绩效指标表。"""
    strat = strategy_returns.fillna(0.0)
    bench = benchmark_returns.reindex(strat.index).fillna(0.0)
    excess = strat - bench

    n = max(len(strat), 1)
    nav = (1 + strat).cumprod()
    bench_nav = (1 + bench).cumprod()

    total_ret = float(nav.iloc[-1] - 1.0) if len(nav) else 0.0
    bench_total = float(bench_nav.iloc[-1] - 1.0) if len(bench_nav) else 0.0
    ann_ret = float((1 + total_ret) ** (ann_days / n) - 1) if n else 0.0
    ann_bench = float((1 + bench_total) ** (ann_days / n) - 1) if n else 0.0
    ann_excess = ann_ret - ann_bench

    vol = float(strat.std() * math.sqrt(ann_days)) if len(strat) > 1 else 0.0
    sharpe = float(strat.mean() / strat.std() * math.sqrt(ann_days)) if strat.std() > 0 else None
    mdd = max_drawdown(nav)
    calmar = float(ann_ret / abs(mdd)) if mdd < 0 else None

    # Alpha / Beta（日频回归）
    alpha, beta = None, None
    if len(strat) > 10 and bench.std() > 0:
        cov = np.cov(strat.values, bench.values)
        beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] > 0 else None
        if beta is not None:
            alpha_daily = strat.mean() - beta * bench.mean()
            alpha = float(alpha_daily * ann_days)

    return {
        "total_return": total_ret,
        "annualized_return": ann_ret,
        "benchmark_total_return": bench_total,
        "benchmark_annualized_return": ann_bench,
        "excess_return": total_ret - bench_total,
        "annualized_excess_return": ann_excess,
        "volatility": vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": mdd,
        "calmar_ratio": calmar,
        "win_rate": win_rate(strat),
        "profit_loss_ratio": profit_loss_ratio(strat),
        "information_ratio": float(excess.mean() / excess.std() * math.sqrt(ann_days))
        if excess.std() > 0
        else None,
        "alpha": alpha,
        "beta": beta,
        "trading_days": n,
    }
