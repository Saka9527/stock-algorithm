# -*- coding: utf-8 -*-
"""
因子+回测全链路验证服务。

与 API `EngineBacktestCreate` 请求体对齐，通过传参适配任意因子与回测任务。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from multi_factor import config as project_config
from multi_factor.engine.backtest_storage import BacktestStorage
from multi_factor.ifind.config_loader import IFindConfig, load_ifind_config
from multi_factor.service import engine_service

DEFAULT_VALIDATION_OUTPUT = project_config.OUTPUT_DIR / "validation"


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def query_db_data_range(
    ifind_cfg: IFindConfig | None = None,
    years: float = 3.0,
) -> dict[str, Any]:
    """
    探测数据库可用全量区间（日 K 与因子宽表交集，默认取最近 N 年）。
    """
    cfg = ifind_cfg or load_ifind_config(str(project_config.IFIND_CONFIG_PATH))
    if not cfg.db_url:
        raise ValueError("需要配置 database 才能探测数据区间")

    daily_tbl = cfg.table("daily")
    daily_date = cfg.cols("daily").col("date")
    factor_tbl = cfg.table("factor")
    factor_date = cfg.cols("factor").col("date")

    sql = f"""
    SELECT
      (SELECT MIN(`{daily_date}`) FROM `{daily_tbl}`) AS daily_min,
      (SELECT MAX(`{daily_date}`) FROM `{daily_tbl}`) AS daily_max,
      (SELECT COUNT(DISTINCT `{daily_date}`) FROM `{daily_tbl}`) AS daily_days,
      (SELECT MIN(`{factor_date}`) FROM `{factor_tbl}`) AS factor_min,
      (SELECT MAX(`{factor_date}`) FROM `{factor_tbl}`) AS factor_max,
      (SELECT COUNT(DISTINCT `{factor_date}`) FROM `{factor_tbl}`) AS factor_days
    """
    engine = create_engine(cfg.db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        row = conn.execute(text(sql)).mappings().first()

    daily_min = row["daily_min"]
    daily_max = row["daily_max"]
    factor_min = row["factor_min"]
    factor_max = row["factor_max"]
    start_d = max(daily_min, factor_min)
    end_d = min(daily_max, factor_max)

    if years and years > 0:
        end_ts = end_d
        start_by_years = end_ts.replace(year=end_ts.year - int(years))
        if start_by_years.month != end_ts.month or start_by_years.day != end_ts.day:
            try:
                start_by_years = end_ts.replace(year=end_ts.year - int(years))
            except ValueError:
                start_by_years = end_ts.replace(year=end_ts.year - int(years), day=28)
        if start_by_years > start_d:
            start_d = start_by_years

    return {
        "start": start_d.strftime("%Y%m%d"),
        "end": end_d.strftime("%Y%m%d"),
        "daily_range": [str(daily_min), str(daily_max)],
        "factor_range": [str(factor_min), str(factor_max)],
        "daily_trading_days": int(row["daily_days"] or 0),
        "factor_trading_days": int(row["factor_days"] or 0),
        "years": years,
    }


def build_backtest_request(body: dict[str, Any]) -> dict[str, Any]:
    """
    将验证/API 传参规范为引擎回测请求体（复用 EngineBacktestCreate 字段）。

    支持:
    - factor_code: 单因子快捷参数（factors 为空时生效）
    - use_full_data_range: 为 true 时自动填充 start/end 为库内全量 3 年
    - output_subdir: 验证输出子目录（相对 output/validation）
    """
    req = dict(body)
    ifind_path = req.get("ifind_config_path") or str(project_config.IFIND_CONFIG_PATH)
    ifind_cfg = load_ifind_config(ifind_path)

    factor_code = (req.pop("factor_code", None) or "").strip().upper()
    if factor_code and not req.get("factors"):
        req["factors"] = [{"code": factor_code}]

    if req.pop("use_full_data_range", False):
        years = float(req.pop("data_years", 3.0))
        span = query_db_data_range(ifind_cfg, years=years)
        req["start"] = span["start"]
        req["end"] = span["end"]
        req["_data_range"] = span

    if "start" not in req or "end" not in req:
        span = query_db_data_range(ifind_cfg, years=float(req.pop("data_years", 3.0)))
        req.setdefault("start", span["start"])
        req.setdefault("end", span["end"])
        req.setdefault("_data_range", span)

    subdir = req.pop("output_subdir", None)
    if subdir:
        req["output_dir"] = str(DEFAULT_VALIDATION_OUTPUT / subdir)
    elif factor_code and "output_dir" not in req:
        req["output_dir"] = str(DEFAULT_VALIDATION_OUTPUT / factor_code.lower())

    req.setdefault("run_single_factor_analysis", True)
    return req


def _check_report(
    report: dict[str, Any],
    factor_codes: list[str],
    report_dir: Path,
    run_id: str,
) -> list[dict[str, Any]]:
    """执行全链路检查项，返回 [{name, passed, detail}, ...]。"""
    run = report["run"]
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "passed": ok, "detail": detail})

    add("db_run_record", run.get("run_id") == run_id)
    for code in factor_codes:
        add(f"config_factor_{code}", code in str(run.get("factors_json", "")).upper())
    add("return_overview", report["return_overview"].get("total_return") is not None)
    add("risk_metrics", report["risk_metrics"].get("sharpe_ratio") is not None)
    add("nav_curve", len(report["nav_curve"]) > 0, str(len(report["nav_curve"])))
    hm = report["monthly_heatmap"]
    add(
        "monthly_heatmap",
        hm["available"] and len(hm["data"]) > 0,
        f"available={hm['available']} months={len(hm['data'])}",
    )
    add(
        "holding_profit_top10",
        len(report["holding_analysis"]["profit_top10"]) > 0,
        str(len(report["holding_analysis"]["profit_top10"])),
    )
    add(
        "holding_loss_top10",
        len(report["holding_analysis"]["loss_top10"]) > 0,
        str(len(report["holding_analysis"]["loss_top10"])),
    )
    add(
        "factor_attribution",
        len(report["factor_attribution"]) == len(factor_codes),
        ",".join(f["factor_code"] for f in report["factor_attribution"]),
    )
    add("stock_trades", len(report["trades"]) > 0, str(len(report["trades"])))

    base_files = [
        "summary.json", "portfolio.csv", "stock_trades.csv",
        "trades.csv", "performance.csv",
    ]
    for f in base_files:
        add(f"file_{f}", (report_dir / f).exists())
    for code in factor_codes:
        add(
            f"file_factor_analysis_{code}",
            (report_dir / "factor_analysis" / code / "analysis.json").exists(),
        )
    return checks


def validate_backtest_chain(body: dict[str, Any]) -> dict[str, Any]:
    """
    执行回测并验证全链路（因子分析 → 合成 → 回测 → 落库 → 报告文件）。

    Parameters
    ----------
    body : 与 POST /api/v1/engine/backtest/run-sync 相同，额外支持:
        factor_code, use_full_data_range, data_years, output_subdir

    Returns
    -------
    含 run_result, report, checks, passed, summary 的验证报告
    """
    timings: dict[str, float] = {}
    t_all = time.perf_counter()
    t_step = time.perf_counter()
    req = build_backtest_request(body)
    data_range = req.pop("_data_range", None)
    factor_codes = [
        str(f["code"]).upper() for f in (req.get("factors") or []) if f.get("code")
    ]
    timings["build_request_sec"] = round(time.perf_counter() - t_step, 2)

    t_step = time.perf_counter()
    run_result = engine_service.run_engine_backtest(req)
    timings["engine_sec"] = round(time.perf_counter() - t_step, 2)
    if run_result.get("timings"):
        timings["pipeline"] = run_result["timings"]
    run_id = run_result.get("run_id")
    if not run_id:
        raise RuntimeError("回测完成但未落库，请检查 database 配置")

    t_step = time.perf_counter()
    ifind_cfg = load_ifind_config(req.get("ifind_config_path") or str(project_config.IFIND_CONFIG_PATH))
    storage = BacktestStorage(ifind_cfg)
    report = storage.get_full_report(run_id)
    timings["db_verify_sec"] = round(time.perf_counter() - t_step, 2)
    timings["total_sec"] = round(time.perf_counter() - t_all, 2)
    if not report:
        raise RuntimeError(f"落库记录不存在: {run_id}")

    report_dir = Path(run_result["output_dir"])
    checks = _check_report(report, factor_codes, report_dir, run_id)
    passed = sum(1 for c in checks if c["passed"])
    total = len(checks)
    run = report["run"]

    return {
        "ok": passed == total,
        "passed": passed,
        "total": total,
        "checks": checks,
        "request": {k: v for k, v in req.items() if k != "ifind_config_path"},
        "data_range": data_range,
        "run_id": run_id,
        "run_result": run_result,
        "summary": {
            "run_id": run_id,
            "factors": factor_codes,
            "period": f"{run.get('start_date')} ~ {run.get('end_date')}",
            "trading_days": run.get("trading_days"),
            "total_return": _to_float(run.get("total_return")),
            "annualized_return": _to_float(run.get("annualized_return")),
            "excess_return": _to_float(run.get("excess_return")),
            "max_drawdown": _to_float(run.get("max_drawdown")),
            "sharpe_ratio": _to_float(run.get("sharpe_ratio")),
            "nav_points": len(report["nav_curve"]),
            "trades": len(report["trades"]),
            "heatmap_months": len(report["monthly_heatmap"]["data"]),
        },
        "factor_analyses": run_result.get("factor_analyses"),
        "sample_trade": report["trades"][0] if report["trades"] else None,
        "output_dir": str(report_dir),
        "timings": timings,
    }


def format_validation_report(result: dict[str, Any]) -> str:
    """人类可读的验证报告文本。"""
    lines = []
    for c in result["checks"]:
        status = "PASS" if c["passed"] else "FAIL"
        detail = f" — {c['detail']}" if c.get("detail") else ""
        lines.append(f"[{status}] {c['name']}{detail}")
    lines.append("")
    lines.append("=== Summary ===")
    lines.append(json.dumps(result["summary"], ensure_ascii=False, indent=2, default=str))
    lines.append("")
    lines.append(f"Result: {result['passed']}/{result['total']} checks passed")
    return "\n".join(lines)
