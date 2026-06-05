# -*- coding: utf-8 -*-
"""整合因子研究与 RQAlpha 回测结果，生成完整报告。"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from multi_factor import config

plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def _safe_read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    for enc in ("utf-8", "gbk", "utf-8-sig"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path)


def build_backtest_report(result: dict, output_dir: Path) -> Path:
    """从 run_func 返回值生成图表与汇总 Excel。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    analyser = result.get("sys_analyser") or result

    summary = analyser.get("summary", {})
    portfolio = analyser.get("portfolio")
    trades = analyser.get("trades")
    benchmark_pf = analyser.get("benchmark_portfolio")

    report_xlsx = output_dir / "full_backtest_report.xlsx"
    with pd.ExcelWriter(report_xlsx, engine="openpyxl") as writer:
        if summary:
            pd.DataFrame([summary]).T.rename(columns={0: "value"}).to_excel(
                writer, sheet_name="收益风险指标"
            )
        if portfolio is not None and not portfolio.empty:
            portfolio.to_excel(writer, sheet_name="组合净值")
        if trades is not None and not trades.empty:
            trades.to_excel(writer, sheet_name="成交明细")
        if benchmark_pf is not None and not benchmark_pf.empty:
            benchmark_pf.to_excel(writer, sheet_name="基准净值")

    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    if portfolio is not None and "unit_net_value" in portfolio.columns:
        _plot_nav_vs_benchmark(portfolio, benchmark_pf, output_dir / "nav_vs_benchmark.png")

    return report_xlsx


def merge_factor_and_backtest_report(
    factor_analysis_dir: Path,
    backtest_report_dir: Path,
    output_path: Path,
) -> Path:
    """合并因子检验 Excel 与回测报告为一份总报告。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        backtest_xlsx = backtest_report_dir / "full_backtest_report.xlsx"
        if backtest_xlsx.exists():
            xl = pd.ExcelFile(backtest_xlsx)
            for sheet in xl.sheet_names:
                xl.parse(sheet).to_excel(writer, sheet_name=f"回测_{sheet[:20]}")

        if factor_analysis_dir.exists():
            for sub in sorted(factor_analysis_dir.iterdir()):
                fa = sub / "factor_analysis.xlsx"
                if fa.exists():
                    xl = pd.ExcelFile(fa)
                    for sheet in xl.sheet_names:
                        name = f"{sub.name}_{sheet}"[:31]
                        xl.parse(sheet).to_excel(writer, sheet_name=name)

    return output_path


def _plot_nav_vs_benchmark(portfolio: pd.DataFrame, benchmark, save_path: Path):
    fig, ax = plt.subplots(figsize=(10, 5))
    nav = portfolio["unit_net_value"]
    if isinstance(nav.index, pd.RangeIndex) and "date" in portfolio.columns:
        idx = pd.to_datetime(portfolio["date"])
    else:
        idx = pd.to_datetime(portfolio.index)
    ax.plot(idx, nav.values, label="策略", linewidth=1.5)

    if benchmark is not None and not benchmark.empty:
        bnav = benchmark.get("unit_net_value", benchmark.iloc[:, 0])
        bidx = (
            pd.to_datetime(benchmark["date"])
            if "date" in benchmark.columns
            else pd.to_datetime(benchmark.index)
        )
        ax.plot(bidx, bnav.values, label="基准", linewidth=1.2, alpha=0.85)

    ax.set_title("策略 vs 基准 净值")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(save_path, dpi=120)
    plt.close(fig)


def print_summary(summary: dict) -> None:
    """控制台打印核心指标。"""
    keys = [
        ("total_returns", "总收益率"),
        ("annualized_returns", "年化收益率"),
        ("benchmark_total_returns", "基准总收益"),
        ("alpha", "Alpha"),
        ("beta", "Beta"),
        ("sharpe", "夏普比率"),
        ("max_drawdown", "最大回撤"),
        ("volatility", "波动率"),
        ("information_ratio", "信息比率"),
    ]
    print("\n========== 回测摘要 ==========")
    for k, label in keys:
        if k in summary:
            v = summary[k]
            if isinstance(v, float):
                print(f"  {label}: {v:.4f}")
            else:
                print(f"  {label}: {v}")
    print("==============================\n")
