# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

数据收集平台。当前功能：A股分钟/日线K线、复权因子、财务数据、合约详情、板块分类、指数权重、Tick冷数据。
wave3 选股策略（3浪3技术指标筛选）。未来扩展爬虫、非结构化数据收集等。
项目跨 Windows/Linux 双平台运行（xtquant 任务仅限 Windows）。

## Python Environment

```bash
C:\Users\yangming\.conda\envs\py10\python.exe  # conda activate py10
```

## Commands

```bash
# 按 DAG 执行每日流水线（分钟线→复权因子）
python run_job.py --mode pipeline [--date YYYYMMDD] [--task NAME]

# 补历史数据
python run_job.py --mode backfill --task divid_factors --start 20200101 --end 20260407
python run_job.py --mode backfill --task a_share_financial --start 20000101 --end 20260409
python run_job.py --mode backfill --task a_share_tick --start 20260101 --end 20260408

# 单次执行分钟线（兼容旧模式）
python run_job.py --mode once [--date YYYYMMDD] [--limit-stocks N]

# 仅导出CSV
python run_job.py --mode export-only [--date YYYYMMDD]

# 启动定时任务（每天执行 daily pipeline）
python run_job.py --mode scheduler [--hour H] [--minute M]

# 运行测试
pytest tests/ -v
```

## Architecture

```
data_collect/              # 主包
├── config.py              # YAML配置加载
├── pipeline.py            # DAG任务编排（拓扑排序、平台过滤、失败跳过）
├── utils/                 # 公共工具
│   ├── db.py              # PostgreSQL: 连接、schema查询、批量写入
│   ├── notify.py          # 钉钉通知
│   ├── xtquant_utils.py   # xtquant公共工具（require_xtdata, get_a_share_codes）
│   ├── date_utils.py      # 交易日工具
│   ├── df_utils.py        # DataFrame对齐（纯函数）
│   ├── export.py          # CSV导出
│   ├── indicators.py      # 通达信指标函数
│   ├── progress.py        # 双进度条
│   └── retry.py           # 重试策略
└── jobs/                  # 每个任务实现 run(run_date, **kwargs) -> str
    ├── a_share_minute.py  # A股分钟线
    ├── a_share_daily.py   # A股日线K线
    ├── a_share_financial.py # A股财务数据（8张表，自动建表）
    ├── a_share_index_weight.py # 指数成分权重（快照+变更记录）
    ├── a_share_instrument.py # 合约详情（快照+变更记录）
    ├── a_share_sector.py  # 板块/行业分类（快照+变更记录）
    ├── a_share_tick.py    # A股Tick冷数据（Parquet+zstd，不入库）
    ├── divid_factors.py   # 复权因子（支持 run_backfill）
    └── wave3.py           # 3浪3选股

run_job.py                 # CLI入口
config.yaml                # 实际配置（gitignore）
sql/                       # 建表SQL
```

### Pipeline 编排

任务依赖在 `config.yaml` 的 `pipelines` 段定义，框架自动拓扑排序执行。
每个 job 模块必须实现 `run(run_date: str, **kwargs) -> str` 标准接口。
支持 `platform: windows/linux` 标记，当前平台不匹配的任务自动跳过。

### 新增任务步骤

1. 在 `data_collect/jobs/` 下创建模块，实现 `run()` 函数（可选 `run_backfill()`）
2. 在 `config.yaml` 的 `pipelines.daily.tasks` 中添加任务定义
3. 如需建表，在 `sql/` 下添加 SQL 文件

## Configuration

`config.yaml`（YAML）包含：数据库连接、钉钉webhook、调度参数、导出路径、pipeline定义、Tick存储路径。
环境变量 `DATA_COLLECT_CONFIG` 可指定自定义配置路径。

## Key Details

- 任务在子进程中执行（`ProcessPoolExecutor(max_workers=1)`），确保资源释放
- DB字段对齐是动态的：运行时读取 `information_schema.columns`
- 股票代码格式转换：`000001.SZ` → `sz000001`(8字符) 或 `000001`(6字符)
- xtdata时间戳为UTC毫秒，+8小时转北京时间
- 钉钉消息必须包含"白白胖胖说"
- Pipeline 失败任务会跳过其下游任务，汇总结果发钉钉通知
- Tick数据按 年/月/日/股票代码.parquet 存储，Parquet+zstd 压缩，幂等写入（已存在则跳过）
