# -*- coding: utf-8 -*-
"""封装回测/因子计算，供 API 与 CLI 共用。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from multi_factor import config
from multi_factor.service.serializers import (
    dataframe_to_records,
    panel_to_split_json,
    summary_dict_to_json,
)


SourceType = Literal["rqdatac", "ifind", "demo"]


@dataclass
class BacktestRequest:
    source: SourceType = "rqdatac"
    start: str = config.START_DATE
    end: str = config.END_DATE
    index: str = config.UNIVERSE_INDEX
    top_n: int = config.TOP_N
    skip_factor_analysis: bool = False
    skip_backtest: bool = False
    scores_only: bool = False
    ifind_config: str | None = None
    local_backtest: bool = True
    factor_weights: dict | None = None
    use_engine: bool = False


@dataclass
class BacktestResult:
    summary: dict = field(default_factory=dict)
    factor_scores_shape: tuple[int, int] | None = None
    output_dir: str = ""
    message: str = ""


def _load_scores() -> pd.DataFrame:
    path = config.FACTOR_SCORES_PATH
    if not path.exists():
        raise FileNotFoundError(f"因子得分文件不存在: {path}，请先执行回测任务")
    return pd.read_pickle(path)


def run_pipeline(req: BacktestRequest) -> BacktestResult:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    weights = req.factor_weights or config.FACTOR_WEIGHTS

    if req.source == "ifind" and req.use_engine:
        from multi_factor.service.engine_service import run_engine_backtest

        body: dict = {
            "start": req.start,
            "end": req.end,
            "ifind_config_path": req.ifind_config,
            "top_n": req.top_n,
        }
        if req.factor_weights:
            mapping = {"pe": "PE_TTM", "pb": "PB_MRQ", "roe": "ROE_TTM", "momentum": "MOMENTUM_20"}
            body["factors"] = [
                {"code": mapping.get(k, k).upper(), "weight": v}
                for k, v in req.factor_weights.items()
            ]
        eng = run_engine_backtest(body)
        scores_shape = tuple(eng["factor_scores_shape"]) if eng.get("factor_scores_shape") else None
        return BacktestResult(
            summary=eng.get("performance", {}),
            factor_scores_shape=scores_shape,
            output_dir=eng.get("output_dir", str(config.OUTPUT_DIR)),
            message="ok",
        )
    elif req.source == "ifind":
        from multi_factor.ifind.pipeline import run_ifind_pipeline

        run_ifind_pipeline(
            start=req.start,
            end=req.end,
            config_path=req.ifind_config,
            top_n=req.top_n,
            skip_factor_analysis=req.skip_factor_analysis,
            scores_only=req.scores_only,
            skip_backtest=req.skip_backtest,
            local_backtest=req.local_backtest,
            weights=weights,
        )
    elif req.source == "demo":
        from multi_factor.demo_scores import generate_demo_scores

        generate_demo_scores(req.start, req.end, config.FACTOR_SCORES_PATH)
        if not req.scores_only and not req.skip_backtest:
            _run_rqalpha(req)
    else:
        from multi_factor.data_utils import init_rqdatac, verify_rqdatac_connection
        from multi_factor.factor_analysis import (
            precompute_composite_scores,
            run_full_factor_research,
        )

        init_rqdatac()
        verify_rqdatac_connection()
        if not req.skip_factor_analysis:
            run_full_factor_research(
                req.start, req.end, req.index, weights, config.FACTOR_ANALYSIS_DIR
            )
        precompute_composite_scores(
            req.start, req.end, req.index, weights, config.FACTOR_SCORES_PATH
        )
        if not req.scores_only and not req.skip_backtest:
            _run_rqalpha(req)

    summary = _read_summary()
    scores = _load_scores() if config.FACTOR_SCORES_PATH.exists() else None
    return BacktestResult(
        summary=summary,
        factor_scores_shape=scores.shape if scores is not None else None,
        output_dir=str(config.OUTPUT_DIR),
        message="ok",
    )


def _run_rqalpha(req: BacktestRequest) -> dict:
    from rqalpha import run_func

    from multi_factor.report import build_backtest_report
    from multi_factor.strategy import get_backtest_config, handle_bar, init

    bt_config = get_backtest_config(req.start, req.end)
    bt_config["extra"]["context_vars"]["top_n"] = req.top_n
    result = run_func(config=bt_config, init=init, handle_bar=handle_bar)
    build_backtest_report(result, config.BACKTEST_REPORT_DIR)
    analyser = result.get("sys_analyser", result)
    summary = analyser.get("summary", {})
    path = config.BACKTEST_REPORT_DIR / "summary.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return summary


def _read_summary() -> dict:
    from multi_factor.service.engine_service import ENGINE_REPORT_DIR, engine_report_ready

    if engine_report_ready():
        with open(ENGINE_REPORT_DIR / "summary.json", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("performance", data)
    path = config.BACKTEST_REPORT_DIR / "summary.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    csv_path = config.BACKTEST_REPORT_DIR / "summary.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path, encoding="utf-8-sig", index_col=0)
        return df.iloc[:, 0].to_dict()
    eng_perf = ENGINE_REPORT_DIR / "performance.csv"
    if eng_perf.exists():
        df = pd.read_csv(eng_perf, encoding="utf-8-sig", index_col=0)
        return df.iloc[:, 0].to_dict()
    return {}


def get_health() -> dict:
    rqdatac_ok = False
    rqdatac_detail = ""
    try:
        from multi_factor.data_utils import init_rqdatac, verify_rqdatac_connection

        init_rqdatac()
        verify_rqdatac_connection()
        import rqdatac as rq

        quota = rq.user.get_quota()
        rqdatac_ok = True
        rqdatac_detail = {
            "license_type": quota.get("license_type"),
            "remaining_days": quota.get("remaining_days"),
        }
    except Exception as e:
        rqdatac_detail = str(e)

    from multi_factor.service.engine_service import engine_report_ready

    return {
        "status": "ok",
        "rqdatac": {"connected": rqdatac_ok, "detail": rqdatac_detail},
        "factor_scores_ready": config.FACTOR_SCORES_PATH.exists(),
        "backtest_report_ready": (config.BACKTEST_REPORT_DIR / "summary.json").exists()
        or (config.BACKTEST_REPORT_DIR / "summary.csv").exists(),
        "engine_report_ready": engine_report_ready(),
    }


def get_factor_scores(
    date: str | None = None,
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
    fmt: str = "records",
) -> dict:
    df = _load_scores()
    df.index = pd.to_datetime(df.index)
    if start:
        df = df.loc[df.index >= pd.Timestamp(start)]
    if end:
        df = df.loc[df.index <= pd.Timestamp(end)]
    if symbols:
        cols = [c for c in symbols if c in df.columns]
        df = df[cols]
    if date:
        dt = pd.Timestamp(date)
        if dt not in df.index:
            idx = df.index[df.index <= dt]
            if len(idx) == 0:
                raise ValueError(f"无可用交易日: {date}")
            dt = idx[-1]
        df = df.loc[[dt]]

    if fmt == "split":
        payload = panel_to_split_json(df)
    else:
        payload = {"records": dataframe_to_records(df)}
    payload["meta"] = {
        "rows": int(df.shape[0]),
        "cols": int(df.shape[1]),
        "start": df.index.min().strftime("%Y-%m-%d") if len(df) else None,
        "end": df.index.max().strftime("%Y-%m-%d") if len(df) else None,
    }
    return payload


def get_top_stocks(date: str, top_n: int = 30) -> dict:
    df = _load_scores()
    df.index = pd.to_datetime(df.index)
    dt = pd.Timestamp(date)
    if dt not in df.index:
        idx = df.index[df.index <= dt]
        if len(idx) == 0:
            raise ValueError(f"无可用交易日: {date}")
        dt = idx[-1]
    row = df.loc[dt].dropna().sort_values(ascending=False).head(top_n)
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "top_n": top_n,
        "stocks": [
            {"order_book_id": code, "score": float(score)}
            for code, score in row.items()
        ],
    }


def get_backtest_summary() -> dict:
    return {"summary": summary_dict_to_json(_read_summary())}


def get_backtest_portfolio(limit: int | None = None) -> dict:
    from multi_factor.service.engine_service import ENGINE_REPORT_DIR, engine_report_ready

    path = config.BACKTEST_REPORT_DIR / "portfolio.csv"
    if not path.exists() and engine_report_ready():
        path = ENGINE_REPORT_DIR / "portfolio.csv"
    if not path.exists():
        raise FileNotFoundError("portfolio.csv 不存在，请先执行回测")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if limit:
        df = df.tail(limit)
    return {"records": dataframe_to_records(df)}


def get_backtest_trades(limit: int | None = None) -> dict:
    from multi_factor.service.engine_service import ENGINE_REPORT_DIR, engine_report_ready

    path = config.BACKTEST_REPORT_DIR / "trades.csv"
    if not path.exists() and engine_report_ready():
        path = ENGINE_REPORT_DIR / "trades.csv"
    if not path.exists():
        raise FileNotFoundError("trades.csv 不存在，请先执行回测")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if limit:
        df = df.tail(limit)
    return {"records": dataframe_to_records(df)}


def get_factor_ic_summary(factor_name: str) -> dict:
    path = config.FACTOR_ANALYSIS_DIR / factor_name / "factor_analysis.xlsx"
    if not path.exists():
        raise FileNotFoundError(f"因子分析不存在: {factor_name}")
    df = pd.read_excel(path, sheet_name="IC汇总")
    return {"factor": factor_name, "ic_summary": dataframe_to_records(df)}
