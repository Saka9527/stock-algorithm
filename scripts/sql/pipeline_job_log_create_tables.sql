USE blader;

CREATE TABLE IF NOT EXISTS pipeline_job_log
(
    id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    job_type      VARCHAR(64)  NOT NULL COMMENT '任务类型：factor_performance/nightly/backtest_warmup',
    job_status    VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/succeeded/failed',
    start_date    DATE         NULL COMMENT '数据区间起始',
    end_date      DATE         NULL COMMENT '数据区间结束',
    params_json   JSON         NULL COMMENT '任务参数 JSON',
    result_json   JSON         NULL COMMENT '执行结果 JSON',
    success_count INT          NULL DEFAULT 0 COMMENT '成功子任务数',
    failed_count  INT          NULL DEFAULT 0 COMMENT '失败子任务数',
    error_message TEXT         NULL COMMENT '失败原因',
    started_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
    finished_at   DATETIME     NULL COMMENT '结束时间',
    duration_sec  INT          NULL COMMENT '耗时秒',
    is_deleted    INT          NULL DEFAULT 0 COMMENT '逻辑删除',
    PRIMARY KEY (id),
    KEY idx_pipeline_job_type (job_type, started_at),
    KEY idx_pipeline_job_status (job_status, started_at)
)
    ENGINE = InnoDB
    DEFAULT CHARSET = utf8mb4
    COLLATE = utf8mb4_general_ci
    COMMENT ='流水线定时任务日志';
