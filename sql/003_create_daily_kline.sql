-- A股日线K线
CREATE TABLE IF NOT EXISTS daily_kline (
    stock_code  VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    amount      DOUBLE PRECISION,
    PRIMARY KEY (stock_code, trade_date)
);
