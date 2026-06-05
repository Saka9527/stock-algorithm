# -*- coding: utf-8 -*-
"""BaoStock 同步库表结构与 MySQL 写入。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from multi_factor.baostock.code_convert import bs_to_ths
from multi_factor.baostock.config_loader import (
    TABLE_DAILY,
    TABLE_QUOTA,
    TABLE_SYNC_STATE,
    BaostockConfig,
)

_MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS {daily} (
    trade_date   DATE NOT NULL COMMENT '交易日',
    stock_code   VARCHAR(12) NOT NULL COMMENT '600000.SH',
    open         DECIMAL(12, 4) NULL,
    high         DECIMAL(12, 4) NULL,
    low          DECIMAL(12, 4) NULL,
    close        DECIMAL(12, 4) NULL,
    volume       BIGINT NULL COMMENT '成交量(股)',
    amount       DECIMAL(20, 4) NULL COMMENT '成交额(元)',
    tradestatus  TINYINT NULL COMMENT '1正常 0停牌',
    PRIMARY KEY (trade_date, stock_code),
    KEY idx_daily_code_date (stock_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='BaoStock前复权日K';

CREATE TABLE IF NOT EXISTS {quota} (
    quota_date    DATE PRIMARY KEY COMMENT '自然日',
    request_count INT NOT NULL DEFAULT 0 COMMENT '已用请求次数',
    daily_limit   INT NOT NULL DEFAULT 40000 COMMENT '日上限',
    updated_at    DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='BaoStock API配额';

CREATE TABLE IF NOT EXISTS {sync_state} (
    stock_code      VARCHAR(12) PRIMARY KEY,
    last_trade_date DATE NULL COMMENT '已同步至该交易日',
    row_count       INT DEFAULT 0,
    updated_at      DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='BaoStock同步游标';

CREATE TABLE IF NOT EXISTS stock_sync_skip (
    stock_code VARCHAR(12) PRIMARY KEY,
    reason     VARCHAR(64) NULL COMMENT '跳过原因',
    updated_at DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='无数据/跳过股票';
"""


class BaostockStore:
    """MySQL 存储：日 K、同步状态、API 配额。"""

    def __init__(self, cfg: BaostockConfig):
        self.cfg = cfg
        self.engine: Engine = create_engine(cfg.db_url, pool_pre_ping=True)
        self.daily = cfg.table("daily")
        self.quota = cfg.table("quota")
        self.sync_state = cfg.table("sync_state")

    def init_schema(self) -> None:
        ddl = _MYSQL_DDL.format(
            daily=self.daily,
            quota=self.quota,
            sync_state=self.sync_state,
        )
        with self.engine.begin() as conn:
            for stmt in ddl.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(text(stmt))

    def load_sync_state(self) -> dict[str, str | None]:
        sql = text(f"SELECT stock_code, last_trade_date FROM `{self.sync_state}`")
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return {r[0]: (r[1].isoformat() if r[1] else None) for r in rows}

    def count_rows(self, stock_code: str) -> int:
        sql = text(f"SELECT COUNT(*) FROM `{self.daily}` WHERE stock_code = :code")
        with self.engine.connect() as conn:
            return int(conn.execute(sql, {"code": stock_code}).scalar() or 0)

    def count_stats(self) -> tuple[int, int]:
        with self.engine.connect() as conn:
            rows = int(
                conn.execute(text(f"SELECT COUNT(*) FROM `{self.daily}`")).scalar() or 0
            )
            stocks = int(
                conn.execute(
                    text(f"SELECT COUNT(DISTINCT stock_code) FROM `{self.daily}`")
                ).scalar()
                or 0
            )
        return rows, stocks

    def cleanup_false_sync_state(self) -> int:
        """删除「标记完成但无日 K」的游标，便于退市/空代码重试。"""
        sql = text(
            f"""
            DELETE s FROM `{self.sync_state}` s
            LEFT JOIN `{self.daily}` d ON s.stock_code = d.stock_code
            WHERE d.stock_code IS NULL
              AND COALESCE(s.row_count, 0) = 0
            """
        )
        with self.engine.begin() as conn:
            result = conn.execute(sql)
            return int(result.rowcount or 0)

    def load_daily_stock_codes(self) -> set[str]:
        sql = text(f"SELECT DISTINCT stock_code FROM `{self.daily}`")
        with self.engine.connect() as conn:
            return {r[0] for r in conn.execute(sql).fetchall()}

    def load_skip_codes(self) -> set[str]:
        sql = text("SELECT stock_code FROM stock_sync_skip")
        with self.engine.connect() as conn:
            try:
                return {r[0] for r in conn.execute(sql).fetchall()}
            except Exception:
                return set()

    def mark_skip(self, stock_code: str, reason: str = "no_data") -> None:
        sql = text(
            """
            INSERT INTO stock_sync_skip (stock_code, reason, updated_at)
            VALUES (:code, :reason, :ts)
            ON DUPLICATE KEY UPDATE reason = VALUES(reason), updated_at = VALUES(updated_at)
            """
        )
        with self.engine.begin() as conn:
            conn.execute(
                sql,
                {"code": stock_code, "reason": reason, "ts": datetime.now()},
            )

    def upsert_daily(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        work = df.copy()
        work["stock_code"] = work["code"].map(bs_to_ths)
        work = work.rename(columns={"date": "trade_date"})
        for col in ("open", "high", "low", "close", "amount"):
            work[col] = pd.to_numeric(work[col], errors="coerce")
        work["volume"] = pd.to_numeric(work["volume"], errors="coerce")
        work["tradestatus"] = pd.to_numeric(work["tradestatus"], errors="coerce")
        cols = [
            "trade_date",
            "stock_code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "tradestatus",
        ]
        work = work[cols].drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
        work = work.replace({pd.NA: None})
        work = work.astype(object).where(pd.notnull(work), None)
        records = work.to_dict(orient="records")
        for rec in records:
            for key in ("open", "high", "low", "close", "amount", "volume", "tradestatus"):
                val = rec.get(key)
                if val is None:
                    continue
                if isinstance(val, float) and pd.isna(val):
                    rec[key] = None
                    continue
                if key == "volume" and rec[key] is not None:
                    rec[key] = int(rec[key])
                if key == "tradestatus" and rec[key] is not None:
                    rec[key] = int(rec[key])
        sql = text(
            f"""
            INSERT INTO `{self.daily}`
                (trade_date, stock_code, open, high, low, close, volume, amount, tradestatus)
            VALUES
                (:trade_date, :stock_code, :open, :high, :low, :close, :volume, :amount, :tradestatus)
            ON DUPLICATE KEY UPDATE
                open = VALUES(open),
                high = VALUES(high),
                low = VALUES(low),
                close = VALUES(close),
                volume = VALUES(volume),
                amount = VALUES(amount),
                tradestatus = VALUES(tradestatus)
            """
        )
        chunk = 500
        with self.engine.begin() as conn:
            for i in range(0, len(records), chunk):
                conn.execute(sql, records[i : i + chunk])
        return len(records)

    def update_sync_state(
        self,
        stock_code: str,
        last_trade_date: str | None,
        row_count: int,
    ) -> None:
        sql = text(
            f"""
            INSERT INTO `{self.sync_state}`
                (stock_code, last_trade_date, row_count, updated_at)
            VALUES
                (:stock_code, :last_trade_date, :row_count, :updated_at)
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
                    "stock_code": stock_code,
                    "last_trade_date": last_trade_date,
                    "row_count": row_count,
                    "updated_at": datetime.now(),
                },
            )


def run_sql_script(engine: Engine, script_path: str | Path) -> None:
    path = Path(script_path)
    content = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in content.split(";"):
            stmt = stmt.strip()
            if stmt and not stmt.startswith("--"):
                conn.execute(text(stmt))
