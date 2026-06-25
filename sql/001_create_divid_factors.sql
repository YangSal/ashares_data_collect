-- 复权因子表
-- xtquant.get_divid_factors() 返回的除权除息数据

CREATE TABLE IF NOT EXISTS divid_factors (
    stock_code   VARCHAR(20)    NOT NULL,   -- 股票代码，如 000001.SZ
    date         DATE           NOT NULL,   -- 除权除息日
    interest     NUMERIC(12,4),             -- 每股派息（税前）
    stock_bonus  NUMERIC(12,6),             -- 每股送股
    stock_gift   NUMERIC(12,6),             -- 每股转增
    allot_num    NUMERIC(12,6),             -- 每股配股数
    allot_price  NUMERIC(12,4),             -- 配股价
    dr           NUMERIC(18,10),            -- 除权因子（等比复权用）
    created_at   TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (stock_code, date)
);

COMMENT ON TABLE divid_factors IS 'A股除权除息因子表';
COMMENT ON COLUMN divid_factors.dr IS '除权因子，用于等比前复权/后复权计算';
