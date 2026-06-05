USE blader;

CREATE TABLE IF NOT EXISTS backtest_run
(
    id                          BIGINT            NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id                      VARCHAR(64)       NOT NULL COMMENT '回测运行唯一标识 UUID',
    start_date                  DATE              NOT NULL COMMENT '回测区间起始日期',
    end_date                    DATE              NOT NULL COMMENT '回测区间结束日期',
    universe                    VARCHAR(32)       NOT NULL DEFAULT 'all_a' COMMENT '股票池：all_a/csi300/csi500/csi1000',
    top_n                       INT               NOT NULL DEFAULT 30 COMMENT '最大持股数量',
    rebalance_freq              VARCHAR(16)       NOT NULL DEFAULT 'daily' COMMENT '调仓周期：daily/weekly/monthly',
    weight_mode                 VARCHAR(16)       NOT NULL DEFAULT 'equal' COMMENT '因子权重模式',
    industry_neutral            TINYINT           NOT NULL DEFAULT 0 COMMENT '行业中性化 0否 1是',
    cap_neutral                 TINYINT           NOT NULL DEFAULT 0 COMMENT '市值中性化 0否 1是',
    initial_cash                DECIMAL(20, 2)    NOT NULL DEFAULT 1000000.00 COMMENT '初始资金',
    buy_commission              DECIMAL(12, 8)    NOT NULL DEFAULT 0.00030000 COMMENT '买入佣金率',
    sell_commission             DECIMAL(12, 8)    NOT NULL DEFAULT 0.00130000 COMMENT '卖出佣金率',
    slippage                    DECIMAL(12, 8)    NOT NULL DEFAULT 0.00100000 COMMENT '滑点比例',
    factors_json                JSON              NULL COMMENT '因子配置 JSON',
    config_json                 JSON              NULL COMMENT '完整回测参数 JSON',
    total_return                DECIMAL(16, 8)    NULL COMMENT '累计收益率',
    annualized_return           DECIMAL(16, 8)    NULL COMMENT '年化收益率',
    benchmark_total_return      DECIMAL(16, 8)    NULL COMMENT '基准累计收益率',
    benchmark_annualized_return DECIMAL(16, 8)    NULL COMMENT '基准年化收益率',
    excess_return               DECIMAL(16, 8)    NULL COMMENT '超额收益（累计）',
    annualized_excess_return    DECIMAL(16, 8)    NULL COMMENT '年化超额收益',
    alpha                       DECIMAL(16, 8)    NULL COMMENT 'Alpha',
    beta                        DECIMAL(16, 8)    NULL COMMENT 'Beta',
    max_drawdown                DECIMAL(16, 8)    NULL COMMENT '最大回撤',
    sharpe_ratio                DECIMAL(16, 8)    NULL COMMENT '夏普比率',
    calmar_ratio                DECIMAL(16, 8)    NULL COMMENT '卡玛比率',
    win_rate                    DECIMAL(12, 8)    NULL COMMENT '胜率',
    profit_loss_ratio           DECIMAL(16, 8)    NULL COMMENT '盈亏比',
    volatility                  DECIMAL(16, 8)    NULL COMMENT '年化波动率',
    information_ratio           DECIMAL(16, 8)    NULL COMMENT '信息比率',
    trading_days                INT               NULL COMMENT '交易天数',
    monthly_heatmap_available   TINYINT           NOT NULL DEFAULT 0 COMMENT '月度热力图是否可展示 0否 1是',
    monthly_heatmap_note        VARCHAR(256)      NULL COMMENT '热力图不可展示时的说明',
    output_dir                  VARCHAR(512)      NULL COMMENT '报告文件目录',
    calc_version                VARCHAR(32)       NULL DEFAULT 'v1' COMMENT '计算版本',
    created_at                  DATETIME          NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at                  DATETIME          NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted                  INT               NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_backtest_run_id (run_id),
    KEY idx_backtest_run_date (start_date, end_date),
    KEY idx_backtest_run_created (created_at)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='策略回测运行记录（保留最近20次）';

CREATE TABLE IF NOT EXISTS backtest_nav
(
    id              BIGINT         NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id          VARCHAR(64)    NOT NULL COMMENT '关联 backtest_run.run_id',
    trade_date      DATE           NOT NULL COMMENT '交易日',
    strategy_equity DECIMAL(24, 4) NOT NULL COMMENT '策略总资产',
    strategy_nav    DECIMAL(16, 8) NOT NULL COMMENT '策略单位净值',
    benchmark_nav   DECIMAL(16, 8) NULL COMMENT '基准单位净值',
    excess_nav      DECIMAL(16, 8) NULL COMMENT '超额净值（策略-基准累计）',
    daily_return    DECIMAL(16, 8) NULL COMMENT '策略日收益率',
    updated_at      DATETIME       NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      INT            NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_backtest_nav (run_id, trade_date),
    KEY idx_backtest_nav_date (run_id, trade_date)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='回测净值曲线（策略/基准/超额）';

CREATE TABLE IF NOT EXISTS backtest_monthly_return
(
    id           BIGINT         NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id       VARCHAR(64)    NOT NULL COMMENT '关联 backtest_run.run_id',
    year_num     INT            NOT NULL COMMENT '年份',
    month_num    INT            NOT NULL COMMENT '月份 1-12',
    return_pct   DECIMAL(16, 8) NOT NULL COMMENT '月度收益率',
    updated_at   DATETIME       NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted   INT            NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_backtest_monthly (run_id, year_num, month_num),
    KEY idx_backtest_monthly_run (run_id)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='回测月度收益热力图数据';

CREATE TABLE IF NOT EXISTS backtest_holding_pnl
(
    id           BIGINT          NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id       VARCHAR(64)     NOT NULL COMMENT '关联 backtest_run.run_id',
    stock_code   VARCHAR(32)     NOT NULL COMMENT '股票代码',
    total_pnl    DECIMAL(24, 4)  NOT NULL COMMENT '回测期间累计盈亏金额',
    total_return DECIMAL(16, 8)  NULL COMMENT '回测期间累计收益率贡献',
    rank_type    VARCHAR(16)     NOT NULL COMMENT 'profit=盈利Top loss=亏损Top',
    rank_num     INT             NOT NULL COMMENT '排名 1-10',
    updated_at   DATETIME        NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted   INT             NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_backtest_holding (run_id, rank_type, rank_num),
    KEY idx_backtest_holding_stock (run_id, stock_code)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='持仓分析：盈利/亏损 Top10';

CREATE TABLE IF NOT EXISTS backtest_trade
(
    id           BIGINT          NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id       VARCHAR(64)     NOT NULL COMMENT '关联 backtest_run.run_id',
    trade_date   DATE            NOT NULL COMMENT '调仓成交日期',
    signal_date  DATE            NULL COMMENT '信号日期',
    action       VARCHAR(8)      NOT NULL COMMENT '操作：buy/sell',
    stock_code   VARCHAR(32)     NOT NULL COMMENT '股票代码',
    quantity     DECIMAL(24, 4)  NULL COMMENT '成交数量（股）',
    price        DECIMAL(16, 4)  NULL COMMENT '成交价',
    weight_delta DECIMAL(16, 8)  NULL COMMENT '权重变动',
    equity_after DECIMAL(24, 4)  NOT NULL COMMENT '调仓后总资产',
    updated_at   DATETIME        NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted   INT             NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    KEY idx_backtest_trade_run (run_id, trade_date),
    KEY idx_backtest_trade_stock (run_id, stock_code)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='回测交易记录（逐股调仓明细）';

CREATE TABLE IF NOT EXISTS backtest_factor_attribution
(
    id                BIGINT         NOT NULL AUTO_INCREMENT COMMENT '主键',
    run_id            VARCHAR(64)    NOT NULL COMMENT '关联 backtest_run.run_id',
    factor_code       VARCHAR(64)    NOT NULL COMMENT '因子编码',
    factor_weight     DECIMAL(12, 8) NULL COMMENT '因子配置权重',
    contribution_pct  DECIMAL(16, 8) NOT NULL COMMENT '对策略收益贡献占比',
    contribution_ret  DECIMAL(16, 8) NULL COMMENT '因子边际收益贡献',
    updated_at        DATETIME       NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted        INT            NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_backtest_factor_attr (run_id, factor_code),
    KEY idx_backtest_factor_attr (run_id)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='因子归因：各因子对策略收益的贡献占比';
