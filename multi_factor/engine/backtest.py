# -*- coding: utf-8 -*-
"""
A 股本地回测引擎。

规则：
- T+1：信号日收盘确定目标持仓，下一交易日收盘成交；
- 手续费：买入 0.03%，卖出 0.13%（可配置）；
- 滑点：成交额比例；
- 等权 Top-N，每日/周/月调仓；
- 每日收盘调仓逻辑：仅在调仓日发信号，非调仓日持仓不变。
"""

from __future__ import annotations

import pandas as pd

from multi_factor.engine.rebalance import rebalance_dates
from multi_factor.engine.strategy_config import StrategyConfig


def _select_top_n(scores: pd.Series, top_n: int) -> list[str]:
    s = scores.dropna()
    if s.empty:
        return []
    return s.nlargest(top_n).index.tolist()


def _target_weights(
    scores: pd.Series,
    columns: pd.Index,
    top_n: int,
    gross: float,
) -> pd.Series:
    w = pd.Series(0.0, index=columns)
    picks = _select_top_n(scores, top_n)
    if picks:
        w[picks] = gross / len(picks)
    return w


def _trade_cost(
    delta: pd.Series,
    equity: float,
    cfg: StrategyConfig,
) -> tuple[float, float, float]:
    """买卖分开计费 + 滑点。返回 (总成本, 佣金, 滑点)。"""
    buy = delta[delta > 0].sum()
    sell = (-delta[delta < 0]).sum()
    turnover = buy + sell
    if turnover <= 0:
        return 0.0, 0.0, 0.0
    notional = turnover * equity
    slip = notional * cfg.slippage
    comm = buy * equity * cfg.buy_commission + sell * equity * cfg.sell_commission
    if comm < cfg.min_commission and turnover > 0:
        comm = cfg.min_commission
    return slip + comm, comm, slip


def _append_stock_trades(
    stock_trade_rows: list[dict],
    dt,
    signal_date,
    old_weights: pd.Series,
    new_weights: pd.Series,
    equity: float,
    close: pd.DataFrame | None,
) -> None:
    """记录逐股调仓明细：买入/卖出、数量、成交价、调仓后总资产。"""
    symbols = old_weights.index.union(new_weights.index)
    for sym in symbols:
        old_w = float(old_weights.get(sym, 0.0))
        new_w = float(new_weights.get(sym, 0.0))
        delta = new_w - old_w
        if abs(delta) < 1e-8:
            continue
        price = None
        quantity = None
        if close is not None and sym in close.columns and dt in close.index:
            px = close.loc[dt, sym]
            if pd.notna(px) and float(px) > 0:
                price = float(px)
                quantity = abs(delta) * equity / price
        stock_trade_rows.append(
            {
                "trade_date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)[:10],
                "signal_date": (
                    signal_date.strftime("%Y-%m-%d")
                    if signal_date is not None and hasattr(signal_date, "strftime")
                    else (str(signal_date)[:10] if signal_date is not None else None)
                ),
                "action": "buy" if delta > 0 else "sell",
                "stock_code": sym,
                "quantity": quantity,
                "price": price,
                "weight_delta": abs(delta),
                "equity_after": equity,
            }
        )


def run_backtest(
    composite_scores: pd.DataFrame,
    returns: pd.DataFrame,
    cfg: StrategyConfig,
    close: pd.DataFrame | None = None,
) -> dict:
    """
    运行回测。

    Returns
    -------
    dict with keys: summary, nav, portfolio, trades, positions, benchmark_returns
    """
    idx = returns.index
    cols = returns.columns
    rebal_set = set(rebalance_dates(idx, cfg.rebalance_freq))

    cash = cfg.initial_cash
    equity = cash
    weights = pd.Series(0.0, index=cols)
    pending_target: pd.Series | None = None
    signal_date = None

    nav_records = []
    trade_rows = []
    stock_trade_rows: list[dict] = []
    position_rows = []

    for i, dt in enumerate(idx):
        day_ret = returns.loc[dt].fillna(0.0)
        # 当日收益按昨日收盘后持仓计算
        port_ret = float((weights * day_ret).sum())
        equity_before = equity
        equity = equity * (1.0 + port_ret)
        nav = equity / cfg.initial_cash

        # T+1 成交：上一信号日的目标在今日收盘执行
        if cfg.t_plus_one and pending_target is not None:
            delta = pending_target - weights
            turnover = float(delta.abs().sum())
            if turnover > 1e-8:
                cost, comm, slip = _trade_cost(delta, equity, cfg)
                equity -= cost
                nav = equity / cfg.initial_cash
                trade_rows.append(
                    {
                        "date": dt,
                        "signal_date": signal_date,
                        "turnover": turnover,
                        "commission": comm,
                        "slippage": slip,
                        "total_cost": cost,
                        "equity_after": equity,
                    }
                )
                _append_stock_trades(
                    stock_trade_rows, dt, signal_date, weights, pending_target, equity, close
                )
            weights = pending_target.copy()
            pending_target = None
            signal_date = None
        elif not cfg.t_plus_one and pending_target is not None:
            # 同日成交（非 T+1 模式）
            delta = pending_target - weights
            cost, comm, slip = _trade_cost(delta, equity, cfg)
            equity -= cost
            _append_stock_trades(
                stock_trade_rows, dt, signal_date, weights, pending_target, equity, close
            )
            weights = pending_target.copy()
            pending_target = None
            signal_date = None

        # 收盘信号
        if dt in rebal_set and dt in composite_scores.index:
            row = composite_scores.loc[dt]
            target = _target_weights(row, cols, cfg.top_n, cfg.target_gross_exposure)
            if cfg.t_plus_one:
                pending_target = target
                signal_date = dt
            else:
                delta = target - weights
                cost, comm, slip = _trade_cost(delta, equity, cfg)
                equity -= cost
                weights = target
                trade_rows.append(
                    {
                        "date": dt,
                        "signal_date": dt,
                        "turnover": float(delta.abs().sum()),
                        "commission": comm,
                        "slippage": slip,
                        "total_cost": cost,
                        "equity_after": equity,
                    }
                )
                _append_stock_trades(
                    stock_trade_rows, dt, dt, weights, target, equity, close
                )

        nav_records.append(
            {
                "date": dt,
                "equity": equity,
                "unit_net_value": nav,
                "daily_return": port_ret,
                "equity_before": equity_before,
            }
        )
        held = weights[weights > 0]
        for sym, w in held.items():
            position_rows.append(
                {
                    "date": dt,
                    "stock_code": sym,
                    "weight": float(w),
                    "equity": equity,
                }
            )

    portfolio = pd.DataFrame(nav_records).set_index("date")
    trades = pd.DataFrame(trade_rows)
    positions = pd.DataFrame(position_rows)

    return {
        "portfolio": portfolio,
        "nav": portfolio["unit_net_value"],
        "strategy_returns": portfolio["unit_net_value"].pct_change().fillna(0.0),
        "trades": trades,
        "stock_trades": stock_trade_rows,
        "positions": positions,
        "benchmark_returns": None,  # pipeline 填充
    }
