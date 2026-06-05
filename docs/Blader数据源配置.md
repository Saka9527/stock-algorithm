# Blader MySQL 数据源配置说明

对接库：`blader` @ `117.25.133.3:3306`  
核心表：`factor_data_wide`（因子宽表）、`stock_daily_qfq`（BaoStock 前复权日 K）  
历史长表 `factor_calc_data` 仅用于一次性迁移脚本 `scripts/migrate_factor_calc_to_wide.py`。

---

## 1. 配置文件

已生成本地配置（**勿提交 Git**）：

- `config/ifind_config.yaml` — 实际运行使用
- `config/ifind_config.blader.example.yaml` — 模板（无密码）

推荐使用环境变量存放密码：

```powershell
set BLADER_DB_USER=stock-dev
set BLADER_DB_PASSWORD=你的密码
```

配置文件中 `database.password` 可留空，优先读取环境变量。

---

## 2. 表与字段映射

### stock_daily_qfq（前复权日 K，BaoStock 同步）

| 逻辑字段 | 数据库列 | 说明 |
|----------|----------|------|
| date | trade_date | 交易日期 |
| code | stock_code | 如 `600000.SH` |
| open | open | 开盘价 |
| high | high | 最高价 |
| low | low | 最低价 |
| close | close | 收盘价（前复权） |
| volume | volume | 成交量 |

同步脚本：`python scripts/sync_baostock_daily.py`。数据区间以库内为准（约近 3 年）。

> 旧表 `stock_history_daily` 已弃用，请在 `ifind_config.yaml` 中配置 `tables.daily: stock_daily_qfq`。

### factor_data_wide（因子，宽表：一行 = 一股一日）

| 逻辑因子 | 物理列 | 说明 |
|----------|--------|------|
| PE_TTM | pe_ttm | 市盈率 TTM |
| PB / PB_MRQ | pb_mrq | 市净率 MRQ（原 `pb` 列，JSON 中 `PB_MRQ` 已迁移） |
| PS_TTM | ps_ttm | 市销率 TTM |
| PCF_NCF_TTM | pcf_ncf_ttm | 市现率 TTM |
| ROE_TTM | roe_ttm | ROE TTM |
| MOMENTUM_20 | mom_20d | 20 日动量 |
| MAIN_NET_INFLOW_RATIO | net_inflow_5d | 5 日主力净流入 |
| CURRENT_MV / MARKET_VALUE | float_cap / total_cap | 流通/总市值 |
| close | close | 收盘价（BaoStock 同步） |
| PE、PEG_LYR 等 | factor_ext_json | 扩展 JSON 字段 |

主键：`(data_date, stock_code)`。映射详见 `multi_factor/ifind/factor_wide.py`。

**逻辑因子映射**（`factor_mapping`）：

| 策略因子 | factor_code |
|----------|-------------|
| pe | PE_TTM |
| pb | PB_MRQ |
| roe | ROE_TTM |
| momentum | 由日 K 收盘价计算 20 日收益率 |

库内另有 `PE`、`ROE`、`PB_MRQ` 等，可在 `ifind_config.yaml` 的 `factor_mapping` 中调整。

**数据区间（当前库）**：约 `2023-06-02` ~ `2026-06-02`

### stock_trading_day（交易日历）

| 逻辑字段 | 数据库列 |
|----------|----------|
| date | trade_date |

---

## 3. 股票池说明

当前库**无沪深300成分股表**，配置为：

```yaml
universe:
  mode: factor_distinct
```

含义：在 `factor_data_wide` 中，取截至调仓日、且存在 PE/PB/ROE 因子记录的股票作为全市场池。  
若后续有成分股表，可改为 `mode: index_members` 并配置 `tables.index_members`。

---

## 4. 运行命令

```bash
# CLI
python run_backtest.py --source ifind --start 20251110 --end 20260530 --top-n 30 --local-backtest

# 跳过因子检验（更快）
python run_backtest.py --source ifind --start 20251110 --end 20260530 --skip-factor-analysis --local-backtest

# API 异步任务
curl -X POST http://127.0.0.1:8000/api/v1/jobs/backtest \
  -H "Content-Type: application/json" \
  -d "{\"source\":\"ifind\",\"start\":\"20251110\",\"end\":\"20260530\",\"skip_factor_analysis\":true,\"local_backtest\":true}"
```

**注意**：动量因子依赖日 K；回测区间建议落在日 K 有数据的范围内（目前约 2025-11 起）。

### 批量补全（calc + 同步因子对齐 + 绩效）

`scripts/run_factor_backfill_all.py` 按日 K 可用区间，分三阶段写 `factor_data_wide` / `factor_performance_*`：

**历史数据迁移（长表 -> 宽表，一次性）：**

```bash
python scripts/migrate_factor_calc_to_wide.py --batch-days 30
python scripts/migrate_factor_calc_to_wide.py --start 20230601 --end 20240601 --dry-run
```

| 阶段 | 对象 | 说明 |
|------|------|------|
| align | PE、PB、ROE_TTM 等 iFinD 同步因子 | 将库内已有历史按交易日历前向填充到目标区间（不占用 RQ 额度） |
| calc | MOMENTUM_20、ROE_YOY、MAIN_NET_INFLOW_RATIO | Python 计算落库（动量用日 K；主力净流入优先 Akshare） |
| performance | 区间内可与收益对齐的因子 | 复用 `factor_metrics.py` 预热 IC/分组收益 |

```bash
# 推荐：勿与后台其它 factor 任务并行，避免 factor_data_wide 锁等待
python scripts/run_factor_backfill_all.py --start 20251110 --end 20260602

# 分步
python scripts/run_factor_backfill_all.py --start 20251110 --end 20260602 --skip-calc --skip-performance
python scripts/run_factor_backfill_all.py --start 20251110 --end 20260602 --skip-align --calc-factors MOMENTUM_20 --skip-performance
python scripts/run_factor_backfill_all.py --start 20251110 --end 20260602 --skip-calc --skip-align

# 单因子对齐
python scripts/run_factor_job.py --factor PE --align --start 20251110 --end 20260602 --run-type full
```

---

## 5. 因子维度统计表（新增）

`factor_data_wide` 存储的是**股票维度**因子数值（宽表）。  
若要沉淀**因子维度**统计（IC、夏普、分组收益等），使用 Python 任务写入：

- `factor_performance_summary`：区间汇总指标（`ic_mean`、`ic_ir`、`win_rate`、`sharpe_*` 等）
- `factor_performance_series`：时序数据（`ic` 走势、`group` 收益、`quantile` 分位组）

任务脚本：

```bash
# 单因子
python scripts/run_factor_performance_job.py --factor-code MOMENTUM_20 --start 20251110 --end 20260530

# 全量因子
python scripts/run_factor_performance_job.py --start 20251110 --end 20260530
```

说明：

- 统计逻辑复用 `multi_factor/ifind/factor_metrics.py`（与因子市场接口一致）
- 首次运行自动建表，后续同参数区间自动 upsert
- 可供回测报告、API 与 BI 直接查询，减少实时重算开销

字段说明（与 `factor_base_info` 一样带 COMMENT）：

- `factor_performance_summary`（因子绩效汇总表）
  - 主键与维度：`id`、`factor_code`、`start_date`、`end_date`、`period`、`quantiles`、`top_pct`
  - IC 指标：`ic_mean`、`ic_std`、`ic_ir`、`win_rate`、`positive_count`、`negative_count`、`total_count`
  - 分组收益指标：`sharpe_top_group`、`sharpe_bottom_group`、`sharpe_long_short`
  - 数据范围与管理字段：`data_start`、`data_end`、`stock_count_avg`、`calc_version`、`updated_at`、`is_deleted`
- `factor_performance_series`（因子绩效时序表）
  - 主键与维度：`id`、`factor_code`、`start_date`、`end_date`、`period`
  - 序列标识：`series_type`（`ic/group/quantile`）、`series_date`
  - 序列值：`payload_json`（JSON 载荷，不同 `series_type` 结构不同）
  - 管理字段：`updated_at`、`is_deleted`

SQL 交付（建表/改表统一 SQL 形式）：

- 建表：`scripts/sql/factor_performance_create_tables.sql`
- 注释修正：`scripts/sql/factor_performance_alter_comments.sql`
- Python 任务中的 `ensure_tables()` 会按上述 SQL 文件自动执行（幂等）

---

## 6. JDBC URL 写法

若从 Spring 配置复制 JDBC，可直接写入 `database.url`：

```yaml
database:
  url: "jdbc:mysql://117.25.133.3:3306/blader?useSSL=false&serverTimezone=GMT%2B8&..."
  username: stock-dev
  password: ${BLADER_DB_PASSWORD}
```

程序会自动转换为 `mysql+pymysql://...`。

---

## 7. 常见问题

| 问题 | 处理 |
|------|------|
| 连接失败 | 检查 IP/端口/防火墙、账号权限 |
| 动量为空 | 缩短区间至日 K 覆盖范围 |
| 股票池过大 | 增加成分股表或 SQL 过滤；或减小 `factor_distinct` 范围 |
| PE/PB 无数据 | 检查 `factor_code` 映射是否与库内一致 |

查看库内因子类型：

```sql
SELECT MIN(data_date), MAX(data_date), COUNT(*) FROM factor_data_wide;
```
