# -*- coding: utf-8 -*-
"""每日/月度 Parquet 归档：并行读取 + 预透视宽表，加速全量区间加载。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import pandas as pd

from multi_factor.ifind.code_convert import normalize_codes

if TYPE_CHECKING:
    from multi_factor.ifind.provider import IFindDataProvider

logger = logging.getLogger(__name__)

STANDARD_COLUMNS = ("open", "high", "low", "close", "volume")
META_FILE = "_archive_meta.json"
DATES_INDEX_FILE = "_dates_index.json"


def _norm_date(s) -> pd.Timestamp:
    return pd.to_datetime(s).normalize()


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _yyyymmdd(s) -> str:
    return pd.Timestamp(s).strftime("%Y%m%d")


def _months_between(start: str, end: str) -> list[str]:
    idx = pd.period_range(_norm_date(start), _norm_date(end), freq="M")
    return [p.strftime("%Y%m") for p in idx]


class DailyParquetArchive:
    """
    目录结构:
      daily/20230605.parquet          # 长表，按日
      monthly/202306_close.parquet    # 宽表，按月预透视（读取更快）
    """

    def __init__(self, root: str | Path, read_workers: int = 8, use_monthly_wide: bool = True):
        self.root = Path(root)
        self.daily_dir = self.root / "daily"
        self.monthly_dir = self.root / "monthly"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.monthly_dir.mkdir(parents=True, exist_ok=True)
        self.read_workers = max(1, int(read_workers))
        self.use_monthly_wide = use_monthly_wide
        self._dates_cache: list[str] | None = None

    def file_path(self, trade_date: str) -> Path:
        return self.daily_dir / f"{_yyyymmdd(trade_date)}.parquet"

    def monthly_path(self, year_month: str, field: str) -> Path:
        return self.monthly_dir / f"{year_month}_{field}.parquet"

    def _load_dates_index(self) -> list[str]:
        if self._dates_cache is not None:
            return self._dates_cache
        idx_path = self.root / DATES_INDEX_FILE
        if idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                self._dates_cache = list(data.get("dates") or [])
                if self._dates_cache:
                    return self._dates_cache
            except Exception:
                pass
        self._dates_cache = sorted(p.stem for p in self.daily_dir.glob("*.parquet") if p.stem.isdigit())
        return self._dates_cache

    def _refresh_dates_index(self) -> None:
        dates = sorted(p.stem for p in self.daily_dir.glob("*.parquet") if p.stem.isdigit())
        self._dates_cache = dates
        (self.root / DATES_INDEX_FILE).write_text(
            json.dumps({"dates": dates, "updated_at": datetime.now().isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )

    def list_dates(self) -> list[str]:
        return list(self._load_dates_index())

    def dates_in_range(self, start: str, end: str) -> list[str]:
        start_s, end_s = _yyyymmdd(start), _yyyymmdd(end)
        return [d for d in self._load_dates_index() if start_s <= d <= end_s]

    def coverage_ratio(self, start: str, end: str, expected_dates: list[str] | None = None) -> float:
        expected_dates = expected_dates or self.dates_in_range(start, end)
        if not expected_dates:
            return 0.0
        have = set(self.dates_in_range(start, end))
        return sum(1 for d in expected_dates if d in have) / len(expected_dates)

    def monthly_coverage(self, start: str, end: str, fields: tuple[str, ...] = ("close", "volume")) -> float:
        months = _months_between(start, end)
        if not months:
            return 0.0
        hit = 0
        for ym in months:
            if all(self.monthly_path(ym, f).is_file() for f in fields):
                hit += 1
        return hit / len(months)

    def write_day(self, trade_date: str, df: pd.DataFrame) -> Path:
        path = self.file_path(trade_date)
        out = df.copy()
        if "trade_date" not in out.columns:
            out["trade_date"] = pd.Timestamp(_norm_date(trade_date))
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
        out["stock_code"] = out["stock_code"].astype(str)
        cols = ["trade_date", "stock_code", *STANDARD_COLUMNS]
        cols = [c for c in cols if c in out.columns]
        out[cols].to_parquet(path, index=False, compression="zstd")
        return path

    @staticmethod
    def _read_one_parquet(path: Path) -> pd.DataFrame:
        df = pd.read_parquet(path)
        if "stock_code" not in df.columns and "code" in df.columns:
            df = df.rename(columns={"code": "stock_code"})
        return df

    def read_days_long(self, dates: list[str], workers: int | None = None) -> pd.DataFrame:
        if not dates:
            return pd.DataFrame()
        paths = [self.file_path(d) for d in dates if self.file_path(d).exists()]
        if not paths:
            return pd.DataFrame()
        workers = workers or self.read_workers
        frames: list[pd.DataFrame] = []
        if workers <= 1 or len(paths) < 8:
            frames = [self._read_one_parquet(p) for p in paths]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._read_one_parquet, p): p for p in paths}
                for fut in as_completed(futs):
                    frames.append(fut.result())
        df = pd.concat(frames, ignore_index=True)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.normalize()
        return df

    @staticmethod
    def long_to_panels(
        long_df: pd.DataFrame,
        fields: Iterable[str],
        code_target: str = "rq",
    ) -> dict[str, pd.DataFrame]:
        if long_df.empty:
            return {}
        use_fields = [f for f in fields if f in long_df.columns]
        if not use_fields:
            return {}
        df = long_df.copy()
        df["stock_code"] = normalize_codes(df["stock_code"].astype(str), code_target)
        df = df.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
        df = df.set_index(["trade_date", "stock_code"])
        out: dict[str, pd.DataFrame] = {}
        for f in use_fields:
            wide = df[f].unstack("stock_code").sort_index()
            out[f] = wide
        return out

    def _read_monthly_panel(self, path: Path) -> pd.DataFrame:
        return pd.read_parquet(path)

    def read_bundle_monthly(
        self,
        start: str,
        end: str,
        fields: tuple[str, ...] = STANDARD_COLUMNS,
    ) -> dict[str, pd.DataFrame]:
        months = _months_between(start, end)
        tasks: list[tuple[str, Path]] = []
        for ym in months:
            for f in fields:
                path = self.monthly_path(ym, f)
                if path.exists():
                    tasks.append((f, path))
        out: dict[str, list[pd.DataFrame]] = {f: [] for f in fields}
        if not tasks:
            return {}
        workers = self.read_workers
        if workers <= 1 or len(tasks) < 4:
            for f, path in tasks:
                out[f].append(self._read_monthly_panel(path))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._read_monthly_panel, path): (f, path) for f, path in tasks}
                for fut in as_completed(futs):
                    f, _ = futs[fut]
                    out[f].append(fut.result())
        bundle: dict[str, pd.DataFrame] = {}
        start_d, end_d = _norm_date(start), _norm_date(end)
        for f, parts in out.items():
            if not parts:
                continue
            panel = pd.concat(parts).sort_index()
            panel.index = pd.to_datetime(panel.index).normalize()
            bundle[f] = panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]
        return bundle

    def read_bundle(
        self,
        start: str,
        end: str,
        fields: tuple[str, ...] = STANDARD_COLUMNS,
        code_target: str = "rq",
    ) -> dict[str, pd.DataFrame]:
        use_fields = tuple(f for f in fields if f in STANDARD_COLUMNS)
        if self.use_monthly_wide and self.monthly_coverage(start, end, ("close", "volume")) >= 0.95:
            bundle = self.read_bundle_monthly(start, end, use_fields)
            if bundle.get("close") is not None and not bundle["close"].empty:
                return bundle

        dates = self.dates_in_range(start, end)
        long_df = self.read_days_long(dates)
        panels = self.long_to_panels(long_df, use_fields, code_target=code_target)
        start_d, end_d = _norm_date(start), _norm_date(end)
        for f, panel in list(panels.items()):
            if not panel.empty:
                panels[f] = panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]
        return panels

    def compact_month(self, year_month: str, fields: tuple[str, ...] = ("close", "volume")) -> dict:
        dates = [d for d in self.list_dates() if d.startswith(year_month)]
        if not dates:
            return {"year_month": year_month, "written": 0}
        long_df = self.read_days_long(dates)
        panels = self.long_to_panels(long_df, fields)
        written = 0
        for f, panel in panels.items():
            if panel.empty:
                continue
            panel.to_parquet(self.monthly_path(year_month, f), compression="zstd")
            written += 1
        return {"year_month": year_month, "days": len(dates), "written": written}

    def compact_monthly_range(
        self,
        start: str,
        end: str,
        fields: tuple[str, ...] = ("close", "volume"),
    ) -> dict:
        months = _months_between(start, end)
        results = [self.compact_month(ym, fields) for ym in months]
        self.update_meta(monthly_files=len(list(self.monthly_dir.glob("*.parquet"))))
        return {"months": len(months), "details": results}

    def update_meta(self, **kwargs) -> None:
        meta_path = self.root / META_FILE
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update(kwargs)
        meta["updated_at"] = datetime.now().isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_day_from_provider(self, provider: "IFindDataProvider", trade_date: str) -> int:
        c = provider.cfg.cols("daily")
        t = provider.cfg.table("daily")
        field_map = {name: c.col(name) for name in STANDARD_COLUMNS if name in c.mapping}
        select_parts = [f"`{c.col('date')}` AS trade_date", f"`{c.col('code')}` AS stock_code"]
        for logical, physical in field_map.items():
            select_parts.append(f"`{physical}` AS {logical}")
        sql = f"""
        SELECT {", ".join(select_parts)}
        FROM `{t}`
        WHERE `{c.col('date')}` = :d
        """
        extra = provider.cfg.filter_sql("daily")
        if extra:
            sql += f" AND ({extra})"
        df = provider._query_sql(sql, params={"d": _sql_date(trade_date)})
        if df.empty:
            return 0
        self.write_day(trade_date, df)
        return len(df)

    def build_range(
        self,
        provider: "IFindDataProvider",
        start: str,
        end: str,
        incremental: bool = True,
        trading_dates: list[str] | None = None,
        compact_monthly: bool = True,
    ) -> dict:
        if trading_dates is None:
            trading_dates = [_yyyymmdd(d) for d in provider.get_trading_dates(start, end, prefer_parquet=False)]
        if incremental:
            have = set(self.dates_in_range(start, end))
            trading_dates = [d for d in trading_dates if d not in have]
        written = 0
        rows = 0
        failed: list[dict] = []
        for i, d in enumerate(trading_dates, 1):
            try:
                n = self.export_day_from_provider(provider, d)
                if n > 0:
                    written += 1
                    rows += n
                if i % 50 == 0:
                    logger.info("parquet archive progress %s/%s", i, len(trading_dates))
            except Exception as ex:
                failed.append({"date": d, "error": str(ex)})
        self._refresh_dates_index()
        monthly_result = None
        if compact_monthly and (written > 0 or self.monthly_coverage(start, end) < 0.95):
            monthly_result = self.compact_monthly_range(start, end)
        self.update_meta(
            start=start,
            end=end,
            files=len(self.list_dates()),
            last_build_rows=rows,
            last_build_files=written,
        )
        return {
            "written_days": written,
            "rows": rows,
            "skipped_existing": incremental,
            "failed": failed,
            "monthly_compact": monthly_result,
            "archive_dir": str(self.daily_dir),
        }
