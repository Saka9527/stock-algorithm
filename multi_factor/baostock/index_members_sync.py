# -*- coding: utf-8 -*-
"""指数成分股同步至 index_members 表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from multi_factor.engine.strategy_config import UNIVERSE_INDEX_CODES
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.index_members import (
    CSINDEX_CONS_CODE,
    fetch_baostock_members,
    fetch_csindex_members,
    upsert_members,
)


@dataclass
class IndexMembersSyncResult:
    index_code: str
    trade_date: str
    count: int
    source: str


class IndexMembersSync:
    """将沪深300/中证500/中证1000 成分快照写入 MySQL。"""

    POOLS = ("csi300", "csi500", "csi1000")

    def __init__(self, ifind_config_path: str | None = None):
        self.cfg = load_ifind_config(ifind_config_path)
        if not self.cfg.db_url:
            raise ValueError("index_members 同步需要 database 配置")
        self.engine = create_engine(self.cfg.db_url, pool_pre_ping=True)
        self.table = self.cfg.tables.get("index_members", "index_members")

    def ensure_table(self) -> None:
        root = Path(__file__).resolve().parents[2]
        sql_path = root / "scripts" / "sql" / "index_members_create_tables.sql"
        sql = sql_path.read_text(encoding="utf-8")
        with self.engine.begin() as conn:
            for stmt in sql.split(";"):
                s = stmt.strip()
                if s:
                    conn.execute(text(s))

    def _month_end_dates(self, start: str, end: str) -> list[str]:
        from multi_factor.ifind.provider import IFindDataProvider

        provider = IFindDataProvider(self.cfg)
        dates = provider.get_trading_dates(start, end, prefer_parquet=False)
        if len(dates) == 0:
            return []
        s = pd.Series(dates, index=dates)
        return [d.strftime("%Y%m%d") for d in s.groupby(s.index.to_period("M")).max().tolist()]

    def sync_index(
        self,
        index_code: str,
        start: str,
        end: str,
        *,
        monthly: bool = True,
    ) -> list[IndexMembersSyncResult]:
        results: list[IndexMembersSyncResult] = []
        dates = self._month_end_dates(start, end) if monthly else [end]
        if not dates:
            dates = [end]

        for dt in dates:
            source = "baostock"
            try:
                snap_dt, codes = fetch_baostock_members(index_code, dt)
            except Exception:
                if index_code.upper() not in CSINDEX_CONS_CODE:
                    raise
                snap_dt, codes = fetch_csindex_members(index_code)
                source = "csindex"
            trade_date = snap_dt.strftime("%Y%m%d")
            n = upsert_members(self.engine, self.table, index_code, trade_date, codes, source)
            results.append(
                IndexMembersSyncResult(
                    index_code=index_code.upper(),
                    trade_date=trade_date,
                    count=n,
                    source=source,
                )
            )
        return results

    def sync_pools(
        self,
        pools: list[str] | None = None,
        start: str = "",
        end: str = "",
        *,
        monthly: bool = True,
    ) -> dict:
        pools = pools or list(self.POOLS)
        if not start or not end:
            from multi_factor.ifind.provider import IFindDataProvider

            provider = IFindDataProvider(self.cfg)
            start, end = provider.query_data_date_range()
        out = []
        for pool in pools:
            index_code = UNIVERSE_INDEX_CODES.get(pool)
            if not index_code:
                continue
            out.extend(self.sync_index(index_code, start, end, monthly=monthly))
        return {
            "start": start,
            "end": end,
            "pools": pools,
            "details": [r.__dict__ for r in out],
        }
