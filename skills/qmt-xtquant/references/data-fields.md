# 数据字段 schema

xtdata 各类数据的字段列表与枚举值。grep 字段名（如 `tot_assets`、`s_fa_eps_basic`）可快速定位含义。

## 目录
- [行情字段](#行情字段)：tick、K 线、除权
- [level2 字段](#level2-字段)
- [数据字典枚举](#数据字典枚举)：证券状态、委托类型/方向、成交标志、现金替代
- [财务表字段](#财务表字段)：Balance / Income / CashFlow / Pershareindex / Capital / 股东
- [合约信息字段](#合约信息字段)

---

## 行情字段

### tick - 分笔数据
```
time                 时间戳（UTC 毫秒，+8h 转北京时间）
lastPrice            最新价
open high low         开/高/低
lastClose            前收盘价
amount               成交总额
volume               成交总量
pvolume              原始成交总量
stockStatus          证券状态（见数据字典）
openInt              持仓量
lastSettlementPrice  前结算
askPrice / bidPrice   委卖价 / 委买价（多档为数组）
askVol / bidVol       委卖量 / 委买量
transactionNum       成交笔数
```

### 1m / 5m / 1d 等 - K 线数据
```
time                 时间戳（UTC 毫秒）
open high low close   开/高/低/收
volume               成交量
amount               成交额
settelementPrice     今结算
openInterest         持仓量
preClose             前收价
suspendFlag          停牌标记 0 正常 / 1 停牌 / -1 当日起复牌
```

### 除权数据（get_divid_factors 返回）
```
interest             每股股利（税前，元）
stockBonus           每股红股（股）
stockGift            每股转增股本（股）
allotNum             每股配股数（股）
allotPrice           配股价格（元）
gugai                是否股改（股改有特殊复权算法）
dr                   除权系数
```

---

## level2 字段

本项目用不到，列出备查。

### l2quote - level2 实时行情快照
```
time lastPrice open high low amount volume pvolume openInt stockStatus
transactionNum lastClose lastSettlementPrice settlementPrice pe
askPrice bidPrice askVol bidVol（多档）
```
### l2order - 逐笔委托
```
time price volume entrustNo entrustType entrustDirection
```
### l2transaction - 逐笔成交
```
time price volume amount tradeIndex buyNo sellNo tradeType tradeFlag
```
### l2quoteaux - 实时行情补充（总买总卖）
```
time avgBidPrice totalBidQuantity avgOffPrice totalOffQuantity
withdrawBidQuantity withdrawBidAmount withdrawOffQuantity withdrawOffAmount
```
### l2orderqueue - 一档委托队列
```
time bidLevelPrice bidLevelVolume offerLevelPrice offerLevelVolume
bidLevelNumber offLevelNumber
```

---

## 数据字典枚举

### 证券状态 stockStatus
```
0,10 未知   11 开盘前S   12 集合竞价C   13 连续交易T   14 休市B   15 闭市E
16 波动性中断V   17 临时停牌P   18 收盘集合竞价U   19 盘中集合竞价M
20 暂停交易至闭市N   21 获取字段异常   22 盘后固定价格行情   23 盘后固定价格行情完毕
```
### 委托类型 entrustType / 成交类型 tradeType
```
0 未知   1 正常交易   2 即时成交剩余撤销   3 ETF 基金申报
4 最优五档即时成交剩余撤销   5 全额成交或撤销   6 本方最优价格   7 对手方最优价格
```
### 委托方向 entrustDirection（逐笔委托，上交所撤单在此区分）
```
1 买入   2 卖出   3 撤买（上交所）   4 撤卖（上交所）
```
### 成交标志 tradeFlag（逐笔成交，深交所撤单在此）
```
0 未知   1 外盘   2 内盘   3 撤单（深交所）
```
### 现金替代标志（ETF 申赎清单成份股）
```
0 禁止现金替代   1 允许现金替代   2 必须现金替代
3 非沪市退补   4 非沪市必须   5 非沪深退补   6 非沪深必须
7 港市退补（跨沪深港ETF）   8 港市必须（跨沪深港ETF）
```

---

## 财务表字段

8 张表，公共字段 `m_anntime`(披露日期) / `m_timetag`(截止日期)。下列为常用字段（完整字段较多，按需 grep）。

### Balance - 资产负债表（常用）
```
cash_equivalents          货币资金
tradable_fin_assets       交易性金融资产
bill_receivable           应收票据
account_receivable        应收账款
advance_payment           预付款项
inventories               存货
total_current_assets      流动资产合计
fix_assets                固定资产
intang_assets             无形资产
goodwill                  商誉
total_non_current_assets  非流动资产合计
tot_assets                资产总计
shortterm_loan            短期借款
accounts_payable          应付账款
advance_peceipts          预收账款
taxes_surcharges_payable  应交税费
total_current_liability   流动负债合计
long_term_loans           长期借款
bonds_payable             应付债券
non_current_liabilities   非流动负债合计
tot_liab                  负债合计
cap_stk                   实收资本(或股本)
cap_rsrv                  资本公积
surplus_rsrv              盈余公积
undistributed_profit      未分配利润
tot_shrhldr_eqy_excl_min_int  归属于母公司股东权益合计
minority_int              少数股东权益
total_equity              所有者权益合计
tot_liab_shrhldr_eqy      负债和股东权益总计
```

### Income - 利润表（常用）
```
revenue                          营业总收入
revenue_inc                      营业收入
total_operating_cost             营业总成本
total_expense                    营业成本
less_taxes_surcharges_ops        营业税金及附加
sale_expense                     销售费用
less_gerl_admin_exp              管理费用
financial_expense                财务费用
research_expenses                研发费用
less_impair_loss_assets          资产减值损失
plus_net_invest_inc              投资收益
oper_profit                      营业利润
plus_non_oper_rev                营业外收入
less_non_oper_exp                营业外支出
tot_profit                       利润总额
inc_tax                          所得税费用
net_profit_incl_min_int_inc      净利润
net_profit_excl_min_int_inc      归属于母公司所有者的净利润
minority_int_inc                 少数股东损益
s_fa_eps_basic                   基本每股收益
s_fa_eps_diluted                 稀释每股收益
```

### CashFlow - 现金流量表（常用）
```
goods_sale_and_service_render_cash  销售商品、提供劳务收到的现金
stot_cash_inflows_oper_act          经营活动现金流入小计
stot_cash_outflows_oper_act         经营活动现金流出小计
net_cash_flows_oper_act             经营活动产生的现金流量净额
stot_cash_inflows_inv_act           投资活动现金流入小计
net_cash_flows_inv_act              投资活动产生的现金流量净额
cash_pay_acq_const_fiolta           购建固定资产、无形资产和其他长期投资支付的现金
stot_cash_inflows_fnc_act           筹资活动现金流入小计
net_cash_flows_fnc_act              筹资活动产生的现金流量净额
net_incr_cash_cash_equ              现金及现金等价物净增加额
cash_cash_equ_end_period            期末现金及现金等价物余额
net_profit                          净利润
depr_fa_coga_dpba                   固定资产折旧、油气资产折耗、生产性物资折旧
amort_intang_assets                 无形资产摊销
```

### Pershareindex - 主要指标 / 每股指标
```
s_fa_ocfps                每股经营活动现金流量
s_fa_bps                  每股净资产
s_fa_eps_basic            基本每股收益
s_fa_eps_diluted          稀释每股收益
s_fa_undistributedps      每股未分配利润
s_fa_surpluscapitalps     每股资本公积金
adjusted_earnings_per_share  扣非每股收益
du_return_on_equity       净资产收益率
sales_gross_profit        销售毛利率
inc_revenue_rate          主营收入同比增长
du_profit_rate            净利润同比增长
inc_net_profit_rate       归母净利润同比增长
adjusted_net_profit_rate  扣非净利润同比增长
equity_roe                加权净资产收益率
net_roe                   摊薄净资产收益率
gross_profit              毛利率
net_profit                净利率
actual_tax_rate           实际税率
gear_ratio                资产负债比率
inventory_turnover        存货周转率
```

### Capital - 股本表
```
total_capital                  总股本
circulating_capital            已上市流通A股
restrict_circulating_capital   限售流通股份
m_timetag                      报告截止日
m_anntime                      公告日
```

### Top10holder / Top10flowholder - 十大股东 / 十大流通股东
```
declareDate 公告日期   endDate 截止日期   name 股东名称   type 股东类型
quantity 持股数量   reason 变动原因   ratio 持股比例   nature 股份性质   rank 持股排名
```

### Holdernum - 股东数
```
declareDate 公告日期   endDate 截止日期   shareholder 股东总数
shareholderA/B/H A/B/H 股东户数   shareholderFloat 已流通股东户数   shareholderOther 未流通股东户数
```

---

## 合约信息字段

`get_instrument_detail(code, iscomplete=False)` 常用字段：
```
ExchangeID 合约市场代码    InstrumentID 合约代码    InstrumentName 合约名称
ExchangeCode 交易所代码    UniCode 统一规则代码
OpenDate 股票 IPO 日期(str)    CreateDate 期货上市日期(str)    ExpireDate 退市/到期日(int)
PreClose 前收盘价    SettlementPrice 前结算价
UpStopPrice 当日涨停价    DownStopPrice 当日跌停价
FloatVolume 流通股本    TotalVolume 总股本
PriceTick 最小价格变动单位    VolumeMultiple 合约乘数(非期货默认1)
InstrumentStatus 停牌状态    IsTrading 是否可交易    IsRecent 是否近月合约
ProductID/ProductName 期货品种ID/名称    LongMarginRatio/ShortMarginRatio 多/空头保证金率
```

`iscomplete=True` 额外字段（节选，期货/期权相关）：
```
Abbreviation 名称拼音简写    UnderlyingCode 标的合约    DayCountFromIPO 自IPO交易日数
ChargeType 手续费方式(0未知/1元每手/2费率)    ChargeOpen/ChargeClose 开/平仓手续费(率)
ChargeTodayOpen/ChargeTodayClose 开今/平今手续费(率)    OptionType 期权类型(-1非期权/0认购/1认沽)
OptExercisePrice 期权行权价/可转债转股价    OptUndlCode/OptUndlMarket 期权标的代码/市场
HSGTFlag 沪深港通标的标识    BondParValue 债券面值    Ccy 币种
DeliveryYear/DeliveryMonth 交割年/月    OpenInterestMultiple 交割月持仓倍数
```
