---
name: qmt-xtquant
description: |
  QMT/MiniQMT xtquant 原生 API 知识库，用于编写、调试或扩展调用 `xtquant.xtdata` 的 A 股数据采集任务。
  适用场景：下载/读取 K线、tick、财务、合约信息、板块分类、指数权重、除权因子数据；股票代码与
  UTC 毫秒时间戳转换；MiniQMT 连接失败排障（返回 -1、端口冲突、session、权限）。覆盖 xtdata 的
  download_*→get_* 核心范式、全部函数签名、各类数据字段 schema，以及关键坑（get_market_data 与
  get_market_data_ex 返回结构不同、单股订阅 ≤50、tick 时间戳为 UTC 毫秒需 +8h、静态信息无需频繁下载）。
  当处理 xtquant / xtdata / MiniQMT / QMT 相关的行情数据接口、字段含义或下载逻辑时使用；
  亦含 xttrader 交易模块（报单/撤单/查询/回调）与分品种数据字典（stock/index/future）参考。
---

# QMT xtquant 数据采集知识库

`xtquant` 是迅投 MiniQMT 衍生的 Python 量化库。`xtdata` 模块提供行情（历史/实时 K 线和分笔）、
财务、合约基础信息、板块/行业分类、指数权重等数据。本知识库以**数据采集**为主；交易模块 `xttrader` 另有精简参考备查。

官方文档：https://dict.thinktrader.net/nativeApi/start_now.html

## 运行模型（必读）

- **xtdata 不直连行情服务器**，而是连接本地运行的 **MiniQMT 客户端**，由 MiniQMT 处理数据请求再回传到
  Python 层。因此**运行任何脚本前，MiniQMT 必须已启动并登录**（登录时需勾选"极简模式"）。
- xtquant 大部分历史数据以**压缩格式存储在本地**。获取接口（`get_*`）只读本地缓存，本地数据不足时
  必须先用下载接口（`download_*`）补充，否则取不到。
- 仅 Windows 平台可用（本项目所有 xtquant 任务都标了 `platform: windows`）。

## 核心范式：先 download_ 再 get_

接口前缀语义：`download_` 下载到本地 / `get_` 读本地 / `subscribe_`、`unsubscribe_` 订阅实时。

```python
from xtquant import xtdata
# 1) 下载（同步阻塞，下载完才返回；下载接口本身不返回数据）
xtdata.download_history_data2(stock_list, period="1d",
                              start_time="20240101", end_time="20240131")
# 2) 读取（从本地缓存）
data = xtdata.get_market_data_ex([], stock_list, period="1d",
                                 start_time="20240101", end_time="20240131",
                                 count=-1, dividend_type="none", fill_data=False)
```

- 历史部分用 `download_history_data2` 补；实时部分用 `subscribe_quote` 订阅，之后 `get_*` 会自动拼接
  本地历史 + 服务器实时。
- 静态信息（板块、合约）也是先 `download_sector_data()` / `download_history_contracts()` 再 `get_*`。

## get_market_data vs get_market_data_ex（关键区别）

两者参数完全相同，但 **K 线返回结构不同**。本项目统一用 `_ex`（见
[a_share_daily.py](../../../data_collect/jobs/a_share_daily.py)）：

| 接口 | K 线返回结构 | 取单只 |
|------|------------|--------|
| `get_market_data` | `{field: DataFrame(index=股票, columns=时间)}` | 需跨多个 field 切片 |
| `get_market_data_ex` | `{股票代码: DataFrame(index=时间, columns=field)}` | `data[code]` 直接拿到该股 DataFrame |

- `get_market_data_ex` 额外支持**日线以上周期**（`1w/1mon/1q/1hy/1y`）和 ETF 申赎清单。
- `tick` 周期下两者都返回 `{股票: np.ndarray}`（按 `time` 升序）。

## 关键约定

- **代码格式**：`code.market`，如 `000001.SZ`、`600000.SH`、`000300.SH`、`430047.BJ`。
- **周期 period**：`tick 1m 5m 15m 30m 1h 1d 1w 1mon 1q 1hy 1y`。
- **复权 dividend_type**：`none / front / back / front_ratio / back_ratio`（仅对 K 线有效，对 tick 无效）。
- **时间戳**：返回的 `time` 字段是 **UTC 毫秒**。转北京时间用
  `pd.to_datetime(time, unit="ms") + pd.Timedelta(hours=8)`——不要用官方示例里的 `time.localtime`
  （依赖机器时区，跨平台不可靠）。
- **时间范围**：`[start_time, end_time]` 是**闭区间**。`count`：`-1` 全部、`0` 不返回、`>0` 以 `end_time`
  为基准向前取 N 条。`start/end 留空 + count=-1` = 全量（范围过大会很慢，按需裁剪）。
- **请求限制**：单股订阅建议 ≤50 只，更多时改用全推 `subscribe_whole_quote`；板块等静态信息按周/日
  定期更新即可，无需频繁下载。

## 常用函数速查

| 用途 | 函数 |
|------|------|
| A 股代码列表 | `get_stock_list_in_sector("沪深A股")`（需先 `download_sector_data()`） |
| 批量下载 K 线 | `download_history_data2(stock_list, period, start, end, callback)` |
| 读 K 线 | `get_market_data_ex(field_list, stock_list, period, start, end, count, dividend_type, fill_data)` |
| 全推快照 | `get_full_tick(code_list)` |
| 除权因子 | `get_divid_factors(code, start, end)`（先下载历史 K 线） |
| 合约信息 | `get_instrument_detail(code, iscomplete=True)` |
| 财务数据 | `download_financial_data2(...)` → `get_financial_data(...)` |
| 板块成分/列表 | `get_stock_list_in_sector(name)` / `get_sector_list()` |
| 指数权重 | `download_index_weight()` → `get_index_weight(index_code)` |
| 交易日 | `get_trading_dates(market, ...)` / `get_trading_calendar(market, ...)` |

## 详细参考（按需加载）

- **函数签名 / 参数 / 返回值** → [references/xtdata-api.md](references/xtdata-api.md)
- **数据字段 schema**（K 线/tick/除权/合约/8 张财务表/数据字典） → [references/data-fields.md](references/data-fields.md)
- **可复制代码范式**（下载+读取、复权算法、板块、财务、tick、时间/代码转换、VIP 连接） → [references/recipes.md](references/recipes.md)
- **连接与排障**（返回 -1、端口冲突、session、C 盘权限、版本变更） → [references/troubleshooting.md](references/troubleshooting.md)
- **分品种数据字典**（stock/index/future 字段释义、合约信息哨兵值与坑） → [references/data-dictionary.md](references/data-dictionary.md)
- **xttrader 交易模块（精简）**（报单/撤单/查询/回调/常量，为将来交易预留） → [references/xttrader-api.md](references/xttrader-api.md)
