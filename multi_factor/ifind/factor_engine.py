# -*- coding: utf-8 -*-
"""基于 iFinD 本地表计算 PE/PB/ROE/动量及合成因子（不依赖 RQDatac / execute_factor）。"""

from __future__ import annotations

import pandas as pd

from multi_factor import config as project_config
from multi_factor.ifind.provider import IFindDataProvider


def _cross_section_rank(panel: pd.DataFrame, ascending: bool = True) -> pd.DataFrame:
    return panel.rank(axis=1, ascending=ascending, pct=True)


def compute_raw_factors(
    provider: IFindDataProvider,
    universe: list[str],
    start: str,
    end: str,
    momentum_window: int | None = None,
) -> dict[str, pd.DataFrame]:
    """返回原始因子宽表（未排名）。"""
    momentum_window = momentum_window or provider.cfg.momentum_window
    trading_days = pd.DatetimeIndex(provider.get_trading_dates(start, end))

    close = provider.load_daily_field("close", start, end).reindex(columns=universe)
    mom = close / close.shift(momentum_window) - 1.0

    pe = provider.align_to_trading_days(
        provider.load_fundamental_field("pe", start, end).reindex(columns=universe),
        trading_days,
    )
    pb = provider.align_to_trading_days(
        provider.load_fundamental_field("pb", start, end).reindex(columns=universe),
        trading_days,
    )
    roe = provider.align_to_trading_days(
        provider.load_fundamental_field("roe", start, end).reindex(columns=universe),
        trading_days,
    )

    return {"pe": pe, "pb": pb, "roe": roe, "momentum": mom}


def compute_ranked_factors(
    provider: IFindDataProvider,
    universe: list[str],
    start: str,
    end: str,
    weights: dict | None = None,
) -> dict[str, pd.DataFrame]:
    """返回排名后的单因子及合成因子（越大越好）。"""
    weights = weights or project_config.FACTOR_WEIGHTS
    raw = compute_raw_factors(provider, universe, start, end)

    ranked = {
        "pe": _cross_section_rank(-raw["pe"], ascending=True),
        "pb": _cross_section_rank(-raw["pb"], ascending=True),
        "roe": _cross_section_rank(raw["roe"], ascending=True),
        "momentum": _cross_section_rank(raw["momentum"], ascending=True),
    }

    total_w = sum(weights.values())
    composite = None
    for name, w in weights.items():
        if w <= 0 or name not in ranked:
            continue
        term = (w / total_w) * ranked[name]
        composite = term if composite is None else composite + term
    ranked["composite"] = composite
    return ranked


def precompute_composite_scores(
    provider: IFindDataProvider,
    start: str,
    end: str,
    weights: dict | None,
    save_path,
) -> pd.DataFrame:
    from pathlib import Path

    universe = provider.get_universe(start, end)
    ranked = compute_ranked_factors(provider, universe, start, end, weights)
    df = ranked["composite"]
    path = Path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(path)
    return df
