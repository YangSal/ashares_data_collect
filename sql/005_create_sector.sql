-- 板块/行业分类
CREATE TABLE IF NOT EXISTS sector_stock (
    sector_name TEXT NOT NULL,
    stock_code  VARCHAR(20) NOT NULL,
    update_date DATE NOT NULL,
    PRIMARY KEY (sector_name, stock_code)
);

-- 板块成分变更记录（调入/调出）
CREATE TABLE IF NOT EXISTS sector_changelog (
    sector_name TEXT NOT NULL,
    stock_code  VARCHAR(20) NOT NULL,
    changed_at  DATE NOT NULL,
    action      VARCHAR(10) NOT NULL,  -- 'add' 或 'remove'
    PRIMARY KEY (sector_name, stock_code, changed_at, action)
);
