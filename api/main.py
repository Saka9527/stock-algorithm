# -*- coding: utf-8 -*-
"""
多因子回测 HTTP API（FastAPI）。

启动:
  python run_api.py
  # 或: uvicorn api.main:app --host 0.0.0.0 --port 8000

文档: http://127.0.0.1:8000/docs

可选鉴权: 环境变量 MULTI_FACTOR_API_KEY，请求头 X-API-Key
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api.schemas import (
    ApiResponse,
    BacktestJobCreate,
    EngineBacktestCreate,
    EngineBacktestValidate,
    FactorPerformanceWarmupCreate,
    JobStatusResponse,
)
from multi_factor.service import backtest_validation, engine_service, nightly_jobs, runner
from multi_factor.service.factor_market import get_factor_market_service
from multi_factor.service.jobs import job_manager
from multi_factor.service.runner import BacktestRequest

API_KEY = os.environ.get("MULTI_FACTOR_API_KEY", "").strip()

OPENAPI_TAGS = [
    {
        "name": "系统",
        "description": "健康检查、服务状态",
    },
    {
        "name": "因子",
        "description": "合成因子得分矩阵、TopN 选股、RQFactor IC 汇总（依赖已跑批的 pkl/Excel）",
    },
    {
        "name": "因子市场",
        "description": "Blader `factor_base_info` + `factor_data_wide`：列表、IC 走势、分组收益",
    },
    {
        "name": "多因子引擎",
        "description": "新版引擎：多因子合成、T+1 回测、单因子分析、完整绩效与报告（推荐 iFinD/Blader）",
    },
    {
        "name": "回测",
        "description": "读取最近一次回测结果（传统 pipeline 或引擎报告，自动择优）",
    },
    {
        "name": "任务",
        "description": "异步提交长耗时回测，通过 job_id 轮询",
    },
]

app = FastAPI(
    title="Multi-Factor Backtest API",
    description=(
        "米筐 / Blader 多因子策略 HTTP 接口。\n\n"
        "- **因子市场**: 单因子 IC、分层收益（`/api/v1/factors`）\n"
        "- **多因子引擎**: 多因子合成 + T+1 回测 + 绩效报告（`/api/v1/engine`）\n"
        "- **传统任务**: RQDatac / 旧版 iFinD pipeline（`/api/v1/jobs/backtest`）\n"
    ),
    version="1.1.0",
    openapi_tags=OPENAPI_TAGS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("API_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def ok(data=None, message: str = "ok") -> ApiResponse:
    return ApiResponse(code=0, message=message, data=data)


# =============================================================================
# 系统
# =============================================================================


@app.get("/health/live", include_in_schema=False)
def health_live():
    """容器探活（无需鉴权）。"""
    return {"status": "ok"}


@app.get(
    "/api/v1/health",
    response_model=ApiResponse,
    tags=["系统"],
    summary="健康检查",
    description="返回服务状态、RQDatac 连接、因子得分与回测报告是否就绪。",
)
def health(_: None = Depends(verify_api_key)):
    return ok(runner.get_health())


# =============================================================================
# 因子（合成得分 / TopN）
# =============================================================================


@app.get(
    "/api/v1/factor-scores",
    response_model=ApiResponse,
    tags=["因子"],
    summary="因子得分矩阵",
    description="读取 `output/factor_scores.pkl`（需先执行回测或引擎任务）。支持单日或区间、`records`/`split` 格式。",
)
def factor_scores(
    date: str | None = Query(None, description="截面日期 YYYY-MM-DD"),
    start: str | None = Query(None, description="区间起始 YYYY-MM-DD"),
    end: str | None = Query(None, description="区间结束 YYYY-MM-DD"),
    symbols: str | None = Query(None, description="逗号分隔股票代码，如 600000.XSHG,000001.XSHE"),
    format: str = Query("records", description="records | split", pattern="^(records|split)$"),
    _: None = Depends(verify_api_key),
):
    sym_list = [s.strip() for s in symbols.split(",")] if symbols else None
    try:
        return ok(
            runner.get_factor_scores(
                date=date, start=start, end=end, symbols=sym_list, fmt=format
            )
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factor-scores/top",
    response_model=ApiResponse,
    tags=["因子"],
    summary="某日 Top N 选股",
    description="按合成因子得分降序，返回指定交易日得分最高的 N 只股票。",
)
def factor_scores_top(
    date: str = Query(..., description="调仓/截面日期 YYYY-MM-DD", examples=["2023-12-01"]),
    top_n: int = Query(30, ge=1, le=200, description="返回股票数量"),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(runner.get_top_stocks(date, top_n))
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factor-analysis/{factor_name}/ic",
    response_model=ApiResponse,
    tags=["因子"],
    summary="RQFactor 单因子 IC 汇总",
    description="读取 `output/factor_analysis/{factor_name}/factor_analysis.xlsx` 的 IC 汇总表。"
    " factor_name: pe | pb | roe | momentum | composite",
)
def factor_ic(
    factor_name: str,
    _: None = Depends(verify_api_key),
):
    try:
        return ok(runner.get_factor_ic_summary(factor_name))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# =============================================================================
# 因子市场（Blader 单因子）
# =============================================================================


@app.get(
    "/api/v1/factors",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="因子列表（含 IC 均值、夏普）",
    description="从 `factor_base_info` 读取全部有效因子，并计算区间内 IC 均值、多空夏普等（耗时较长）。",
)
def list_factors_market(
    start: str | None = Query(None, description="分析区间起 YYYYMMDD"),
    end: str | None = Query(None, description="分析区间止 YYYYMMDD"),
    period: int = Query(1, ge=1, le=20, description="IC/收益前瞻持有期(交易日)"),
    quantiles: int = Query(5, ge=2, le=10, description="分层组数"),
    top_pct: float = Query(0.2, gt=0, lt=0.5, description="Top/Bottom 比例"),
    prefer_db: bool = Query(True, description="优先读取 factor_performance_* 统计表"),
    persist_on_compute: bool = Query(False, description="当实时计算时是否回填统计表"),
    ifind_config: str | None = Query(None, description="ifind 配置文件路径"),
    _: None = Depends(verify_api_key),
):
    svc = get_factor_market_service(ifind_config)
    return ok(
        svc.list_factors_with_summary(
            start=start,
            end=end,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
            prefer_db=prefer_db,
            persist_on_compute=persist_on_compute,
        )
    )


def _factor_db_miss_warmup_response(
    *,
    factor_code: str,
    start: str,
    end: str,
    period: int,
    quantiles: int,
    top_pct: float,
    ifind_config: str | None,
    detail: str,
):
    job_id = job_manager.submit_factor_performance(
        {
            "start": start,
            "end": end,
            "factor_code": factor_code.upper(),
            "period": period,
            "quantiles": quantiles,
            "top_pct": top_pct,
            "ifind_config": ifind_config,
        }
    )
    return ok(
        {
            "factor_code": factor_code.upper(),
            "start": start,
            "end": end,
            "data_source": "db_miss",
            "warmup_started": True,
            "warmup_job_id": job_id,
            "detail": detail,
        },
        message="db miss, warmup started",
    )


@app.post(
    "/api/v1/factors/performance/warmup",
    response_model=ApiResponse,
    tags=["因子市场", "任务"],
    summary="提交因子绩效离线预热任务",
    description="离线计算并写入 factor_performance_summary / factor_performance_series，避免查询时实时重算。",
)
def warmup_factor_performance(
    body: FactorPerformanceWarmupCreate,
    _: None = Depends(verify_api_key),
):
    payload = body.model_dump(exclude_none=True)
    job_id = job_manager.submit_factor_performance(payload)
    return ok({"job_id": job_id, "job_type": "factor_performance"}, message="job submitted")


@app.get(
    "/api/v1/factors/detail",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="单因子完整报告",
    description="元数据 + IC 汇总 + IC 走势 + Top/Bottom 分组收益 + 分位组收益。",
)
def factor_detail(
    factor_code: str = Query(..., description="因子代码，如 MOMENTUM_20"),
    start: str | None = Query(None, description="区间起"),
    end: str | None = Query(None, description="区间止"),
    period: int = Query(1, ge=1, le=20, description="持有期"),
    quantiles: int = Query(5, ge=2, le=10, description="分位组数"),
    top_pct: float = Query(0.2, gt=0, lt=0.5, description="Top/Bottom 比例"),
    prefer_db: bool = Query(True, description="优先读取 factor_performance_* 统计表"),
    persist_on_compute: bool = Query(False, description="当实时计算时是否回填统计表"),
    ifind_config: str | None = Query(None),
    _: None = Depends(verify_api_key),
):
    svc = get_factor_market_service(ifind_config)
    s, e = svc._default_dates(start, end)
    try:
        return ok(
            svc.get_factor_report(
                factor_code,
                start=s,
                end=e,
                period=period,
                quantiles=quantiles,
                top_pct=top_pct,
                prefer_db=prefer_db,
                persist_on_compute=persist_on_compute,
            )
        )
    except ValueError as e2:
        msg = str(e2)
        if prefer_db and not persist_on_compute and "未命中 factor_performance_*" in msg:
            return _factor_db_miss_warmup_response(
                factor_code=factor_code,
                start=s,
                end=e,
                period=period,
                quantiles=quantiles,
                top_pct=top_pct,
                ifind_config=ifind_config,
                detail=msg,
            )
        raise HTTPException(status_code=404, detail=msg) from e2
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factors/summary",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="单因子元数据与 IC 汇总",
)
def factor_summary(
    factor_code: str = Query(..., description="因子代码，如 MOMENTUM_20"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    period: int = Query(1, ge=1, le=20),
    quantiles: int = Query(5, ge=2, le=10),
    top_pct: float = Query(0.2, gt=0, lt=0.5),
    prefer_db: bool = Query(True),
    persist_on_compute: bool = Query(False),
    ifind_config: str | None = Query(None),
    _: None = Depends(verify_api_key),
):
    svc = get_factor_market_service(ifind_config)
    s, e = svc._default_dates(start, end)
    try:
        rep = svc.get_factor_report(
            factor_code,
            start=s,
            end=e,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
            prefer_db=prefer_db,
            persist_on_compute=persist_on_compute,
        )
        return ok(
            {
                "meta": rep["meta"],
                "summary": rep["summary"],
                "period": rep["period"],
                "data_source": rep.get("data_source"),
            }
        )
    except ValueError as e2:
        msg = str(e2)
        if prefer_db and not persist_on_compute and "未命中 factor_performance_*" in msg:
            return _factor_db_miss_warmup_response(
                factor_code=factor_code,
                start=s,
                end=e,
                period=period,
                quantiles=quantiles,
                top_pct=top_pct,
                ifind_config=ifind_config,
                detail=msg,
            )
        raise HTTPException(status_code=404, detail=msg) from e2
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factors/ic-trend",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="IC 走势序列",
)
def factor_ic_trend(
    factor_code: str = Query(..., description="因子代码，如 MOMENTUM_20"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    period: int = Query(1, ge=1, le=20),
    quantiles: int = Query(5, ge=2, le=10),
    top_pct: float = Query(0.2, gt=0, lt=0.5),
    prefer_db: bool = Query(True),
    persist_on_compute: bool = Query(False),
    ifind_config: str | None = Query(None),
    _: None = Depends(verify_api_key),
):
    svc = get_factor_market_service(ifind_config)
    s, e = svc._default_dates(start, end)
    try:
        rep = svc.get_factor_report(
            factor_code,
            start=s,
            end=e,
            period=period,
            quantiles=quantiles,
            top_pct=top_pct,
            prefer_db=prefer_db,
            persist_on_compute=persist_on_compute,
        )
        return ok(
            {
                "factor_code": factor_code,
                "summary": {
                    "ic_mean": rep["summary"].get("ic_mean"),
                    "win_rate": rep["summary"].get("win_rate"),
                    "ic_ir": rep["summary"].get("ic_ir"),
                },
                "ic_trend": rep["ic_trend"],
                "data_source": rep.get("data_source"),
            }
        )
    except ValueError as e2:
        msg = str(e2)
        if prefer_db and not persist_on_compute and "未命中 factor_performance_*" in msg:
            return _factor_db_miss_warmup_response(
                factor_code=factor_code,
                start=s,
                end=e,
                period=period,
                quantiles=quantiles,
                top_pct=top_pct,
                ifind_config=ifind_config,
                detail=msg,
            )
        raise HTTPException(status_code=404, detail=msg) from e2
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factors/group-returns",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="分组收益曲线数据",
)
def factor_group_returns(
    factor_code: str = Query(..., description="因子代码，如 MOMENTUM_20"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    period: int = Query(1, ge=1, le=20),
    top_pct: float = Query(0.2, gt=0, lt=0.5),
    quantiles: int = Query(5, ge=2, le=10),
    prefer_db: bool = Query(True),
    persist_on_compute: bool = Query(False),
    ifind_config: str | None = Query(None),
    _: None = Depends(verify_api_key),
):
    svc = get_factor_market_service(ifind_config)
    s, e = svc._default_dates(start, end)
    try:
        rep = svc.get_factor_report(
            factor_code,
            start=s,
            end=e,
            period=period,
            top_pct=top_pct,
            quantiles=quantiles,
            prefer_db=prefer_db,
            persist_on_compute=persist_on_compute,
        )
        return ok(
            {
                "factor_code": factor_code,
                "top_pct": top_pct,
                "group_returns": rep["group_returns"],
                "quantile_returns": rep["quantile_returns"],
                "data_source": rep.get("data_source"),
            }
        )
    except ValueError as e2:
        msg = str(e2)
        if prefer_db and not persist_on_compute and "未命中 factor_performance_*" in msg:
            return _factor_db_miss_warmup_response(
                factor_code=factor_code,
                start=s,
                end=e,
                period=period,
                quantiles=quantiles,
                top_pct=top_pct,
                ifind_config=ifind_config,
                detail=msg,
            )
        raise HTTPException(status_code=404, detail=msg) from e2
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/factors/raw-values",
    response_model=ApiResponse,
    tags=["因子市场"],
    summary="因子原始计算值",
    description="来自 `factor_data_wide` 的宽表截面。",
)
def factor_raw_values(
    factor_code: str = Query(..., description="因子代码，如 MOMENTUM_20"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    date: str | None = Query(None, description="单日截面 YYYY-MM-DD"),
    format: str = Query("split", pattern="^(records|split)$"),
    ifind_config: str | None = Query(None),
    _: None = Depends(verify_api_key),
):
    from multi_factor.service.serializers import dataframe_to_records, panel_to_split_json

    svc = get_factor_market_service(ifind_config)
    s, e = svc._default_dates(start, end)
    panel = svc.provider.load_factor_panel_by_code(factor_code, s, e)
    if date:
        import pandas as pd

        dt = pd.Timestamp(date)
        if dt not in panel.index:
            idx = panel.index[panel.index <= dt]
            dt = idx[-1] if len(idx) else panel.index[0]
        panel = panel.loc[[dt]]
    payload = (
        panel_to_split_json(panel)
        if format == "split"
        else {"records": dataframe_to_records(panel)}
    )
    payload["factor_code"] = factor_code.upper()
    return ok(payload)


# =============================================================================
# 多因子引擎
# =============================================================================


@app.post(
    "/api/v1/engine/backtest/run-sync",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="同步执行引擎回测",
    description=(
        "执行完整流水线：单因子分析 → 多因子合成 → T+1 回测 → 绩效与图表。"
        "耗时可长达数分钟，生产环境建议用 `/api/v1/engine/jobs/backtest`。"
    ),
)
def engine_backtest_sync(body: EngineBacktestCreate, _: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.run_engine_backtest(body.model_dump(exclude_none=True)))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post(
    "/api/v1/engine/backtest/validate",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测验证"],
    summary="因子+回测全链路验证",
    description=(
        "参数与 run-sync 对齐，支持 factor_code 单因子快捷传参、"
        "use_full_data_range 自动使用库内全量 3 年区间；"
        "执行回测并校验落库、报告文件与各项分析数据。"
    ),
)
def engine_backtest_validate(body: EngineBacktestValidate, _: None = Depends(verify_api_key)):
    try:
        result = backtest_validation.validate_backtest_chain(
            body.model_dump(exclude_none=True)
        )
        return ok(result, message="validation passed" if result["ok"] else "validation failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/data-range",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测验证"],
    summary="数据库可用回测区间",
    description="返回日K与因子宽表交集区间，默认最近 3 年。",
)
def engine_backtest_data_range(
    years: float = Query(3.0, gt=0, le=10),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(backtest_validation.query_db_data_range(years=years))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post(
    "/api/v1/pipeline/nightly/run",
    response_model=ApiResponse,
    tags=["流水线", "定时任务"],
    summary="执行凌晨预热流水线",
    description="因子绩效批量落库 + Redis 水位；可选回测预热。生产环境建议用 scripts/run_nightly_pipeline.py 守护。",
)
def pipeline_nightly_run(
    skip_backtest_warmup: bool = Query(True, description="是否跳过回测预热"),
    workers: int = Query(0, ge=0, le=16, description="因子绩效并发，0=配置默认"),
    data_years: float = Query(0, ge=0, le=10, description="数据年数，0=配置默认"),
    _: None = Depends(verify_api_key),
):
    try:
        result = nightly_jobs.run_nightly_pipeline(
            years=data_years or None,
            workers=workers or None,
            warmup_backtest=not skip_backtest_warmup,
        )
        return ok(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post(
    "/api/v1/engine/jobs/backtest",
    response_model=ApiResponse,
    tags=["多因子引擎", "任务"],
    summary="提交引擎回测异步任务",
)
def engine_backtest_job(body: EngineBacktestCreate, _: None = Depends(verify_api_key)):
    job_id = job_manager.submit_engine(body.model_dump(exclude_none=True))
    return ok({"job_id": job_id, "job_type": "engine"}, message="job submitted")


@app.get(
    "/api/v1/engine/backtest/summary",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎回测完整摘要 JSON",
)
def engine_summary(_: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_engine_summary())
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/performance",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎绩效指标",
    description="年化收益、最大回撤、夏普、卡玛、Alpha/Beta、超额收益等。",
)
def engine_performance(_: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_engine_performance())
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/portfolio",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎组合净值序列",
)
def engine_portfolio(
    limit: int | None = Query(None, ge=1, le=10000, description="最多返回最近 N 行"),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_engine_portfolio(limit))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/trades",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎成交明细",
)
def engine_trades(
    limit: int | None = Query(None, ge=1, le=10000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_engine_trades(limit))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/positions",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎持仓明细",
)
def engine_positions(
    limit: int | None = Query(None, ge=1, le=50000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_engine_positions(limit))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/composite-scores",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎合成因子得分矩阵",
)
def engine_composite_scores(
    date: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    format: str = Query("records", pattern="^(records|split)$"),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(
            engine_service.get_engine_composite_scores(
                date=date, start=start, end=end, fmt=format
            )
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/factor-analysis/{factor_code}",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎单因子分析报告",
    description="读取 `output/engine_report/factor_analysis/{factor_code}/analysis.json`。",
)
def engine_factor_analysis(
    factor_code: str,
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_engine_factor_analysis(factor_code))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/engine/charts",
    response_model=ApiResponse,
    tags=["多因子引擎"],
    summary="引擎图表文件列表",
)
def engine_charts_list(_: None = Depends(verify_api_key)):
    return ok(engine_service.list_engine_chart_files())


@app.get(
    "/api/v1/engine/charts/{file_path:path}",
    tags=["多因子引擎"],
    summary="下载引擎图表 PNG",
    description="file_path 为 charts 列表中的相对路径，如 `nav_vs_benchmark.png`。",
    responses={200: {"content": {"image/png": {}}}},
)
def engine_chart_file(file_path: str, _: None = Depends(verify_api_key)):
    base = engine_service.ENGINE_REPORT_DIR.resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(status_code=404, detail="chart not found")
    return FileResponse(target, media_type="image/png")


@app.get(
    "/api/v1/engine/backtest/history",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测记录列表",
    description="返回最近 20 次回测运行记录，含区间、参数摘要与核心绩效指标。",
)
def engine_backtest_history(
    limit: int = Query(20, ge=1, le=20, description="最多返回条数，上限 20"),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.list_backtest_history(limit))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测完整报告",
    description="含配置、收益概览、风险指标、净值曲线、月度热力图、持仓分析、因子归因、交易记录。",
)
def engine_backtest_report(run_id: str, _: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_backtest_report(run_id))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}/nav",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测净值曲线",
    description="策略总资产/净值、基准净值、超额净值，X 轴为时间。",
)
def engine_backtest_nav(
    run_id: str,
    limit: int | None = Query(None, ge=1, le=10000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_backtest_nav(run_id, limit))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}/monthly-heatmap",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测月度收益热力图",
    description="回测不足一个月时不返回数据，附说明文案；正收益红色，负收益绿色。",
)
def engine_backtest_monthly_heatmap(run_id: str, _: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_backtest_monthly_heatmap(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}/holding-analysis",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测持仓分析",
    description="盈利/亏损 Top10 股票，反映回测历史盈亏来源（非当前实盘持仓）。",
)
def engine_backtest_holding_analysis(run_id: str, _: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_backtest_holding_analysis(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}/factor-attribution",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测因子归因",
    description="各因子对策略收益的贡献占比。",
)
def engine_backtest_factor_attribution(run_id: str, _: None = Depends(verify_api_key)):
    try:
        return ok(engine_service.get_backtest_factor_attribution(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/runs/{run_id}/trades",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="历史回测交易记录",
    description="每次调仓的日期、操作、股票代码、数量、成交价、调仓后总资产。",
)
def engine_backtest_trades_db(
    run_id: str,
    limit: int | None = Query(None, ge=1, le=50000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(engine_service.get_backtest_trades_db(run_id, limit))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get(
    "/api/v1/engine/backtest/latest-report",
    response_model=ApiResponse,
    tags=["多因子引擎", "回测历史"],
    summary="最近一次回测完整报告（数据库）",
)
def engine_backtest_latest_report(_: None = Depends(verify_api_key)):
    try:
        report = engine_service.get_latest_backtest_from_db()
        if not report:
            raise HTTPException(status_code=404, detail="暂无已落库的回测记录")
        return ok(report)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# 回测（读取最近一次结果）
# =============================================================================


@app.get(
    "/api/v1/backtest/summary",
    response_model=ApiResponse,
    tags=["回测"],
    summary="回测绩效摘要",
    description="优先读取引擎报告，其次传统 `backtest_report`。",
)
def backtest_summary(_: None = Depends(verify_api_key)):
    return ok(runner.get_backtest_summary())


@app.get(
    "/api/v1/backtest/portfolio",
    response_model=ApiResponse,
    tags=["回测"],
    summary="组合净值 CSV",
)
def backtest_portfolio(
    limit: int | None = Query(None, ge=1, le=10000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(runner.get_backtest_portfolio(limit))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get(
    "/api/v1/backtest/trades",
    response_model=ApiResponse,
    tags=["回测"],
    summary="成交明细 CSV",
)
def backtest_trades(
    limit: int | None = Query(None, ge=1, le=10000),
    _: None = Depends(verify_api_key),
):
    try:
        return ok(runner.get_backtest_trades(limit))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# =============================================================================
# 异步任务
# =============================================================================


@app.post(
    "/api/v1/jobs/backtest",
    response_model=ApiResponse,
    tags=["任务"],
    summary="提交回测异步任务",
    description="支持 rqdatac / ifind / demo。`source=ifind` 且 `use_engine=true` 时走新版引擎。",
)
def create_backtest_job(body: BacktestJobCreate, _: None = Depends(verify_api_key)):
    req = BacktestRequest(
        source=body.source,
        start=body.start,
        end=body.end,
        index=body.index,
        top_n=body.top_n,
        skip_factor_analysis=body.skip_factor_analysis,
        skip_backtest=body.skip_backtest,
        scores_only=body.scores_only,
        ifind_config=body.ifind_config,
        local_backtest=body.local_backtest,
        factor_weights=body.factor_weights,
        use_engine=body.use_engine,
    )
    job_id = job_manager.submit(req)
    job_type = "engine" if body.use_engine and body.source == "ifind" else "legacy"
    return ok({"job_id": job_id, "job_type": job_type}, message="job submitted")


@app.get(
    "/api/v1/jobs/{job_id}",
    response_model=ApiResponse,
    tags=["任务"],
    summary="查询异步任务状态",
)
def get_job(job_id: str, _: None = Depends(verify_api_key)):
    rec = job_manager.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    return ok(
        JobStatusResponse(
            job_id=rec.job_id,
            status=rec.status,
            created_at=rec.created_at,
            finished_at=rec.finished_at,
            request=rec.request,
            result=rec.result,
            error=rec.error,
        ).model_dump()
    )


@app.post(
    "/api/v1/backtest/run-sync",
    response_model=ApiResponse,
    tags=["回测", "任务"],
    summary="同步执行传统回测",
    description="阻塞直到完成。`use_engine=true` + `source=ifind` 时等价于引擎同步接口。",
)
def run_backtest_sync(body: BacktestJobCreate, _: None = Depends(verify_api_key)):
    req = BacktestRequest(
        source=body.source,
        start=body.start,
        end=body.end,
        index=body.index,
        top_n=body.top_n,
        skip_factor_analysis=body.skip_factor_analysis,
        skip_backtest=body.skip_backtest,
        scores_only=body.scores_only,
        ifind_config=body.ifind_config,
        local_backtest=body.local_backtest,
        factor_weights=body.factor_weights,
        use_engine=body.use_engine,
    )
    try:
        bt = runner.run_pipeline(req)
        return ok(
            {
                "summary": bt.summary,
                "factor_scores_shape": list(bt.factor_scores_shape)
                if bt.factor_scores_shape
                else None,
                "output_dir": bt.output_dir,
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
