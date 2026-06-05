# -*- coding: utf-8 -*-
"""回测与因子分析图表输出。"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _setup_chinese_font():
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def plot_nav_vs_benchmark(
    nav: pd.Series,
    benchmark_returns: pd.Series,
    output_path: Path,
    title: str = "策略净值 vs 基准 vs 超额",
    equity: pd.Series | None = None,
) -> None:
    _setup_chinese_font()
    bench_nav = (1 + benchmark_returns.reindex(nav.index).fillna(0)).cumprod()
    excess_nav = nav - bench_nav.reindex(nav.index).fillna(1.0) + 1.0
    fig, ax = plt.subplots(figsize=(12, 5))
    y_label = "总资产" if equity is not None else "单位净值"
    if equity is not None:
        initial = float(equity.iloc[0])
        bench_equity = bench_nav * initial
        bench_aligned = bench_equity.reindex(equity.index).ffill().fillna(initial)
        excess_line = equity - bench_aligned
        ax.plot(equity.index, equity.values, label="策略总资产", linewidth=1.5)
        ax.plot(bench_aligned.index, bench_aligned.values, label="基准总资产", alpha=0.85)
        ax.plot(excess_line.index, excess_line.values, label="超额收益", alpha=0.75, linestyle="--")
    else:
        ax.plot(nav.index, nav.values, label="策略净值", linewidth=1.5)
        ax.plot(bench_nav.index, bench_nav.values, label="基准净值", alpha=0.85)
        ax.plot(excess_nav.index, excess_nav.values, label="超额净值", alpha=0.75, linestyle="--")
    ax.set_xlabel("时间")
    ax.set_ylabel(y_label)
    ax.legend()
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def plot_ic_trend(ic_series: pd.Series, output_path: Path, title: str = "IC 走势") -> None:
    _setup_chinese_font()
    fig, ax = plt.subplots(figsize=(12, 4))
    colors = ["#c0392b" if v >= 0 else "#27ae60" for v in ic_series.fillna(0)]
    ax.bar(ic_series.index, ic_series.values, color=colors, width=1.0)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_title(title)
    ax.grid(True, alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def plot_quantile_nav(qret: pd.DataFrame, output_path: Path, title: str = "分层收益净值") -> None:
    """qret: index=date, columns=q1..qN 日收益。"""
    _setup_chinese_font()
    if qret.empty:
        return
    nav = (1 + qret.fillna(0)).cumprod()
    fig, ax = plt.subplots(figsize=(12, 5))
    for col in nav.columns:
        ax.plot(nav.index, nav[col], label=col)
    ax.legend()
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _monthly_return_color(value: float, vmax: float = 0.15) -> tuple[float, float, float, float]:
    """A 股惯例：正收益红色（越深越大），负收益绿色（越深越小）。"""
    if np.isnan(value):
        return (0.9, 0.9, 0.9, 1.0)
    if value >= 0:
        intensity = min(value / vmax, 1.0) if vmax > 0 else 0.0
        return (0.55 + 0.45 * intensity, 0.1, 0.1, 1.0)
    intensity = min(abs(value) / vmax, 1.0) if vmax > 0 else 0.0
    return (0.1, 0.45 + 0.45 * intensity, 0.15, 1.0)


def plot_monthly_return_heatmap(
    strategy_returns: pd.Series,
    output_path: Path,
    title: str = "月度收益热力图",
) -> None:
    _setup_chinese_font()
    r = strategy_returns.copy()
    r.index = pd.to_datetime(r.index)
    monthly = r.groupby([r.index.year, r.index.month]).apply(lambda x: (1 + x).prod() - 1)
    if monthly.empty or len(monthly) < 2:
        return
    years = sorted({y for y, _ in monthly.index})
    months = list(range(1, 13))
    mat = np.full((len(years), 12), np.nan)
    for (y, m), v in monthly.items():
        if y in years:
            mat[years.index(y), m - 1] = v
    vmax = max(0.05, float(np.nanmax(np.abs(mat))))
    color_mat = np.zeros((len(years), 12, 4))
    for yi in range(len(years)):
        for mi in range(12):
            color_mat[yi, mi] = _monthly_return_color(mat[yi, mi], vmax)
    fig, ax = plt.subplots(figsize=(12, max(3, len(years) * 0.4)))
    ax.imshow(color_mat, aspect="auto")
    ax.set_yticks(range(len(years)))
    ax.set_yticklabels(years)
    ax.set_xticks(range(12))
    ax.set_xticklabels(months)
    ax.set_title(title + "（红=盈利 绿=亏损）")
    for yi in range(len(years)):
        for mi in range(12):
            val = mat[yi, mi]
            if not np.isnan(val):
                ax.text(mi, yi, f"{val:.1%}", ha="center", va="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
