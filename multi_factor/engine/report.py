# -*- coding: utf-8 -*-
"""导出 CSV / JSON / 图表 / HTML 回测报告。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from multi_factor.engine.backtest_analytics import monthly_heatmap_eligible
from multi_factor.engine.visualization import (
    plot_ic_trend,
    plot_monthly_return_heatmap,
    plot_nav_vs_benchmark,
    plot_quantile_nav,
)
from multi_factor.ifind.factor_metrics import compute_quantile_returns, compute_rank_ic_series
from multi_factor.ifind.factor_metrics import _sort_ascending


def save_backtest_outputs(
    bt_result: dict,
    perf: dict,
    output_dir: Path,
    benchmark_returns: pd.Series,
    start: str | None = None,
    end: str | None = None,
) -> Path:
    """保存回测 CSV 与净值图。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    bt_result["portfolio"].to_csv(output_dir / "portfolio.csv", encoding="utf-8-sig")
    if not bt_result["trades"].empty:
        bt_result["trades"].to_csv(output_dir / "trades.csv", encoding="utf-8-sig", index=False)
    stock_trades = bt_result.get("stock_trades") or []
    if stock_trades:
        pd.DataFrame(stock_trades).to_csv(
            output_dir / "stock_trades.csv", encoding="utf-8-sig", index=False
        )
    if not bt_result["positions"].empty:
        bt_result["positions"].to_csv(output_dir / "positions.csv", encoding="utf-8-sig", index=False)

    pd.DataFrame([perf]).T.to_csv(output_dir / "performance.csv", encoding="utf-8-sig", header=["value"])

    nav = bt_result["nav"]
    plot_nav_vs_benchmark(
        nav,
        benchmark_returns,
        output_dir / "nav_vs_benchmark.png",
        equity=bt_result["portfolio"]["equity"],
    )
    heatmap_ok = True
    heatmap_note = ""
    if start and end:
        heatmap_ok, heatmap_note = monthly_heatmap_eligible(start, end)
    if heatmap_ok:
        plot_monthly_return_heatmap(
            bt_result["strategy_returns"], output_dir / "monthly_return_heatmap.png"
        )
    else:
        note_path = output_dir / "monthly_heatmap_note.txt"
        note_path.write_text(heatmap_note, encoding="utf-8")
    return output_dir


def save_factor_analysis_outputs(
    factor_code: str,
    analysis: dict,
    hub_returns: pd.DataFrame,
    hub_panel: pd.DataFrame,
    output_dir: Path,
) -> None:
    """单因子 IC 图、分层图。"""
    fac_dir = output_dir / "factor_analysis" / factor_code
    fac_dir.mkdir(parents=True, exist_ok=True)

    meta = analysis.get("meta", {})
    ascending = analysis.get("ascending", _sort_ascending(meta.get("sort_type")))
    ic_series = compute_rank_ic_series(
        hub_panel, hub_returns, period=analysis.get("period", 1), ascending=ascending
    )
    if not ic_series.empty:
        plot_ic_trend(ic_series, fac_dir / "ic_trend.png", title=f"{factor_code} IC 走势")

    qret = compute_quantile_returns(
        hub_panel,
        hub_returns,
        period=analysis.get("period", 1),
        ascending=ascending,
        quantiles=5,
    )
    if not qret.empty:
        plot_quantile_nav(qret, fac_dir / "quantile_nav.png", title=f"{factor_code} 分层净值")

    with open(fac_dir / "analysis.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2, default=str)


def write_html_report(
    perf: dict,
    cfg_summary: dict,
    output_dir: Path,
) -> Path:
    """简易 HTML 回测摘要。"""
    html_path = output_dir / "backtest_report.html"
    rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in {**cfg_summary, **perf}.items()
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>多因子回测报告</title></head>
<body>
<h1>多因子策略回测报告</h1>
<table border="1" cellpadding="6">{rows}</table>
<h2>图表</h2>
<ul>
<li><a href="nav_vs_benchmark.png">净值 vs 基准</a></li>
<li><a href="monthly_return_heatmap.png">月度收益热力图</a></li>
</ul>
</body></html>"""
    html_path.write_text(html, encoding="utf-8")
    return html_path
