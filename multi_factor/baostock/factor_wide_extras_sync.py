# -*- coding: utf-8 -*-
"""BaoStock 补齐 factor_data_wide 扩展字段：市值、ST、停牌、20日换手。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from multi_factor.baostock.client import BaostockClient
from multi_factor.baostock.code_convert import bs_to_ths, ths_to_bs
from multi_factor.baostock.config_loader import load_baostock_config
from multi_factor.baostock.db import BaostockStore
from multi_factor.baostock.factor_wide_sync import ensure_factor_wide_schema
from multi_factor.baostock.quota import DailyRequestQuota
from multi_factor.baostock.sync import SyncPlan, SyncResult
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_wide import upsert_wide_extras_overwrite

_ROLLING_WINDOW = 20
_LOOKBACK_CALENDAR_DAYS = 45


@dataclass
class FactorWideExtrasSyncResult(SyncResult):
    pass


class FactorWideExtrasSync:
    """同步 float_cap/total_cap/is_st/is_suspended/turnover_20d 至 factor_data_wide。"""

    DEFAULT_BATCH_SIZE = 80
    _OVERHEAD_REQUESTS = 4

    def __init__(self, *, baostock_config_path: str | None = None, ifind_config_path: str | None = None):
        self.bs_cfg = load_baostock_config(baostock_config_path)
        self.ifind_cfg = load_ifind_config(ifind_config_path)
        self.store = BaostockStore(self.bs_cfg)
        self.engine: Engine = create_engine(self.ifind_cfg.db_url, pool_pre_ping=True)
        self.wide_table = self.ifind_cfg.table("factor")
        ensure_factor_wide_schema(self.engine, self.wide_table)
        self._ensure_extras_state_table()
        self._share_cache: dict[str, pd.DataFrame] = {}

    def _ensure_extras_state_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS factor_wide_extras_sync_state (
                        stock_code VARCHAR(12) PRIMARY KEY,
                        last_trade_date DATE NULL,
                        row_count INT DEFAULT 0,
                        updated_at DATETIME NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            )

    def _date_window(self, end_date: str | None = None) -> tuple[str, str]:
        end = end_date or date.today().isoformat()
        start = (date.fromisoformat(end) - timedelta(days=int(self.bs_cfg.years * 365.25))).isoformat()
        return start, end

    def _next_day(self, d: str) -> str:
        return (date.fromisoformat(d) + timedelta(days=1)).isoformat()

    def _lookback_start(self, start: str) -> str:
        return (date.fromisoformat(start) - timedelta(days=_LOOKBACK_CALENDAR_DAYS)).isoformat()

    def _load_sync_state(self) -> dict[str, str | None]:
        sql = text("SELECT stock_code, last_trade_date FROM factor_wide_extras_sync_state")
        with self.engine.connect() as conn:
            try:
                rows = conn.execute(sql).fetchall()
            except Exception:
                return {}
        return {r[0]: (r[1].isoformat() if r[1] else None) for r in rows}

    def _load_target_stocks(self, start: str, end: str) -> list[str]:
        sql = text(
            f"""
            SELECT DISTINCT stock_code FROM `{self.wide_table}`
            WHERE data_date >= :s AND data_date <= :e
            ORDER BY stock_code
            """
        )
        with self.engine.connect() as conn:
            ths_codes = [r[0] for r in conn.execute(sql, {"s": start, "e": end}).fetchall()]
        return [ths_to_bs(c) for c in ths_codes if c]

    def _load_share_snapshots(self, client: BaostockClient, bs_code: str, start: str, end: str) -> pd.DataFrame:
        if bs_code in self._share_cache:
            return self._share_cache[bs_code]
        y0 = date.fromisoformat(start).year
        y1 = date.fromisoformat(end).year
        frames: list[pd.DataFrame] = []
        for year in range(y0, y1 + 1):
            df = client.query_profit_shares(bs_code, year)
            if not df.empty:
                frames.append(df)
        if not frames:
            self._share_cache[bs_code] = pd.DataFrame(columns=["statDate", "totalShare", "liqaShare"])
            return self._share_cache[bs_code]
        out = pd.concat(frames, ignore_index=True)
        out["statDate"] = pd.to_datetime(out["statDate"]).dt.normalize()
        out["totalShare"] = pd.to_numeric(out["totalShare"], errors="coerce")
        out["liqaShare"] = pd.to_numeric(out["liqaShare"], errors="coerce")
        out = out.dropna(subset=["statDate"]).sort_values("statDate").drop_duplicates("statDate", keep="last")
        self._share_cache[bs_code] = out
        return out

    @staticmethod
    def _ffill_shares_to_daily(share_df: pd.DataFrame, dates: pd.Series) -> tuple[pd.Series, pd.Series]:
        if share_df.empty or dates.empty:
            empty = pd.Series(index=dates.index, dtype=float)
            return empty, empty
        idx = pd.DatetimeIndex(pd.to_datetime(dates).dt.normalize())
        base = share_df.set_index("statDate")[["totalShare", "liqaShare"]].sort_index()
        aligned = base.reindex(idx.union(base.index)).sort_index().ffill().reindex(idx)
        return aligned["totalShare"], aligned["liqaShare"]

    def _df_to_records(self, df: pd.DataFrame, stock_code: str, plan_start: str) -> list[dict]:
        if df.empty:
            return []
        work = df.copy()
        work["date"] = pd.to_datetime(work["date"]).dt.normalize()
        for col in ("close", "volume", "turn"):
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")
        work["turnover_20d"] = work["turn"].rolling(_ROLLING_WINDOW, min_periods=1).mean()
        work["is_st"] = pd.to_numeric(work.get("isST"), errors="coerce").fillna(0).astype(int)
        work["is_suspended"] = (pd.to_numeric(work.get("tradestatus"), errors="coerce").fillna(1) == 0).astype(int)

        bs_code = ths_to_bs(stock_code)
        total_share, liqa_share = self._ffill_shares_to_daily(
            self._share_cache.get(bs_code, pd.DataFrame()),
            work["date"],
        )
        work["float_cap"] = work["close"] * liqa_share
        work["total_cap"] = work["close"] * total_share

        # 股本缺失时用换手率反推流通市值
        missing_float = work["float_cap"].isna() | (work["float_cap"] <= 0)
        valid_turn = work["turn"].notna() & (work["turn"] > 0) & work["volume"].notna() & (work["volume"] > 0)
        est_float_shares = work["volume"] / (work["turn"] / 100.0)
        work.loc[missing_float & valid_turn, "float_cap"] = (
            work.loc[missing_float & valid_turn, "close"] * est_float_shares.loc[missing_float & valid_turn]
        )
        missing_total = work["total_cap"].isna() | (work["total_cap"] <= 0)
        work.loc[missing_total, "total_cap"] = work.loc[missing_total, "float_cap"]

        plan_start_d = pd.Timestamp(plan_start).normalize()
        work = work[work["date"] >= plan_start_d]

        records: list[dict] = []
        for _, row in work.iterrows():
            rec = {
                "data_date": row["date"].strftime("%Y-%m-%d"),
                "stock_code": stock_code,
                "float_cap": _safe_float(row.get("float_cap")),
                "total_cap": _safe_float(row.get("total_cap")),
                "is_st": int(row.get("is_st") or 0),
                "is_suspended": int(row.get("is_suspended") or 0),
                "turnover_20d": _safe_float(row.get("turnover_20d")),
            }
            if rec["turnover_20d"] is None and rec["float_cap"] is None and rec["total_cap"] is None:
                if rec["is_st"] == 0 and rec["is_suspended"] == 0:
                    continue
            records.append(rec)
        return records

    def build_plan(self, client: BaostockClient, *, end_date: str | None = None) -> SyncPlan:
        start, end = self._date_window(end_date)
        stocks = self._load_target_stocks(start, end)
        sync_state = self._load_sync_state()
        todo: list[str] = []
        skipped = 0
        for code in stocks:
            ths = bs_to_ths(code)
            last = sync_state.get(ths)
            fetch_start = self._next_day(last) if last else start
            if last and fetch_start > end:
                skipped += 1
                continue
            todo.append(code)
        return SyncPlan(
            start_date=start,
            end_date=end,
            stocks=todo,
            estimated_requests=self._OVERHEAD_REQUESTS + len(todo) * 4,
            skipped_up_to_date=skipped,
        )

    def _update_sync_state(self, stock_code: str, last_trade_date: str | None, row_count: int) -> None:
        sql = text(
            """
            INSERT INTO factor_wide_extras_sync_state (stock_code, last_trade_date, row_count, updated_at)
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
                {"code": stock_code, "last": last_trade_date, "cnt": row_count, "ts": datetime.now()},
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
        sync_state = self._load_sync_state()

        for code in stocks:
            ths = bs_to_ths(code)
            last = sync_state.get(ths)
            fetch_start = self._next_day(last) if last else plan.start_date
            if fetch_start > plan.end_date:
                continue
            query_start = self._lookback_start(fetch_start)
            try:
                self._load_share_snapshots(client, code, plan.start_date, plan.end_date)
                df = client.query_extras_k_data_plus(code, query_start, plan.end_date)
                records = self._df_to_records(df, ths, fetch_start)
                if not records:
                    continue
                n = upsert_wide_extras_overwrite(self.engine, records)
                rows_written += n
                last_day = max(r["data_date"] for r in records)
                self._update_sync_state(ths, last_day, len(records))
                sync_state[ths] = last_day
                synced += 1
                done = synced_offset + synced
                if done % 30 == 0:
                    snap = quota.snapshot()
                    print(
                        f"[extras] {done}/{total_stocks} 完成，"
                        f"写入 {rows_written} 行，API {snap.request_count}/{snap.daily_limit}",
                        flush=True,
                    )
            except RuntimeError as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                if "API 请求已达上限" in msg:
                    stopped_early = True
                    stop_reason = msg
                    break
                print(f"[extras] 跳过 {ths}: {msg}", flush=True)
            except Exception as exc:
                msg = str(exc)
                errors.append(f"{ths}: {msg}")
                print(f"[extras] 跳过 {ths}: {msg}", flush=True)

        return synced, rows_written, errors, stopped_early, stop_reason

    def run(
        self,
        *,
        end_date: str | None = None,
        max_stocks: int | None = None,
        batch_size: int | None = None,
        dry_run: bool = False,
    ) -> FactorWideExtrasSyncResult:
        self.store.init_schema()
        batch_size = batch_size or self.DEFAULT_BATCH_SIZE
        quota = DailyRequestQuota(self.store, daily_limit=self.bs_cfg.daily_api_limit)
        client = BaostockClient(quota)

        with client.session():
            effective_end = end_date or client.query_latest_trading_day(*self._date_window())
            plan = self.build_plan(client, end_date=effective_end)

        if dry_run:
            snap = quota.snapshot()
            return FactorWideExtrasSyncResult(
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
        all_errors: list[str] = []
        stopped_early = False
        stop_reason = ""

        with client.session():
            for i in range(0, len(stocks), batch_size):
                batch = stocks[i : i + batch_size]
                synced, rows, errors, stopped, reason = self._sync_batch(
                    client,
                    plan,
                    batch,
                    quota,
                    synced_offset=synced_total,
                    total_stocks=len(stocks),
                )
                synced_total += synced
                rows_total += rows
                all_errors.extend(errors)
                if stopped:
                    stopped_early = True
                    stop_reason = reason
                    break

        snap = quota.snapshot()
        return FactorWideExtrasSyncResult(
            start_date=plan.start_date,
            end_date=plan.end_date,
            stocks_total=len(plan.stocks) + plan.skipped_up_to_date,
            stocks_synced=synced_total,
            stocks_skipped=plan.skipped_up_to_date + (len(plan.stocks) - synced_total),
            rows_written=rows_total,
            api_requests=snap.request_count,
            api_limit=snap.daily_limit,
            api_remaining=snap.remaining,
            stopped_early=stopped_early,
            stop_reason=stop_reason or ("; ".join(all_errors[:3]) if all_errors else ""),
            errors=all_errors,
        )

    def status(self) -> dict:
        snap = DailyRequestQuota(self.store, self.bs_cfg.daily_api_limit).snapshot()
        with self.engine.connect() as conn:
            state_cnt = conn.execute(text("SELECT COUNT(*) FROM factor_wide_extras_sync_state")).scalar() or 0
            null_cnt = conn.execute(
                text(
                    f"""
                    SELECT COUNT(*) FROM `{self.wide_table}`
                    WHERE data_date >= DATE_SUB(CURDATE(), INTERVAL 1100 DAY)
                      AND (float_cap IS NULL OR total_cap IS NULL OR turnover_20d IS NULL)
                    """
                )
            ).scalar() or 0
        return {
            "wide_table": self.wide_table,
            "sync_state_rows": int(state_cnt),
            "rows_with_null_extras_recent": int(null_cnt),
            "years": self.bs_cfg.years,
            "api_requests": snap.request_count,
            "api_limit": snap.daily_limit,
            "api_remaining": snap.remaining,
        }


def _safe_float(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None
