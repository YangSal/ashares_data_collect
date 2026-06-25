# 连接与排障

MiniQMT/xtquant 常见报错与版本变更。多数连接问题源于 MiniQMT 未正确启动或路径/权限不对。

## 目录
- [导入报错](#导入报错)
- [连接返回 -1](#连接返回--1)
- [xtdatacenter 端口占用](#xtdatacenter-端口占用)
- [session 与 down_queue 文件](#session-与-down_queue-文件)
- [其他](#其他)
- [版本变更要点](#版本变更要点)

---

## 导入报错

**`No module named 'xtquant.IPythonAPiClient'`**：Python 版本不支持。xtquant 需 **64 位 Python 3.6–3.12**
（不同版本导入时自动切换）。换受支持的版本重试。本项目用 conda `py10`（Python 3.10）。

---

## 连接返回 -1

`xtdata.connect()` 或交易 `connect()` 返回 -1，按序排查：

1. **MiniQMT 未启动**：运行脚本前客户端必须已启动并登录，登录时勾选**极简模式**。
2. **路径不对**（交易/投研端连接时）：
   - miniqmt（券商端）：指向安装目录下 `\userdata_mini`
   - 投研端：指向安装目录下 `\userdata`
3. **C 盘权限**：客户端装在 C 盘时，每次都要用**管理员权限**运行脚本才能连上。**建议不要装在 C 盘**。
   验证写权限：
   ```python
   with open(r"d:\qmt\userdata_mini\example.txt", "w") as f:
       f.write("123")     # 抛 PermissionError 即权限问题
   ```
4. **session 冲突**：换一个 `session`（任意整数）。同一 session 的两次 connect **必须间隔 >3 秒**。
5. **无下单权限**（交易）：MiniQMT 开启后若 `userdata_mini` 下没有 `up_queue_xtquant` 文件，说明没有对应
   下单权限，需联系券商开通。

---

## xtdatacenter 端口占用

`xtdatacenter.init` 提示监听 **58609** 端口失败：通常是启动了两个 xtdc 服务。

方法 1（推荐）——手动指定端口：
```python
from xtquant import xtdatacenter as xtdc
xtdc.set_token("你的token")
xtdc.init(False)            # 不自动监听默认端口
port = 58601
xtdc.listen(port=port)      # 或 listen(port=(58620, 58630)) 自动选可用端口
```
方法 2——关闭所有 py 程序或重启电脑，再执行 `xtdc.init`。

---

## session 与 down_queue 文件

- `userdata_mini` 下生成大量 `down_queue` 文件：是 xttrade 指定新 session 产生的，可删除。通过**限定
  session id 范围**避免大量产生。
- session id 为整数，同时运行的多个策略不能重复。常用 `int(time.time())` 生成。

---

## 其他

- **投资备注被截断**：极简客户端 `order_remark` 最大 **24 个英文字符**（1 个中文占 3 个），超出丢弃；
  大 QMT 无此限制。
- **取不到历史数据**：先确认已 `download_*` 到本地；获取接口只读本地缓存。
- **数据范围过大很慢**：`get_*` 的 `[start_time, end_time, count]` 按需裁剪，勿无脑全量。

---

## 版本变更要点

影响数据采集的关键演进（按需对照客户端/库版本）：

- `download_history_data2` / `download_financial_data2`：批量下载接口（2021-07）。
- `get_market_data_ex` 支持**日线以上周期** `1w/1mon/1q/1hy/1y`（2024-01）、支持 ETF 申赎清单（2023-10）。
- `get_full_kline`：获取最新交易日 K 线全推（2024-05，需开启 K 线全推）。
- `get_instrument_detail` 增 `ExchangeCode`/`UniCode`（2023-08）、支持获取全部字段（2024-01）。
- `get_stock_list_in_sector` 增 `real_timetag` 参数（2024-05）。
- `get_trade_times` 更名为 `get_trading_time`（2024-01）。
- 获取板块成份股增加北交所板块（2023-12）。
- `download_history_data` 增量下载参数 `incrementally`（2023-11）。
- `volumn` 拼写早期已修正为 `volume`（影响 tick/l2quote 成交量、合约总/流通股本）。
- 部分特色数据（大单统计 `get_transactioncount`、板块详情 `get_sector_info`、期权 `get_option_detail_data`）
  需投研/VIP 权限。

完整 changelog 与 xtquant 库下载见 https://dict.thinktrader.net/nativeApi/download_xtquant.html
