# -*- coding: utf-8 -*-
"""
分批将 factor_calc_data（长表）迁移到 factor_data_wide（宽表）。

用法:
  python scripts/migrate_factor_calc_to_wide.py
  python scripts/migrate_factor_calc_to_wide.py --start 20230601 --end 20240601 --batch-days 30
  python scripts/migrate_factor_calc_to_wide.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_wide import (
    WIDE_TABLE,
    long_rows_to_wide_records,
    upsert_wide_records,
)


def parse_args():
    p = argparse.ArgumentParser(description="factor_calc_data -> factor_data_wide 分批迁移")
    p.add_argument("--start", default="", help="起始 data_date YYYYMMDD，默认源表最小日期")
    p.add_argument("--end", default="", help="结束 data_date YYYYMMDD，默认源表最大日期")
    p.add_argument("--batch-days", type=int, default=30, help="每批覆盖的日历天数")
    p.add_argument("--chunk-rows", type=int, default=8000, help="每批内长表读取后的宽表 upsert 分块")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--dry-run", action="store_true", help="只统计不落库")
    p.add_argument(
        "--source-table",
        default="factor_calc_data",
        help="源长表名（默认 factor_calc_data）",
    )
    return p.parse_args()


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def resolve_range(engine, source_table: str, start: str, end: str) -> tuple[str, str]:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                f"""
            SELECT MIN(data_date), MAX(data_date)
            FROM `{source_table}`
            WHERE is_deleted = 0 AND status = 1
            """
            )
        ).fetchone()
    if not row or row[0] is None:
        raise ValueError(f"{source_table} 无有效数据")
    s = start or pd.Timestamp(row[0]).strftime("%Y%m%d")
    e = end or pd.Timestamp(row[1]).strftime("%Y%m%d")
    return s, e


def iter_date_batches(start: str, end: str, batch_days: int):
    dates = pd.date_range(_sql_date(start), _sql_date(end), freq="D")
    if len(dates) == 0:
        return
    step = max(1, int(batch_days))
    for i in range(0, len(dates), step):
        batch = dates[i : i + step]
        yield batch.min().strftime("%Y-%m-%d"), batch.max().strftime("%Y-%m-%d")


def upsert_wide_batch(engine, records: list[dict], chunk_rows: int, dry_run: bool) -> int:
    if not records:
        return 0
    if dry_run:
        return len(records)
    return upsert_wide_records(engine, records, chunk_size=chunk_rows)


def migrate_batch(
    engine,
    source_table: str,
    batch_start: str,
    batch_end: str,
    chunk_rows: int,
    dry_run: bool,
) -> dict:
    sql = f"""
    SELECT stock_code, data_date, factor_code, factor_value
    FROM `{source_table}`
    WHERE is_deleted = 0 AND status = 1
      AND data_date >= :s AND data_date <= :e
    """
    with engine.begin() as conn:
        df = pd.read_sql(
            text(sql),
            conn,
            params={"s": batch_start, "e": batch_end},
        )
    if df.empty:
        return {"long_rows": 0, "wide_rows": 0, "upserted": 0}

    df["data_date"] = pd.to_datetime(df["data_date"]).dt.strftime("%Y-%m-%d")
    long_rows = df.to_dict("records")
    wide_records = long_rows_to_wide_records(long_rows)
    upserted = upsert_wide_batch(engine, wide_records, chunk_rows, dry_run)
    return {"long_rows": len(long_rows), "wide_rows": len(wide_records), "upserted": upserted}


def main():
    args = parse_args()
    cfg = load_ifind_config(args.ifind_config or None)
    if not cfg.db_url:
        raise ValueError("需要配置 database")
    engine = create_engine(cfg.db_url, pool_pre_ping=True)

    start, end = resolve_range(engine, args.source_table, args.start, args.end)
    print(f">>> migrate {args.source_table} -> {WIDE_TABLE}")
    print(f">>> range: {start} ~ {end}, batch_days={args.batch_days}, dry_run={args.dry_run}")

    totals = {"long_rows": 0, "wide_rows": 0, "upserted": 0, "batches": 0}
    for bs, be in iter_date_batches(start, end, args.batch_days):
        res = migrate_batch(
            engine, args.source_table, bs, be, args.chunk_rows, args.dry_run
        )
        totals["long_rows"] += res["long_rows"]
        totals["wide_rows"] += res["wide_rows"]
        totals["upserted"] += res["upserted"]
        totals["batches"] += 1
        print(f"    {bs}~{be}: long={res['long_rows']} wide={res['wide_rows']} upserted={res['upserted']}")

    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
