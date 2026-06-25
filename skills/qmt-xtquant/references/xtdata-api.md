# xtdata 接口参考

`from xtquant import xtdata` 后调用。所有 `download_*` 同步阻塞，下载完才返回，**本身不返回数据**；
数据用对应 `get_*` 读取。

## 目录
- [行情订阅](#行情订阅)
- [行情获取](#行情获取)
- [历史数据下载](#历史数据下载)
- [财务数据](#财务数据)
- [合约基础信息](#合约基础信息)
- [交易日历](#交易日历)
- [板块分类](#板块分类)
- [指数权重](#指数权重)
- [可转债 / ETF / 新股](#可转债--etf--新股)
- [连接管理](#连接管理)

字段含义见 [data-fields.md](data-fields.md)，可复制代码见 [recipes.md](recipes.md)。

---

## 行情订阅

### subscribe_quote
```
subscribe_quote(stock_code, period='1d', start_time='', end_time='', count=0, callback=None)
```
订阅单股行情，返回订阅号（>0 成功，-1 失败）。数据从 `callback(datas)` 推送，`datas` 形如
`{stock_code: [data1, data2, ...]}`。仅订阅实时通常传 `count=0`。单股订阅数量建议 ≤50。

### subscribe_whole_quote
```
subscribe_whole_quote(code_list, callback=None)
```
订阅全推分笔行情，返回订阅号。`code_list` 可传市场码 `['SH','SZ']`（全市场）或合约码
`['600000.SH','000001.SZ']`。`callback(datas)` 的 `datas` 形如 `{stock: data}`。高订阅数场景首选。

### unsubscribe_quote(seq)
反订阅，`seq` 为订阅时返回的订阅号。

### run()
阻塞当前线程维持运行以持续处理回调。**用 callback 订阅时必须调用**，否则程序执行到末尾直接退出。

---

## 行情获取

### get_market_data_ex（本项目主用）
```
get_market_data_ex(field_list=[], stock_list=[], period='1d', start_time='', end_time='',
                   count=-1, dividend_type='none', fill_data=True)
```
返回 `{股票代码: pd.DataFrame}`，每只 DataFrame 的 index 为时间、columns 为字段。`field_list=[]` 取全部
字段。相比 `get_market_data` 额外支持日线以上周期（`1w/1mon/1q/1hy/1y`）与 ETF 申赎清单。
tick 周期返回 `{股票: np.ndarray}`。

### get_market_data
```
get_market_data(field_list=[], stock_list=[], period='1d', start_time='', end_time='',
               count=-1, dividend_type='none', fill_data=True)
```
从缓存获取行情。K 线周期返回 `{field: pd.DataFrame}`，每个 DataFrame 的 index 为股票、columns 为时间
（**与 `_ex` 相反**）。tick 周期返回 `{股票: np.ndarray}`。`count` 语义见 SKILL.md。时间范围为闭区间。

### get_local_data
```
get_local_data(field_list=[], stock_list=[], period='1d', start_time='', end_time='',
              count=-1, dividend_type='none', fill_data=True, data_dir=data_dir)
```
直接从本地数据文件读取（仅 level1），用于快速批量取历史。`data_dir` 默认自动从 MiniQMT 获取，一般无需传。

### get_full_tick(code_list)
获取全推快照数据，返回 `{股票: data}`。`code_list` 同 `subscribe_whole_quote`。

### get_full_kline
```
get_full_kline(field_list=[], stock_list=[], period='1m', start_time='', end_time='',
              count=1, dividend_type='none', fill_data=True)
```
获取**最新交易日**的 K 线全推数据（不含历史），返回 `{field: DataFrame}`。需开启 K 线全推。

### get_divid_factors(stock_code, start_time='', end_time='')
获取除权数据，返回 `pd.DataFrame`（字段 `interest/stockBonus/stockGift/allotNum/allotPrice/gugai/dr`）。
取前需先下载该股历史 K 线。复权算法见 [recipes.md](recipes.md)。

---

## 历史数据下载

### download_history_data
```
download_history_data(stock_code, period, start_time='', end_time='', incrementally=None)
```
补充单只历史行情。`incrementally`：`True/False` 显式控制增量；`None` 时按 `start_time` 判断（为空则
增量下载，从本地最后一条往后接）。

### download_history_data2（本项目主用，批量）
```
download_history_data2(stock_list, period, start_time='', end_time='', callback=None, incrementally=None)
```
批量补充历史行情。`callback(data)` 回传进度 dict：`{'finished':1,'total':50,'stockcode':'000001.SZ','message':''}`。

### download_history_contracts()
下载过期（退市）合约信息。完成后过期合约可经 `get_instrument_detail()` 查询；过期板块名可由
`[i for i in xtdata.get_sector_list() if "过期" in i]` 查看，再用 `get_stock_list_in_sector` 取退市标的。

---

## 财务数据

### get_financial_data
```
get_financial_data(stock_list, table_list=[], start_time='', end_time='', report_type='report_time')
```
返回 `{股票: {表名: pd.DataFrame}}`。`table_list` 取值：`Balance`(资产负债)、`Income`(利润)、
`CashFlow`(现金流)、`Capital`(股本)、`Holdernum`(股东数)、`Top10holder`(十大股东)、
`Top10flowholder`(十大流通股东)、`Pershareindex`(每股指标)。
`report_type`：`report_time`(按截止日期) 或 `announce_time`(按披露日期)。字段见 [data-fields.md](data-fields.md)。

### download_financial_data(stock_list, table_list=[])
下载财务数据（全量）。

### download_financial_data2（本项目主用）
```
download_financial_data2(stock_list, table_list=[], start_time='', end_time='', callback=None)
```
按 `m_anntime`（披露日期）字段在 `[start_time, end_time]` 范围筛选下载。`callback` 同 `download_history_data2`。

---

## 合约基础信息

### get_instrument_detail(stock_code, iscomplete=False)
返回合约信息 dict，找不到返回 `None`。`iscomplete=False` 返回常用字段（市场、名称、IPO 日、涨跌停价、
流通/总股本、最小变动价位等）；`iscomplete=True` 返回全部字段（含期货/期权手续费、保证金等）。
完整字段列表见 [data-fields.md](data-fields.md)。可用于校验代码是否正确。

### get_instrument_type(stock_code)
返回 `{类型: bool}`，类型有 `index/stock/fund/etf`。找不到返回 `None`。

---

## 交易日历

### get_trading_dates(market, start_time='', end_time='', count=-1)
返回时间戳列表。`market` 如 `'SH'`、`'SZ'`。

### get_trading_calendar(market, start_time='', end_time='')
返回完整交易日列表（8 位日期字符串）。`end_time` 可填未来时间以获取未来交易日（需先下节假日数据）。

### get_holidays()
返回截止到当年的节假日列表（8 位日期字符串）。

### download_holiday_data()
下载节假日数据。

### get_trading_time(stock_code)
获取合约的交易时段（旧名 `get_trade_times`）。

### get_period_list()
返回可用周期列表。

---

## 板块分类

### download_sector_data()
下载板块分类信息（同步）。**取板块/成分股前必须先下载。**

### get_sector_list()
返回板块名列表 `[sector1, sector2, ...]`。

### get_stock_list_in_sector(sector_name, real_timetag=...)
返回板块成分股列表。常用板块名：`沪深A股`、`上证A股`、`深证A股`、`北证A股`、`中金所`。

### 自定义板块管理
- `create_sector_folder(parent_node, folder_name, overwrite)` — 创建目录节点
- `create_sector(parent_node, sector_name, overwrite)` — 创建板块
- `add_sector(sector_name, stock_list)` — 添加自定义板块
- `remove_stock_from_sector(sector_name, stock_list)` — 移除成分股
- `remove_sector(sector_name)` — 移除板块
- `reset_sector(sector_name, stock_list)` — 重置板块成分

---

## 指数权重

### download_index_weight()
下载指数成分权重信息（同步）。

### get_index_weight(index_code)
返回 `{成分股: 权重}`。需先 `download_index_weight()`。

---

## 可转债 / ETF / 新股

- `download_cb_data()` / `get_cb_info(stockcode)` — 可转债信息下载 / 获取
- `download_etf_info()` / `get_etf_info()` — ETF 申赎清单下载 / 获取（返回所有申赎数据 dict）
- `get_ipo_info(start_time, end_time)` — 新股申购信息 `list[dict]`，字段含 `securityCode/codeName/market/
  actIssueQty/onlineIssueQty/onlineSubCode/onlineSubMaxQty/publishPrice/isProfit/industryPe/afterPE`。

---

## 连接管理

- `connect(port=...)` — 连接到 MiniQMT（一般自动选择端口，多个 QMT 共存时自动优选）。
- `reconnect(...)` — 重连到指定 ip/端口。
- `get_quote_server_status()` — 查询当前行情连接站点状态。

VIP/token 模式（`xtquant.xtdatacenter`）与连接排障见 [troubleshooting.md](troubleshooting.md) 与 [recipes.md](recipes.md)。
