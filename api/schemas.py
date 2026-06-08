# -*- coding: utf-8 -*-
"""API 请求/响应模型（Swagger 文档）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 统一响应
# ---------------------------------------------------------------------------


class ApiResponse(BaseModel):
    """统一 JSON 响应包装。"""

    code: int = Field(0, description="业务码，0 表示成功")
    message: str = Field("ok", description="提示信息")
    data: Any = Field(None, description="业务数据")

    model_config = {
        "json_schema_extra": {
            "examples": [{"code": 0, "message": "ok", "data": {"status": "ok"}}]
        }
    }


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------


class HealthData(BaseModel):
    status: str = Field("ok", description="服务状态")
    rqdatac: dict = Field(default_factory=dict, description="米筐 RQDatac 连接信息")
    factor_scores_ready: bool = Field(description="output/factor_scores.pkl 是否存在")
    backtest_report_ready: bool = Field(description="传统回测 summary 是否存在")
    engine_report_ready: bool = Field(description="引擎回测 summary.json 是否存在")


# ---------------------------------------------------------------------------
# 因子得分 / TopN
# ---------------------------------------------------------------------------


class TopStockItem(BaseModel):
    order_book_id: str = Field(description="股票代码，RQ 格式如 600000.XSHG")
    score: float = Field(description="合成因子得分，越大越优")


class TopStocksData(BaseModel):
    date: str
    top_n: int
    stocks: list[TopStockItem]


# ---------------------------------------------------------------------------
# 异步任务（传统 pipeline）
# ---------------------------------------------------------------------------


class BacktestJobCreate(BaseModel):
    """提交回测任务（RQDatac / 旧版 iFinD pipeline / demo）。"""

    source: Literal["rqdatac", "ifind", "demo"] = Field(
        "rqdatac", description="数据源：rqdatac | ifind | demo"
    )
    start: str = Field(..., description="开始日期 YYYYMMDD", examples=["20230101"])
    end: str = Field(..., description="结束日期 YYYYMMDD", examples=["20231229"])
    index: str = Field("000300.XSHG", description="[rqdatac] 股票池指数代码")
    top_n: int = Field(30, ge=1, le=200, description="持仓数量")
    skip_factor_analysis: bool = Field(False, description="跳过 RQFactor 因子检验")
    skip_backtest: bool = Field(False, description="仅因子研究，不回测")
    scores_only: bool = Field(False, description="仅预计算得分")
    ifind_config: str | None = Field(None, description="iFinD/Blader 配置文件路径")
    local_backtest: bool = Field(True, description="[ifind] 使用本地撮合而非 RQAlpha")
    factor_weights: dict[str, float] | None = Field(
        None, description="因子权重，如 {\"pe\":0.25,\"pb\":0.25}"
    )
    use_engine: bool = Field(
        False,
        description="[ifind] 为 true 时走新版多因子引擎（T+1/完整绩效），忽略 local_backtest",
    )


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: str
    finished_at: str | None = None
    request: dict
    result: dict | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# 因子市场
# ---------------------------------------------------------------------------


class FactorMarketListItem(BaseModel):
    factor_code: str
    factor_name: str | None = None
    factor_type: str | None = None
    factor_desc: str | None = None
    sort_type: str | None = None
    ic_mean: float | None = None
    ic_win_rate: float | None = None
    sharpe_ratio: float | None = None
    data_start: str | None = None
    data_end: str | None = None
    error: str | None = None


class FactorPerformanceWarmupCreate(BaseModel):
    """离线预热因子绩效统计（写入 factor_performance_*）。"""

    start: str = Field(..., description="开始日期 YYYYMMDD")
    end: str = Field(..., description="结束日期 YYYYMMDD")
    factor_code: str | None = Field(None, description="单因子代码，如 MOMENTUM_20")
    factor_codes: list[str] | None = Field(
        None, description="批量因子代码列表，优先级高于 factor_code"
    )
    period: int = Field(1, ge=1, le=20, description="IC/收益前瞻期（交易日）")
    quantiles: int = Field(5, ge=2, le=10, description="分层组数")
    top_pct: float = Field(0.2, gt=0, lt=0.5, description="Top/Bottom 分组比例")
    ifind_config: str | None = Field(None, description="ifind 配置路径")

# ---------------------------------------------------------------------------
# 多因子引擎
# ---------------------------------------------------------------------------


class FactorSpecIn(BaseModel):
    code: str = Field(..., description="因子代码，如 PE_TTM、PB、MOMENTUM_20")
    weight: float = Field(1.0, ge=0, description="自定义权重（weight_mode=custom 时生效）")
    ascending: bool | None = Field(
        None, description="True=因子值越大越好；None 时从 factor_base_info 读取"
    )


class EngineBacktestCreate(BaseModel):
    """新版多因子引擎回测参数（Blader / iFinD 数据）。"""

    start: str = Field(..., description="开始日期 YYYYMMDD 或 YYYY-MM-DD", examples=["20251110"])
    end: str = Field(..., description="结束日期", examples=["20260530"])
    ifind_config_path: str | None = Field(None, description="Blader 配置 YAML 路径")

    factors: list[FactorSpecIn] = Field(
        default_factory=list,
        description="因子列表；空则使用默认 PE_TTM/PB/ROE_TTM/MOMENTUM_20",
    )
    weight_mode: Literal["equal", "custom"] = Field("equal", description="等权或自定义权重")
    industry_neutral: bool = Field(False, description="行业中性化（需行业因子数据）")
    cap_neutral: bool = Field(False, description="市值中性化（默认 MARKET_VALUE 因子）")

    universe: Literal["all_a", "csi300", "csi500", "csi1000"] = Field(
        "all_a",
        description="股票池：all_a 全市场；csi300/500/1000 使用指数成分（index_members 表 / BaoStock / 中证官网）",
    )
    top_n: int = Field(30, ge=1, le=200, description="持仓股票数")
    rebalance_freq: Literal["daily", "weekly", "monthly"] = Field(
        "daily", description="调仓频率"
    )

    exclude_st: bool = Field(True, description="剔除 ST（需标记字段）")
    exclude_suspended: bool = Field(True, description="剔除停牌（成交量=0）")
    exclude_new_days: int = Field(60, ge=0, description="剔除上市不足 N 日新股")
    exclude_limit: bool = Field(True, description="剔除涨跌停无法交易")

    initial_cash: float = Field(1_000_000, gt=0, description="初始资金")
    buy_commission: float = Field(0.0003, description="买入佣金率")
    sell_commission: float = Field(0.0013, description="卖出佣金率")
    slippage: float = Field(0.001, description="滑点比例")

    run_single_factor_analysis: bool = Field(True, description="是否运行单因子 IC/分层分析")
    output_dir: str | None = Field(None, description="报告输出目录，默认 output/engine_report")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "start": "20251110",
                    "end": "20260530",
                    "factors": [
                        {"code": "PE_TTM", "weight": 0.25, "ascending": False},
                        {"code": "PB", "weight": 0.25, "ascending": False},
                        {"code": "ROE_TTM", "weight": 0.25, "ascending": True},
                    ],
                    "weight_mode": "equal",
                    "universe": "all_a",
                    "top_n": 30,
                    "rebalance_freq": "daily",
                }
            ]
        }
    }


class EnginePerformanceData(BaseModel):
    total_return: float | None = None
    annualized_return: float | None = None
    benchmark_total_return: float | None = None
    annualized_excess_return: float | None = None
    volatility: float | None = None
    sharpe_ratio: float | None = None
    max_drawdown: float | None = None
    calmar_ratio: float | None = None
    win_rate: float | None = None
    profit_loss_ratio: float | None = None
    information_ratio: float | None = None
    alpha: float | None = None
    beta: float | None = None


class ReturnOverviewData(BaseModel):
    total_return: float | None = Field(None, description="累计收益率")
    annualized_return: float | None = Field(None, description="年化收益率")
    excess_return: float | None = Field(None, description="超额收益（累计）")
    annualized_excess_return: float | None = Field(None, description="年化超额收益")
    alpha: float | None = Field(None, description="Alpha")
    beta: float | None = Field(None, description="Beta")


class RiskMetricsData(BaseModel):
    max_drawdown: float | None = Field(None, description="最大回撤")
    sharpe_ratio: float | None = Field(None, description="夏普比率")
    calmar_ratio: float | None = Field(None, description="卡玛比率")
    win_rate: float | None = Field(None, description="胜率")
    profit_loss_ratio: float | None = Field(None, description="盈亏比")
    volatility: float | None = None
    information_ratio: float | None = None


class MonthlyHeatmapData(BaseModel):
    available: bool = Field(description="是否可展示月度热力图")
    note: str = Field("", description="不可展示时的说明")
    data: list[dict] = Field(default_factory=list, description="year/month/return_pct")


class BacktestHistoryItem(BaseModel):
    run_id: str
    start_date: str
    end_date: str
    universe: str
    top_n: int
    rebalance_freq: str
    total_return: float | None = None
    annualized_return: float | None = None
    excess_return: float | None = None
    max_drawdown: float | None = None
    sharpe_ratio: float | None = None
    monthly_heatmap_available: bool | None = None
    created_at: str | None = None


class EngineBacktestResultData(BaseModel):
    run_id: str | None = Field(None, description="落库后的回测运行 ID")
    performance: dict
    return_overview: ReturnOverviewData | None = None
    risk_metrics: RiskMetricsData | None = None
    monthly_heatmap: MonthlyHeatmapData | None = None
    output_dir: str
    factor_scores_shape: list[int] | None = None
    factor_analyses: dict | None = None


class EngineBacktestValidate(EngineBacktestCreate):
    """因子+回测全链路验证（参数与 run-sync 对齐，扩展自动区间与单因子快捷传参）。"""

    factor_code: str | None = Field(
        None,
        description="单因子快捷参数；factors 为空时自动构建 [{code: factor_code}]",
        examples=["ROE_TTM"],
    )
    use_full_data_range: bool = Field(
        False,
        description="为 true 时自动使用数据库日K与因子宽表交集的最近 N 年全量区间",
    )
    data_years: float = Field(3.0, gt=0, le=10, description="全量区间年数，默认 3 年")
    output_subdir: str | None = Field(
        None,
        description="验证报告输出子目录，默认 output/validation/{factor_code}",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "factor_code": "ROE_TTM",
                    "use_full_data_range": True,
                    "top_n": 20,
                    "rebalance_freq": "monthly",
                    "universe": "all_a",
                }
            ]
        }
    }
