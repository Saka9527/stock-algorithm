-- 指数成分股（沪深300/中证500/中证1000）
CREATE TABLE IF NOT EXISTS index_members (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    trade_date  DATE         NOT NULL COMMENT '成分生效日期（调仓日）',
    index_code  VARCHAR(16)  NOT NULL COMMENT '指数代码，如 000300.SH',
    stock_code  VARCHAR(12)  NOT NULL COMMENT '成分股 THS 代码，如 600000.SH',
    source      VARCHAR(32)  NULL COMMENT '数据来源：baostock/csindex',
    update_time DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_index_date_stock (index_code, trade_date, stock_code),
    KEY idx_trade_date (trade_date),
    KEY idx_index_code (index_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数成分股历史快照';
