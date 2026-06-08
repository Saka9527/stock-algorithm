# -*- coding: utf-8 -*-
"""因子市场：factor_base_info + factor_data_wide + factor_performance_* 对外查询。"""

from __future__ import annotations

from typing import Any

from multi_factor import config as project_config
from multi_factor.cache.redis_cache import get_cache
from multi_factor.ifind.config_loader import IFindConfig, load_ifind_config
from multi_factor.ifind.factor_metrics import build_factor_report
from multi_factor.ifind.factor_performance_jobs import (
    FactorPerformanceJobRunner,
    FactorPerformanceReader,
)
from multi_factor.ifind.provider import IFindDataProvider

def _cache_key(
    factor_code: str, start: str, end: str, period: int, quantiles: int, top_pct: float
) -> str:
    return f"fp:report:{factor_code}|{start}|{end}|p{period}|q{quantiles}|t{top_pct}"


def _get_cached_report(key: str, ttl: int = 3600) -> dict | None:
    return get_cache().get_json(key)


def _set_cached_report(key: str, data: dict, ttl: int) -> None:
    get_cache().set_json(key, data, ttl=ttl)


class FactorMarketService:
    def __init__(self, config_path: str | None = None):
        self.cfg: IFindConfig = load_ifind_config(config_path)
        self.provider = IFindDataProvider(self.cfg)
        self._perf_reader: FactorPerformanceReader | None = None
        self._perf_runner: FactorPerformanceJobRunner | None = None

    def _get_perf_reader(self) -> FactorPerformanceReader | None:
        if not self.cfg.db_url:
            return None
        if self._perf_reader is None:
            try:
                self._perf_reader = FactorPerformanceReader(self.cfg)
            except Exception:
                return None
        return self._perf_reader

    def _get_perf_runner(self) -> FactorPerformanceJobRunner | None:
        if not self.cfg.db_url:
            return None
        if self._perf_runner is None:
            try:
                self._perf_runner = FactorPerformanceJobRunner(self.cfg)
            except Exception:
                return None
        return self._perf_runner

    def list_factors(self) -> list[dict]:
        return self.provider.list_factor_base_info()

    def _default_dates(self, start: str | None, end: str | None) -> tuple[str, str]:
        if start and end:
            return start.replace("-", "")[:8], end.replace("-", "")[:8]
        s, e = self.provider.query_data_date_range()
        if start:
            s = start.replace("-", "")[:8]
        if end:
            e = end.replace("-", "")[:8]
        return s, e

    def _compute_factor_report(
        self,
        factor_code: str,
        meta: dict,
        start: str,
        end: str,
        period: int,
        quantiles: int,
        top_pct: float,
    ) -> dict[str, Any]:
        returns = self.provider.get_daily_returns(start, end)
        if returns.empty:
            raise ValueError(f"行情区间 {start}-{end} 无日频数据")

        panel = self.provider.load_factor_panel_by_code(factor_code, start, end)
        if panel.empty:
            panel = self.provider.load_factor_panel_by_code(factor_code, "20200101", end)
        if panel.empty:
            raise ValueError(f"因子 {factor_code} 在 {start}-{end} 无数据")

        panel = self.provider.align_to_trading_days(panel, returns.index)
        common_cols = panel.columns.intersection(returns.columns)
        panel = panel[common_cols]
        returns = returns[common_cols]
        if panel.notna().sum().sum() == 0:
            raise ValueError(f"因子 {factor_code} 对齐后仍无有效截面")

        report = build_factor_report(
            panel,
            returns,
            meta,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
        )
        report["data_source"] = "compute"
        return report

    def get_factor_report(
        self,
        factor_code: str,
        start: str | None = None,
        end: str | None = None,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
        use_cache: bool = True,
        prefer_db: bool = True,
        persist_on_compute: bool = False,
    ) -> dict[str, Any]:
        """
        获取单因子完整报告。

        prefer_db=True 时优先读 factor_performance_summary/series；
        无记录则实时计算，persist_on_compute=True 时写回统计表。
        """
        start, end = self._default_dates(start, end)
        code = factor_code.upper()
        ttl = self.cfg.performance.cache_ttl_report
        key = _cache_key(code, start, end, period, quantiles, top_pct)
        if use_cache:
            cached = _get_cached_report(key, ttl)
            if cached:
                return cached

        meta = self.provider.get_factor_meta(code)
        if not meta:
            raise KeyError(f"因子不存在: {factor_code}")

        report: dict[str, Any] | None = None
        if prefer_db:
            reader = self._get_perf_reader()
            if reader:
                try:
                    report = reader.build_report(
                        meta, start, end, period=period, quantiles=quantiles, top_pct=top_pct
                    )
                except Exception:
                    report = None

        if report is None:
            if prefer_db and not persist_on_compute:
                raise ValueError(
                    "未命中 factor_performance_* 统计表数据，已跳过实时重算以避免长耗时。"
                    "如需实时计算请设置 prefer_db=false，或设置 persist_on_compute=true 回填后再查。"
                )
            report = self._compute_factor_report(
                code, meta, start, end, period, quantiles, top_pct
            )
            if persist_on_compute:
                runner = self._get_perf_runner()
                if runner:
                    try:
                        runner.storage.ensure_tables()
                        runner.storage.upsert_summary(
                            code, start, end, period, quantiles, top_pct, report["summary"]
                        )
                        rows = runner._series_rows(code, start, end, period, report)
                        runner.storage.upsert_series_rows(rows)
                        report["data_source"] = "compute+persist"
                    except Exception:
                        pass

        if use_cache:
            _set_cached_report(key, report, ttl)
        return report

    def list_factors_with_summary(
        self,
        start: str | None = None,
        end: str | None = None,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
        prefer_db: bool = True,
        persist_on_compute: bool = False,
    ) -> list[dict]:
        """因子市场列表页：基础信息 + IC均值 + 夏普（多空）。"""
        start, end = self._default_dates(start, end)
        db_map: dict[str, dict] = {}
        if prefer_db:
            reader = self._get_perf_reader()
            if reader:
                try:
                    db_map = reader.list_summaries(
                        start, end, period=period, quantiles=quantiles, top_pct=top_pct
                    )
                except Exception:
                    db_map = {}

        items = []
        for meta in self.list_factors():
            code = meta["factor_code"].upper()
            if code in db_map:
                s = db_map[code]
                items.append(
                    {
                        **meta,
                        "ic_mean": s.get("ic_mean"),
                        "ic_win_rate": s.get("win_rate"),
                        "sharpe_ratio": s.get("sharpe_long_short") or s.get("sharpe_top_group"),
                        "data_start": s.get("data_start"),
                        "data_end": s.get("data_end"),
                        "data_source": "db",
                    }
                )
                continue
            if prefer_db and not persist_on_compute:
                items.append(
                    {
                        **meta,
                        "ic_mean": None,
                        "ic_win_rate": None,
                        "sharpe_ratio": None,
                        "data_start": None,
                        "data_end": None,
                        "data_source": "db_miss",
                        "error": "未命中 factor_performance_*，请先离线预热统计表",
                    }
                )
                continue
            try:
                rep = self.get_factor_report(
                    code,
                    start=start,
                    end=end,
                    period=period,
                    quantiles=quantiles,
                    top_pct=top_pct,
                    use_cache=True,
                    prefer_db=False,
                    persist_on_compute=persist_on_compute,
                )
                s = rep["summary"]
                items.append(
                    {
                        **meta,
                        "ic_mean": s.get("ic_mean"),
                        "ic_win_rate": s.get("win_rate"),
                        "sharpe_ratio": s.get("sharpe_long_short")
                        or s.get("sharpe_top_group"),
                        "data_start": s.get("data_start"),
                        "data_end": s.get("data_end"),
                        "data_source": rep.get("data_source", "compute"),
                    }
                )
            except Exception as ex:
                items.append({**meta, "error": str(ex)})
        return items


_default_service: FactorMarketService | None = None
_default_config_path: str | None = None


def get_factor_market_service(config_path: str | None = None) -> FactorMarketService:
    global _default_service, _default_config_path
    path = config_path or str(project_config.IFIND_CONFIG_PATH)
    if _default_service is None or _default_config_path != path:
        _default_service = FactorMarketService(path)
        _default_config_path = path
    return _default_service
