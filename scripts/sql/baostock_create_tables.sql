-- BaoStock 前复权日 K 精简表（SQLite / MySQL 通用 DDL 参考）

CREATE TABLE IF NOT EXISTS stock_daily_qfq (
    trade_date   DATE         NOT NULL COMMENT '交易日',
    stock_code   VARCHAR(12)  NOT NULL COMMENT '600000.SH',
    open         DECIMAL(12, 4) NULL,
    high         DECIMAL(12, 4) NULL,
    low          DECIMAL(12, 4) NULL,
    close        DECIMAL(12, 4) NULL,
    volume       BIGINT       NULL COMMENT '成交量(股)',
    amount       DECIMAL(20, 4) NULL COMMENT '成交额(元)',
    tradestatus  TINYINT      NULL COMMENT '1正常 0停牌',
    PRIMARY KEY (trade_date, stock_code)
);

CREATE INDEX IF NOT EXISTS idx_daily_code_date
    ON stock_daily_qfq (stock_code, trade_date);

CREATE TABLE IF NOT EXISTS api_quota (
    quota_date    DATE PRIMARY KEY COMMENT '自然日',
    request_count INTEGER NOT NULL DEFAULT 0 COMMENT '已用请求次数',
    daily_limit   INTEGER NOT NULL DEFAULT 40000 COMMENT '日上限',
    updated_at    DATETIME NULL
);

CREATE TABLE IF NOT EXISTS stock_sync_state (
    stock_code      VARCHAR(12) PRIMARY KEY,
    last_trade_date DATE NULL COMMENT '已同步至该交易日',
    row_count       INTEGER DEFAULT 0,
    updated_at      DATETIME NULL
);
