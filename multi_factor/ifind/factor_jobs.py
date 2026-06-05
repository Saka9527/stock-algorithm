# -*- coding: utf-8 -*-
"""可复用因子生成任务：Python 计算 + 数据库落表。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Protocol

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from multi_factor.data_utils import init_rqdatac
from multi_factor.ifind.code_convert import normalize_code, rq_to_ths
from multi_factor.ifind.config_loader import IFindConfig
from multi_factor.ifind.factor_wide import (
    long_rows_to_wide_records,
    storage_stock_code,
    upsert_wide_records,
    wide_column_for_factor,
)
from multi_factor.ifind.provider import IFindDataProvider


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


class FactorCalculator(Protocol):
    """因子计算器接口。"""

    factor_code: str
    factor_name: str
    factor_type: str
    factor_desc: str
    sort_type: str

    def compute(
        self,
        provider: IFindDataProvider,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        返回长表:
        columns = [stock_code, data_date, factor_value]
        """


@dataclass
class MomentumNCalculator:
    """N 日动量因子。"""

    window: int = 20
    factor_code: str = "MOMENTUM_20"
    factor_name: str = "20日动量"
    factor_type: str = "技术面"
    factor_desc: str = "过去20个交易日累计收益率: close/lag(close,20)-1"
    sort_type: str = "desc"

    def __post_init__(self) -> None:
        self.factor_code = f"MOMENTUM_{int(self.window)}"
        self.factor_name = f"{int(self.window)}日动量"
        self.factor_desc = (
            f"过去{int(self.window)}个交易日累计收益率: close/lag(close,{int(self.window)})-1"
        )

    def compute(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        # 为保证窗口完整，向前多取一些历史
        start_ts = pd.Timestamp(start)
        lookback_start = (start_ts - pd.Timedelta(days=max(self.window * 3, 90))).strftime("%Y%m%d")
        close = provider.load_daily_field("close", lookback_start, end)
        if close.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])
        mom = close / close.shift(self.window) - 1.0
        mom = mom.loc[(mom.index >= pd.Timestamp(start)) & (mom.index <= pd.Timestamp(end))]
        long = (
            mom.stack(future_stack=True)
            .rename("factor_value")
            .reset_index()
            .rename(columns={"date": "data_date", "code": "stock_code"})
        )
        long["data_date"] = pd.to_datetime(long["data_date"]).dt.strftime("%Y-%m-%d")
        long["stock_code"] = long["stock_code"].astype(str)
        long["factor_value"] = pd.to_numeric(long["factor_value"], errors="coerce")
        long = long[long["factor_value"].notna()]
        return long


@dataclass
class RoeYoyCalculator:
    """ROE 同比增长率因子（基于 ROE_TTM 时间序列）。"""

    lookback: int = 240
    source_factor_codes: tuple[str, ...] = ("ROE_TTM", "ROE")
    factor_code: str = "ROE_YOY"
    factor_name: str = "ROE同比增长率"
    factor_type: str = "基本面"
    factor_desc: str = "ROE_TTM 同比增长率: ROE_TTM/lag(ROE_TTM,240)-1"
    sort_type: str = "desc"

    def compute(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        start_ts = pd.Timestamp(start)
        lookback_start = (
            start_ts - pd.Timedelta(days=max(self.lookback * 5, 1000))
        ).strftime("%Y%m%d")
        panel = pd.DataFrame()
        for src_code in self.source_factor_codes:
            panel = provider.load_factor_panel_by_code(src_code, lookback_start, end)
            if not panel.empty:
                break
        if panel.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])
        panel = provider.align_to_trading_days(panel, panel.index)
        yoy = pd.DataFrame(index=panel.index, columns=panel.columns, dtype=float)
        for lb in [self.lookback, max(self.lookback // 2, 60), 20]:
            tmp = panel / panel.shift(lb) - 1.0
            if tmp.notna().sum().sum() > 0:
                yoy = tmp
                break
        yoy = yoy.loc[(yoy.index >= pd.Timestamp(start)) & (yoy.index <= pd.Timestamp(end))]
        long = (
            yoy.stack(future_stack=True)
            .rename("factor_value")
            .reset_index()
            .rename(columns={"date": "data_date", "code": "stock_code"})
        )
        long["data_date"] = pd.to_datetime(long["data_date"]).dt.strftime("%Y-%m-%d")
        long["stock_code"] = long["stock_code"].astype(str)
        long["factor_value"] = pd.to_numeric(long["factor_value"], errors="coerce")
        return long[long["factor_value"].notna()]


@dataclass
class MainNetInflowRatioCalculator:
    """主力资金净流入率因子（Akshare / RQData：过去5日主力大单净流入 / 流通市值）。"""

    source_table: str = "strategy_stock_fundamental"
    source_date_col: str = "query_date"
    source_code_col: str = "stock_code"
    source_value_col: str = "ths_nbcai_net_amt_to_total_profit_stock"
    rolling_window: int = 5
    large_order_threshold: float = 200000.0
    chunk_size: int = 100
    akshare_sleep_sec: float = 0.25
    _value_em_cache: dict = field(default_factory=dict, repr=False)
    factor_code: str = "MAIN_NET_INFLOW_RATIO"
    factor_name: str = "主力资金净流入率"
    factor_type: str = "资金面"
    factor_desc: str = "过去5日主力大单净流入 / 流通市值（Akshare stock_individual_fund_flow）"
    sort_type: str = "desc"

    @staticmethod
    def _pick_col(columns: list[str], candidates: list[str]) -> str | None:
        for col in columns:
            for cand in candidates:
                if cand in str(col):
                    return col
        return None

    @staticmethod
    def _parse_amount_to_float(v) -> float | None:
        if pd.isna(v):
            return None
        s = str(v).strip().replace(",", "")
        if not s:
            return None
        mul = 1.0
        if s.endswith("亿"):
            mul = 1e8
            s = s[:-1]
        elif s.endswith("万"):
            mul = 1e4
            s = s[:-1]
        s = s.replace("%", "")
        try:
            return float(s) * mul
        except Exception:
            return None

    def _safe_call(self, fn, *args, retries: int = 3, sleep_sec: float = 1.0, **kwargs):
        last_err = None
        for _ in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as ex:
                last_err = ex
                time.sleep(sleep_sec)
        if last_err is not None:
            raise last_err

    @staticmethod
    def _to_ths_code(code: str) -> str:
        return rq_to_ths(normalize_code(code, "rq"))

    @staticmethod
    def _to_akshare_market(code: str) -> tuple[str, str]:
        ths = MainNetInflowRatioCalculator._to_ths_code(code)
        num, exch = ths.split(".")
        market = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exch, "sz")
        return num, market

    def _load_float_mv_panel(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        engine = getattr(provider, "_engine", None)
        sql_text = getattr(provider, "_sql_text", None)
        if engine is None or sql_text is None:
            return pd.DataFrame()

        sql = """
        SELECT stock_code, query_date AS data_date, ths_current_mv_stock AS float_mv
        FROM strategy_stock_fundamental
        WHERE query_date >= :s AND query_date <= :e
          AND ths_current_mv_stock IS NOT NULL
          AND ths_current_mv_stock > 0
        """
        with engine.begin() as conn:
            df = pd.read_sql(
                sql_text(sql),
                conn,
                params={"s": _sql_date(start), "e": _sql_date(end)},
            )
        if df.empty:
            return pd.DataFrame()

        df["stock_code"] = df["stock_code"].map(self._to_ths_code)
        df["data_date"] = pd.to_datetime(df["data_date"]).dt.normalize()
        df["float_mv"] = pd.to_numeric(df["float_mv"], errors="coerce")
        df = df[df["float_mv"].notna() & (df["float_mv"] > 0)]
        if df.empty:
            return pd.DataFrame()
        return (
            df.pivot_table(index="data_date", columns="stock_code", values="float_mv", aggfunc="last")
            .sort_index()
        )

    def _load_latest_shares_map(self, provider: IFindDataProvider) -> dict[str, float]:
        engine = getattr(provider, "_engine", None)
        sql_text = getattr(provider, "_sql_text", None)
        if engine is None or sql_text is None:
            return {}
        sql = """
        SELECT t.stock_code, t.ths_total_shares_stock AS total_shares
        FROM strategy_stock_fundamental t
        JOIN (
          SELECT stock_code, MAX(query_date) AS max_query_date
          FROM strategy_stock_fundamental
          WHERE ths_total_shares_stock IS NOT NULL AND ths_total_shares_stock > 0
          GROUP BY stock_code
        ) s ON t.stock_code = s.stock_code AND t.query_date = s.max_query_date
        WHERE t.ths_total_shares_stock IS NOT NULL AND t.ths_total_shares_stock > 0
        """
        with engine.begin() as conn:
            df = pd.read_sql(sql_text(sql), conn)
        if df.empty:
            return {}
        df["stock_code"] = df["stock_code"].map(self._to_ths_code)
        df["total_shares"] = pd.to_numeric(df["total_shares"], errors="coerce")
        df = df[df["total_shares"].notna() & (df["total_shares"] > 0)]
        return df.set_index("stock_code")["total_shares"].to_dict()

    def _estimate_float_mv_panel(
        self, provider: IFindDataProvider, start: str, end: str, universe: list[str]
    ) -> pd.DataFrame:
        close = provider.load_daily_field("close", start, end)
        if close.empty:
            return pd.DataFrame()
        close.columns = [self._to_ths_code(c) for c in close.columns]
        universe_ths = [self._to_ths_code(c) for c in universe] if universe else list(close.columns)
        close = close.reindex(columns=[c for c in universe_ths if c in close.columns])
        if close.empty:
            return pd.DataFrame()

        shares_map = self._load_latest_shares_map(provider)
        out = pd.DataFrame(index=close.index, columns=close.columns, dtype=float)
        for code in close.columns:
            shares = shares_map.get(code)
            if shares is None or shares <= 0:
                continue
            out[code] = close[code] * float(shares)

        if out.notna().sum().sum() >= len(universe_ths) * 0.2:
            return out

        engine = getattr(provider, "_engine", None)
        sql_text = getattr(provider, "_sql_text", None)
        if engine is None or sql_text is None:
            return out

        sql = """
        SELECT t.stock_code, t.query_date AS snap_date, t.ths_current_mv_stock AS float_mv
        FROM strategy_stock_fundamental t
        JOIN (
          SELECT stock_code, MAX(query_date) AS max_query_date
          FROM strategy_stock_fundamental
          WHERE ths_current_mv_stock IS NOT NULL AND ths_current_mv_stock > 0
          GROUP BY stock_code
        ) s ON t.stock_code = s.stock_code AND t.query_date = s.max_query_date
        """
        with engine.begin() as conn:
            snap = pd.read_sql(sql_text(sql), conn)
        if snap.empty:
            return out

        snap["stock_code"] = snap["stock_code"].map(self._to_ths_code)
        snap["snap_date"] = pd.to_datetime(snap["snap_date"]).dt.normalize()
        snap["float_mv"] = pd.to_numeric(snap["float_mv"], errors="coerce")
        snap = snap[snap["float_mv"].notna() & (snap["float_mv"] > 0)]
        for _, row in snap.iterrows():
            code = row["stock_code"]
            if code not in out.columns:
                continue
            snap_dt = row["snap_date"]
            ref_close = None
            if snap_dt in close.index and pd.notna(close.at[snap_dt, code]):
                ref_close = float(close.at[snap_dt, code])
            else:
                hist = close[code].dropna()
                if hist.empty:
                    continue
                ref_close = float(hist.iloc[-1])
            if not ref_close or ref_close <= 0:
                continue
            ratio = float(row["float_mv"]) / ref_close
            out[code] = close[code] * ratio
        return out

    def _load_float_mv_map_from_spot(self) -> dict[str, float]:
        import akshare as ak

        try:
            spot = self._safe_call(ak.stock_zh_a_spot_em, retries=2)
        except Exception:
            return {}
        if spot is None or spot.empty:
            return {}
        spot_cols = list(spot.columns)
        code_col = self._pick_col(spot_cols, ["代码", "股票代码"])
        mv_col = self._pick_col(spot_cols, ["流通市值", "自由流通市值"])
        if not code_col or not mv_col:
            return {}
        sub = spot[[code_col, mv_col]].copy()
        sub.columns = ["code", "float_mv"]
        sub["stock_code"] = sub["code"].astype(str).str.zfill(6).map(self._to_ths_code)
        sub["float_mv"] = pd.to_numeric(sub["float_mv"], errors="coerce")
        sub = sub[sub["float_mv"].notna() & (sub["float_mv"] > 0)]
        if sub.empty:
            return {}
        return sub.set_index("stock_code")["float_mv"].to_dict()

    def _resolve_float_mv_map(
        self, provider: IFindDataProvider, start: str, end: str, universe: list[str]
    ) -> dict[str, float]:
        mv_map = self._load_float_mv_map_from_spot()
        if len(mv_map) >= max(100, len(universe) // 10):
            return mv_map

        panel = self._build_float_mv_panel(provider, start, end, universe)
        if not panel.empty and panel.shape[1] >= max(100, len(universe) // 10):
            return panel.iloc[-1].dropna().to_dict()

        est = self._estimate_float_mv_panel(provider, start, end, universe)
        if not est.empty:
            return est.iloc[-1].dropna().to_dict()
        return mv_map

    def _build_float_mv_panel(
        self, provider: IFindDataProvider, start: str, end: str, universe: list[str]
    ) -> pd.DataFrame:
        panel = self._load_float_mv_panel(provider, start, end)
        if panel.empty or panel.shape[1] < max(50, len(universe) // 20):
            est = self._estimate_float_mv_panel(provider, start, end, universe)
            if not est.empty:
                panel = est if panel.empty else panel.reindex(columns=est.columns.union(panel.columns, sort=False)).combine_first(est)
        if panel.empty:
            return panel
        trading = provider.get_trading_dates(start, end)
        trading = pd.DatetimeIndex(pd.to_datetime(trading))
        return panel.reindex(trading).ffill()

    def _fetch_value_em_panel(self, code: str) -> pd.DataFrame:
        import akshare as ak

        num, _ = self._to_akshare_market(code)
        if num in self._value_em_cache:
            return self._value_em_cache[num]
        panel = self._safe_call(ak.stock_value_em, symbol=num, retries=2)
        if panel is None or panel.empty:
            return pd.DataFrame()
        self._value_em_cache[num] = panel
        return panel

    def _get_float_mv_at_date(self, code: str, dt: pd.Timestamp) -> float | None:
        panel = self._fetch_value_em_panel(code)
        if panel.empty:
            return None
        date_col = self._pick_col(list(panel.columns), ["数据日期"])
        mv_col = self._pick_col(list(panel.columns), ["流通市值"])
        if not date_col or not mv_col:
            return None
        sub = panel.copy()
        sub["data_date"] = pd.to_datetime(sub[date_col]).dt.normalize()
        sub[mv_col] = pd.to_numeric(sub[mv_col], errors="coerce")
        sub = sub[sub["data_date"] <= dt.normalize()]
        sub = sub[sub[mv_col].notna() & (sub[mv_col] > 0)]
        if sub.empty:
            return None
        return float(sub.iloc[-1][mv_col])

    def _compute_stock_series(self, code: str, start: str, end: str) -> pd.DataFrame:
        import akshare as ak

        ths_code = self._to_ths_code(code)
        num, market = self._to_akshare_market(code)
        try:
            flow = self._safe_call(ak.stock_individual_fund_flow, stock=num, market=market, retries=2)
            value = self._fetch_value_em_panel(code)
        except Exception:
            return pd.DataFrame()
        if flow is None or flow.empty or value is None or value.empty:
            return pd.DataFrame()

        flow_date_col = self._pick_col(list(flow.columns), ["日期"])
        main_col = self._pick_col(list(flow.columns), ["主力净流入-净额", "主力净流入"])
        large_col = self._pick_col(list(flow.columns), ["大单净流入-净额", "大单净流入"])
        super_col = self._pick_col(list(flow.columns), ["超大单净流入-净额", "超大单净流入"])
        value_date_col = self._pick_col(list(value.columns), ["数据日期"])
        mv_col = self._pick_col(list(value.columns), ["流通市值"])
        if not flow_date_col or not value_date_col or not mv_col:
            return pd.DataFrame()

        f = flow.copy()
        f["data_date"] = pd.to_datetime(f[flow_date_col]).dt.normalize()
        if main_col:
            f["main_net"] = pd.to_numeric(f[main_col], errors="coerce")
        elif large_col and super_col:
            f["main_net"] = pd.to_numeric(f[large_col], errors="coerce") + pd.to_numeric(
                f[super_col], errors="coerce"
            )
        else:
            return pd.DataFrame()

        v = value.copy()
        v["data_date"] = pd.to_datetime(v[value_date_col]).dt.normalize()
        v["float_mv"] = pd.to_numeric(v[mv_col], errors="coerce")
        merged = f[["data_date", "main_net"]].merge(
            v[["data_date", "float_mv"]], on="data_date", how="inner"
        )
        if merged.empty:
            return pd.DataFrame()
        merged = merged.sort_values("data_date")
        merged["net_5d"] = (
            merged["main_net"].rolling(int(self.rolling_window), min_periods=int(self.rolling_window)).sum()
        )
        merged["factor_value"] = merged["net_5d"] / merged["float_mv"].replace(0, np.nan)

        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        merged = merged[
            (merged["data_date"] >= start_ts)
            & (merged["data_date"] <= end_ts)
            & merged["factor_value"].notna()
        ]
        if merged.empty:
            return pd.DataFrame()
        merged["stock_code"] = ths_code
        merged["data_date"] = merged["data_date"].dt.strftime("%Y-%m-%d")
        return merged[["stock_code", "data_date", "factor_value"]]

    def _compute_from_akshare_history(
        self, provider: IFindDataProvider, start: str, end: str
    ) -> pd.DataFrame:
        universe = provider.get_universe(start, end)
        if not universe:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        frames: list[pd.DataFrame] = []
        for code in universe:
            try:
                one = self._compute_stock_series(code, start, end)
            except Exception:
                one = pd.DataFrame()
            if not one.empty:
                frames.append(one)
            time.sleep(self.akshare_sleep_sec)

        if not frames:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])
        out = pd.concat(frames, ignore_index=True)
        out["factor_value"] = pd.to_numeric(out["factor_value"], errors="coerce")
        return out[out["factor_value"].notna()][["stock_code", "data_date", "factor_value"]]

    def _compute_from_akshare_snapshot(self, provider: IFindDataProvider, end: str) -> pd.DataFrame:
        import akshare as ak

        rank = None
        for fn, kwargs in (
            (ak.stock_fund_flow_individual, {"symbol": "5日排行"}),
            (ak.stock_individual_fund_flow_rank, {"indicator": "5日"}),
        ):
            try:
                rank = self._safe_call(fn, retries=2, **kwargs)
                if rank is not None and not rank.empty:
                    break
            except Exception:
                rank = None
        if rank is None or rank.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        rank_cols = list(rank.columns)
        rank_code_col = self._pick_col(rank_cols, ["代码", "股票代码"])
        rank_inflow_col = self._pick_col(
            rank_cols,
            [
                "5日主力净流入-净额",
                "5日大单净流入-净额",
                "主力净流入-净额",
                "资金流入净额",
                "净流入",
            ],
        )
        if not rank_code_col or not rank_inflow_col:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        rank_sub = rank[[rank_code_col, rank_inflow_col]].copy()
        rank_sub.columns = ["code", "net_inflow_5d"]
        rank_sub["stock_code"] = (
            rank_sub["code"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6).map(self._to_ths_code)
        )
        rank_sub["net_inflow_5d"] = rank_sub["net_inflow_5d"].map(self._parse_amount_to_float)
        rank_sub = rank_sub[rank_sub["net_inflow_5d"].notna()]

        universe = {self._to_ths_code(c) for c in provider.get_universe(end, end)}
        if universe:
            rank_sub = rank_sub[rank_sub["stock_code"].isin(universe)]
        if rank_sub.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        data_date = pd.Timestamp(end)
        close = provider.load_daily_field("close", end, end)
        if not close.empty:
            data_date = pd.Timestamp(close.index.max())

        rows: list[dict] = []
        for _, row in rank_sub.iterrows():
            code = row["stock_code"]
            float_mv = self._get_float_mv_at_date(code, data_date)
            if float_mv is None or float_mv <= 0:
                continue
            rows.append(
                {
                    "stock_code": code,
                    "data_date": data_date.strftime("%Y-%m-%d"),
                    "factor_value": row["net_inflow_5d"] / float_mv,
                }
            )
            time.sleep(self.akshare_sleep_sec)
        if not rows:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])
        out = pd.DataFrame(rows)
        out["factor_value"] = pd.to_numeric(out["factor_value"], errors="coerce")
        return out[out["factor_value"].notna()][["stock_code", "data_date", "factor_value"]]

    def _compute_from_akshare(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        end_ts = pd.Timestamp(end)
        days_span = (end_ts - pd.Timestamp(start)).days
        # 短区间优先走全市场 5 日排行快照（更快）；长区间走个股历史接口
        if days_span <= 3:
            snap = self._compute_from_akshare_snapshot(provider, end)
            if not snap.empty:
                return snap
        hist = self._compute_from_akshare_history(provider, start, end)
        if not hist.empty:
            return hist
        return self._compute_from_akshare_snapshot(provider, end)

    def _compute_from_rqdatac(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        init_rqdatac()
        import rqdatac as rq

        start_ts = pd.Timestamp(start)
        lookback_start = (start_ts - pd.Timedelta(days=40)).strftime("%Y%m%d")
        all_dates = provider.get_trading_dates(lookback_start, end)
        all_dates = pd.DatetimeIndex(pd.to_datetime(all_dates))
        target_dates = all_dates[(all_dates >= pd.Timestamp(start)) & (all_dates <= pd.Timestamp(end))]
        if target_dates.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        universe = provider.get_universe(start, end)
        if not universe:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        out_frames: list[pd.DataFrame] = []
        tick_enabled = True
        for i in range(0, len(universe), self.chunk_size):
            codes = universe[i : i + self.chunk_size]
            daily_net: pd.DataFrame | None = None

            if tick_enabled:
                try:
                    tick = rq.get_capital_flow(codes, lookback_start, end, frequency="tick")
                except Exception:
                    tick_enabled = False
                    tick = None
                if tick is not None and not tick.empty:
                    t = tick.reset_index()
                    if "order_book_id" not in t.columns:
                        # 单只股票时索引只有 datetime
                        if len(codes) == 1:
                            t["order_book_id"] = codes[0]
                        else:
                            t = pd.DataFrame()
                    if not t.empty:
                        t["date"] = pd.to_datetime(t["datetime"]).dt.normalize()
                        t["value"] = pd.to_numeric(t["value"], errors="coerce").astype("float64")
                        t["direction"] = pd.to_numeric(t["direction"], errors="coerce").astype("float64")
                        t = t[t["value"].notna() & t["direction"].notna()]
                        if not t.empty:
                            big = t[t["value"] >= float(self.large_order_threshold)].copy()
                            if not big.empty:
                                big["signed_value"] = np.where(big["direction"] > 0, big["value"], -big["value"])
                                daily_net = (
                                    big.groupby(["order_book_id", "date"], as_index=False)["signed_value"]
                                    .sum()
                                    .rename(columns={"signed_value": "main_large_net_inflow"})
                                )

            # tick 权限或额度不足时，退化为日频主动买卖净额近似
            if daily_net is None:
                try:
                    day = rq.get_capital_flow(codes, lookback_start, end, frequency="1d")
                except Exception:
                    continue
                if day is None or day.empty:
                    continue
                d = day.reset_index()
                d["date"] = pd.to_datetime(d["date"]).dt.normalize()
                d["buy_value"] = pd.to_numeric(d["buy_value"], errors="coerce").astype("float64")
                d["sell_value"] = pd.to_numeric(d["sell_value"], errors="coerce").astype("float64")
                d["main_large_net_inflow"] = d["buy_value"] - d["sell_value"]
                daily_net = d[["order_book_id", "date", "main_large_net_inflow"]]
                if daily_net.empty:
                    continue

            net_panel = (
                daily_net.pivot_table(
                    index="date",
                    columns="order_book_id",
                    values="main_large_net_inflow",
                    aggfunc="sum",
                )
                .reindex(all_dates)
                .fillna(0.0)
            )
            net_5d = net_panel.rolling(int(self.rolling_window), min_periods=int(self.rolling_window)).sum()

            try:
                float_mv = rq.get_factor(codes, "a_share_market_val_in_circulation", lookback_start, end)
            except Exception:
                continue
            if float_mv is None or float_mv.empty:
                continue
            float_mv = (
                float_mv.rename(columns={"a_share_market_val_in_circulation": "float_mv"})
                if "a_share_market_val_in_circulation" in float_mv.columns
                else float_mv
            )
            if isinstance(float_mv, pd.Series):
                float_mv = float_mv.to_frame("float_mv")
            if "float_mv" not in float_mv.columns:
                float_mv.columns = ["float_mv"]
            mv_panel = float_mv["float_mv"].unstack("order_book_id").reindex(all_dates)
            ratio_panel = net_5d.divide(mv_panel.replace(0.0, np.nan))
            ratio_panel = ratio_panel.loc[target_dates]
            long = (
                ratio_panel.stack(future_stack=True)
                .rename("factor_value")
                .reset_index()
                .rename(columns={"date": "data_date", "order_book_id": "stock_code"})
            )
            if not long.empty:
                out_frames.append(long)

        if not out_frames:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        out = pd.concat(out_frames, ignore_index=True)
        out["data_date"] = pd.to_datetime(out["data_date"]).dt.strftime("%Y-%m-%d")
        out["stock_code"] = out["stock_code"].map(rq_to_ths).astype(str)
        out["factor_value"] = pd.to_numeric(out["factor_value"], errors="coerce")
        out = out[out["factor_value"].notna()]
        out = out.drop_duplicates(subset=["stock_code", "data_date"], keep="last")
        return out[["stock_code", "data_date", "factor_value"]]

    def compute(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        try:
            ak_df = self._compute_from_akshare(provider, start, end)
            if not ak_df.empty:
                return ak_df
        except Exception:
            pass
        try:
            rq_df = self._compute_from_rqdatac(provider, start, end)
            if not rq_df.empty:
                return rq_df
        except Exception:
            pass

        # 兜底：沿用旧字段，避免因外部数据源不可用导致任务完全空跑
        engine = getattr(provider, "_engine", None)
        sql_text = getattr(provider, "_sql_text", None)
        if engine is None or sql_text is None:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        start_sql = _sql_date(start)
        end_sql = _sql_date(end)
        sql = f"""
        SELECT `{self.source_date_col}` AS data_date,
               `{self.source_code_col}` AS stock_code,
               `{self.source_value_col}` AS factor_value
        FROM `{self.source_table}`
        WHERE `{self.source_date_col}` >= :s
          AND `{self.source_date_col}` <= :e
          AND `{self.source_value_col}` IS NOT NULL
        """
        with engine.begin() as conn:
            df = pd.read_sql(sql_text(sql), conn, params={"s": start_sql, "e": end_sql})
        if df.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])
        df["data_date"] = pd.to_datetime(df["data_date"]).dt.strftime("%Y-%m-%d")
        df["stock_code"] = df["stock_code"].astype(str)
        df["factor_value"] = pd.to_numeric(df["factor_value"], errors="coerce")
        return df[df["factor_value"].notna()][["stock_code", "data_date", "factor_value"]]


def _storage_stock_code(factor_code: str, code: str) -> str:
    """宽表统一 THS 代码（factor_data_wide / stock_daily_qfq）。"""
    return storage_stock_code(code)


@dataclass
class PanelAlignBackfillCalculator:
    """
    将 factor_data_wide 已有历史按交易日历前向填充到目标区间。
    用于 Blader/iFinD 同步因子在日 K 窗口内缺日的补全（不依赖 RQ/Akshare 额度）。
    """

    factor_code: str
    lookback_start: str = "20200101"
    min_coverage_ratio: float = 0.8
    factor_name: str = ""
    factor_type: str = "同步补全"
    factor_desc: str = "按交易日历对历史因子值前向填充"
    sort_type: str = "desc"

    def __post_init__(self) -> None:
        self.factor_code = str(self.factor_code).upper()
        if not self.factor_name:
            self.factor_name = self.factor_code

    def compute(self, provider: IFindDataProvider, start: str, end: str) -> pd.DataFrame:
        panel = provider.load_factor_panel_by_code(self.factor_code, self.lookback_start, end)
        if panel.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        trading = provider.get_trading_dates(start, end)
        if len(trading) == 0:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        aligned = provider.align_to_trading_days(panel, pd.DatetimeIndex(trading))
        start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
        aligned = aligned.loc[(aligned.index >= start_ts) & (aligned.index <= end_ts)]
        if aligned.empty:
            return pd.DataFrame(columns=["stock_code", "data_date", "factor_value"])

        long = (
            aligned.stack(future_stack=True)
            .rename("factor_value")
            .reset_index()
            .rename(columns={"date": "data_date", "code": "stock_code"})
        )
        long["data_date"] = pd.to_datetime(long["data_date"]).dt.strftime("%Y-%m-%d")
        long["stock_code"] = long["stock_code"].map(
            lambda c: _storage_stock_code(self.factor_code, str(c))
        )
        long["factor_value"] = pd.to_numeric(long["factor_value"], errors="coerce")
        return long[long["factor_value"].notna()][["stock_code", "data_date", "factor_value"]]


def count_factor_dates_in_window(
    provider: IFindDataProvider, factor_code: str, start: str, end: str
) -> int:
    """区间内 factor_data_wide 对应因子列有值的 distinct 交易日数量。"""
    panel = provider.load_factor_panel_by_code(factor_code, start, end)
    if panel.empty:
        return 0
    return int(panel.notna().any(axis=1).sum())


def list_panel_backfill_factor_codes(
    provider: IFindDataProvider,
    start: str,
    end: str,
    meta_list: list[dict] | None = None,
    min_coverage_ratio: float = 0.8,
) -> list[str]:
    """
    factor_base_info 中已有历史、但区间内覆盖率不足、且非 Python 计算器的因子。
    """
    metas = meta_list or provider.list_factor_base_info()
    computable = set(list_computable_factor_codes(metas))
    trading_n = len(provider.get_trading_dates(start, end))
    if trading_n <= 0:
        return []
    need_n = max(1, int(trading_n * min_coverage_ratio))
    codes: list[str] = []
    for item in metas:
        code = str(item["factor_code"]).upper()
        if code in computable:
            continue
        hist = provider.load_factor_panel_by_code(code, "20200101", end)
        if hist.empty:
            continue
        have = count_factor_dates_in_window(provider, code, start, end)
        if have < need_n:
            codes.append(code)
    return sorted(set(codes))


def create_panel_align_calculator(factor_code: str) -> PanelAlignBackfillCalculator:
    return PanelAlignBackfillCalculator(factor_code=str(factor_code).upper())


def create_factor_calculator(factor_code: str) -> FactorCalculator:
    """根据 factor_code 创建可落库计算器（不支持则抛 ValueError）。"""
    code = str(factor_code).upper()
    if code.startswith("MOMENTUM_"):
        n = int(code.split("_", 1)[1])
        return MomentumNCalculator(window=n)
    if code == "MOMENTUM":
        return MomentumNCalculator(window=20)
    if code in ("ROE_YOY", "ROE_GROWTH_YOY"):
        return RoeYoyCalculator()
    if code in ("MAIN_NET_INFLOW_RATIO", "MAIN_NET_INFLOW"):
        return MainNetInflowRatioCalculator()
    raise ValueError(
        f"暂不支持因子 {factor_code} 的任务计算。"
        f"当前支持: MOMENTUM_N / ROE_YOY / MAIN_NET_INFLOW_RATIO"
    )


def list_computable_factor_codes(meta_list: list[dict] | None = None) -> list[str]:
    """从 factor_base_info 中筛出可用任务计算器生成的因子代码。"""
    codes: list[str] = []
    if meta_list:
        for item in meta_list:
            code = str(item["factor_code"]).upper()
            try:
                create_factor_calculator(code)
                codes.append(code)
            except ValueError:
                continue
        return sorted(set(codes))
    for code in ("MOMENTUM_20", "ROE_YOY", "MAIN_NET_INFLOW_RATIO"):
        try:
            create_factor_calculator(code)
            codes.append(code)
        except ValueError:
            pass
    return codes


def resolve_job_dates(
    provider: IFindDataProvider, start: str, end: str, run_type: str = "incr"
) -> tuple[str, str]:
    if end:
        e = str(end).replace("-", "")[:8]
    else:
        e = pd.Timestamp.today().strftime("%Y%m%d")
    if start:
        s = str(start).replace("-", "")[:8]
    else:
        if run_type == "full":
            daily = provider.load_daily_field("close", "20000101", e)
            s = daily.index.min().strftime("%Y%m%d") if len(daily) else "20000101"
        else:
            s = (pd.Timestamp(e) - pd.Timedelta(days=90)).strftime("%Y%m%d")
    return s, e


def resolve_daily_available_range(provider: IFindDataProvider) -> tuple[str, str]:
    """从日 K 表推断可回测日期区间。"""
    daily = provider.load_daily_field("close", "20000101", pd.Timestamp.today().strftime("%Y%m%d"))
    if daily.empty:
        today = pd.Timestamp.today().strftime("%Y%m%d")
        return today, today
    return daily.index.min().strftime("%Y%m%d"), daily.index.max().strftime("%Y%m%d")


def list_factors_ready_for_backtest(
    provider: IFindDataProvider, start: str, end: str, meta_list: list[dict] | None = None
) -> list[str]:
    """factor_base_info 中在区间内具备因子值且可与收益对齐的因子。"""
    metas = meta_list or provider.list_factor_base_info()
    returns = provider.get_daily_returns(start, end)
    if returns.empty:
        return []
    ready: list[str] = []
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    for item in metas:
        code = str(item["factor_code"]).upper()
        panel = provider.load_factor_panel_by_code(code, start, end)
        if panel.empty:
            continue
        panel = panel.loc[(panel.index >= start_ts) & (panel.index <= end_ts)]
        if panel.empty or panel.notna().sum().sum() == 0:
            continue
        common = panel.columns.intersection(returns.columns)
        if len(common) == 0:
            continue
        sub = panel[common].dropna(how="all")
        if sub.empty:
            continue
        ready.append(code)
    return sorted(set(ready))


class FactorStorage:
    """因子数据落库。"""

    def __init__(self, cfg: IFindConfig):
        if not cfg.db_url:
            raise ValueError("当前任务仅支持数据库模式，请在 ifind_config.yaml 配置 database")
        self.cfg = cfg
        self.engine = create_engine(cfg.db_url, pool_pre_ping=True)

    def ensure_job_log_table(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS factor_calc_job_log (
          id BIGINT PRIMARY KEY AUTO_INCREMENT,
          factor_code VARCHAR(64) NOT NULL,
          run_type VARCHAR(16) NOT NULL,
          start_date DATE NULL,
          end_date DATE NULL,
          affected_rows BIGINT DEFAULT 0,
          status VARCHAR(16) NOT NULL,
          err_msg TEXT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
        with self.engine.begin() as conn:
            conn.execute(text(sql))

    def ensure_indexes(self) -> None:
        """宽表索引由库表 DDL 维护，此处不再 ALTER 旧长表。"""
        return

    def upsert_factor_meta(self, calc: FactorCalculator) -> None:
        table = self.cfg.table("factor_base")
        update_sql = f"""
        UPDATE `{table}`
        SET factor_name=:name,
            factor_type=:ftype,
            factor_desc=:fdesc,
            sort_type=:stype,
            is_valid=1,
            is_deleted=0,
            update_time=NOW()
        WHERE factor_code=:code
        """
        insert_sql = f"""
        INSERT INTO `{table}`
        (id, factor_code, factor_name, factor_type, factor_desc, ths_indicator, calc_rule, is_valid, sort_type, create_time, update_time, is_deleted)
        SELECT COALESCE(MAX(id), 0) + 1, :code, :name, :ftype, :fdesc, NULL, :crule, 1, :stype, NOW(), NOW(), 0
        FROM `{table}`
        """
        with self.engine.begin() as conn:
            params = {
                "code": calc.factor_code,
                "name": calc.factor_name,
                "ftype": calc.factor_type,
                "fdesc": calc.factor_desc,
                "stype": calc.sort_type,
                "crule": calc.factor_desc,
            }
            ret = conn.execute(text(update_sql), params)
            if ret.rowcount == 0:
                conn.execute(text(insert_sql), params)

    def upsert_factor_data(self, factor_code: str, rows: list[dict], chunk_size: int = 5000) -> int:
        """写入 factor_data_wide（专用列或 factor_ext_json）。"""
        if not rows:
            return 0
        fc = str(factor_code).upper()
        for r in rows:
            r["factor_code"] = fc
            r["stock_code"] = _storage_stock_code(fc, str(r["stock_code"]))
            if "data_date" in r:
                r["data_date"] = pd.to_datetime(r["data_date"]).strftime("%Y-%m-%d")
            r["factor_value"] = r.get("factor_value")

        wide_records = long_rows_to_wide_records(rows)
        return upsert_wide_records(self.engine, wide_records, chunk_size=chunk_size)

    def write_job_log(
        self,
        factor_code: str,
        run_type: str,
        start: str,
        end: str,
        affected_rows: int,
        status: str,
        err_msg: str | None = None,
    ) -> None:
        sql = """
        INSERT INTO factor_calc_job_log
        (factor_code, run_type, start_date, end_date, affected_rows, status, err_msg, created_at)
        VALUES (:factor_code, :run_type, :start_date, :end_date, :affected_rows, :status, :err_msg, :created_at)
        """
        with self.engine.begin() as conn:
            conn.execute(
                text(sql),
                {
                    "factor_code": factor_code,
                    "run_type": run_type,
                    "start_date": _sql_date(start),
                    "end_date": _sql_date(end),
                    "affected_rows": int(affected_rows),
                    "status": status,
                    "err_msg": err_msg,
                    "created_at": datetime.now(),
                },
            )


class FactorJobRunner:
    """执行因子任务（计算 + 落库 + 日志）。"""

    def __init__(self, cfg: IFindConfig):
        self.cfg = cfg
        self.provider = IFindDataProvider(cfg)
        self.storage = FactorStorage(cfg)

    def run(
        self,
        calc: FactorCalculator,
        start: str,
        end: str,
        run_type: str = "incr",
        dry_run: bool = False,
    ) -> dict:
        self.storage.ensure_job_log_table()
        self.storage.ensure_indexes()
        try:
            df = calc.compute(self.provider, start, end)
            rows = df.to_dict("records")
            if dry_run:
                result = {
                    "factor_code": calc.factor_code,
                    "rows": len(rows),
                    "status": "dry_run",
                    "start": start,
                    "end": end,
                }
                self.storage.write_job_log(
                    calc.factor_code, run_type, start, end, len(rows), "success", "dry_run"
                )
                return result

            self.storage.upsert_factor_meta(calc)
            affected = self.storage.upsert_factor_data(calc.factor_code, rows)
            self.storage.write_job_log(calc.factor_code, run_type, start, end, affected, "success")
            return {
                "factor_code": calc.factor_code,
                "rows": affected,
                "status": "success",
                "start": start,
                "end": end,
            }
        except Exception as ex:
            self.storage.write_job_log(calc.factor_code, run_type, start, end, 0, "failed", str(ex))
            raise

