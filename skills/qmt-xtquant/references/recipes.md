# 可复制代码范式

均假设 `from xtquant import xtdata` 且 MiniQMT 已启动登录。与本项目 [jobs/](../../../data_collect/jobs/)
的写法对齐。

## 目录
- [取 A 股代码列表](#取-a-股代码列表)
- [批量下载 + 按股读取 K 线（项目范式）](#批量下载--按股读取-k-线项目范式)
- [tick 数据读取](#tick-数据读取)
- [除权因子与复权计算](#除权因子与复权计算)
- [财务数据下载 + 读取](#财务数据下载--读取)
- [指数权重 / 合约信息](#指数权重--合约信息)
- [全推快照 / 对手价](#全推快照--对手价)
- [时间戳与代码格式转换](#时间戳与代码格式转换)
- [VIP / token 连接](#vip--token-连接)

---

## 取 A 股代码列表
```python
xtdata.download_sector_data()                       # 板块信息先下载（静态，按日/周更新即可）
codes = xtdata.get_stock_list_in_sector("沪深A股")   # 失败可回退合并 上证A股/深证A股/北证A股
```

## 批量下载 + 按股读取 K 线（项目范式）
`get_market_data_ex` 返回 `{code: DataFrame}`，逐只取出后标准化。分批（如 50 只）下载可控内存。
```python
import pandas as pd

def download_and_read(batch, trade_date, period="1d"):
    # 1) 先下载到本地（同步阻塞）
    xtdata.download_history_data2(batch, period, trade_date, trade_date)
    # 2) 从本地读取
    raw = xtdata.get_market_data_ex(
        field_list=["time", "open", "high", "low", "close", "volume", "amount"],
        stock_list=batch, period=period,
        start_time=trade_date, end_time=trade_date,
        count=-1, dividend_type="none", fill_data=False,
    )
    chunks = []
    for code in batch:
        df = raw.get(code)
        if df is None or df.empty:
            continue
        df = df.copy()
        # time 是 UTC 毫秒 → 北京时间
        df["trade_date"] = (pd.to_datetime(df["time"], unit="ms")
                            + pd.Timedelta(hours=8)).dt.date
        df["stock_code"] = code
        chunks.append(df)
    return chunks
```

## tick 数据读取
tick 周期返回 `{code: np.ndarray}`（结构化数组，非 DataFrame），按 `time` 升序：
```python
xtdata.download_history_data2(["000001.SZ"], "tick", "20240102", "20240102")
raw = xtdata.get_market_data_ex([], ["000001.SZ"], period="tick",
                                start_time="20240102", end_time="20240102", count=-1)
arr = raw["000001.SZ"]            # np.ndarray，可 pd.DataFrame(arr) 转换
df = pd.DataFrame(arr)
df["dt"] = pd.to_datetime(df["time"], unit="ms") + pd.Timedelta(hours=8)
```

## 除权因子与复权计算
`get_divid_factors` 返回 DataFrame（index 为除权日）。官方等比/非等比复权算法：
```python
def gen_divid_ratio(quote_datas, divid_datas):
    drl, dr, qi, di = [], 1.0, 0, 0
    qdl, ddl = len(quote_datas), len(divid_datas)
    while qi < qdl and di < ddl:
        qd, dd = quote_datas.iloc[qi], divid_datas.iloc[di]
        if qd.name >= dd.name:
            dr *= dd['dr']; di += 1
        if qd.name <= dd.name:
            drl.append(dr); qi += 1
    while qi < qdl:
        drl.append(dr); qi += 1
    return pd.DataFrame(drl, index=quote_datas.index, columns=quote_datas.columns)

def process_forward_ratio(quote_datas, divid_datas):   # 等比前复权
    drl = gen_divid_ratio(quote_datas, divid_datas)
    return (quote_datas * (drl / drl.iloc[-1])).apply(lambda x: round(x, 2))

def process_backward_ratio(quote_datas, divid_datas):  # 等比后复权
    drl = gen_divid_ratio(quote_datas, divid_datas)
    return (quote_datas * drl).apply(lambda x: round(x, 2))

s = '002594.SZ'
dd = xtdata.get_divid_factors(s)
ori = xtdata.get_market_data(['open','high','low','close'], [s], '1d', dividend_type='none')['close'].T
fq = process_forward_ratio(ori, dd)
```
注：直接传 `dividend_type='front'/'back'` 让 xtdata 复权通常更省事；手算仅在需自定义时用。

## 财务数据下载 + 读取
```python
tables = ["Balance", "Income", "CashFlow", "Pershareindex"]
xtdata.download_financial_data2(codes, tables, start_time="20240101", end_time="20241231")
data = xtdata.get_financial_data(codes, tables, report_type="announce_time")
df_income = data["000001.SZ"]["Income"]   # {code: {table: DataFrame}}
```

## 指数权重 / 合约信息
```python
xtdata.download_index_weight()
weights = xtdata.get_index_weight("000300.SH")    # {成分股: 权重}

detail = xtdata.get_instrument_detail("000001.SZ", iscomplete=True)
print(detail["InstrumentName"], detail["OpenDate"], detail["TotalVolume"])
```

## 全推快照 / 对手价
```python
tick = xtdata.get_full_tick(["000001.SZ"])
t = tick["000001.SZ"]
# 买一价为对手价（卖出场景）；买一为 0（跌停）则用最新价
fix_price = t["bidPrice"][0] if t["bidPrice"][0] != 0 else t["lastPrice"]
```

## 时间戳与代码格式转换
```python
import pandas as pd
# xtdata 时间戳（UTC 毫秒）→ 北京时间
bj = pd.to_datetime(ms_ts, unit="ms") + pd.Timedelta(hours=8)

# QMT 代码 ↔ 通达信/库内格式（本项目约定，非 QMT 接口）
def to_tdx(code):           # '000001.SZ' -> 'sz000001'
    num, mkt = code.split(".")
    return mkt.lower() + num
def to_six(code):           # '000001.SZ' -> '000001'
    return code.split(".")[0]
```

## VIP / token 连接
独立行情服务（不依赖已登录的 MiniQMT 客户端）用 `xtdatacenter`：
```python
from xtquant import xtdatacenter as xtdc
from xtquant import xtdata

xtdc.set_token("你的token")                 # 投研用户中心获取，必须先于 init
xtdc.set_kline_mirror_enabled(True)         # 可选：开启 K 线全推（VIP）
xtdc.init()
port = xtdc.listen(port=58621)              # 或 listen(port=(58620, 58630)) 自动选可用端口
xtdata.connect(port=port)
xtdata.run()                                # 维持连接
```
端口占用、连接返回 -1 等问题见 [troubleshooting.md](troubleshooting.md)。
