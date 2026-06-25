-- 指数成分权重
CREATE TABLE IF NOT EXISTS index_weight (
    index_code  VARCHAR(20) NOT NULL,
    stock_code  VARCHAR(20) NOT NULL,
    weight      DOUBLE PRECISION,
    update_date DATE NOT NULL,
    PRIMARY KEY (index_code, stock_code)
);

-- 指数权重变更记录
CREATE TABLE IF NOT EXISTS index_weight_changelog (
    index_code  VARCHAR(20) NOT NULL,
    stock_code  VARCHAR(20) NOT NULL,
    changed_at  DATE NOT NULL,
    old_weight  DOUBLE PRECISION,
    new_weight  DOUBLE PRECISION,
    PRIMARY KEY (index_code, stock_code, changed_at)
);
