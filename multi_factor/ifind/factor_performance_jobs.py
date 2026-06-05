# -*- coding: utf-8 -*-
"""因子维度绩效任务：计算并落库 IC/夏普/分组收益等统计。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

from multi_factor.ifind.config_loader import IFindConfig
from multi_factor.ifind.factor_metrics import _sort_ascending, build_factor_report
from multi_factor.ifind.provider import IFindDataProvider


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


class FactorPerformanceStorage:
    """绩效结果落库。"""

    def __init__(self, cfg: IFindConfig):
        if not cfg.db_url:
            raise ValueError("当前任务仅支持数据库模式，请配置 ifind_config.yaml database")
        self.cfg = cfg
        self.engine = create_engine(cfg.db_url, pool_pre_ping=True)
        self.project_root = Path(__file__).resolve().parent.parent.parent

    def _run_sql_script(self, relative_path: str, ignore_errors: bool = False) -> None:
        path = self.project_root / relative_path
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        stmts: list[str] = []
        buf: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            buf.append(line)
            if stripped.endswith(";"):
                stmts.append("\n".join(buf).rstrip().rstrip(";"))
                buf = []
        if buf:
            stmts.append("\n".join(buf).rstrip().rstrip(";"))
        with self.engine.begin() as conn:
            for stmt in stmts:
                if ignore_errors:
                    try:
                        conn.execute(text(stmt))
                    except Exception:
                        pass
                else:
                    conn.execute(text(stmt))

    def ensure_tables(self) -> None:
        self._run_sql_script("scripts/sql/factor_performance_create_tables.sql")
        self._run_sql_script("scripts/sql/factor_performance_alter_comments.sql", ignore_errors=True)

    def upsert_summary(
        self,
        factor_code: str,
        start: str,
        end: str,
        period: int,
        quantiles: int,
        top_pct: float,
        summary: dict[str, Any],
    ) -> None:
        sql = """
        INSERT INTO factor_performance_summary (
          factor_code, start_date, end_date, period, quantiles, top_pct,
          ic_mean, ic_std, ic_ir, win_rate,
          positive_count, negative_count, total_count,
          sharpe_top_group, sharpe_bottom_group, sharpe_long_short,
          data_start, data_end, stock_count_avg, calc_version, updated_at
        ) VALUES (
          :factor_code, :start_date, :end_date, :period, :quantiles, :top_pct,
          :ic_mean, :ic_std, :ic_ir, :win_rate,
          :positive_count, :negative_count, :total_count,
          :sharpe_top_group, :sharpe_bottom_group, :sharpe_long_short,
          :data_start, :data_end, :stock_count_avg, 'v1', :updated_at
        )
        ON DUPLICATE KEY UPDATE
          ic_mean=VALUES(ic_mean),
          ic_std=VALUES(ic_std),
          ic_ir=VALUES(ic_ir),
          win_rate=VALUES(win_rate),
          positive_count=VALUES(positive_count),
          negative_count=VALUES(negative_count),
          total_count=VALUES(total_count),
          sharpe_top_group=VALUES(sharpe_top_group),
          sharpe_bottom_group=VALUES(sharpe_bottom_group),
          sharpe_long_short=VALUES(sharpe_long_short),
          data_start=VALUES(data_start),
          data_end=VALUES(data_end),
          stock_count_avg=VALUES(stock_count_avg),
          updated_at=VALUES(updated_at)
        """
        params = {
            "factor_code": factor_code,
            "start_date": _sql_date(start),
            "end_date": _sql_date(end),
            "period": period,
            "quantiles": quantiles,
            "top_pct": round(float(top_pct), 4),
            "ic_mean": summary.get("ic_mean"),
            "ic_std": summary.get("ic_std"),
            "ic_ir": summary.get("ic_ir"),
            "win_rate": summary.get("win_rate"),
            "positive_count": summary.get("positive_count"),
            "negative_count": summary.get("negative_count"),
            "total_count": summary.get("total_count"),
            "sharpe_top_group": summary.get("sharpe_top_group"),
            "sharpe_bottom_group": summary.get("sharpe_bottom_group"),
            "sharpe_long_short": summary.get("sharpe_long_short"),
            "data_start": summary.get("data_start"),
            "data_end": summary.get("data_end"),
            "stock_count_avg": summary.get("stock_count_avg"),
            "updated_at": datetime.now(),
        }
        with self.engine.begin() as conn:
            conn.execute(text(sql), params)

    def upsert_series_rows(self, rows: list[dict], chunk_size: int = 2000) -> int:
        if not rows:
            return 0
        sql = """
        INSERT INTO factor_performance_series (
          factor_code, start_date, end_date, period,
          series_type, series_date, payload_json, updated_at
        ) VALUES (
          :factor_code, :start_date, :end_date, :period,
          :series_type, :series_date, CAST(:payload_json AS JSON), :updated_at
        )
        ON DUPLICATE KEY UPDATE
          payload_json=VALUES(payload_json),
          updated_at=VALUES(updated_at)
        """
        with self.engine.begin() as conn:
            for i in range(0, len(rows), chunk_size):
                conn.execute(text(sql), rows[i : i + chunk_size])
        return len(rows)


def _norm_yyyymmdd(s: str) -> str:
    return str(s).replace("-", "")[:8]


class FactorPerformanceReader:
    """从 factor_performance_* 读取已落库的因子维度统计。"""

    def __init__(self, cfg: IFindConfig):
        if not cfg.db_url:
            raise ValueError("需要数据库连接")
        self.cfg = cfg
        self.engine = create_engine(cfg.db_url, pool_pre_ping=True)

    def load_summary(
        self,
        factor_code: str,
        start: str,
        end: str,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
    ) -> dict[str, Any] | None:
        sql = """
        SELECT *
        FROM factor_performance_summary
        WHERE factor_code = :fc
          AND start_date = :s AND end_date = :e
          AND period = :p AND quantiles = :q AND top_pct = :t
        LIMIT 1
        """
        df = pd.read_sql(
            text(sql),
            self.engine,
            params={
                "fc": factor_code.upper(),
                "s": _sql_date(start),
                "e": _sql_date(end),
                "p": period,
                "q": quantiles,
                "t": round(float(top_pct), 4),
            },
        )
        if df.empty:
            return None
        row = df.iloc[0]
        return {
            "ic_mean": float(row["ic_mean"]) if pd.notna(row.get("ic_mean")) else None,
            "ic_std": float(row["ic_std"]) if pd.notna(row.get("ic_std")) else None,
            "ic_ir": float(row["ic_ir"]) if pd.notna(row.get("ic_ir")) else None,
            "win_rate": float(row["win_rate"]) if pd.notna(row.get("win_rate")) else None,
            "positive_count": int(row["positive_count"]) if pd.notna(row.get("positive_count")) else 0,
            "negative_count": int(row["negative_count"]) if pd.notna(row.get("negative_count")) else 0,
            "total_count": int(row["total_count"]) if pd.notna(row.get("total_count")) else 0,
            "sharpe_top_group": float(row["sharpe_top_group"])
            if pd.notna(row.get("sharpe_top_group"))
            else None,
            "sharpe_bottom_group": float(row["sharpe_bottom_group"])
            if pd.notna(row.get("sharpe_bottom_group"))
            else None,
            "sharpe_long_short": float(row["sharpe_long_short"])
            if pd.notna(row.get("sharpe_long_short"))
            else None,
            "data_start": str(row["data_start"])[:10] if pd.notna(row.get("data_start")) else None,
            "data_end": str(row["data_end"])[:10] if pd.notna(row.get("data_end")) else None,
            "stock_count_avg": int(row["stock_count_avg"])
            if pd.notna(row.get("stock_count_avg"))
            else 0,
        }

    def load_series(
        self,
        factor_code: str,
        start: str,
        end: str,
        period: int = 1,
    ) -> pd.DataFrame:
        sql = """
        SELECT series_type, series_date, payload_json
        FROM factor_performance_series
        WHERE factor_code = :fc
          AND start_date = :s AND end_date = :e
          AND period = :p
        ORDER BY series_date
        """
        return pd.read_sql(
            text(sql),
            self.engine,
            params={
                "fc": factor_code.upper(),
                "s": _sql_date(start),
                "e": _sql_date(end),
                "p": period,
            },
        )

    def list_summaries(
        self,
        start: str,
        end: str,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
    ) -> dict[str, dict]:
        """factor_code -> summary 字典。"""
        sql = """
        SELECT factor_code, ic_mean, ic_ir, win_rate,
               sharpe_top_group, sharpe_bottom_group, sharpe_long_short,
               data_start, data_end
        FROM factor_performance_summary
        WHERE start_date = :s AND end_date = :e
          AND period = :p AND quantiles = :q AND top_pct = :t
        """
        df = pd.read_sql(
            text(sql),
            self.engine,
            params={
                "s": _sql_date(start),
                "e": _sql_date(end),
                "p": period,
                "q": quantiles,
                "t": round(float(top_pct), 4),
            },
        )
        out: dict[str, dict] = {}
        for _, row in df.iterrows():
            code = str(row["factor_code"]).upper()
            out[code] = {
                "ic_mean": float(row["ic_mean"]) if pd.notna(row["ic_mean"]) else None,
                "ic_ir": float(row["ic_ir"]) if pd.notna(row["ic_ir"]) else None,
                "win_rate": float(row["win_rate"]) if pd.notna(row["win_rate"]) else None,
                "sharpe_long_short": float(row["sharpe_long_short"])
                if pd.notna(row["sharpe_long_short"])
                else None,
                "sharpe_top_group": float(row["sharpe_top_group"])
                if pd.notna(row["sharpe_top_group"])
                else None,
                "data_start": str(row["data_start"])[:10] if pd.notna(row["data_start"]) else None,
                "data_end": str(row["data_end"])[:10] if pd.notna(row["data_end"]) else None,
            }
        return out

    @staticmethod
    def _parse_payload(raw: Any) -> dict:
        if raw is None or (isinstance(raw, float) and pd.isna(raw)):
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        return json.loads(str(raw))

    def build_report(
        self,
        meta: dict,
        start: str,
        end: str,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
    ) -> dict[str, Any] | None:
        summary = self.load_summary(
            meta["factor_code"], start, end, period=period, quantiles=quantiles, top_pct=top_pct
        )
        if summary is None:
            return None
        sdf = self.load_series(meta["factor_code"], start, end, period=period)
        ic_trend: list[dict] = []
        group_returns: list[dict] = []
        quantile_returns: dict[str, list[dict]] = {}

        for _, row in sdf.iterrows():
            payload = self._parse_payload(row["payload_json"])
            dt = str(row["series_date"])[:10]
            stype = str(row["series_type"])
            if stype == "ic":
                ic_trend.append({"date": dt, "ic": payload.get("ic")})
            elif stype == "group":
                group_returns.append(
                    {
                        "date": dt,
                        "top_group": payload.get("top_group"),
                        "bottom_group": payload.get("bottom_group"),
                        "top_group_nav": payload.get("top_group_nav"),
                        "bottom_group_nav": payload.get("bottom_group_nav"),
                    }
                )
            elif stype == "quantile":
                qname = str(payload.get("quantile", "q1"))
                quantile_returns.setdefault(qname, []).append(
                    {
                        "date": dt,
                        "return": payload.get("return"),
                        "nav": payload.get("nav"),
                    }
                )

        return {
            "meta": meta,
            "period": period,
            "ascending": _sort_ascending(meta.get("sort_type")),
            "summary": summary,
            "ic_trend": ic_trend,
            "group_returns": group_returns,
            "quantile_returns": quantile_returns,
            "data_source": "db",
        }


class FactorPerformanceJobRunner:
    """因子绩效任务：读取因子面板+收益，计算并落表。"""

    def __init__(self, cfg: IFindConfig):
        self.cfg = cfg
        self.provider = IFindDataProvider(cfg)
        self.storage = FactorPerformanceStorage(cfg)

    def _series_rows(
        self,
        factor_code: str,
        start: str,
        end: str,
        period: int,
        report: dict[str, Any],
    ) -> list[dict]:
        rows: list[dict] = []
        now = datetime.now()
        for item in report.get("ic_trend", []):
            rows.append(
                {
                    "factor_code": factor_code,
                    "start_date": _sql_date(start),
                    "end_date": _sql_date(end),
                    "period": period,
                    "series_type": "ic",
                    "series_date": item["date"],
                    "payload_json": pd.Series(item).to_json(force_ascii=False),
                    "updated_at": now,
                }
            )
        for item in report.get("group_returns", []):
            rows.append(
                {
                    "factor_code": factor_code,
                    "start_date": _sql_date(start),
                    "end_date": _sql_date(end),
                    "period": period,
                    "series_type": "group",
                    "series_date": item["date"],
                    "payload_json": pd.Series(item).to_json(force_ascii=False),
                    "updated_at": now,
                }
            )
        quant = report.get("quantile_returns", {})
        for qname, series in quant.items():
            for item in series:
                payload = {"quantile": qname, **item}
                rows.append(
                    {
                        "factor_code": factor_code,
                        "start_date": _sql_date(start),
                        "end_date": _sql_date(end),
                        "period": period,
                        "series_type": "quantile",
                        "series_date": item["date"],
                        "payload_json": pd.Series(payload).to_json(force_ascii=False),
                        "updated_at": now,
                    }
                )
        return rows

    def run_one(
        self,
        factor_code: str,
        start: str,
        end: str,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
    ) -> dict[str, Any]:
        factor_code = factor_code.upper()
        meta = self.provider.get_factor_meta(factor_code) or {
            "factor_code": factor_code,
            "sort_type": "desc",
        }
        close = self.provider.get_daily_returns(start, end)
        panel = self.provider.load_factor_panel_by_code(factor_code, start, end)
        if panel.empty:
            panel = self.provider.load_factor_panel_by_code(factor_code, "20200101", end)
        if panel.empty:
            raise ValueError(f"因子 {factor_code} 无数据")
        panel = self.provider.align_to_trading_days(panel, close.index)
        common = panel.columns.intersection(close.columns)
        panel = panel[common]
        close = close[common]
        if panel.empty or close.empty:
            raise ValueError(f"因子 {factor_code} 与收益无交集")

        report = build_factor_report(
            panel,
            close,
            meta=meta,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
        )
        self.storage.upsert_summary(
            factor_code=factor_code,
            start=start,
            end=end,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
            summary=report.get("summary", {}),
        )
        series_rows = self._series_rows(factor_code, start, end, period, report)
        n = self.storage.upsert_series_rows(series_rows)
        return {
            "factor_code": factor_code,
            "summary": report.get("summary", {}),
            "series_rows": n,
            "start": start,
            "end": end,
        }

    def run_batch(
        self,
        factor_codes: list[str] | None,
        start: str,
        end: str,
        period: int = 1,
        quantiles: int = 5,
        top_pct: float = 0.2,
    ) -> dict[str, Any]:
        self.storage.ensure_tables()
        if not factor_codes:
            factor_codes = [m["factor_code"] for m in self.provider.list_factor_base_info()]
        out = {"success": [], "failed": []}
        for code in factor_codes:
            try:
                out["success"].append(
                    self.run_one(code, start, end, period=period, quantiles=quantiles, top_pct=top_pct)
                )
            except Exception as ex:
                out["failed"].append({"factor_code": code, "error": str(ex)})
        return out

