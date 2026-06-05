# -*- coding: utf-8 -*-
"""调仓日程：每日 / 每周 / 每月第一个交易日。"""

from __future__ import annotations

import pandas as pd

from multi_factor.engine.strategy_config import RebalanceFreq


def rebalance_dates(trading_index: pd.DatetimeIndex, freq: RebalanceFreq) -> pd.DatetimeIndex:
    """返回调仓信号日（收盘生成目标持仓）。"""
    if freq == "daily":
        return trading_index

    s = pd.Series(trading_index, index=trading_index)
    if freq == "weekly":
        iso = s.index.isocalendar()
        weekly = s.groupby([iso.year, iso.week]).first()
        return pd.DatetimeIndex(weekly.values)

    if freq == "monthly":
        monthly = s.groupby([s.index.year, s.index.month]).first()
        return pd.DatetimeIndex(monthly.values)

    raise ValueError(f"未知调仓频率: {freq}")
