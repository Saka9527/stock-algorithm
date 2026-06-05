# -*- coding: utf-8 -*-
"""可交易性过滤：ST、停牌、新股、涨跌停。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from multi_factor.engine.strategy_config import StrategyConfig


def _is_st_code(code: str) -> bool:
    """无 ST 标记表时：代码段无法可靠识别，仅作占位（可扩展名称表）。"""
    return False


def build_tradeable_mask(
    close: pd.DataFrame,
    returns: pd.DataFrame,
    volume: pd.DataFrame | None,
    universe: pd.DataFrame,
    cfg: StrategyConfig,
) -> pd.DataFrame:
    """
    返回与 close 同形的 bool 矩阵，True 表示当日可交易（可买入/持有）。
    涨跌停：当日收益绝对值 >= limit_threshold 视为无法买入（卖出可保留，此处统一剔除）。
    """
    mask = universe.copy() & close.notna()

    if cfg.exclude_suspended and volume is not None:
        # 成交量为 0 或缺失视为停牌
        suspended = volume.fillna(0) <= 0
        mask &= ~suspended

    if cfg.exclude_st:
        st_cols = [c for c in close.columns if _is_st_code(str(c))]
        if st_cols:
            mask[st_cols] = False

    if cfg.exclude_new_days > 0:
        # 以首次出现在 close 非空为上市日
        first_seen = close.apply(lambda s: s.first_valid_index())
        for col in close.columns:
            fd = first_seen[col]
            if pd.isna(fd):
                continue
            min_date = fd + pd.Timedelta(days=cfg.exclude_new_days)
            mask.loc[mask.index < min_date, col] = False

    if cfg.exclude_limit:
        prev = close.shift(1)
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = close / prev - 1.0
        limit_up = pct >= cfg.limit_threshold
        limit_down = pct <= -cfg.limit_threshold
        # 涨停不可买、跌停不可卖；选股阶段统一剔除
        mask &= ~(limit_up | limit_down)

    return mask.fillna(False)
