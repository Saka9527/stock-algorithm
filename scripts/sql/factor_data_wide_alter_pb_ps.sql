-- factor_data_wide: pb -> pb_mrq，迁移 JSON PB_MRQ，新增 ps_ttm / pcf_ncf_ttm
USE blader;

-- 1) 新增列（若已存在请跳过对应语句）
ALTER TABLE factor_data_wide
    ADD COLUMN IF NOT EXISTS pb_mrq DECIMAL(18, 6) NULL COMMENT '市净率 MRQ' AFTER pe_ttm;

ALTER TABLE factor_data_wide
    ADD COLUMN IF NOT EXISTS ps_ttm DECIMAL(18, 6) NULL COMMENT '市销率 TTM' AFTER pb_mrq;

ALTER TABLE factor_data_wide
    ADD COLUMN IF NOT EXISTS pcf_ncf_ttm DECIMAL(18, 6) NULL COMMENT '市现率 TTM' AFTER ps_ttm;

-- 2) pb_mrq <- factor_ext_json.PB_MRQ，其次原 pb 列
UPDATE factor_data_wide
SET pb_mrq = COALESCE(
        CAST(JSON_UNQUOTE(JSON_EXTRACT(factor_ext_json, '$.PB_MRQ')) AS DECIMAL(18, 6)),
        pb_mrq,
        pb
    )
WHERE pb_mrq IS NULL
   OR JSON_EXTRACT(factor_ext_json, '$.PB_MRQ') IS NOT NULL
   OR pb IS NOT NULL;

-- 3) 清理 JSON 中已落库的 PB_MRQ
UPDATE factor_data_wide
SET factor_ext_json = JSON_REMOVE(factor_ext_json, '$.PB_MRQ')
WHERE JSON_EXTRACT(factor_ext_json, '$.PB_MRQ') IS NOT NULL;

-- 4) 删除旧 pb 列（MySQL 8 需手动确认列存在）
-- ALTER TABLE factor_data_wide DROP COLUMN pb;

CREATE TABLE IF NOT EXISTS factor_wide_sync_state (
    stock_code      VARCHAR(12) PRIMARY KEY,
    last_trade_date DATE NULL,
    row_count       INT DEFAULT 0,
    updated_at      DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='BaoStock->factor_data_wide 同步游标';
