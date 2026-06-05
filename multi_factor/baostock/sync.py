# -*- coding: utf-8 -*-
"""从 BaoStock 同步近 N 年前复权日 K 至 MySQL。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from multi_factor.baostock.client import BaostockClient
from multi_factor.baostock.code_convert import bs_to_ths
from multi_factor.baostock.config_loader import BaostockConfig, load_baostock_config
from multi_factor.baostock.db import BaostockStore
from multi_factor.baostock.quota import DailyRequestQuota


@dataclass
class SyncPlan:
    start_date: str
    end_date: str
    stocks: list[str]
    estimated_requests: int
    skipped_up_to_date: int = 0


@dataclass
class SyncResult:
    start_date: str
    end_date: str
    stocks_total: int
    stocks_synced: int
    stocks_skipped: int
    rows_written: int
    api_requests: int
    api_limit: int
    api_remaining: int
    stopped_early: bool = False
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)


class BaostockDailySync:
    """同步前复权日 K；每次 API 调用计入 api_quota。"""

    _OVERHEAD_REQUESTS = 4
    DEFAULT_BATCH_SIZE = 150

    def __init__(self, cfg: BaostockConfig | None = None, *, config_path: str | None = None):
        self.cfg = cfg or load_baostock_config(config_path)
        self.store = BaostockStore(self.cfg)

    def _date_window(self, end_date: str | None = None) -> tuple[str, str]:
        end = end_date or date.today().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=int(self.cfg.years * 365.25))).isoformat()
        return start, end

    def _next_day(self, d: str) -> str:
        return (date.fromisoformat(d) + timedelta(days=1)).isoformat()

    def build_plan(
        self,
        client: BaostockClient,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        codes: list[str] | None = None,
    ) -> SyncPlan:
        win_start, win_end = self._date_window(end_date)
        start = start_date or win_start
        end = end_date or win_end

        if codes:
            universe = sorted(set(codes))
        elif self.cfg.use_full_universe:
            universe = client.query_stock_universe()
        else:
            trade_day = client.query_latest_trading_day(start, end)
            universe = client.query_all_stock(trade_day)

        sync_state = self.store.load_sync_state()
        db_codes = self.store.load_daily_stock_codes()
        skip_codes = self.store.load_skip_codes()
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
            if ths in db_codes:
                todo_partial.append(code)
            else:
                todo_new.append(code)
        todo = sorted(todo_new) + sorted(todo_partial)

        estimated = self._OVERHEAD_REQUESTS + len(todo)
        return SyncPlan(
            start_date=start,
            end_date=end,
            stocks=todo,
            estimated_requests=estimated,
            skipped_up_to_date=skipped,
        )

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
        sync_state = self.store.load_sync_state()
        db_codes = self.store.load_daily_stock_codes()

        for code in stocks:
            ths = bs_to_ths(code)
            last = sync_state.get(ths)
            fetch_start = self._next_day(last) if last else plan.start_date
            if fetch_start > plan.end_date:
                continue
            try:
                df = client.query_history_k_data_plus(code, fetch_start, plan.end_date)
                n = self.store.upsert_daily(df)
                rows_written += n
                if not df.empty and "date" in df.columns:
                    last_day = str(df["date"].max())
                elif last and fetch_start >= plan.end_date:
                    last_day = plan.end_date
                elif last:
                    last_day = last
                else:
                    last_day = None
                if last_day is None and df.empty:
                    if ths not in db_codes:
                        self.store.mark_skip(ths, "no_data")
                    continue
                total_rows = self.store.count_rows(ths)
                self.store.update_sync_state(ths, last_day, total_rows)
                sync_state[ths] = last_day
                db_codes.add(ths)
                synced += 1
                done = synced_offset + synced
                if done % 50 == 0:
                    snap = quota.snapshot()
                    rows_total, stocks_in_db = self.store.count_stats()
                    print(
                        f"[sync] {done}/{total_stocks} 完成，"
                        f"本批写入 {rows_written} 行，"
                        f"库内 {stocks_in_db} 只/{rows_total} 行，"
                        f"API {snap.request_count}/{snap.daily_limit}",
                        flush=True,
                    )
            except RuntimeError as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                if "API 请求已达上限" in msg:
                    stopped_early = True
                    stop_reason = msg
                    break
                print(f"[sync] 跳过 {ths}: {msg}", flush=True)
            except Exception as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                print(f"[sync] 跳过 {ths}: {msg}", flush=True)

        return synced, rows_written, errors, stopped_early, stop_reason

    def run(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        codes: list[str] | None = None,
        max_stocks: int | None = None,
        batch_size: int | None = None,
        dry_run: bool = False,
    ) -> SyncResult:
        self.store.init_schema()
        removed = self.store.cleanup_false_sync_state()
        if removed:
            print(f"[sync] 清理无效游标 {removed} 条", flush=True)

        batch_size = batch_size or self.DEFAULT_BATCH_SIZE
        quota = DailyRequestQuota(self.store, daily_limit=self.cfg.daily_api_limit)
        client = BaostockClient(quota)

        win_start, win_end = self._date_window(end_date)
        start = start_date or win_start
        end = end_date or win_end

        synced_total = 0
        rows_written_total = 0
        errors: list[str] = []
        stopped_early = False
        stop_reason = ""
        plan: SyncPlan | None = None

        with client.session():
            effective_end = end if end_date else client.query_latest_trading_day(start, end)
            plan = self.build_plan(
                client,
                start_date=start,
                end_date=effective_end,
                codes=codes,
            )

        if dry_run:
            snap = quota.snapshot()
            return SyncResult(
                start_date=plan.start_date,
                end_date=plan.end_date,
                stocks_total=len(plan.stocks) + plan.skipped_up_to_date,
                stocks_synced=0,
                stocks_skipped=plan.skipped_up_to_date,
                rows_written=0,
                api_requests=snap.request_count,
                api_limit=snap.daily_limit,
                api_remaining=snap.remaining,
                stopped_early=False,
                stop_reason=(
                    f"dry-run: 待同步 {len(plan.stocks)} 只，"
                    f"已跳过 {plan.skipped_up_to_date} 只，"
                    f"预计总请求约 {plan.estimated_requests} 次"
                ),
            )

        assert plan is not None
        stocks = plan.stocks[:max_stocks] if max_stocks else plan.stocks
        if quota.remaining < self._OVERHEAD_REQUESTS + len(stocks):
            allowed = max(0, quota.remaining - self._OVERHEAD_REQUESTS)
            if allowed < len(stocks):
                stocks = stocks[:allowed]
                stopped_early = True
                stop_reason = f"配额不足，今日仅处理 {len(stocks)}/{len(plan.stocks)} 只股票"

        for bi in range(0, len(stocks), batch_size):
            batch = stocks[bi : bi + batch_size]
            batch_no = bi // batch_size + 1
            batch_total = (len(stocks) + batch_size - 1) // batch_size
            print(
                f"[sync] 批次 {batch_no}/{batch_total}，本批 {len(batch)} 只",
                flush=True,
            )
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
            rows_written_total += rows
            errors.extend(batch_errors)
            if batch_stopped:
                stopped_early = True
                stop_reason = batch_reason
                break

        snap = quota.snapshot()
        return SyncResult(
            start_date=plan.start_date,
            end_date=plan.end_date,
            stocks_total=len(plan.stocks) + plan.skipped_up_to_date,
            stocks_synced=synced_total,
            stocks_skipped=plan.skipped_up_to_date + len(plan.stocks) - synced_total,
            rows_written=rows_written_total,
            api_requests=snap.request_count,
            api_limit=snap.daily_limit,
            api_remaining=snap.remaining,
            stopped_early=stopped_early,
            stop_reason=stop_reason,
            errors=errors,
        )

    def quota_status(self) -> dict:
        self.store.init_schema()
        quota = DailyRequestQuota(self.store, daily_limit=self.cfg.daily_api_limit)
        snap = quota.snapshot()
        rows_total, stocks_in_db = self.store.count_stats()
        skip_n = len(self.store.load_skip_codes())
        return {
            "quota_date": snap.quota_date,
            "api_requests": snap.request_count,
            "api_limit": snap.daily_limit,
            "api_remaining": snap.remaining,
            "rows_total": rows_total,
            "stocks_in_db": stocks_in_db,
            "skip_codes": skip_n,
        }
