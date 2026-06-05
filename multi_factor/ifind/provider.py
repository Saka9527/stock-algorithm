# -*- coding: utf-8 -*-
"""从 iFinD / Blader 同步库（SQL / CSV）读取行情、因子、指数成分。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from multi_factor.ifind.code_convert import normalize_code, normalize_codes
from multi_factor.ifind.config_loader import IFindConfig
from multi_factor.ifind.factor_wide import (
    WIDE_TABLE,
    wide_column_for_factor,
)


def _norm_date(s) -> pd.Timestamp:
    return pd.to_datetime(s).normalize()


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


class IFindDataProvider:
    """读取同步表，输出宽表 panel（index=日期, columns=股票代码 RQ 格式）。"""

    def __init__(self, cfg: IFindConfig):
        self.cfg = cfg
        self._use_csv = bool(cfg.csv_dir) and not cfg.db_url
        self._engine = None
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

    def _load_daily_close_blader(self, start: str, end: str) -> pd.DataFrame:
        """Blader 旧表 stock_history_daily：close 为 0 时回退 pre_close。"""
        c = self.cfg.cols("daily")
        t = self.cfg.table("daily")
        sql = f"""
        SELECT `{c.col('date')}` AS dt,
               `{c.col('code')}` AS sym,
               CASE
                 WHEN `{c.col('close')}` IS NULL OR `{c.col('close')}` = 0
                 THEN `{c.col('pre_close')}`
                 ELSE `{c.col('close')}`
               END AS px
        FROM `{t}`
        WHERE `{c.col('date')}` >= :s AND `{c.col('date')}` <= :e
        """
        extra = self.cfg.filter_sql("daily")
        if extra:
            sql += f" AND ({extra})"
        df = self._query_sql(sql, params={"s": _sql_date(start), "e": _sql_date(end)})
        df = df.rename(columns={"dt": "date", "sym": "code", "px": "close"})
        return self._to_panel(df, "close", code_target="rq")

    def get_trading_dates(self, start: str, end: str) -> pd.DatetimeIndex:
        start_d, end_d = _norm_date(start), _norm_date(end)
        cal_key = "calendar" if "calendar" in self.cfg.tables else None
        if cal_key:
            try:
                df = self._rename_logical(self._read_table(cal_key), cal_key)
                dates = pd.DatetimeIndex(pd.to_datetime(df["date"]).dt.normalize())
                out = dates[(dates >= start_d) & (dates <= end_d)]
                if len(out):
                    return out
            except (FileNotFoundError, ValueError, KeyError):
                pass

        daily = self.load_daily_field("close", start, end)
        if daily.empty and self.cfg.is_blader and "factor" in self.cfg.tables:
            return self._trading_dates_from_factor(start, end)
        return daily.index[(daily.index >= start_d) & (daily.index <= end_d)]

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
        if field == "close" and self.cfg.is_blader and "pre_close" in self.cfg.cols("daily").mapping:
            panel = self._load_daily_close_blader(start, end)
            start_d, end_d = _norm_date(start), _norm_date(end)
            return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

        c = self.cfg.cols("daily")
        df = self._read_table(
            "daily",
            where=f"`{c.col('date')}` >= :s AND `{c.col('date')}` <= :e",
            params={"s": _sql_date(start), "e": _sql_date(end)},
        )
        df = self._rename_logical(df, "daily")
        if field not in df.columns:
            raise KeyError(f"daily 表无字段 {field}")
        panel = self._to_panel(df, field, code_target="rq")
        start_d, end_d = _norm_date(start), _norm_date(end)
        return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

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

    def get_factor_meta(self, factor_code: str) -> dict | None:
        code = str(factor_code).upper()
        for item in self.list_factor_base_info():
            if item["factor_code"].upper() == code:
                return item
        return None

    def load_factor_panel_by_code(
        self, factor_code: str, start: str, end: str
    ) -> pd.DataFrame:
        """按 factor_code 从 factor_data_wide（或旧长表）加载宽表面板。"""
        return self._load_factor_panel(factor_code.upper(), start, end, value_name="value")

    def _load_factor_panel(
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
        c = self.cfg.cols("index_members")
        df = self._read_table("index_members")
        df = self._rename_logical(df, "index_members")
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        as_of = _norm_date(as_of)
        sub = df[df["index_code"].astype(str) == str(index_code)]
        if sub.empty:
            raise ValueError(f"指数成分为空: index_code={index_code}")
        last_dt = sub[sub["date"] <= as_of]["date"].max()
        if pd.isna(last_dt):
            last_dt = sub["date"].min()
        codes = sub.loc[sub["date"] == last_dt, "code"].astype(str).tolist()
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

    def get_daily_returns(self, start: str, end: str) -> pd.DataFrame:
        close = self.load_daily_field("close", start, end)
        return close.pct_change()

    def get_benchmark_returns(self, start: str, end: str) -> pd.Series:
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
                    return s.loc[(s.index >= start_d) & (s.index <= end_d)]
            except (KeyError, FileNotFoundError, ValueError):
                pass
        return self.get_daily_returns(start, end).mean(axis=1)
