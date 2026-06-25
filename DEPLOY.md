# 部署文档

## 部署架构

```
┌─────────────────────────────────────┐
│  Windows 服务器（24/7）             │
│  ├─ QMT 客户端（保持登录）          │
│  ├─ Python 3.10 conda 环境          │
│  ├─ run_job.py --mode scheduler     │  ← 常驻进程（NSSM 守护）
│  └─ Z:\A股冷数据\TICK\（NAS挂载）   │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│  PostgreSQL 服务器                  │
│  database: ashares                   │
└─────────────────────────────────────┘
```

## 一、首次部署

### 1. Python 环境

```powershell
# 安装 Miniconda：https://docs.conda.io/en/latest/miniconda.html
conda create -n py10 python=3.10 -y
conda activate py10
```

### 2. 部署代码

```powershell
git clone <repo_url> D:\proj\data_collect
cd D:\proj\data_collect
pip install -r requirements.txt
pip install xtquant   # 或从 QMT 安装目录拷贝
```

### 3. 配置

```powershell
copy config.example.yaml config.yaml
notepad config.yaml
```

填入：
- `database.host/user/password`
- `dingtalk.webhook_token`
- `tick_storage.base_dir`（NAS 路径，如 `Z:\A股冷数据\TICK`）
- `wave3.email.{sender, app_key, recipients}`

### 4. 初始化数据库表

```powershell
psql -h <pg_host> -U <user> -d ashares -f sql/001_create_divid_factors.sql
psql -h <pg_host> -U <user> -d ashares -f sql/002_create_financial_tables.sql
```

其余表由任务首次运行时 `CREATE TABLE IF NOT EXISTS` 自动创建。

### 5. QMT 准备

- 安装 QMT 客户端（迅投极速交易终端）
- **保持登录**且接收行情订阅
- xtquant 通过本地 socket 与 QMT 通信

验证：
```powershell
python -c "from xtquant import xtdata; print(xtdata.get_trading_dates('SH', count=1))"
```

### 6. 跑通验证

```powershell
# 跑一次主流水线（用前一交易日数据）
python run_job.py --mode pipeline --date 20260425
```

应看到：
1. 终端打印 ASCII DAG
2. 8 个任务依次执行（含 wave3）
3. 钉钉收到汇总通知

## 二、scheduler 常驻部署（关键）

scheduler 是 BlockingScheduler，必须常驻运行。**推荐 NSSM**。

### 方案 A：NSSM（推荐）

[NSSM](https://nssm.cc/) 把任意进程注册为 Windows 服务，崩溃自动重启、开机自启。

```powershell
# 1. 下载 nssm.exe → C:\tools\nssm.exe
# 2. 创建服务（弹出 GUI 配置）
C:\tools\nssm.exe install DataCollectScheduler
```

GUI 填：

| 字段 | 值 |
|------|----|
| Path | `C:\Users\yangming\.conda\envs\py10\python.exe` |
| Startup directory | `D:\proj\data_collect` |
| Arguments | `run_job.py --mode scheduler` |
| I/O → Output (stdout) | `D:\proj\data_collect\logs\scheduler.out.log` |
| I/O → Error (stderr) | `D:\proj\data_collect\logs\scheduler.err.log` |
| Exit actions | 默认 Restart（崩溃自动重启） |

```powershell
# 3. 启动
C:\tools\nssm.exe start DataCollectScheduler

# 4. 状态检查
C:\tools\nssm.exe status DataCollectScheduler
sc query DataCollectScheduler

# 5. 改完代码/配置后重启
C:\tools\nssm.exe restart DataCollectScheduler
```

**优点**：开机自启、崩溃重启、日志分离、`services.msc` 可视化管理。

### 方案 B：Windows Task Scheduler（简单）

任务计划程序：
- 触发器：用户登录时（不能用"启动时"——QMT 需要桌面会话）
- 操作：`C:\Users\yangming\.conda\envs\py10\python.exe`
- 参数：`run_job.py --mode scheduler`
- 起始位置：`D:\proj\data_collect`

**缺点**：崩溃不自动重启、日志难抓。

### 方案 C：pm2（需 Node.js）

```powershell
npm install -g pm2 pm2-windows-startup
pm2-startup install
pm2 start "python run_job.py --mode scheduler" --name data-collect
pm2 save
```

支持 `pm2 logs`、`pm2 restart`、崩溃重启。

## 三、日常运维

### 日志查看

```powershell
type D:\proj\data_collect\logs\scheduler.out.log
type D:\proj\data_collect\logs\scheduler.err.log

# 实时跟踪
Get-Content D:\proj\data_collect\logs\scheduler.out.log -Wait -Tail 50
```

### 重启 / 升级代码

```powershell
cd D:\proj\data_collect
git pull
C:\tools\nssm.exe restart DataCollectScheduler
```

### 手动补数

```powershell
# 单天
python run_job.py --mode pipeline --task a_share_minute --date 20260420

# 区间
python run_job.py --mode backfill --task a_share_daily --start 20260101 --end 20260425

# 查漏补缺
python run_job.py --mode verify --task a_share_daily --start 20260401 --end 20260425
```

### 单独触发某 pipeline

```powershell
# 只注册 weekly_tick_verify（用于临时验证）
python run_job.py --mode scheduler --pipeline weekly_tick_verify

# 立即执行一次
python run_job.py --mode pipeline --pipeline weekly_tick_verify
```

## 四、风险与监控

| 风险点 | 表现 | 应对 |
|-------|------|------|
| QMT 掉线 | xtquant 调用挂死 | task `timeout` 触发硬杀子进程 + 钉钉告警 |
| PostgreSQL 不可达 | 任务异常 | 钉钉显示 `↳ 失败: psycopg2.OperationalError...` |
| NAS Z: 盘掉线 | tick 任务写入失败 | 重试一次 + 钉钉告警 |
| 服务器重启 | scheduler 自启 | **QMT 需手动登录**（无法 headless） |
| chinese_calendar 过期 | 跨年时 `is_market_day` 抛 NotImplementedError | `pip install -U chinese_calendar` |
| 磁盘满 | 服务停 | 监控 logs/、tick parquet 增量，定期归档 |

### 钉钉告警检查

scheduler 跑完每个 pipeline 都会发钉钉汇总。如果某次没收到：
1. `nssm status DataCollectScheduler` 看服务状态
2. `scheduler.err.log` 看异常
3. QMT 是否还在登录、是否被前一窗口卡住

## 五、Pipeline 调度时间表

| Pipeline | 触发时间 | 内容 |
|----------|---------|------|
| `daily` | 每天 15:30 | 8 个任务（daily, wave3, minute, divid, instrument, sector, index_weight, tick） |
| `weekly_tick_verify` | 周六 08:00 | tick 补漏过去 5 个交易日 |
| `weekly_kline_verify` | 周日 08:00 | daily/minute K 线补漏过去 10 个交易日 |
| `monthly_financial` | 每月最后一天 23:00 | 财务数据全量增量 |

## 六、最小化部署清单

```
[ ] Windows 主机 + QMT 已登录
[ ] conda py10 + pip install -r requirements.txt + xtquant
[ ] config.yaml（数据库、钉钉、tick 路径）
[ ] PostgreSQL 表初始化（前两个 SQL）
[ ] 跑通 python run_job.py --mode pipeline --date YYYYMMDD
[ ] 钉钉收到汇总通知
[ ] NSSM 包装 scheduler 服务
[ ] 服务状态 RUNNING、日志正常输出
```
