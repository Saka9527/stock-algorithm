# -*- coding: utf-8 -*-
"""因子宽表 Parquet 归档：按月预透视，加速全量区间因子加载。"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from multi_factor.archive.daily_parquet import _months_between, _norm_date
from multi_factor.ifind.factor_wide import WIDE_COLUMN_BY_FACTOR_CODE, wide_column_for_factor

if TYPE_CHECKING:
    from multi_factor.ifind.provider import IFindDataProvider

logger = logging.getLogger(__name__)

META_FILE = "_archive_meta.json"
FACTORS_INDEX_FILE = "_factors_index.json"


def dedicated_factor_codes() -> list[str]:
    """有物理列、适合归档的因子代码（去重）。"""
    seen: set[str] = set()
    out: list[str] = []
    for code in WIDE_COLUMN_BY_FACTOR_CODE:
        col = wide_column_for_factor(code)
        if not col or col in seen:
            continue
        seen.add(col)
        out.append(code.upper())
    return sorted(out)


class FactorParquetArchive:
    """
    目录结构:
      {root}/PCF_NCF_TTM/monthly/202306.parquet   # index=日期, columns=股票(RQ)
    """

    def __init__(self, root: str | Path, read_workers: int = 8):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.read_workers = max(1, int(read_workers))

    def factor_root(self, factor_code: str) -> Path:
        return self.root / str(factor_code).upper()

    def monthly_path(self, factor_code: str, year_month: str) -> Path:
        return self.factor_root(factor_code) / "monthly" / f"{year_month}.parquet"

    def list_factors(self) -> list[str]:
        idx_path = self.root / FACTORS_INDEX_FILE
        if idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
                factors = list(data.get("factors") or [])
                if factors:
                    return factors
            except Exception:
                pass
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and (p / "monthly").is_dir()
        )

    def _refresh_factors_index(self) -> None:
        factors = sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and any((p / "monthly").glob("*.parquet"))
        )
        (self.root / FACTORS_INDEX_FILE).write_text(
            json.dumps({"factors": factors, "updated_at": datetime.now().isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )

    def list_months(self, factor_code: str) -> list[str]:
        monthly = self.factor_root(factor_code) / "monthly"
        if not monthly.is_dir():
            return []
        return sorted(p.stem for p in monthly.glob("*.parquet") if p.stem.isdigit() and len(p.stem) == 6)

    def monthly_coverage(self, factor_code: str, start: str, end: str) -> float:
        months = _months_between(start, end)
        if not months:
            return 0.0
        have = set(self.list_months(factor_code))
        return sum(1 for ym in months if ym in have) / len(months)

    def write_month(self, factor_code: str, year_month: str, panel: pd.DataFrame) -> Path | None:
        if panel is None or panel.empty:
            return None
        out = panel.copy()
        out.index = pd.to_datetime(out.index).normalize()
        path = self.monthly_path(factor_code, year_month)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path, compression="zstd")
        return path

    def _read_monthly_panel(self, path: Path) -> pd.DataFrame:
        panel = pd.read_parquet(path)
        panel.index = pd.to_datetime(panel.index).normalize()
        return panel

    def read_panel(self, factor_code: str, start: str, end: str) -> pd.DataFrame:
        months = _months_between(start, end)
        tasks = [self.monthly_path(factor_code, ym) for ym in months if self.monthly_path(factor_code, ym).exists()]
        if not tasks:
            return pd.DataFrame()
        parts: list[pd.DataFrame] = []
        workers = self.read_workers
        if workers <= 1 or len(tasks) < 4:
            parts = [self._read_monthly_panel(p) for p in tasks]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._read_monthly_panel, p): p for p in tasks}
                for fut in as_completed(futs):
                    parts.append(fut.result())
        panel = pd.concat(parts).sort_index()
        panel = panel[~panel.index.duplicated(keep="last")]
        start_d, end_d = _norm_date(start), _norm_date(end)
        return panel.loc[(panel.index >= start_d) & (panel.index <= end_d)]

    def update_factor_meta(self, factor_code: str, **kwargs) -> None:
        fc = factor_code.upper()
        meta_path = self.factor_root(fc) / META_FILE
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update(kwargs)
        meta["updated_at"] = datetime.now().isoformat()
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_range(
        self,
        provider: "IFindDataProvider",
        factor_code: str,
        start: str,
        end: str,
        *,
        incremental: bool = True,
    ) -> dict:
        fc = factor_code.upper()
        panel = provider.load_factor_panel_sql(fc, start, end)
        if panel.empty:
            return {"factor_code": fc, "written_months": 0, "rows": 0, "skipped": True}

        written = 0
        rows = 0
        have = set(self.list_months(fc)) if incremental else set()
        for period, sub in panel.groupby(panel.index.to_period("M")):
            ym = period.strftime("%Y%m")
            if incremental and ym in have:
                continue
            path = self.write_month(fc, ym, sub)
            if path:
                written += 1
                rows += int(sub.shape[0] * sub.shape[1])
        self.update_factor_meta(
            fc,
            start=start,
            end=end,
            months=len(self.list_months(fc)),
            last_build_months=written,
        )
        self._refresh_factors_index()
        return {
            "factor_code": fc,
            "written_months": written,
            "rows": rows,
            "total_months": len(self.list_months(fc)),
            "archive_dir": str(self.factor_root(fc)),
        }
