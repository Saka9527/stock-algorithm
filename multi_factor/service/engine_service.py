# -*- coding: utf-8 -*-
"""新多因子引擎 API 服务层。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from multi_factor import config as project_config
from multi_factor.engine.backtest_analytics import (
    monthly_heatmap_eligible,
    return_overview_from_perf,
    risk_metrics_from_perf,
)
from multi_factor.engine.backtest_storage import BacktestStorage, MAX_BACKTEST_HISTORY
from multi_factor.engine.pipeline import run_engine_pipeline
from multi_factor.engine.strategy_config import DEFAULT_FACTORS, FactorSpec, StrategyConfig
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.service.serializers import dataframe_to_records, panel_to_split_json

ENGINE_REPORT_DIR = project_config.OUTPUT_DIR / "engine_report"


def engine_report_ready() -> bool:
    return (ENGINE_REPORT_DIR / "summary.json").exists()


def request_to_strategy(body: dict) -> StrategyConfig:
    """将 API 请求体转为 StrategyConfig。"""
    factors = []
    for f in body.get("factors") or []:
        factors.append(
            FactorSpec(
                code=f["code"],
                weight=float(f.get("weight", 1.0)),
                ascending=f.get("ascending"),
            )
        )
    out_dir = body.get("output_dir")
    return StrategyConfig(
        start=str(body["start"]),
        end=str(body["end"]),
        ifind_config_path=str(
            body.get("ifind_config_path") or project_config.IFIND_CONFIG_PATH
        ),
        factors=factors,
        weight_mode=body.get("weight_mode", "equal"),
        industry_neutral=bool(body.get("industry_neutral", False)),
        cap_neutral=bool(body.get("cap_neutral", False)),
        universe=body.get("universe", "all_a"),
        top_n=int(body.get("top_n", 30)),
        rebalance_freq=body.get("rebalance_freq", "daily"),
        exclude_st=bool(body.get("exclude_st", True)),
        exclude_suspended=bool(body.get("exclude_suspended", True)),
        exclude_new_days=int(body.get("exclude_new_days", 60)),
        exclude_limit=bool(body.get("exclude_limit", True)),
        initial_cash=float(body.get("initial_cash", 1_000_000)),
        buy_commission=float(body.get("buy_commission", 0.0003)),
        sell_commission=float(body.get("sell_commission", 0.0013)),
        slippage=float(body.get("slippage", 0.001)),
        run_single_factor_analysis=bool(body.get("run_single_factor_analysis", True)),
        output_dir=Path(out_dir) if out_dir else ENGINE_REPORT_DIR,
    )


def _get_backtest_storage() -> BacktestStorage:
    ifind_cfg = load_ifind_config(str(project_config.IFIND_CONFIG_PATH))
    return BacktestStorage(ifind_cfg)


def run_engine_backtest(body: dict) -> dict[str, Any]:
    """执行引擎流水线并返回摘要。"""
    cfg = request_to_strategy(body)
    if not cfg.factors:
        cfg.factors = list(DEFAULT_FACTORS)
    result = run_engine_pipeline(cfg)
    perf = result["performance"]
    heatmap_ok, heatmap_note = monthly_heatmap_eligible(cfg.start, cfg.end)
    return {
        "run_id": result.get("run_id"),
        "performance": perf,
        "return_overview": return_overview_from_perf(perf),
        "risk_metrics": risk_metrics_from_perf(perf),
        "monthly_heatmap": {
            "available": heatmap_ok,
            "note": heatmap_note if not heatmap_ok else "",
        },
        "output_dir": str(result["output_dir"]),
        "factor_scores_shape": list(result["composite_scores"].shape),
        "factor_analyses": {
            k: v.get("summary") if isinstance(v, dict) and "summary" in v else v
            for k, v in result.get("factor_analyses", {}).items()
        },
    }


def _read_json(name: str) -> dict:
    path = ENGINE_REPORT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"引擎报告不存在: {path}，请先 POST /api/v1/engine/backtest/run-sync")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_engine_performance() -> dict:
    if (ENGINE_REPORT_DIR / "performance.csv").exists():
        df = pd.read_csv(ENGINE_REPORT_DIR / "performance.csv", encoding="utf-8-sig", index_col=0)
        return {"performance": df.iloc[:, 0].to_dict()}
    data = _read_json("summary.json")
    return {"performance": data.get("performance", {})}


def get_engine_summary() -> dict:
    return _read_json("summary.json")


def get_engine_portfolio(limit: int | None = None) -> dict:
    path = ENGINE_REPORT_DIR / "portfolio.csv"
    if not path.exists():
        raise FileNotFoundError("portfolio.csv 不存在")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if limit:
        df = df.tail(limit)
    return {"records": dataframe_to_records(df)}


def get_engine_trades(limit: int | None = None) -> dict:
    path = ENGINE_REPORT_DIR / "trades.csv"
    if not path.exists():
        raise FileNotFoundError("trades.csv 不存在")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if limit:
        df = df.tail(limit)
    return {"records": dataframe_to_records(df)}


def get_engine_positions(limit: int | None = None) -> dict:
    path = ENGINE_REPORT_DIR / "positions.csv"
    if not path.exists():
        raise FileNotFoundError("positions.csv 不存在")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if limit:
        df = df.tail(limit)
    return {"records": dataframe_to_records(df)}


def get_engine_factor_analysis(factor_code: str) -> dict:
    path = ENGINE_REPORT_DIR / "factor_analysis" / factor_code.upper() / "analysis.json"
    if not path.exists():
        raise FileNotFoundError(f"因子分析不存在: {factor_code}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_engine_composite_scores(
    date: str | None = None,
    start: str | None = None,
    end: str | None = None,
    fmt: str = "records",
) -> dict:
    path = ENGINE_REPORT_DIR / "composite_scores.pkl"
    if not path.exists():
        path = project_config.FACTOR_SCORES_PATH
    if not path.exists():
        raise FileNotFoundError("合成得分不存在，请先执行引擎回测")
    df = pd.read_pickle(path)
    df.index = pd.to_datetime(df.index)
    if start:
        df = df.loc[df.index >= pd.Timestamp(start)]
    if end:
        df = df.loc[df.index <= pd.Timestamp(end)]
    if date:
        dt = pd.Timestamp(date)
        if dt not in df.index:
            idx = df.index[df.index <= dt]
            dt = idx[-1] if len(idx) else df.index[0]
        df = df.loc[[dt]]
    payload = panel_to_split_json(df) if fmt == "split" else {"records": dataframe_to_records(df)}
    payload["meta"] = {
        "rows": int(df.shape[0]),
        "cols": int(df.shape[1]),
        "start": df.index.min().strftime("%Y-%m-%d") if len(df) else None,
        "end": df.index.max().strftime("%Y-%m-%d") if len(df) else None,
    }
    return payload


def list_engine_chart_files() -> dict:
    """返回可访问的图表文件名列表。"""
    charts = []
    for p in ENGINE_REPORT_DIR.glob("*.png"):
        charts.append(p.name)
    for p in (ENGINE_REPORT_DIR / "factor_analysis").rglob("*.png"):
        charts.append(str(p.relative_to(ENGINE_REPORT_DIR)).replace("\\", "/"))
    return {"charts": charts, "report_dir": str(ENGINE_REPORT_DIR)}


def list_backtest_history(limit: int = MAX_BACKTEST_HISTORY) -> dict:
    storage = _get_backtest_storage()
    runs = storage.list_runs(limit=limit)
    return {"runs": runs, "limit": limit, "max_history": MAX_BACKTEST_HISTORY}


def get_backtest_report(run_id: str) -> dict:
    storage = _get_backtest_storage()
    report = storage.get_full_report(run_id)
    if not report:
        raise FileNotFoundError(f"回测记录不存在: {run_id}")
    return report


def get_latest_backtest_from_db() -> dict | None:
    """从数据库读取最近一次回测完整报告。"""
    storage = _get_backtest_storage()
    runs = storage.list_runs(limit=1)
    if not runs:
        return None
    return storage.get_full_report(runs[0]["run_id"])


def get_backtest_nav(run_id: str, limit: int | None = None) -> dict:
    storage = _get_backtest_storage()
    return {"run_id": run_id, "records": storage.get_nav(run_id, limit)}


def get_backtest_monthly_heatmap(run_id: str) -> dict:
    storage = _get_backtest_storage()
    payload = storage.get_monthly_heatmap(run_id)
    return {"run_id": run_id, **payload}


def get_backtest_holding_analysis(run_id: str) -> dict:
    storage = _get_backtest_storage()
    return {"run_id": run_id, **storage.get_holding_analysis(run_id)}


def get_backtest_factor_attribution(run_id: str) -> dict:
    storage = _get_backtest_storage()
    return {"run_id": run_id, "factors": storage.get_factor_attribution(run_id)}


def get_backtest_trades_db(run_id: str, limit: int | None = None) -> dict:
    storage = _get_backtest_storage()
    return {"run_id": run_id, "records": storage.get_trades(run_id, limit)}
