# xttrader 交易模块参考（精简）

> 本项目当前只做数据采集、未交易。此为将来接入交易的备查参考。
> 交易需连接 MiniQMT **交易端**（券商端路径到 `userdata_mini`，投研端到 `userdata`），且账号有下单权限。

```python
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from xtquant import xtconstant
```

## 目录
- [生命周期](#生命周期)
- [下单 / 撤单](#下单--撤单)
- [查询](#查询)
- [回调](#回调)
- [xtconstant 常量](#xtconstant-常量)
- [数据结构](#数据结构)
- [坑](#坑)

## 生命周期

```python
path = r'D:\...\userdata_mini'            # 券商端→userdata_mini；投研端→userdata
session_id = int(time.time())              # 整数，多策略并行时不可重复
trader = XtQuantTrader(path, session_id)
trader.register_callback(MyCallback())     # 见“回调”
trader.start()                             # 启动交易线程
assert trader.connect() == 0               # 0=成功；一次性连接，断开不自动重连
acc = StockAccount('资金账号', 'STOCK')     # 类型见 account_type
trader.subscribe(acc)                      # 0=成功；订阅后才收到主推
# ... 下单/查询 ...
trader.run_forever()                       # 阻塞接收推送；或 trader.stop()
```

- `XtQuantTrader(path, session_id)` / `register_callback(cb)` / `start()` / `connect()→0成功` / `stop()` / `run_forever()`
- `subscribe(acc)` / `unsubscribe(acc)` → 0 成功 / -1 失败
- `set_relaxed_response_order_enabled(True)` — 允许在 `on_stock_order` 等回调里调同步查询而不卡线程（代价：时序变不确定）

## 下单 / 撤单

```python
order_stock(account, stock_code, order_type, order_volume, price_type, price, strategy_name, order_remark)
```
返回订单编号（>0 成功，-1 失败）。`order_volume`：股票单位“股”、债券“张”。

- `order_stock_async(...)` → 返回请求序号 `seq`（与 `on_order_stock_async_response` 对应）
- `cancel_order_stock(account, order_id)` → 0 成功 / -1 失败
- `cancel_order_stock_sysid(account, market, order_sysid)` — 按柜台合同编号撤
- `cancel_order_stock_async(...)` / `cancel_order_stock_sysid_async(...)` — 异步版

## 查询

| 函数 | 返回 |
|------|------|
| `query_stock_asset(acc)` | XtAsset（资金） |
| `query_stock_orders(acc, cancelable_only=False)` | [XtOrder] 当日委托 |
| `query_stock_trades(acc)` | [XtTrade] 当日成交 |
| `query_stock_positions(acc)` | [XtPosition] 持仓 |
| `query_stock_order(acc, order_id)` / `query_stock_position(acc, code)` | 单个 |
| `query_position_statistics(acc)` | [XtPositionStatistics] 期货持仓统计 |
| `query_account_infos()` / `query_account_status()` | 账号信息 / 状态 |
| `query_new_purchase_limit(acc)` / `query_ipo_data()` | 新股申购额度 / 当日新股 |
| `export_data(acc, path, data_type)` / `query_data(...)` | 通用导出 / 查询（csv） |

信用相关：`query_credit_detail` / `query_stk_compacts` / `query_credit_subjects` / `query_credit_slo_code` / `query_credit_assure`。
多数查询有异步版 `query_stock_orders_async` 等（推荐在回调内使用）。

## 回调

继承 `XtQuantTraderCallback`，按需实现：

- `on_disconnected()` — 断线（可在此重连）
- `on_account_status(status)` — XtAccountStatus
- `on_stock_order(order)` — XtOrder 委托变动（成交量、状态变化）
- `on_stock_trade(trade)` — XtTrade 成交
- `on_order_error(err)` — XtOrderError 下单失败
- `on_cancel_error(err)` — XtCancelError 撤单失败
- `on_order_stock_async_response(resp)` — XtOrderResponse 异步下单回报（含 `seq`）

## xtconstant 常量

**委托类型 order_type**
- 股票：`STOCK_BUY` / `STOCK_SELL`
- 信用：`CREDIT_BUY/SELL`、`CREDIT_FIN_BUY`(融资买)、`CREDIT_SLO_SELL`(融券卖)、`CREDIT_*_REPAY`(还款/还券) 等
- 期货六键：`FUTURE_OPEN_LONG/SHORT`、`FUTURE_CLOSE_LONG/SHORT_TODAY/HISTORY`；另有四键/两键/套利/展期枚举
- 股票期权：`STOCK_OPTION_BUY_OPEN/SELL_CLOSE/...`、行权 `*_EXERCISE`、备兑 `*_COVERED_*`
- ETF 申赎：`ETF_PURCHASE` / `ETF_REDEMPTION`

**报价类型 price_type**
- `FIX_PRICE` 限价（最常用）、`LATEST_PRICE` 最新价
- 沪/北股票市价：`MARKET_SH_CONVERT_5_CANCEL`(最优五档剩撤)、`MARKET_PEER_PRICE_FIRST`(对手方最优)、`MARKET_MINE_PRICE_FIRST`(本方最优)
- 深股票市价：`MARKET_SZ_CONVERT_5_CANCEL`、`MARKET_SZ_FULL_OR_CANCEL`、`MARKET_PEER/MINE_PRICE_FIRST`
- 期货市价：`MARKET_BEST`(郑)、`MARKET_CANCEL`/`MARKET_CANCEL_ALL`(大)、`MARKET_CANCEL_1/5`、`MARKET_CONVERT_1/5`(中金)
- ⚠ 市价仅实盘生效，模拟环境只支持限价

**委托状态 order_status**

| 值 | 枚举 | 含义 |
|----|------|------|
| 48 | ORDER_UNREPORTED | 未报 |
| 49 | ORDER_WAIT_REPORTING | 待报 |
| 50 | ORDER_REPORTED | 已报 |
| 51 | ORDER_REPORTED_CANCEL | 已报待撤 |
| 52 | ORDER_PARTSUCC_CANCEL | 部成待撤 |
| 53 | ORDER_PART_CANCEL | 部撤 |
| 54 | ORDER_CANCELED | 已撤 |
| 55 | ORDER_PART_SUCC | 部成 |
| 56 | ORDER_SUCCEEDED | 已成 |
| 57 | ORDER_JUNK | 废单 |
| 255 | ORDER_UNKNOWN | 未知 |

**账号类型 account_type**（`StockAccount(id, type)` 第二参用字符串 `'STOCK'/'CREDIT'/'FUTURE'`）
`SECURITY_ACCOUNT`股票 / `CREDIT_ACCOUNT`信用 / `FUTURE_ACCOUNT`期货 / `STOCK_OPTION_ACCOUNT`股票期权 / `HUGANGTONG_ACCOUNT`沪港通 / `SHENGANGTONG_ACCOUNT`深港通。

**多空 direction**：`DIRECTION_FLAG_LONG`48多 / `DIRECTION_FLAG_SHORT`49空（股票不适用）。
**交易操作 offset_flag**：48开仓 / 49平仓 / 51平今 / 52平昨 …（用于区分股票买卖、期货开平）。

## 数据结构

- **XtAsset**：`account_id, cash`(可用), `frozen_cash, market_value, total_asset`
- **XtOrder**：`stock_code, order_id, order_sysid`(柜台号), `order_type, order_volume, price_type, price, traded_volume, traded_price, order_status, status_msg, order_remark, direction, offset_flag`
- **XtTrade**：`stock_code, traded_id, traded_time, traded_price, traded_volume, traded_amount, order_id, order_remark`
- **XtPosition**：`stock_code, volume`(持仓), `can_use_volume`(可用), `open_price`(开仓), `avg_price`(成本), `market_value, frozen_volume, yesterday_volume, direction`
- **XtOrderError**：`order_id, error_id, error_msg, order_remark`

## 坑

- **path**：券商端→`userdata_mini`，投研端→`userdata`，不要指错。
- **session_id**：整数且并行策略间唯一；同 session 两次 connect 间隔需 >3s。
- **connect 一次性**：断开不自动重连，需在 `on_disconnected` 里自行重连。
- **order_remark ≤24 英文字符**（1 中文=3 字符），超出截断（极简端；大 QMT 无限制）。
- **市价单仅实盘**，模拟环境只支持限价 `FIX_PRICE`。
- 下单权限：`userdata_mini` 下无 `up_queue_xtquant` 文件 = 无下单权限，需联系券商开通。
