# -*- coding: utf-8 -*-
"""从 iFinD / Blader 同步库（SQL / CSV）读取行情、因子、指数成分。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from multi_factor.ifind.code_convert import normalize_code, normalize_codes
from multi_factor.ifind.config_loader import IFindConfig
from multi_factor.cache.redis_cache import cache_key, get_cache
from multi_factor.ifind.factor_wide import (
    WIDE_TABLE,
    wide_column_for_factor,
)


def _norm_date(s) -> pd.Timestamp:
    return pd.to_datetime(s).normalize()


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


_BUNDLE_CACHE_GLOBAL: dict[str, dict[str, pd.DataFrame]] = {}
_BENCHMARK_CACHE_GLOBAL: dict[str, pd.Series] = {}
_FACTOR_PANEL_CACHE_GLOBAL: dict[str, pd.DataFrame] = {}
_BUNDLE_FIELDS = ("close", "volume")


class IFindDataProvider:
    """读取同步表，输出宽表 panel（index=日期, columns=股票代码 RQ 格式）。"""

    def __init__(self, cfg: IFindConfig):
        self.cfg = cfg
        self._use_csv = bool(cfg.csv_dir) and not cfg.db_url
        self._engine = None
        self._close_cache: dict[str, pd.DataFrame] = {}
        self._bundle_cache: dict[str, dict[str, pd.DataFrame]] = {}
        self._returns_cache: dict[str, pd.DataFrame] = {}
        self._factor_panel_cache: dict[str, pd.DataFrame] = {}
        self._benchmark_cache: dict[str, pd.Series] = {}
        self._meta_map: dict[str, dict] | None = None
        self._date_range_cache: tuple[str, str] | None = None
        self._cache = get_cache(cfg.redis.as_dict() if cfg.redis.host else None)
        self._panel_ttl = cfg.performance.cache_ttl_panel
        self._archive = None
        self._factor_archive = None
        if cfg.parquet_archive.enabled:
            from multi_factor import config as project_config
            from multi_factor.archive.daily_parquet import DailyParquetArchive

            root = Path(cfg.parquet_archive.dir)
            if not root.is_absolute():
                root = project_config.PROJECT_ROOT / root
            self._archive = DailyParquetArchive(
                root,
                read_workers=cfg.parquet_archive.read_workers,
                use_monthly_wide=cfg.parquet_archive.use_monthly_wide,
            )
        fq = cfg.parquet_archive.factor
        if fq.enabled:
            from multi_factor import config as project_config
            from multi_factor.archive.factor_parquet import FactorParquetArchive

            froot = Path(fq.dir)
            if not froot.is_absolute():
                froot = project_config.PROJECT_ROOT / froot
            self._factor_archive = FactorParquetArchive(
                froot,
                read_workers=cfg.parquet_archive.read_workers,
            )
        if cfg.db_url and not self._use_csv:
            from sqlalchemy import create_engine, text

            self._engine = create_engine(cfg.db_url, pool_pre_ping=True)
            self._sql_text = text

    def _append_filter(self, where: str, table_key: str) -> str:
        extra = self.cfg.filter_sql(table_key)
        if not extra:
            return where
        if where:
            return f"{where} AND ({extra})"
        return extra

    def _read_table(self, table_key: str, where: str = "", params: dict | None = None) -> pd.DataFrame:
        table = self.cfg.table(table_key)
        where = self._append_filter(where, table_key)
        if self._use_csv:
            path = Path(self.cfg.csv_dir) / f"{table}.csv"
            if not path.exists():
                path = Path(self.cfg.csv_dir) / f"{table_key}.csv"
            if not path.exists():
                raise FileNotFoundError(f"未找到 CSV: {path}")
            df = pd.read_csv(path)
            if where and params:
                pass  # CSV 模式忽略复杂 where
        else:
            sql = f"SELECT * FROM `{table}`"
            if where:
                sql += f" WHERE {where}"
            df = pd.read_sql(self._sql_text(sql), self._engine, params=params or {})
        return df

    def _query_sql(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        return pd.read_sql(self._sql_text(sql), self._engine, params=params or {})

    def _rename_logical(self, df: pd.DataFrame, table_key: str) -> pd.DataFrame:
        cols = self.cfg.cols(table_key)
        inv = {v: k for k, v in cols.mapping.items()}
        out = df.rename(columns=inv)
        missing = [k for k in cols.mapping if k not in out.columns]
        if missing:
            raise ValueError(f"表 {table_key} 缺少列: {missing}，实际列: {list(df.columns)}")
        return out

    def _to_panel(
        self,
        df: pd.DataFrame,
        value_col: str,
        code_target: str = "rq",
    ) -> pd.DataFrame:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df["code"] = normalize_codes(df["code"].astype(str), code_target)
        panel = df.pivot_table(index="date", columns="code", values=value_col, aggfunc="last")
        return panel.sort_index()

    def _load_daily_close_blader(
        self,
        start: str,
        end: str,
        only_dates: list[str] | None = None,
    ) -> pd.DataFrame:
        """Blader 旧表 stock_history_daily：close 为 0 时回退 pre_close。"""
        c = self.cfg.cols("daily")
        t = self.cfg.table("daily")
        if only_dates:
            placeholders = ", ".join(f":d{i}" for i in range(len(only_dates)))
            date_filter = f"`{c.col('date')}` IN ({placeholders})"
            params = {f"d{i}": _sql_date(d) for i, d in enumerate(only_dates)}
        else:
            date_filter = f"`{c.col('date')}` >= :s AND `{c.col('date')}` <= :e"
            params = {"s": _sql_date(start), "e": _sql_date(end)}
        sql = f"""
        SELECT `{c.col('date')}` AS dt,
               `{c.col('code')}` AS sym,
               CASE
                 WHEN `{c.col('close')}` IS NULL OR `{c.col('close')}` = 0
                 THEN `{c.col('pre_close')}`
                 ELSE `{c.col('close')}`
               END AS px
        FROM `{t}`
        WHERE {date_filter}
        """
        extra = self.cfg.filter_sql("daily")
        if extra:
            sql += f" AND ({extra})"
        df = self._query_sql(sql, params=params)
        df = df.rename(columns={"dt": "date", "sym": "code", "px": "close"})
        return self._to_panel(df, "close", code_target="rq")

    def _parquet_usable(self, start: str, end: str) -> bool:
        if not self._archive or not self.cfg.parquet_archive.prefer_read:
            return False
        dates = self._archive.dates_in_range(start, end)
        if len(dates) < 5:
            return False
        return self._archive.coverage_ratio(start, end, dates) >= self.cfg.parquet_archive.min_coverage

    def _factor_parquet_usable(self, factor_code: str, start: str, end: str) -> bool:
        fq = self.cfg.parquet_archive.factor
        if not self._factor_archive or not fq.prefer_read:
            return False
        return self._factor_archive.monthly_coverage(factor_code, start, end) >= fq.min_coverage

    def get_trading_dates(self, start: str, end: str, *, prefer_parquet: bool | None = None) -> pd.DatetimeIndex:
        start_d, end_d = _norm_date(start), _norm_date(end)
        use_pq = (
            self._parquet_usable(start, end)
            if prefer_parquet is None
            else (prefer_parquet and self._parquet_usable(start, end))
        )
        if use_pq:
            dates = self._archive.dates_in_range(start, end)
            idx = pd.DatetimeIndex(pd.to_datetime(dates))
            return idx[(idx >= start_d) & (idx <= end_d)]
        daily = self.load_daily_field("close", start, end)
        daily_dates = (
            daily.index[(daily.index >= start_d) & (daily.index <= end_d)]
            if not daily.empty
            else pd.DatetimeIndex([])
        )

        cal_dates = pd.DatetimeIndex([])
        cal_key = "calendar" if "calendar" in self.cfg.tables else None
        if cal_key:
            try:
                df = self._rename_logical(self._read_table(cal_key), cal_key)
                dates = pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.normalize())
                cal_dates = dates[(dates >= start_d) & (dates <= end_d)]
            except (FileNotFoundError, ValueError, KeyError):
                pass

        # 日历表可能仅覆盖近期区间，优先选用窗口内覆盖更完整的日 K 交易日
        if len(daily_dates) >= len(cal_dates) and len(daily_dates):
            return daily_dates
        if len(cal_dates):
            return cal_dates
        if self.cfg.is_blader and "factor" in self.cfg.tables:
            return self._trading_dates_from_factor(start, end)
        return daily_dates

    def _uses_factor_wide(self) -> bool:
        try:
            return self.cfg.table("factor") == WIDE_TABLE
        except KeyError:
            return False

    def _trading_dates_from_factor(self, start: str, end: str) -> pd.DatetimeIndex:
        c = self.cfg.cols("factor")
        t = self.cfg.table("factor")
        if self._uses_factor_wide():
            sql = f"""
            SELECT DISTINCT `{c.col('date')}` AS dt FROM `{t}`
            WHERE `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
              AND (
                pe_ttm IS NOT NULL OR pb_mrq IS NOT NULL OR roe_ttm IS NOT NULL
                OR mom_20d IS NOT NULL
              )
            """
        else:
            sql = f"""
            SELECT DISTINCT `{c.col('date')}` AS dt FROM `{t}`
            WHERE `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
            """
            extra = self.cfg.filter_sql("factor")
            if extra:
                sql += f" AND ({extra})"
        df = self._query_sql(sql, params={"s": _sql_date(start), "e": _sql_date(end)})
        dates = pd.DatetimeIndex(pd.to_datetime(df["dt"]).dt.normalize())
        start_d, end_d = _norm_date(start), _norm_date(end)
        return dates[(dates >= start_d) & (dates <= end_d)]

    def load_daily_field(self, field: str, start: str, end: str) -> pd.DataFrame:
        if self._parquet_usable(start, end):
            bundle = self._archive.read_bundle(start, end, fields=(field,), code_target="rq")
            panel = bundle.get(field)
            if panel is not None and not panel.empty:
                return panel

        return self._load_daily_field_sql(field, start, end)

    def _load_daily_field_sql(
        self,
        field: str,
        start: str,
        end: str,
        only_dates: list[str] | None = None,
    ) -> pd.DataFrame:
        if field == "close" and self.cfg.is_blader and "pre_close" in self.cfg.cols("daily").mapping:
            panel = self._load_daily_close_blader(start, end, only_dates=only_dates)
            start_d, end_d = _norm_date(start), _norm_date(end)
            return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

        c = self.cfg.cols("daily")
        if only_dates:
            placeholders = ", ".join(f":d{i}" for i in range(len(only_dates)))
            where = f"`{c.col('date')}` IN ({placeholders})"
            params = {f"d{i}": _sql_date(d) for i, d in enumerate(only_dates)}
        else:
            where = f"`{c.col('date')}` >= :s AND `{c.col('date')}` <= :e"
            params = {"s": _sql_date(start), "e": _sql_date(end)}
        df = self._read_table("daily", where=where, params=params)
        df = self._rename_logical(df, "daily")
        if field not in df.columns:
            raise KeyError(f"daily 表无字段 {field}")
        panel = self._to_panel(df, field, code_target="rq")
        start_d, end_d = _norm_date(start), _norm_date(end)
        return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

    def _seed_panel_caches(self, start: str, end: str, bundle: dict[str, pd.DataFrame]) -> None:
        close = bundle.get("close")
        if isinstance(close, pd.DataFrame) and not close.empty:
            key = cache_key("close", start, end)
            self._close_cache[key] = close
            self._returns_cache[cache_key("returns", start, end)] = close.pct_change(fill_method=None)

    def load_daily_bundle(self, start: str, end: str) -> dict[str, pd.DataFrame]:
        """一次加载 close/volume 等字段，优先月度宽表 / 并行日归档。"""
        key = cache_key("bundle", start, end)
        if key in self._bundle_cache:
            return self._bundle_cache[key]
        if key in _BUNDLE_CACHE_GLOBAL:
            bundle = _BUNDLE_CACHE_GLOBAL[key]
            self._bundle_cache[key] = bundle
            self._seed_panel_caches(start, end, bundle)
            return bundle
        use_redis = self.cfg.parquet_archive.bundle_use_redis
        ttl = self.cfg.parquet_archive.bundle_cache_ttl
        redis_key = f"panel:bundle:{key}"
        if use_redis:
            cached = self._cache.get_pickle(redis_key)
            if isinstance(cached, dict) and cached.get("close") is not None:
                self._bundle_cache[key] = cached
                _BUNDLE_CACHE_GLOBAL[key] = cached
                self._seed_panel_caches(start, end, cached)
                return cached
        if self._parquet_usable(start, end):
            bundle = self._archive.read_bundle(
                start, end, fields=_BUNDLE_FIELDS, code_target="rq"
            )
            if bundle.get("close") is not None and not bundle["close"].empty:
                self._bundle_cache[key] = bundle
                _BUNDLE_CACHE_GLOBAL[key] = bundle
                self._seed_panel_caches(start, end, bundle)
                if use_redis:
                    self._cache.set_pickle(redis_key, bundle, ttl=ttl)
                return bundle
        bundle = {
            "close": self._load_daily_field_sql("close", start, end),
            "volume": self._load_daily_field_sql("volume", start, end),
        }
        self._bundle_cache[key] = bundle
        _BUNDLE_CACHE_GLOBAL[key] = bundle
        self._seed_panel_caches(start, end, bundle)
        return bundle

    def load_fundamental_field(self, field: str, start: str, end: str) -> pd.DataFrame:
        if self.cfg.is_blader and "factor" in self.cfg.tables:
            return self._load_factor_long(field, start, end)

        c = self.cfg.cols("fundamental")
        df = self._read_table(
            "fundamental",
            where=f"`{c.col('date')}` >= :s AND `{c.col('date')}` <= :e",
            params={"s": _sql_date(start), "e": _sql_date(end)},
        )
        df = self._rename_logical(df, "fundamental")
        if field not in df.columns:
            raise KeyError(f"fundamental 表无字段 {field}")
        panel = self._to_panel(df, field, code_target="rq")
        start_d, end_d = _norm_date(start), _norm_date(end)
        return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

    def list_factor_base_info(self) -> list[dict]:
        """读取 factor_base_info 全部有效因子元数据。"""
        if "factor_base" not in self.cfg.tables:
            raise KeyError("配置 tables 缺少 factor_base（如 factor_base_info）")
        df = self._read_table("factor_base")
        df = self._rename_logical(df, "factor_base")
        if "is_valid" in df.columns:
            df = df[df["is_valid"].fillna(1).astype(int) == 1]
        records = []
        for _, row in df.iterrows():
            records.append(
                {
                    "factor_code": str(row["factor_code"]),
                    "factor_name": row.get("factor_name"),
                    "factor_type": row.get("factor_type"),
                    "factor_desc": row.get("factor_desc"),
                    "ths_indicator": row.get("ths_indicator"),
                    "sort_type": row.get("sort_type") or "desc",
                }
            )
        return records

    def _factor_meta_map(self) -> dict[str, dict]:
        if self._meta_map is None:
            self._meta_map = {
                m["factor_code"].upper(): m for m in self.list_factor_base_info()
            }
        return self._meta_map

    def get_factor_meta(self, factor_code: str) -> dict | None:
        return self._factor_meta_map().get(str(factor_code).upper())

    def query_data_date_range(self) -> tuple[str, str]:
        """快速查询日K可用区间（避免全表扫描）。"""
        if self._date_range_cache:
            return self._date_range_cache
        key = "meta:data_date_range"
        cached = self._cache.get_json(key)
        if cached:
            self._date_range_cache = (cached["start"], cached["end"])
            return self._date_range_cache
        c = self.cfg.cols("daily")
        t = self.cfg.table("daily")
        sql = f"SELECT MIN(`{c.col('date')}`) AS s, MAX(`{c.col('date')}`) AS e FROM `{t}`"
        df = self._query_sql(sql)
        if df.empty or pd.isna(df.iloc[0]["s"]):
            raise ValueError("无法查询日K日期区间")
        start = pd.Timestamp(df.iloc[0]["s"]).strftime("%Y%m%d")
        end = pd.Timestamp(df.iloc[0]["e"]).strftime("%Y%m%d")
        self._date_range_cache = (start, end)
        self._cache.set_json(key, {"start": start, "end": end}, ttl=3600)
        return self._date_range_cache

    def load_factor_panel_by_code(
        self, factor_code: str, start: str, end: str
    ) -> pd.DataFrame:
        """按 factor_code 从 Parquet / factor_data_wide 加载宽表面板。"""
        fc = factor_code.upper()
        mem_key = cache_key("factor", fc, start, end)
        if mem_key in self._factor_panel_cache:
            return self._factor_panel_cache[mem_key]
        if mem_key in _FACTOR_PANEL_CACHE_GLOBAL:
            panel = _FACTOR_PANEL_CACHE_GLOBAL[mem_key]
            self._factor_panel_cache[mem_key] = panel
            return panel
        use_redis = self.cfg.parquet_archive.factor.use_redis
        redis_key = f"panel:factor:{mem_key}"
        if use_redis:
            cached = self._cache.get_pickle(redis_key)
            if isinstance(cached, pd.DataFrame) and not cached.empty:
                self._factor_panel_cache[mem_key] = cached
                _FACTOR_PANEL_CACHE_GLOBAL[mem_key] = cached
                return cached
        panel = self._load_factor_panel(fc, start, end, value_name="value")
        if not panel.empty:
            self._factor_panel_cache[mem_key] = panel
            _FACTOR_PANEL_CACHE_GLOBAL[mem_key] = panel
            if use_redis:
                self._cache.set_pickle(redis_key, panel, ttl=self._panel_ttl)
        return panel

    def load_factor_panel_sql(
        self, factor_code: str, start: str, end: str, value_name: str = "value"
    ) -> pd.DataFrame:
        """仅从数据库加载因子宽表（构建 Parquet 归档时使用）。"""
        return self._load_factor_panel_sql(factor_code, start, end, value_name=value_name)

    def _load_factor_panel(
        self, factor_code: str, start: str, end: str, value_name: str = "value"
    ) -> pd.DataFrame:
        fc = factor_code.upper()
        if self._factor_parquet_usable(fc, start, end):
            panel = self._factor_archive.read_panel(fc, start, end)
            if panel is not None and not panel.empty:
                return panel
        return self._load_factor_panel_sql(fc, start, end, value_name=value_name)

    def _load_factor_panel_sql(
        self, factor_code: str, start: str, end: str, value_name: str = "value"
    ) -> pd.DataFrame:
        c = self.cfg.cols("factor")
        t = self.cfg.table("factor")
        fc = factor_code.upper()

        if self._uses_factor_wide():
            col = wide_column_for_factor(fc)
            if col:
                sql = f"""
                SELECT `{c.col('date')}` AS dt,
                       `{c.col('code')}` AS sym,
                       `{col}` AS val
                FROM `{t}`
                WHERE `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
                  AND `{col}` IS NOT NULL
                """
                params = {"s": _sql_date(start), "e": _sql_date(end)}
            else:
                sql = f"""
                SELECT `{c.col('date')}` AS dt,
                       `{c.col('code')}` AS sym,
                       CAST(JSON_UNQUOTE(JSON_EXTRACT(factor_ext_json, :path)) AS DECIMAL(24,8)) AS val
                FROM `{t}`
                WHERE `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
                  AND JSON_EXTRACT(factor_ext_json, :path) IS NOT NULL
                """
                params = {
                    "s": _sql_date(start),
                    "e": _sql_date(end),
                    "path": f"$.{fc}",
                }
        else:
            sql = f"""
            SELECT `{c.col('date')}` AS dt,
                   `{c.col('code')}` AS sym,
                   `{c.col('value')}` AS val
            FROM `{t}`
            WHERE `{c.col('factor_code')}` = :fc
              AND `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
            """
            extra = self.cfg.filter_sql("factor")
            if extra:
                sql += f" AND ({extra})"
            params = {"fc": fc, "s": _sql_date(start), "e": _sql_date(end)}

        df = self._query_sql(sql, params=params)
        df = df.rename(columns={"dt": "date", "sym": "code", "val": value_name})
        panel = self._to_panel(df, value_name, code_target="rq")
        start_d, end_d = _norm_date(start), _norm_date(end)
        return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

    def _load_factor_long(self, field: str, start: str, end: str) -> pd.DataFrame:
        fc = self.cfg.factor_mapping.get(field)
        if not fc:
            raise KeyError(
                f"factor_mapping 未配置因子 {field}，请在 ifind_config.yaml 的 factor_mapping 中指定 factor_code"
            )
        return self._load_factor_panel(fc, start, end, value_name=field)

    def align_to_trading_days(self, panel: pd.DataFrame, trading_index: pd.DatetimeIndex) -> pd.DataFrame:
        if panel.empty:
            return panel
        union_idx = trading_index.union(panel.index).sort_values()
        wide = panel.reindex(union_idx).ffill()
        return wide.reindex(trading_index)

    def get_index_members(self, as_of: str, index_code: str | None = None) -> list[str]:
        if self.cfg.universe_mode in ("factor_distinct", "daily_distinct"):
            return self._get_universe_distinct(as_of)

        index_code = index_code or self.cfg.universe_index
        if self.cfg.code_format == "rq":
            index_code = normalize_code(index_code, "ths") if "." in index_code else index_code
        as_of = _norm_date(as_of)

        if "index_members" in self.cfg.tables:
            try:
                c = self.cfg.cols("index_members")
                t = self.cfg.table("index_members")
                sql = f"""
                SELECT `{c.col('date')}` AS dt, `{c.col('code')}` AS sym
                FROM `{t}`
                WHERE `{c.col('index_code')}` = :idx
                """
                extra = self.cfg.filter_sql("index_members")
                if extra:
                    sql += f" AND ({extra})"
                df = self._query_sql(sql, params={"idx": index_code})
                if not df.empty:
                    df["dt"] = pd.to_datetime(df["dt"]).dt.normalize()
                    sub = df[df["dt"] <= as_of]
                    if not sub.empty:
                        last_dt = sub["dt"].max()
                        codes = sub.loc[sub["dt"] == last_dt, "sym"].astype(str).tolist()
                        return normalize_codes(codes, "rq")
            except Exception:
                pass

        from multi_factor.ifind.index_members import fetch_members_snapshot

        _, codes = fetch_members_snapshot(index_code, as_of.strftime("%Y%m%d"))
        return normalize_codes(codes, "rq")

    def _get_universe_distinct(self, as_of: str) -> list[str]:
        as_of = _norm_date(as_of)
        if self.cfg.universe_mode == "daily_distinct":
            c = self.cfg.cols("daily")
            t = self.cfg.table("daily")
            sql = f"""
            SELECT DISTINCT `{c.col('code')}` AS sym FROM `{t}`
            WHERE `{c.col('date')}` <= :d
            """
            extra = self.cfg.filter_sql("daily")
            if extra:
                sql += f" AND ({extra})"
            df = self._query_sql(sql, params={"d": _sql_date(as_of.strftime("%Y%m%d"))})
        else:
            c = self.cfg.cols("factor")
            t = self.cfg.table("factor")
            params = {"d": _sql_date(as_of.strftime("%Y%m%d"))}
            if self._uses_factor_wide():
                sql = f"""
                SELECT DISTINCT `{c.col('code')}` AS sym FROM `{t}`
                WHERE `{c.col('date')}` <= :d
                  AND (pe_ttm IS NOT NULL OR pb_mrq IS NOT NULL OR roe_ttm IS NOT NULL)
                """
            else:
                codes = list(self.cfg.factor_mapping.values())
                placeholders = ",".join([f":f{i}" for i in range(len(codes))])
                for i, code in enumerate(codes):
                    params[f"f{i}"] = code
                sql = f"""
                SELECT DISTINCT `{c.col('code')}` AS sym FROM `{t}`
                WHERE `{c.col('date')}` <= :d
                  AND `{c.col('factor_code')}` IN ({placeholders})
                """
                extra = self.cfg.filter_sql("factor")
                if extra:
                    sql += f" AND ({extra})"
            df = self._query_sql(sql, params=params)

        return normalize_codes(df["sym"].astype(str).tolist(), "rq")

    def get_universe(self, start: str, end: str) -> list[str]:
        members = self.get_index_members(start)
        try:
            close = self.load_daily_field("close", start, end)
            valid = close.columns[close.notna().any()].tolist()
            if valid:
                return sorted(set(members) & set(valid))
        except Exception:
            pass
        return sorted(members)

    def load_daily_close(self, start: str, end: str) -> pd.DataFrame:
        key = cache_key("close", start, end)
        if key in self._close_cache:
            return self._close_cache[key]
        redis_key = f"panel:close:{key}"
        cached = self._cache.get_pickle(redis_key)
        if isinstance(cached, pd.DataFrame) and not cached.empty:
            self._close_cache[key] = cached
            return cached
        close = self.load_daily_field("close", start, end)
        if not close.empty:
            self._close_cache[key] = close
            self._cache.set_pickle(redis_key, close, ttl=self._panel_ttl)
        return close

    def get_daily_returns(self, start: str, end: str) -> pd.DataFrame:
        key = cache_key("returns", start, end)
        if key in self._returns_cache:
            return self._returns_cache[key]
        close = self.load_daily_close(start, end)
        rets = close.pct_change(fill_method=None)
        self._returns_cache[key] = rets
        return rets

    def _load_benchmark_from_daily(self, start: str, end: str) -> pd.Series | None:
        """从日 K 表按基准指数代码拉取单序列（小查询，避免重载全市场）。"""
        try:
            c = self.cfg.cols("daily")
            t = self.cfg.table("daily")
            idx_code = self.cfg.benchmark_index
            where = (
                f"`{c.col('code')}` = :idx "
                f"AND `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e"
            )
            where = self._append_filter(where, "daily")
            sql = f"""
            SELECT `{c.col('date')}` AS dt, `{c.col('close')}` AS px
            FROM `{t}`
            WHERE {where}
            ORDER BY `{c.col('date')}`
            """
            df = self._query_sql(
                sql,
                params={"idx": idx_code, "s": _sql_date(start), "e": _sql_date(end)},
            )
            if df.empty:
                return None
            s = pd.to_datetime(df["dt"]).dt.normalize()
            out = pd.Series(df["px"].astype(float).values, index=s).sort_index().pct_change()
            start_d, end_d = _norm_date(start), _norm_date(end)
            return out.loc[(out.index >= start_d) & (out.index <= end_d)]
        except Exception:
            return None

    def get_benchmark_returns(
        self,
        start: str,
        end: str,
        *,
        market_returns: pd.DataFrame | None = None,
    ) -> pd.Series:
        key = cache_key("benchmark", start, end)
        if key in self._benchmark_cache:
            return self._benchmark_cache[key]
        if key in _BENCHMARK_CACHE_GLOBAL:
            self._benchmark_cache[key] = _BENCHMARK_CACHE_GLOBAL[key]
            return _BENCHMARK_CACHE_GLOBAL[key]
        if "index_daily" in self.cfg.tables:
            try:
                c = self.cfg.cols("index_daily")
                df = self._read_table(
                    "index_daily",
                    where=f"`{c.col('date')}` >= :s AND `{c.col('date')}` <= :e",
                    params={"s": _sql_date(start), "e": _sql_date(end)},
                )
                df = self._rename_logical(df, "index_daily")
                idx_code = self.cfg.benchmark_index
                sub = df[df["index_code"].astype(str) == str(idx_code)].copy()
                if not sub.empty:
                    sub["date"] = pd.to_datetime(sub["date"]).dt.normalize()
                    s = sub.set_index("date")["close"].sort_index().pct_change()
                    start_d, end_d = _norm_date(start), _norm_date(end)
                    out = s.loc[(s.index >= start_d) & (s.index <= end_d)]
                    self._benchmark_cache[key] = out
                    _BENCHMARK_CACHE_GLOBAL[key] = out
                    return out
            except (KeyError, FileNotFoundError, ValueError):
                pass
        bench = self._load_benchmark_from_daily(start, end)
        if bench is not None and not bench.empty:
            self._benchmark_cache[key] = bench
            _BENCHMARK_CACHE_GLOBAL[key] = bench
            return bench
        if market_returns is not None and not market_returns.empty:
            out = market_returns.mean(axis=1)
            self._benchmark_cache[key] = out
            _BENCHMARK_CACHE_GLOBAL[key] = out
            return out
        rets_key = cache_key("returns", start, end)
        if rets_key in self._returns_cache:
            out = self._returns_cache[rets_key].mean(axis=1)
            self._benchmark_cache[key] = out
            _BENCHMARK_CACHE_GLOBAL[key] = out
            return out
        return self.get_daily_returns(start, end).mean(axis=1)
