# 分品种数据字典（stock / index / future 等）

各证券品种用 xtdata 取数的差异、代码格式与字段释义。原始字段清单见 [data-fields.md](data-fields.md)，
接口签名见 [xtdata-api.md](xtdata-api.md)。来源：https://dict.thinktrader.net/dictionary/

## 目录
- [get_instrument_detail 通用坑（哨兵值/特殊日期/类型）](#get_instrument_detail-通用坑)
- [股票 stock](#股票-stock)
- [指数 index](#指数-index)
- [期货 future](#期货-future)
- [其他品种](#其他品种)

## get_instrument_detail 通用坑

`get_instrument_detail(code, iscomplete)` 返回的合约信息有几个**必须知道的哨兵/特殊值**，否则会把无效值当真实数据：

- **无效数值哨兵**：`float` 字段无效时返回 `1.7976931348623157e+308`（DBL_MAX）；`int` 字段无效时返回
  `2147483647`（INT_MAX）。例如股票的 `LongMarginRatio`、`MainContract`、`LastVolume`、`IsTrading` 常是这类占位值，需判无效。
- **OpenDate 特殊值**（上市日期）：`19700101`=新股、`19700102`=老股东增发、`19700103`=新债、
  `19700104`=可转债、`19700105`=配股、`19700106`=配号。
- **ExpireDate**：`0` 或 `99999999` = 暂无退市日/到期日。
- **FloatVolumn 拼写**：部分低版本客户端字段名为 `FloatVolumn`/`TotalVolumn`（少个 e），解析时两种都要兼容。
- **ProductType**（合约类型，默认 `-1`）：
  - 沪深股票期权：`0`=认购、`1`=认沽
  - 国内期货：`1`期货 `2`期权 `3`组合套利 `4`即期 `5`期转现 `6`期权(IF) `7`结算价交易(tas)
  - 外盘：`201`股票 `203`ETF `204`ETN `1`股指期货 `2`能源 `3`农业 `4`金属 …

## 股票 stock

- 代码：`000001.SZ`(深) / `600000.SH`(沪) / `430047.BJ`(北)。
- 取全部 A 股：`get_stock_list_in_sector("沪深A股")`（先 `download_sector_data()`）。
- 合约信息每交易日 9:00 更新（`get_instrument_detail`）。
- **ST 历史**：`download_his_st_data()` →（异步，等待数秒）→ `get_his_st_data(code)`，返回
  `{'ST':[[start,end],...], '*ST':[...], 'PT':[...]}`，历史未 ST 返回 `{}`。需 VIP 权限。
- K 线 / tick 字段见 [data-fields.md](data-fields.md)；tick 另有 `stime`（字符串形式时间戳）。

## 指数 index

- 代码：沪市 `000300.SH`(沪深300)、`000001.SH`(上证指数) 等。
- 取指数列表：`get_sector_list()` 找板块名（如 `沪深指数`/`上证指数`）→ `get_stock_list_in_sector(板块名)`。
- **成分权重**：`download_index_weight()` → `get_index_weight("000300.SH")` 返回 `{成分: 权重}`。
- **迅投自有指数**（需相应权限）：
  - 板块加权指数后缀 `.BKZS`，如 `260992.BKZS`（SW1农林牧渔加权），板块名 `迅投一级行业板块加权指数`。
  - 商品市场指数如 `290000.BKZS`。
- 迅投指数计算规则：成分等权；普通股上市超 20 个交易日（债 5 日）后纳入；涨停打开超 3 日后纳入；
  复牌股涨跌幅 >25% 不纳入。

## 期货 future

- **市场代码映射**（标准 → 迅投）：

  | 交易所 | 标准 | 迅投 |
  |--------|------|------|
  | 上期所 | SHFE | SF |
  | 大商所 | DCE | DF |
  | 郑商所 | CZCE | ZF |
  | 中金所 | CFFEX | IF |
  | 能源中心 | INE | INE |
  | 广期所 | GFEX | GF |

- 代码：具体合约 `rb2401.SF`（螺纹钢2401）、连续合约 `rb00.SF`（品种+00）。
- 取期货代码：`get_stock_list_in_sector("上期所期货")` 或 `"SF"`；主力 `get_stock_list_in_sector("主力板块")`。
- 交易日历：`get_trading_dates("SF", ...)`（按市场代码）。
- **当前主力合约**：`get_main_contract("rb00.SF")` → `"rb2401.SF"`。
- **历史主力合约**：`download_history_data(symbol, period="historymaincontract")` →
  `get_market_data_ex([], [symbol], period="historymaincontract")`（symbol 用主连如 `IF00.IF`；需 VIP）。
- 期货 `get_instrument_detail` 关注：`ProductID`(品种)、`VolumeMultiple`(合约乘数)、`PriceTick`、
  `LongMarginRatio/ShortMarginRatio`(保证金率)、`ExpireDate`(到期)、`InstrumentStatus`(停牌天数)。

## 其他品种

行业/概念、期权、债券、场内基金(ETF) 的字段释义见官方数据字典对应页（结构同上，按需取用）：

- 行业/概念：https://dict.thinktrader.net/dictionary/industry.html
- 期权：https://dict.thinktrader.net/dictionary/option.html
- 债券：https://dict.thinktrader.net/dictionary/bond.html
- 场内基金 / ETF：https://dict.thinktrader.net/dictionary/floorfunds.html
