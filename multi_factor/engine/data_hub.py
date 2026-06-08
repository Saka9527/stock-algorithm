# -*- coding: utf-8 -*-
"""数据中枢：封装 IFindDataProvider，加载行情、因子、股票池。"""

from __future__ import annotations

import pandas as pd

from multi_factor.engine.strategy_config import UNIVERSE_INDEX_CODES, StrategyConfig
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.provider import IFindDataProvider


class DataHub:
    """统一加载回测所需面板数据。"""

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.ifind = load_ifind_config(cfg.ifind_config_path)
        self.provider = IFindDataProvider(self.ifind)
        self.trading_dates: pd.DatetimeIndex | None = None
        self.close: pd.DataFrame | None = None
        self.returns: pd.DataFrame | None = None
        self.volume: pd.DataFrame | None = None
        self.benchmark_returns: pd.Series | None = None
        self._factor_panels: dict[str, pd.DataFrame] = {}
        self._meta_cache: dict[str, dict] | None = None

    def load_base(self) -> None:
        """加载交易日、收盘价、收益率、成交量、基准。"""
        s, e = self.cfg.start, self.cfg.end
        self.trading_dates = self.provider.get_trading_dates(s, e)
        bundle = self.provider.load_daily_bundle(s, e)
        self.close = bundle.get("close")
        if self.close is None or self.close.empty:
            self.close = self.provider.load_daily_close(s, e)
        self.returns = self.close.pct_change(fill_method=None)
        self.returns = self.returns.reindex(self.trading_dates).fillna(0.0)

        try:
            vol = bundle.get("volume")
            self.volume = vol if vol is not None and not vol.empty else self.provider.load_daily_field("volume", s, e)
            self.volume = self.volume.reindex(self.trading_dates)
        except Exception:
            self.volume = None

        self.benchmark_returns = self.provider.get_benchmark_returns(
            s, e, market_returns=self.returns
        )
        self.benchmark_returns = self.benchmark_returns.reindex(self.trading_dates).fillna(0.0)

    def factor_meta_map(self) -> dict[str, dict]:
        if self._meta_cache is None:
            self._meta_cache = {
                m["factor_code"].upper(): m for m in self.provider.list_factor_base_info()
            }
        return self._meta_cache

    def load_factor(self, factor_code: str) -> pd.DataFrame:
        """加载单因子宽表并对齐到交易日（前向填充）。"""
        code = factor_code.upper()
        if code in self._factor_panels:
            return self._factor_panels[code]

        if code in ("MOMENTUM", "MOMENTUM_20"):
            panel = self._compute_momentum_panel()
            self._factor_panels[code] = panel
            return panel

        s, e = self.cfg.start, self.cfg.end
        panel = self.provider.load_factor_panel_by_code(code, s, e)
        if panel.empty:
            panel = self.provider.load_factor_panel_by_code(code, "20200101", e)
        if panel.empty:
            raise ValueError(f"因子 {code} 无数据")

        panel = self.provider.align_to_trading_days(panel, self.trading_dates)
        panel = panel.reindex(columns=self.close.columns, fill_value=float("nan"))
        self._factor_panels[code] = panel
        return panel

    def load_factors(self, codes: list[str]) -> dict[str, pd.DataFrame]:
        return {c: self.load_factor(c) for c in codes}

    def _compute_momentum_panel(self) -> pd.DataFrame:
        """N 日价格动量（收盘价 pct_change）。"""
        win = int(self.ifind.momentum_window or 20)
        mom = self.close / self.close.shift(win) - 1.0
        return mom.reindex(self.trading_dates)

    def universe_mask(self) -> pd.DataFrame:
        """
        每日可投资标的布尔矩阵（index=日期, columns=股票）。
        all_a：有收盘价；csi300/500/1000：指数成分（DB / BaoStock / 中证官网）。
        """
        mask = self.close.notna().copy()
        pool = self.cfg.universe
        if pool == "all_a":
            return mask

        index_code = UNIVERSE_INDEX_CODES.get(pool, "")
        if not index_code:
            return mask

        from multi_factor.ifind.index_members import load_index_members_mask

        members = load_index_members_mask(
            self.provider,
            index_code,
            self.trading_dates,
            start=self.cfg.start,
            end=self.cfg.end,
        )
        if members is None or members.empty or not members.any().any():
            return mask
        members = members.reindex(columns=mask.columns, fill_value=False)
        return mask & members
