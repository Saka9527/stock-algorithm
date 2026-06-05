# -*- coding: utf-8 -*-
import os
import pymysql

conn = pymysql.connect(
    host=os.environ.get("BLADER_DB_HOST", "117.25.133.3"),
    port=int(os.environ.get("BLADER_DB_PORT", "3306")),
    user=os.environ.get("BLADER_DB_USER", "stock-dev"),
    password=os.environ.get("BLADER_DB_PASSWORD", "GQFAJB2ZVWAKIAVVZOOA"),
    database=os.environ.get("BLADER_DB_NAME", "blader"),
    charset="utf8mb4",
    connect_timeout=15,
)
cur = conn.cursor()
cur.execute("SELECT MIN(data_date), MAX(data_date), COUNT(*) FROM factor_data_wide")
print("factor_data_wide range:", cur.fetchone())
cur.execute(
    "SELECT COUNT(*) FROM factor_data_wide WHERE pe_ttm IS NOT NULL OR pb_mrq IS NOT NULL OR mom_20d IS NOT NULL"
)
print("factor_data_wide with core cols:", cur.fetchone()[0])
cur.execute("SELECT MIN(data_date), MAX(data_date), COUNT(*) FROM factor_calc_data WHERE is_deleted=0")
print("factor_calc_data (legacy) range:", cur.fetchone())
cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM stock_daily_qfq")
print("daily range:", cur.fetchone())
cur.execute(
    "SELECT stock_code, trade_date, close FROM stock_daily_qfq "
    "WHERE close > 0 ORDER BY trade_date DESC LIMIT 5"
)
print("daily with close>0:", cur.fetchall())
cur.execute("SHOW TABLES")
tables = [r[0] for r in cur.fetchall()]
print("all tables count:", len(tables))
for kw in ("index", "member", "constituent", "bench"):
    print(kw, [t for t in tables if kw in t.lower()])
conn.close()
