-- 合约详情快照（自动建表，此文件仅供参考）
-- instrument_info: 83字段（排除每日变动字段），PK: stock_code
-- instrument_changelog: 变更记录，PK: (stock_code, changed_at, field_name)

CREATE TABLE IF NOT EXISTS instrument_changelog (
    stock_code  VARCHAR(20) NOT NULL,
    changed_at  DATE NOT NULL,
    field_name  VARCHAR(80) NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    PRIMARY KEY (stock_code, changed_at, field_name)
);
