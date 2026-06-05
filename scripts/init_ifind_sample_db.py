# -*- coding: utf-8 -*-
"""
生成 iFinD 示例 SQLite 库（用于验证 --source ifind 流程）。

  python scripts/init_ifind_sample_db.py
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "ifind_sync.db"
CONFIG_DST = ROOT / "config" / "ifind_config.yaml"


def _make_calendar(start: str, end: str) -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    return pd.DataFrame({"trade_date": days.strftime("%Y-%m-%d")})


def _make_members(codes: list[str], start: str, end: str) -> pd.DataFrame:
    rows = []
    for m in pd.date_range(start, end, freq="MS"):
        for c in codes:
            rows.append(
                {
                    "trade_date": m.strftime("%Y-%m-%d"),
                    "index_code": "000300.SH",
                    "stock_code": c,
                }
            )
    return pd.DataFrame(rows)


def main():
    rng = np.random.default_rng(42)
    codes = [
        "600000.SH",
        "600009.SH",
        "600016.SH",
        "600019.SH",
        "600028.SH",
        "600030.SH",
        "600036.SH",
        "600048.SH",
        "600050.SH",
        "600104.SH",
        "000001.SZ",
        "000002.SZ",
        "000063.SZ",
        "000100.SZ",
        "000157.SZ",
        "000333.SZ",
        "000651.SZ",
        "000725.SZ",
        "000768.SZ",
        "000858.SZ",
    ]
    start, end = "2020-01-02", "2020-12-31"
    days = pd.bdate_range(start, end)

    daily_rows = []
    fund_rows = []
    for code in codes:
        price = 10 + rng.random() * 20
        pe0, pb0, roe0 = 8 + rng.random() * 20, 1 + rng.random() * 3, 0.05 + rng.random() * 0.2
        for i, d in enumerate(days):
            ret = rng.normal(0.0005, 0.02)
            price = max(1.0, price * (1 + ret))
            daily_rows.append(
                {
                    "trade_date": d.strftime("%Y-%m-%d"),
                    "stock_code": code,
                    "open": price * 0.99,
                    "high": price * 1.01,
                    "low": price * 0.98,
                    "close": price,
                    "volume": int(rng.integers(1e5, 1e7)),
                    "amount": price * 1e6,
                }
            )
            if i % 20 == 0:
                pe0 *= 1 + rng.normal(0, 0.05)
                pb0 *= 1 + rng.normal(0, 0.03)
                roe0 = np.clip(roe0 + rng.normal(0, 0.01), 0.01, 0.5)
            fund_rows.append(
                {
                    "trade_date": d.strftime("%Y-%m-%d"),
                    "stock_code": code,
                    "pe_ttm": pe0,
                    "pb_lf": pb0,
                    "roe_ttm": roe0,
                }
            )

    idx_days = days
    idx_price = 100.0
    index_daily = []
    for d in idx_days:
        idx_price *= 1 + rng.normal(0.0003, 0.015)
        index_daily.append(
            {
                "trade_date": d.strftime("%Y-%m-%d"),
                "index_code": "000300.SH",
                "close": idx_price,
            }
        )

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    pd.DataFrame(daily_rows).to_sql("ifind_stock_daily", conn, index=False)
    pd.DataFrame(fund_rows).to_sql("ifind_stock_fundamental", conn, index=False)
    _make_members(codes, start, end).to_sql("ifind_index_members", conn, index=False)
    pd.DataFrame(index_daily).to_sql("ifind_index_daily", conn, index=False)
    _make_calendar(start, end).to_sql("ifind_trading_calendar", conn, index=False)
    conn.close()

    example = ROOT / "config" / "ifind_config.example.yaml"
    if not CONFIG_DST.exists() and example.exists():
        CONFIG_DST.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"示例库已写入: {DB_PATH}")
    print(f"配置: {CONFIG_DST}（若不存在已从 example 复制）")
    print("运行: python run_backtest.py --source ifind --start 20200102 --end 20201231")


if __name__ == "__main__":
    main()
