# -*- coding: utf-8 -*-
"""
纯本地月度调仓回测（行情来自 iFinD 表，不依赖 RQDatac / RQAlpha Bundle）。

用于 --source ifind --local-backtest。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from multi_factor import config as project_config
from multi_factor.ifind.provider import IFindDataProvider


def _monthly_rebalance_dates(trading_index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(trading_index, index=trading_index)
    monthly = s.groupby([s.index.year, s.index.month]).first()
    return pd.DatetimeIndex(monthly.values)


def run_local_backtest(
    provider: IFindDataProvider,
    factor_scores: pd.DataFrame,
    start: str,
    end: str,
    top_n: int | None = None,
    initial_cash: float | None = None,
    slippage: float | None = None,
    commission_rate: float = 0.0008,
    min_commission: float = 5.0,
) -> dict:
    top_n = top_n or project_config.TOP_N
    initial_cash = initial_cash or project_config.INITIAL_CASH
    slippage = slippage if slippage is not None else project_config.SLIPPAGE
    commission_rate *= project_config.COMMISSION_MULTIPLIER

    returns = provider.get_daily_returns(start, end)
    bench = provider.get_benchmark_returns(start, end)
    scores = factor_scores.reindex(index=returns.index).ffill()
    rebalance_days = set(_monthly_rebalance_dates(scores.dropna(how="all").index))

    nav = 1.0
    equity = initial_cash
    weights = pd.Series(0.0, index=returns.columns)
    nav_records = []
    trades = []

    for dt in returns.index:
        if dt in rebalance_days and dt in scores.index:
            row = scores.loc[dt].dropna()
            targets = row.nlargest(top_n).index.tolist()
            new_w = pd.Series(0.0, index=returns.columns)
            if targets:
                w = project_config.TARGET_GROSS_EXPOSURE / len(targets)
                new_w[targets] = w
            turnover = (new_w - weights).abs().sum()
            trade_cost = 0.0
            if turnover > 0:
                traded_notional = turnover * equity
                slip_cost = traded_notional * slippage
                comm = max(traded_notional * commission_rate, min_commission) * max(
                    int(turnover > 0.01), 1
                )
                trade_cost = slip_cost + comm
                equity -= trade_cost
                trades.append(
                    {
                        "date": dt,
                        "turnover": turnover,
                        "slippage_cost": slip_cost,
                        "commission": comm,
                    }
                )
            weights = new_w

        day_ret = float((weights * returns.loc[dt].fillna(0.0)).sum())
        nav *= 1.0 + day_ret
        equity = initial_cash * nav
        nav_records.append({"date": dt, "equity": equity, "unit_net_value": nav})

    equity_df = pd.DataFrame(nav_records).set_index("date")
    strat_ret = equity_df["unit_net_value"].pct_change().fillna(0.0)
    bench = bench.reindex(strat_ret.index).fillna(0.0)
    excess = strat_ret - bench

    ann = 252
    total_ret = float(nav - 1.0)
    n = max(len(strat_ret), 1)
    ann_ret = float((1 + total_ret) ** (ann / n) - 1)
    vol = float(strat_ret.std() * np.sqrt(ann)) if len(strat_ret) > 1 else 0.0
    sharpe = (
        float(strat_ret.mean() / strat_ret.std() * np.sqrt(ann))
        if strat_ret.std() > 0
        else 0.0
    )
    dd = float((equity_df["unit_net_value"] / equity_df["unit_net_value"].cummax() - 1).min())

    summary = {
        "total_returns": total_ret,
        "annualized_returns": ann_ret,
        "benchmark_total_returns": float((1 + bench).prod() - 1.0),
        "sharpe": sharpe,
        "max_drawdown": dd,
        "volatility": vol,
        "information_ratio": float(excess.mean() / excess.std() * np.sqrt(ann))
        if excess.std() > 0
        else 0.0,
    }

    return {
        "summary": summary,
        "portfolio": equity_df,
        "nav": equity_df["unit_net_value"],
        "trades": pd.DataFrame(trades),
        "benchmark_returns": bench,
    }


def save_local_backtest_report(result: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    result["portfolio"].to_csv(output_dir / "portfolio.csv", encoding="utf-8-sig")
    if not result["trades"].empty:
        result["trades"].to_csv(output_dir / "trades.csv", encoding="utf-8-sig", index=False)
    pd.DataFrame([result["summary"]]).T.to_csv(
        output_dir / "summary.csv", encoding="utf-8-sig", header=["value"]
    )
    try:
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(result["nav"].index, result["nav"].values, label="策略")
        bench_nav = (1 + result["benchmark_returns"]).cumprod()
        ax.plot(bench_nav.index, bench_nav.values, label="基准", alpha=0.85)
        ax.legend()
        ax.set_title("iFinD 本地回测净值")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(output_dir / "nav_vs_benchmark.png", dpi=120)
        plt.close(fig)
    except Exception:
        pass
    return output_dir
