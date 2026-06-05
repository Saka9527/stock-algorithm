# -*- coding: utf-8 -*-
"""执行 factor_data_wide 列迁移：pb->pb_mrq，新增 ps_ttm/pcf_ncf_ttm。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text

from multi_factor.ifind.config_loader import load_ifind_config


def _has_column(conn, table: str, col: str) -> bool:
    rows = conn.execute(text(f"SHOW COLUMNS FROM `{table}` LIKE :c"), {"c": col}).fetchall()
    return bool(rows)


def main():
    cfg = load_ifind_config()
    engine = create_engine(cfg.db_url, pool_pre_ping=True)
    table = cfg.table("factor")

    with engine.begin() as conn:
        if not _has_column(conn, table, "pb_mrq"):
            conn.execute(
                text(
                    f"ALTER TABLE `{table}` ADD COLUMN pb_mrq DECIMAL(18,6) NULL "
                    f"COMMENT '市净率 MRQ' AFTER pe_ttm"
                )
            )
            print("added pb_mrq")

        if not _has_column(conn, table, "ps_ttm"):
            conn.execute(
                text(
                    f"ALTER TABLE `{table}` ADD COLUMN ps_ttm DECIMAL(18,6) NULL "
                    f"COMMENT '市销率 TTM' AFTER pb_mrq"
                )
            )
            print("added ps_ttm")

        if not _has_column(conn, table, "pcf_ncf_ttm"):
            conn.execute(
                text(
                    f"ALTER TABLE `{table}` ADD COLUMN pcf_ncf_ttm DECIMAL(18,6) NULL "
                    f"COMMENT '市现率 TTM' AFTER ps_ttm"
                )
            )
            print("added pcf_ncf_ttm")

        n = conn.execute(
            text(
                f"""
                UPDATE `{table}`
                SET pb_mrq = COALESCE(
                    CAST(JSON_UNQUOTE(JSON_EXTRACT(factor_ext_json, '$.PB_MRQ')) AS DECIMAL(18,6)),
                    pb_mrq,
                    pb
                )
                WHERE pb_mrq IS NULL
                   OR JSON_EXTRACT(factor_ext_json, '$.PB_MRQ') IS NOT NULL
                   OR pb IS NOT NULL
                """
            )
        ).rowcount
        print(f"migrated pb_mrq rows: {n}")

        n2 = conn.execute(
            text(
                f"""
                UPDATE `{table}`
                SET factor_ext_json = JSON_REMOVE(factor_ext_json, '$.PB_MRQ')
                WHERE JSON_EXTRACT(factor_ext_json, '$.PB_MRQ') IS NOT NULL
                """
            )
        ).rowcount
        print(f"removed PB_MRQ from json rows: {n2}")

        if _has_column(conn, table, "pb"):
            conn.execute(text(f"ALTER TABLE `{table}` DROP COLUMN pb"))
            print("dropped pb")

        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS factor_wide_sync_state (
                    stock_code VARCHAR(12) PRIMARY KEY,
                    last_trade_date DATE NULL,
                    row_count INT DEFAULT 0,
                    updated_at DATETIME NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )
        print("factor_wide_sync_state ready")


if __name__ == "__main__":
    main()
