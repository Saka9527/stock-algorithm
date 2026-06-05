# -*- coding: utf-8 -*-
"""BaoStock 估值字段同步至 blader.factor_data_wide。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from multi_factor.baostock.client import BaostockClient
from multi_factor.baostock.code_convert import bs_to_ths
from multi_factor.baostock.config_loader import load_baostock_config
from multi_factor.baostock.db import BaostockStore
from multi_factor.baostock.quota import DailyRequestQuota
from multi_factor.baostock.sync import SyncPlan, SyncResult
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_wide import upsert_wide_baostock_overwrite


@dataclass
class FactorWideSyncResult(SyncResult):
    pass


class BaostockFactorWideSync:
    """同步 close/peTTM/pbMRQ/psTTM/pcfNcfTTM 至 factor_data_wide（增量覆盖）。"""

    DEFAULT_BATCH_SIZE = 100
    _OVERHEAD_REQUESTS = 4

    def __init__(self, *, baostock_config_path: str | None = None, ifind_config_path: str | None = None):
        self.bs_cfg = load_baostock_config(baostock_config_path)
        self.ifind_cfg = load_ifind_config(ifind_config_path)
        self.store = BaostockStore(self.bs_cfg)
        self.engine: Engine = create_engine(self.ifind_cfg.db_url, pool_pre_ping=True)
        self.wide_table = self.ifind_cfg.table("factor")

    def _date_window(self, end_date: str | None = None) -> tuple[str, str]:
        end = end_date or date.today().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=int(self.bs_cfg.years * 365.25))).isoformat()
        return start, end

    def _next_day(self, d: str) -> str:
        return (date.fromisoformat(d) + timedelta(days=1)).isoformat()

    def _load_sync_state(self) -> dict[str, str | None]:
        sql = text("SELECT stock_code, last_trade_date FROM factor_wide_sync_state")
        with self.engine.connect() as conn:
            try:
                rows = conn.execute(sql).fetchall()
            except Exception:
                return {}
        return {r[0]: (r[1].isoformat() if r[1] else None) for r in rows}

    def _load_skip_codes(self) -> set[str]:
        return self.store.load_skip_codes()

    def _load_wide_codes(self) -> set[str]:
        sql = text(f"SELECT DISTINCT stock_code FROM `{self.wide_table}`")
        with self.engine.connect() as conn:
            return {r[0] for r in conn.execute(sql).fetchall()}

    def build_plan(self, client: BaostockClient, *, end_date: str | None = None) -> SyncPlan:
        start, end = self._date_window(end_date)
        universe = client.query_stock_universe()
        sync_state = self._load_sync_state()
        skip_codes = self._load_skip_codes()
        wide_codes = self._load_wide_codes()
        todo_new: list[str] = []
        todo_partial: list[str] = []
        skipped = 0
        for code in universe:
            ths = bs_to_ths(code)
            if ths in skip_codes:
                skipped += 1
                continue
            last = sync_state.get(ths)
            fetch_start = self._next_day(last) if last else start
            if last and fetch_start > end:
                skipped += 1
                continue
            if ths in wide_codes:
                todo_partial.append(code)
            else:
                todo_new.append(code)
        todo = sorted(todo_new) + sorted(todo_partial)
        return SyncPlan(
            start_date=start,
            end_date=end,
            stocks=todo,
            estimated_requests=self._OVERHEAD_REQUESTS + len(todo),
            skipped_up_to_date=skipped,
        )

    def _df_to_records(self, df: pd.DataFrame, stock_code: str) -> list[dict]:
        if df.empty:
            return []
        work = df.copy()
        work = work.rename(
            columns={
                "date": "data_date",
                "peTTM": "pe_ttm",
                "pbMRQ": "pb_mrq",
                "psTTM": "ps_ttm",
                "pcfNcfTTM": "pcf_ncf_ttm",
            }
        )
        work["stock_code"] = stock_code
        for col in ("close", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"):
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")
        work = work.replace({pd.NA: None}).astype(object).where(pd.notnull(work), None)
        cols = ["data_date", "stock_code", "close", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"]
        work = work[[c for c in cols if c in work.columns]]
        records = work.to_dict(orient="records")
        for rec in records:
            for key in ("close", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm"):
                val = rec.get(key)
                if isinstance(val, float) and pd.isna(val):
                    rec[key] = None
        return records

    def _update_sync_state(self, stock_code: str, last_trade_date: str | None, row_count: int) -> None:
        sql = text(
            """
            INSERT INTO factor_wide_sync_state (stock_code, last_trade_date, row_count, updated_at)
            VALUES (:code, :last, :cnt, :ts)
            ON DUPLICATE KEY UPDATE
                last_trade_date = VALUES(last_trade_date),
                row_count = VALUES(row_count),
                updated_at = VALUES(updated_at)
            """
        )
        with self.engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "code": stock_code,
                    "last": last_trade_date,
                    "cnt": row_count,
                    "ts": datetime.now(),
                },
            )

    def _count_wide_rows(self, stock_code: str) -> int:
        sql = text(
            f"SELECT COUNT(*) FROM `{self.wide_table}` WHERE stock_code = :code"
        )
        with self.engine.connect() as conn:
            return int(conn.execute(sql, {"code": stock_code}).scalar() or 0)

    def _sync_batch(
        self,
        client: BaostockClient,
        plan: SyncPlan,
        stocks: list[str],
        quota: DailyRequestQuota,
        *,
        synced_offset: int = 0,
        total_stocks: int = 0,
    ) -> tuple[int, int, list[str], bool, str]:
        synced = 0
        rows_written = 0
        errors: list[str] = []
        stopped_early = False
        stop_reason = ""
        sync_state = self._load_sync_state()

        for code in stocks:
            ths = bs_to_ths(code)
            last = sync_state.get(ths)
            fetch_start = self._next_day(last) if last else plan.start_date
            if fetch_start > plan.end_date:
                continue
            try:
                df = client.query_valuation_k_data_plus(code, fetch_start, plan.end_date)
                records = self._df_to_records(df, ths)
                if not records:
                    if not last:
                        self.store.mark_skip(ths, "no_valuation")
                    continue
                n = upsert_wide_baostock_overwrite(self.engine, records)
                rows_written += n
                last_day = str(df["date"].max()) if not df.empty else plan.end_date
                total_rows = self._count_wide_rows(ths)
                self._update_sync_state(ths, last_day, total_rows)
                sync_state[ths] = last_day
                synced += 1
                done = synced_offset + synced
                if done % 50 == 0:
                    snap = quota.snapshot()
                    print(
                        f"[factor] {done}/{total_stocks} 完成，"
                        f"本批写入 {rows_written} 行，API {snap.request_count}/{snap.daily_limit}",
                        flush=True,
                    )
            except RuntimeError as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                if "API 请求已达上限" in msg:
                    stopped_early = True
                    stop_reason = msg
                    break
                print(f"[factor] 跳过 {ths}: {msg}", flush=True)
            except Exception as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                print(f"[factor] 跳过 {ths}: {msg}", flush=True)

        return synced, rows_written, errors, stopped_early, stop_reason

    def run(
        self,
        *,
        end_date: str | None = None,
        max_stocks: int | None = None,
        batch_size: int | None = None,
        dry_run: bool = False,
    ) -> FactorWideSyncResult:
        self.store.init_schema()
        batch_size = batch_size or self.DEFAULT_BATCH_SIZE
        quota = DailyRequestQuota(self.store, daily_limit=self.bs_cfg.daily_api_limit)
        client = BaostockClient(quota)

        with client.session():
            effective_end = end_date or client.query_latest_trading_day(*self._date_window())
            plan = self.build_plan(client, end_date=effective_end)

        if dry_run:
            snap = quota.snapshot()
            return FactorWideSyncResult(
                start_date=plan.start_date,
                end_date=plan.end_date,
                stocks_total=len(plan.stocks) + plan.skipped_up_to_date,
                stocks_synced=0,
                stocks_skipped=plan.skipped_up_to_date,
                rows_written=0,
                api_requests=snap.request_count,
                api_limit=snap.daily_limit,
                api_remaining=snap.remaining,
                stop_reason=f"dry-run: 待同步 {len(plan.stocks)} 只",
            )

        stocks = plan.stocks[:max_stocks] if max_stocks else plan.stocks
        synced_total = 0
        rows_total = 0
        errors: list[str] = []
        stopped_early = False
        stop_reason = ""

        for bi in range(0, len(stocks), batch_size):
            batch = stocks[bi : bi + batch_size]
            print(f"[factor] 批次 {bi // batch_size + 1}，{len(batch)} 只", flush=True)
            with client.session():
                synced, rows, batch_errors, batch_stopped, batch_reason = self._sync_batch(
                    client,
                    plan,
                    batch,
                    quota,
                    synced_offset=synced_total,
                    total_stocks=len(stocks),
                )
            synced_total += synced
            rows_total += rows
            errors.extend(batch_errors)
            if batch_stopped:
                stopped_early = True
                stop_reason = batch_reason
                break

        snap = quota.snapshot()
        return FactorWideSyncResult(
            start_date=plan.start_date,
            end_date=plan.end_date,
            stocks_total=len(plan.stocks) + plan.skipped_up_to_date,
            stocks_synced=synced_total,
            stocks_skipped=plan.skipped_up_to_date + len(plan.stocks) - synced_total,
            rows_written=rows_total,
            api_requests=snap.request_count,
            api_limit=snap.daily_limit,
            api_remaining=snap.remaining,
            stopped_early=stopped_early,
            stop_reason=stop_reason,
            errors=errors,
        )

    def status(self) -> dict:
        snap = DailyRequestQuota(self.store, self.bs_cfg.daily_api_limit).snapshot()
        with self.engine.connect() as conn:
            rows = conn.execute(text(f"SELECT COUNT(*) FROM `{self.wide_table}`")).scalar()
            stocks = conn.execute(
                text(f"SELECT COUNT(DISTINCT stock_code) FROM `{self.wide_table}`")
            ).scalar()
        return {
            "api_requests": snap.request_count,
            "api_limit": snap.daily_limit,
            "api_remaining": snap.remaining,
            "wide_rows": int(rows or 0),
            "wide_stocks": int(stocks or 0),
            "skip_codes": len(self.store.load_skip_codes()),
        }
