# -*- coding: utf-8 -*-
"""多因子截面标准化、加权合成综合得分。"""

from __future__ import annotations

import pandas as pd

from multi_factor.engine.data_hub import DataHub
from multi_factor.engine.filters import build_tradeable_mask
from multi_factor.engine.neutralize import neutralize_panel
from multi_factor.engine.strategy_config import FactorSpec, StrategyConfig
from multi_factor.ifind.factor_metrics import _sort_ascending


def _cross_section_rank(s: pd.Series, ascending: bool) -> pd.Series:
    """截面百分位排名 [0,1]，越大越好。"""
    valid = s.dropna()
    if valid.empty:
        return s
    ranks = valid.rank(ascending=ascending, pct=True, method="average")
    out = pd.Series(index=s.index, dtype=float)
    out.loc[ranks.index] = ranks
    return out


def build_composite_scores(
    hub: DataHub,
    cfg: StrategyConfig,
    factors: list[FactorSpec] | None = None,
) -> pd.DataFrame:
    """
    合成综合因子得分（index=交易日, columns=股票）。
    流程：加载因子 -> 中性化 -> 截面 rank -> 加权求和 -> 不可交易置 NaN。
    """
    factors = factors or cfg.factors
    if not factors:
        raise ValueError("至少配置一个因子")

    weights = cfg.normalized_factor_weights()
    meta_map = hub.factor_meta_map()

    cap_panel = None
    industry_panel = None
    if cfg.cap_neutral:
        try:
            cap_panel = hub.load_factor(cfg.cap_factor_code)
        except ValueError:
            cap_panel = None
    if cfg.industry_neutral:
        try:
            industry_panel = hub.load_factor(cfg.industry_factor_code)
        except ValueError:
            industry_panel = None

    tradeable = build_tradeable_mask(
        hub.close,
        hub.returns,
        hub.volume,
        hub.universe_mask(),
        cfg,
    )

    composite = pd.DataFrame(0.0, index=hub.trading_dates, columns=hub.close.columns)
    composite[:] = float("nan")

    for spec in factors:
        code = spec.code.upper()
        w = weights[code]
        panel = hub.load_factor(code)
        meta = meta_map.get(code, {})
        ascending = spec.ascending
        if ascending is None:
            ascending = _sort_ascending(meta.get("sort_type"))

        if cfg.cap_neutral or cfg.industry_neutral:
            panel = neutralize_panel(
                panel,
                cap_panel,
                industry_panel,
                cfg.cap_neutral and cap_panel is not None,
                cfg.industry_neutral and industry_panel is not None,
            )

        ranked = panel.apply(lambda row: _cross_section_rank(row, ascending), axis=1)
        composite = composite.add(ranked * w, fill_value=0.0)

    composite = composite.where(tradeable)
    return composite
