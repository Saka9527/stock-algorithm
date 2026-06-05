USE blader;

CREATE TABLE IF NOT EXISTS factor_performance_summary
(
    id                  BIGINT            NOT NULL AUTO_INCREMENT COMMENT '主键',
    factor_code         VARCHAR(64)       NOT NULL COMMENT '因子编码，关联 factor_base_info.factor_code',
    start_date          DATE              NOT NULL COMMENT '分析区间起始日期（含）',
    end_date            DATE              NOT NULL COMMENT '分析区间结束日期（含）',
    period              INT               NOT NULL DEFAULT 1 COMMENT 'IC/分组收益前瞻持有期（交易日）',
    quantiles           INT               NOT NULL DEFAULT 5 COMMENT '分层回测分位组数，如 5 组',
    top_pct             DECIMAL(6, 4)     NOT NULL DEFAULT 0.2000 COMMENT 'Top/Bottom 分组比例，如 0.2 表示各 20%',
    ic_mean             DECIMAL(24, 8)    NULL COMMENT 'IC 均值（截面 Spearman 秩相关）',
    ic_std              DECIMAL(24, 8)    NULL COMMENT 'IC 标准差',
    ic_ir               DECIMAL(24, 8)    NULL COMMENT 'IC 信息比率 IC_mean / IC_std',
    win_rate            DECIMAL(12, 8)    NULL COMMENT 'IC 胜率，IC>0 的交易日占比',
    positive_count      INT               NULL COMMENT 'IC 为正的有效交易日数',
    negative_count      INT               NULL COMMENT 'IC 为负的有效交易日数',
    total_count         INT               NULL COMMENT '有效 IC 交易日总数',
    sharpe_top_group    DECIMAL(24, 8)    NULL COMMENT 'Top 分组日收益年化夏普',
    sharpe_bottom_group DECIMAL(24, 8)    NULL COMMENT 'Bottom 分组日收益年化夏普',
    sharpe_long_short   DECIMAL(24, 8)    NULL COMMENT '多空组合（Top-Bottom）日收益年化夏普',
    data_start          DATE              NULL COMMENT '因子截面数据实际起始日',
    data_end            DATE              NULL COMMENT '因子截面数据实际结束日',
    stock_count_avg     INT               NULL COMMENT '分析区间内日均有效股票数',
    calc_version        VARCHAR(32)       NULL DEFAULT 'v1' COMMENT '统计计算版本号',
    updated_at          DATETIME          NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted          INT               NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_factor_perf (factor_code, start_date, end_date, period, quantiles, top_pct),
    KEY idx_factor_perf_code_date (factor_code, start_date, end_date)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='因子绩效汇总表';

CREATE TABLE IF NOT EXISTS factor_performance_series
(
    id           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    factor_code  VARCHAR(64)  NOT NULL COMMENT '因子编码，关联 factor_base_info.factor_code',
    start_date   DATE         NOT NULL COMMENT '分析区间起始日期（与 summary 一致）',
    end_date     DATE         NOT NULL COMMENT '分析区间结束日期（与 summary 一致）',
    period       INT          NOT NULL DEFAULT 1 COMMENT 'IC/收益前瞻持有期（交易日），与 summary.period 一致',
    series_type  VARCHAR(32)  NOT NULL COMMENT '序列类型：ic=IC走势 group=Top/Bottom分组收益 quantile=分位组收益',
    series_date  DATE         NOT NULL COMMENT '序列点位日期（交易日）',
    payload_json JSON         NOT NULL COMMENT '序列载荷 JSON，如 ic:{ic} group:{top_group,bottom_group,top_group_nav,...} quantile:{quantile,return,nav}',
    updated_at   DATETIME     NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted   INT          NULL DEFAULT 0 COMMENT '逻辑删除 0否 1是',
    PRIMARY KEY (id),
    UNIQUE KEY uk_factor_series (factor_code, start_date, end_date, period, series_type, series_date),
    KEY idx_factor_series (factor_code, series_type, series_date)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='因子绩效时序表';
