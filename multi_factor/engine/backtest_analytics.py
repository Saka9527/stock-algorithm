# -*- coding: utf-8 -*-
"""回测衍生分析：月度热力图、持仓盈亏、因子归因、净值序列。"""

from __future__ import annotations

from typing import Any

import pandas as pd

from multi_factor.engine.strategy_config import StrategyConfig


def _sql_date(s) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def monthly_heatmap_eligible(start: str, end: str) -> tuple[bool, str]:
    """
    判断月度热力图是否可展示。

    回测区间不足一个自然月时不展示，返回说明文案。
    """
    s = pd.Timestamp(_sql_date(start))
    e = pd.Timestamp(_sql_date(end))
    if (e.year, e.month) == (s.year, s.month):
        return False, "回测区间不足一个月，无法展示月度收益热力图"
    if e < s:
        return False, "回测区间无效，无法展示月度收益热力图"
    return True, ""


def compute_monthly_returns(strategy_returns: pd.Series) -> list[dict[str, Any]]:
    """按月聚合策略收益，供热力图使用。"""
    r = strategy_returns.copy()
    r.index = pd.to_datetime(r.index)
    monthly = r.groupby([r.index.year, r.index.month]).apply(lambda x: (1 + x).prod() - 1)
    rows = []
    for (y, m), v in monthly.items():
        rows.append({"year": int(y), "month": int(m), "return_pct": float(v)})
    return rows


def build_nav_series(
    portfolio: pd.DataFrame,
    benchmark_returns: pd.Series,
    initial_cash: float,
) -> list[dict[str, Any]]:
    """构建净值曲线：策略总资产/净值、基准净值、超额净值。"""
    bench = benchmark_returns.reindex(portfolio.index).fillna(0.0)
    bench_nav = (1 + bench).cumprod()
    strat_nav = portfolio["unit_net_value"]
    bench_aligned = bench_nav.reindex(strat_nav.index).ffill().fillna(1.0)
    excess_nav = strat_nav - bench_aligned

    rows = []
    for dt in portfolio.index:
        rows.append(
            {
                "trade_date": dt.strftime("%Y-%m-%d"),
                "strategy_equity": float(portfolio.loc[dt, "equity"]),
                "strategy_nav": float(strat_nav.loc[dt]),
                "benchmark_nav": float(bench_nav.loc[dt]) if dt in bench_nav.index else None,
                "excess_nav": float(excess_nav.loc[dt]),
                "daily_return": float(portfolio.loc[dt, "daily_return"]),
            }
        )
    return rows


def compute_holding_pnl_top10(
    positions: pd.DataFrame,
    returns: pd.DataFrame,
    portfolio: pd.DataFrame,
    top_k: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """
    持仓分析：回测历史中盈利/亏损 Top10 股票（非当前实盘持仓）。

    按各股票在持仓期间的累计盈亏金额排序。
    """
    if positions.empty:
        return {"profit_top10": [], "loss_top10": []}

    pos = positions.copy()
    pos["date"] = pd.to_datetime(pos["date"])
    port = portfolio.copy()
    port.index = pd.to_datetime(port.index)
    rets = returns.copy()
    rets.index = pd.to_datetime(rets.index)

    pnl_records = []
    for _, row in pos.iterrows():
        dt = row["date"]
        sym = row["stock_code"]
        w = float(row["weight"])
        if dt not in port.index or sym not in rets.columns:
            continue
        eq_before = float(port.loc[dt, "equity_before"])
        day_ret = float(rets.loc[dt, sym]) if pd.notna(rets.loc[dt, sym]) else 0.0
        pnl = eq_before * w * day_ret
        pnl_records.append({"stock_code": sym, "pnl": pnl, "ret_contrib": w * day_ret})

    if not pnl_records:
        return {"profit_top10": [], "loss_top10": []}

    agg = (
        pd.DataFrame(pnl_records)
        .groupby("stock_code", as_index=False)
        .agg(total_pnl=("pnl", "sum"), total_return=("ret_contrib", "sum"))
    )

    profit = agg[agg["total_pnl"] > 0].nlargest(top_k, "total_pnl")
    loss = agg[agg["total_pnl"] < 0].nsmallest(top_k, "total_pnl")

    def _fmt(df: pd.DataFrame, rank_type: str) -> list[dict]:
        rows = []
        for i, (_, r) in enumerate(df.iterrows(), 1):
            rows.append(
                {
                    "stock_code": r["stock_code"],
                    "total_pnl": float(r["total_pnl"]),
                    "total_return": float(r["total_return"]),
                    "rank_type": rank_type,
                    "rank_num": i,
                }
            )
        return rows

    return {
        "profit_top10": _fmt(profit, "profit"),
        "loss_top10": _fmt(loss, "loss"),
    }


def compute_factor_attribution(
    factor_analyses: dict[str, Any],
    cfg: StrategyConfig,
    strategy_total_return: float,
) -> list[dict[str, Any]]:
    """
    因子归因：各因子对策略收益的贡献占比。

    基于因子配置权重 × 单因子 Top 组累计收益，归一化为贡献占比。
    """
    weights = cfg.normalized_factor_weights()
    if not weights:
        return []

    raw: dict[str, float] = {}
    for code, w in weights.items():
        rep = factor_analyses.get(code.upper(), factor_analyses.get(code, {}))
        if not isinstance(rep, dict) or "error" in rep:
            raw[code] = 0.0
            continue
        summ = rep.get("summary", {})
        group = rep.get("group_returns") or []
        top_ret = 0.0
        if group:
            nav_vals = [g.get("top_group_nav") for g in group if g.get("top_group_nav") is not None]
            if nav_vals:
                top_ret = float(nav_vals[-1]) - 1.0
        if top_ret == 0.0:
            top_ret = float(summ.get("sharpe_long_short") or summ.get("ic_mean") or 0.0)
        raw[code] = w * top_ret

    total_abs = sum(abs(v) for v in raw.values())
    if total_abs <= 0:
        n = len(raw) or 1
        return [
            {
                "factor_code": code,
                "factor_weight": float(weights.get(code, 0)),
                "contribution_pct": 1.0 / n,
                "contribution_ret": 0.0,
            }
            for code in raw
        ]

    rows = []
    for code, v in raw.items():
        pct = abs(v) / total_abs
        rows.append(
            {
                "factor_code": code,
                "factor_weight": float(weights.get(code, 0)),
                "contribution_pct": float(pct),
                "contribution_ret": float(v),
            }
        )
    rows.sort(key=lambda x: x["contribution_pct"], reverse=True)
    return rows


def return_overview_from_perf(perf: dict[str, Any]) -> dict[str, Any]:
    """收益概览：累计收益、年化收益、超额收益、Alpha、Beta。"""
    return {
        "total_return": perf.get("total_return"),
        "annualized_return": perf.get("annualized_return"),
        "excess_return": perf.get("excess_return"),
        "annualized_excess_return": perf.get("annualized_excess_return"),
        "alpha": perf.get("alpha"),
        "beta": perf.get("beta"),
    }


def risk_metrics_from_perf(perf: dict[str, Any]) -> dict[str, Any]:
    """风险指标：最大回撤、夏普、卡玛、胜率、盈亏比。"""
    return {
        "max_drawdown": perf.get("max_drawdown"),
        "sharpe_ratio": perf.get("sharpe_ratio"),
        "calmar_ratio": perf.get("calmar_ratio"),
        "win_rate": perf.get("win_rate"),
        "profit_loss_ratio": perf.get("profit_loss_ratio"),
        "volatility": perf.get("volatility"),
        "information_ratio": perf.get("information_ratio"),
    }
