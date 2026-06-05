# -*- coding: utf-8 -*-
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import baostock as bs
from sqlalchemy import text

from multi_factor.baostock.client import BaostockClient
from multi_factor.baostock.config_loader import load_baostock_config
from multi_factor.baostock.db import BaostockStore
from multi_factor.baostock.quota import DailyRequestQuota
from multi_factor.baostock.sync import BaostockDailySync

cfg = load_baostock_config()
store = BaostockStore(cfg)
sync = BaostockDailySync(cfg)

with store.engine.connect() as conn:
    in_db = conn.execute(
        text("SELECT COUNT(DISTINCT stock_code) FROM stock_daily_qfq")
    ).scalar()
    in_state = conn.execute(text("SELECT COUNT(*) FROM stock_sync_state")).scalar()
    complete = conn.execute(
        text(
            "SELECT COUNT(*) FROM stock_sync_state WHERE last_trade_date >= '2026-06-04'"
        )
    ).scalar()
    partial = conn.execute(
        text(
            "SELECT COUNT(*) FROM stock_sync_state "
            "WHERE last_trade_date IS NOT NULL AND last_trade_date < '2026-06-04'"
        )
    ).scalar()
    rows = conn.execute(text("SELECT COUNT(*) FROM stock_daily_qfq")).scalar()
    max_upd = conn.execute(
        text("SELECT MAX(updated_at) FROM stock_sync_state")
    ).scalar()
    print(f"daily: {rows} rows, {in_db} stocks")
    print(f"sync_state: {in_state} entries, complete={complete}, partial={partial}")
    print(f"last sync_state update: {max_upd}")

    # 有 sync_state 但 daily 无数据
    orphan = conn.execute(
        text(
            """
            SELECT COUNT(*) FROM stock_sync_state s
            LEFT JOIN (SELECT DISTINCT stock_code FROM stock_daily_qfq) d
              ON s.stock_code = d.stock_code
            WHERE d.stock_code IS NULL
            """
        )
    ).scalar()
    print(f"sync_state but no daily rows: {orphan}")

quota = DailyRequestQuota(store, cfg.daily_api_limit)
client = BaostockClient(quota)
with client.session():
    plan = sync.build_plan(client, end_date="2026-06-04")

never_state = 0
partial_only = 0
for code in plan.stocks[:500]:
    ths = __import__("multi_factor.baostock.code_convert", fromlist=["bs_to_ths"]).bs_to_ths(code)
    state = store.load_sync_state()
    last = state.get(ths)
    if last is None:
        never_state += 1
    elif last < "2026-06-04":
        partial_only += 1

print(f"todo total: {len(plan.stocks)}, skipped: {plan.skipped_up_to_date}")
print(f"first 500 todo: never_synced={never_state}, partial_update={partial_only}")
print(f"first 5 todo codes: {plan.stocks[:5]}")
# 找第一个从未入库的
state = store.load_sync_state()
with store.engine.connect() as conn:
    db_codes = set(
        r[0]
        for r in conn.execute(text("SELECT DISTINCT stock_code FROM stock_daily_qfq")).fetchall()
    )
first_new = None
for code in plan.stocks:
    ths = __import__("multi_factor.baostock.code_convert", fromlist=["bs_to_ths"]).bs_to_ths(code)
    if ths not in db_codes:
        first_new = ths
        break
print(f"first todo stock NOT in daily: {first_new}")
