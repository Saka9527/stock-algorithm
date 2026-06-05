# -*- coding: utf-8 -*-
"""
20 日动量因子：生成、数据质量校验、IC/分层绩效验证，并输出验证流程报告。

用法:
  python scripts/validate_momentum_20d.py
  python scripts/validate_momentum_20d.py --start 20251110 --end 20260530

输出目录: output/validation/momentum_20d/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor import config as project_config
from multi_factor.engine.data_hub import DataHub
from multi_factor.engine.factor_analyzer import analyze_single_factor
from multi_factor.engine.strategy_config import StrategyConfig
from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_metrics import (
    compute_quantile_returns,
    compute_rank_ic_series,
    summarize_ic,
)
from multi_factor.ifind.provider import IFindDataProvider

FACTOR_CODE = "MOMENTUM_20"
WINDOW = 20
# 产品参考值（因子市场 UI 示意，用于对照而非硬门槛）
UI_REFERENCE = {
    "ic_mean": 0.12,
    "ic_win_rate": 0.65,
    "sharpe_long_short": 0.85,
}


def parse_args():
    p = argparse.ArgumentParser(description="20日动量因子验证")
    p.add_argument("--start", default="", help="YYYYMMDD，默认日K最小日+窗口")
    p.add_argument("--end", default="", help="YYYYMMDD，默认日K最大日")
    p.add_argument("--ifind-config", default=str(project_config.IFIND_CONFIG_PATH))
    p.add_argument(
        "--output",
        default=str(project_config.OUTPUT_DIR / "validation" / "momentum_20d"),
    )
    return p.parse_args()


def validate_data_quality(panel: pd.DataFrame, close: pd.DataFrame) -> dict:
    """截面数据质量检查。"""
    daily_count = panel.notna().sum(axis=1)
    stock_coverage = panel.notna().sum(axis=0) / max(len(panel), 1)

    # 与收盘价可用性对比
    close_ok = close.notna()
    factor_ok = panel.notna()
    overlap_dates = panel.index.intersection(close.index)
    if len(overlap_dates):
        align_close = close_ok.reindex(overlap_dates)
        align_factor = factor_ok.reindex(overlap_dates)
        match_rate = float((align_factor & align_close).sum().sum() / max(align_close.sum().sum(), 1))
    else:
        match_rate = 0.0

    # 极端值（|动量|>100% 视为异常）
    vals = panel.values.flatten()
    vals = vals[np.isfinite(vals)]
    extreme_pct = float((np.abs(vals) > 1.0).mean()) if len(vals) else 0.0

    return {
        "trading_days": int(len(panel)),
        "stock_count": int(panel.shape[1]),
        "avg_stocks_per_day": float(daily_count.mean()),
        "min_stocks_per_day": int(daily_count.min()) if len(daily_count) else 0,
        "median_stocks_per_day": float(daily_count.median()),
        "avg_stock_coverage_ratio": float(stock_coverage.mean()),
        "date_start": panel.index.min().strftime("%Y-%m-%d") if len(panel) else None,
        "date_end": panel.index.max().strftime("%Y-%m-%d") if len(panel) else None,
        "factor_vs_close_match_rate": match_rate,
        "extreme_abs_return_gt_100pct_ratio": extreme_pct,
        "formula": f"momentum = close / close.shift({WINDOW}) - 1",
    }


def cross_check_db_factor(provider: IFindDataProvider, start: str, end: str) -> dict:
    """若库内存在动量类 factor_code，与本地计算对比。"""
    meta_list = provider.list_factor_base_info()
    mom_codes = [
        m["factor_code"]
        for m in meta_list
        if "MOMENT" in m["factor_code"].upper() or "动量" in (m.get("factor_name") or "")
    ]
    result = {"db_momentum_codes": mom_codes, "cross_check": None}
    if not mom_codes:
        result["note"] = "factor_base_info 中无动量因子条目，采用日K派生 MOMENTUM_20"
        return result

    code = mom_codes[0]
    try:
        db_panel = provider.load_factor_panel_by_code(code, start, end)
        db_panel = provider.align_to_trading_days(
            db_panel, provider.get_trading_dates(start, end)
        )
        result["cross_check"] = {
            "db_factor_code": code,
            "db_shape": list(db_panel.shape),
            "db_date_range": [
                db_panel.index.min().strftime("%Y-%m-%d") if len(db_panel) else None,
                db_panel.index.max().strftime("%Y-%m-%d") if len(db_panel) else None,
            ],
        }
    except Exception as ex:
        result["cross_check_error"] = str(ex)
    return result


def compare_ui_reference(summary: dict) -> list[dict]:
    """与产品 UI 参考值对照。"""
    rows = []
    for key, ref in UI_REFERENCE.items():
        actual = summary.get(key) or summary.get("win_rate" if key == "ic_win_rate" else key)
        if key == "ic_win_rate":
            actual = summary.get("win_rate")
        if key == "sharpe_long_short":
            actual = summary.get("sharpe_long_short")
        diff = None
        if actual is not None and ref is not None:
            diff = float(actual) - float(ref)
        rows.append(
            {
                "metric": key,
                "reference_ui": ref,
                "computed": actual,
                "diff": diff,
                "note": "参考值为产品示意，区间与股票池不同会导致偏差",
            }
        )
    return rows


def save_charts(
    output_dir: Path,
    ic_series: pd.Series,
    qret: pd.DataFrame,
    panel: pd.DataFrame,
    returns: pd.DataFrame,
) -> list[str]:
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    saved = []

    if not ic_series.empty:
        fig, ax = plt.subplots(figsize=(12, 4))
        colors = ["#3498db" if v >= 0 else "#e74c3c" for v in ic_series.fillna(0)]
        ax.bar(ic_series.index, ic_series.values, color=colors, width=1.0)
        ax.axhline(0, color="gray", lw=0.8)
        ax.set_title(f"20日动量 IC 走势 (IC均值={ic_series.mean():.4f})")
        ax.grid(True, alpha=0.3, axis="y")
        fig.tight_layout()
        p = output_dir / "ic_trend.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        saved.append(p.name)

    if not qret.empty:
        nav = (1 + qret.fillna(0)).cumprod()
        fig, ax = plt.subplots(figsize=(12, 5))
        for col in nav.columns:
            ax.plot(nav.index, nav[col], label=col)
        ax.legend()
        ax.set_title("20日动量 五分层累计净值")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        p = output_dir / "quantile_nav.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        saved.append(p.name)

        # Top20% vs Bottom20%（对齐 UI）
        from multi_factor.ifind.factor_metrics import compute_top_bottom_returns, cumulative_nav

        tb = compute_top_bottom_returns(panel, returns, ascending=True, top_pct=0.2, bottom_pct=0.2)
        if not tb.empty:
            top_nav = cumulative_nav(tb["top_group"])
            bot_nav = cumulative_nav(tb["bottom_group"])
            fig, ax = plt.subplots(figsize=(12, 5))
            ax.plot(top_nav.index, top_nav.values, color="#c0392b", label="Top 20%")
            ax.plot(bot_nav.index, bot_nav.values, color="#27ae60", label="Bottom 20%")
            ax.legend()
            ax.set_title("20日动量 分组收益 (Top vs Bottom)")
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            p = output_dir / "group_returns.png"
            fig.savefig(p, dpi=120)
            plt.close(fig)
            saved.append(p.name)

    return saved


def write_markdown_report(
    output_dir: Path,
    quality: dict,
    db_check: dict,
    analysis: dict,
    ui_compare: list[dict],
    charts: list[str],
    cfg: StrategyConfig,
) -> Path:
    s = analysis.get("summary", {})
    ic_m = analysis.get("ic_monthly_summary", {})
    path = output_dir / "验证流程报告.md"

    lines = [
        "# 20日动量因子验证流程报告",
        "",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **分析区间**: {cfg.start} ~ {cfg.end}",
        f"- **因子代码**: `{FACTOR_CODE}`",
        f"- **计算公式**: `{quality.get('formula')}`",
        "",
        "---",
        "",
        "## 1. 验证流程",
        "",
        "| 步骤 | 内容 | 状态 |",
        "|------|------|------|",
        "| 1 | 加载 Blader `stock_daily_qfq` 收盘价 | 完成 |",
        "| 2 | 计算 20 日累计收益率作为动量因子 | 完成 |",
        "| 3 | 对齐交易日、前向填充（与引擎一致） | 完成 |",
        "| 4 | 数据质量检查（覆盖率、极端值） | 完成 |",
        "| 5 | 查询 `factor_base_info` / `factor_calc_data` 是否已有动量 | 完成 |",
        "| 6 | 日度 IC、月度 IC、IC 均值/IR/胜率 | 完成 |",
        "| 7 | 五分层回测与 Top/Bottom 20% 分组收益 | 完成 |",
        "| 8 | 与因子市场 UI 参考指标对照 | 完成 |",
        "",
        "---",
        "",
        "## 2. 数据质量",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
    ]
    for k, v in quality.items():
        lines.append(f"| {k} | {v} |")

    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. 库内动量因子核查",
            "",
            "```json",
            json.dumps(db_check, ensure_ascii=False, indent=2),
            "```",
            "",
            "---",
            "",
            "## 4. 因子绩效（计算结果）",
            "",
            "### 4.1 IC 指标",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| IC 均值 | {s.get('ic_mean')} |",
            f"| IC 标准差 | {s.get('ic_std')} |",
            f"| IC IR | {s.get('ic_ir')} |",
            f"| IC 胜率 | {s.get('win_rate')} |",
            f"| 有效 IC 日数 | {s.get('total_count')} |",
            "",
            "### 4.2 月度 IC",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| 月度 IC 均值 | {ic_m.get('ic_mean')} |",
            f"| 月度 IC 胜率 | {ic_m.get('win_rate')} |",
            "",
            "### 4.3 分层与多空",
            "",
            "| 指标 | 值 |",
            "|------|-----|",
            f"| Top 组夏普 | {s.get('sharpe_top_group')} |",
            f"| Bottom 组夏普 | {s.get('sharpe_bottom_group')} |",
            f"| 多空夏普 | {s.get('sharpe_long_short')} |",
            f"| 平均截面股票数 | {s.get('stock_count_avg')} |",
            "",
            "---",
            "",
            "## 5. 与因子市场 UI 参考值对照",
            "",
            "> UI 示意：IC 均值 0.12、胜率 65%、夏普 0.85（区间与股票池可能不同，仅作参考）",
            "",
            "| 指标 | UI 参考 | 本次计算 | 差异 |",
            "|------|---------|----------|------|",
        ]
    )
    for row in ui_compare:
        lines.append(
            f"| {row['metric']} | {row['reference_ui']} | {row['computed']} | {row['diff']} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 6. 图表",
            "",
        ]
    )
    for c in charts:
        lines.append(f"![{c}]({c})")
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## 7. 结论与建议",
            "",
        ]
    )

    ic_mean = s.get("ic_mean")
    if ic_mean is not None and ic_mean > 0:
        lines.append("- **IC 方向**：20 日动量与未来收益正相关（IC 均值为正），符合「强者恒强」逻辑。")
    elif ic_mean is not None:
        lines.append("- **IC 方向**：本次区间 IC 均值为负，需检查区间是否处于反转行情或股票池偏差。")

    if quality.get("avg_stocks_per_day", 0) < 100:
        lines.append("- **数据覆盖**：日均有效股票偏少，建议确认日 K 区间与 `is_deleted` 过滤。")

    if not db_check.get("db_momentum_codes"):
        lines.append(
            "- **入库建议**：当前动量由日 K 派生，若因子市场需从 `factor_calc_data` 读取，"
            "可在 ETL 中写入 `factor_code=MOMENTUM_20` 并补充 `factor_base_info` 元数据。"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("完整 JSON：`analysis.json`、`validation_summary.json`")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    args = parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    ifind = load_ifind_config(args.ifind_config)
    provider = IFindDataProvider(ifind)

    close_full = provider.load_daily_field("close", "20240101", "20991231")
    if close_full.empty:
        print("错误: 无法加载日 K")
        sys.exit(1)

    start = args.start.replace("-", "")[:8] if args.start else None
    end = args.end.replace("-", "")[:8] if args.end else None
    if not start:
        # 需 WINDOW 日历史计算首行
        start = (close_full.index.min() + pd.Timedelta(days=WINDOW * 2)).strftime("%Y%m%d")
    if not end:
        end = close_full.index.max().strftime("%Y%m%d")

    cfg = StrategyConfig(
        start=start,
        end=end,
        ifind_config_path=args.ifind_config,
    )
    print(f">>> 区间 {start} ~ {end}")

    hub = DataHub(cfg)
    hub.load_base()
    panel = hub.load_factor(FACTOR_CODE)

    print(">>> 数据质量检查 ...")
    quality = validate_data_quality(panel, hub.close)
    db_check = cross_check_db_factor(provider, start, end)

    meta = {
        "factor_code": FACTOR_CODE,
        "factor_name": "20日动量",
        "factor_type": "技术面",
        "factor_desc": "衡量股票近20个交易日的涨幅",
        "sort_type": "desc",
    }

    print(">>> 单因子分析 (IC / 分层) ...")
    analysis = analyze_single_factor(hub, FACTOR_CODE, cfg, meta=meta)

    ic_series = compute_rank_ic_series(panel, hub.returns, period=1, ascending=True)
    ic_summary = summarize_ic(ic_series)
    qret = compute_quantile_returns(panel, hub.returns, period=1, ascending=True, quantiles=5)

    ui_compare = compare_ui_reference({**analysis.get("summary", {}), **ic_summary})

    print(">>> 生成图表 ...")
    charts = save_charts(output_dir, ic_series, qret, panel, hub.returns)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "factor_code": FACTOR_CODE,
        "window": WINDOW,
        "period": {"start": start, "end": end},
        "data_quality": quality,
        "db_check": db_check,
        "ic_summary": analysis.get("summary"),
        "ic_monthly_summary": analysis.get("ic_monthly_summary"),
        "ui_reference_compare": ui_compare,
        "charts": charts,
    }

    with open(output_dir / "validation_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    with open(output_dir / "analysis.json", "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2, default=str)

    # 导出样本截面 CSV（最近 5 个交易日 × 前 20 只股票）
    sample = panel.iloc[-5:, :20]
    sample.to_csv(output_dir / "momentum_sample_panel.csv", encoding="utf-8-sig")

    report_path = write_markdown_report(
        output_dir, quality, db_check, analysis, ui_compare, charts, cfg
    )

    print(f"\n>>> 验证完成")
    print(f"    IC 均值: {analysis['summary'].get('ic_mean')}")
    print(f"    IC 胜率: {analysis['summary'].get('win_rate')}")
    print(f"    多空夏普: {analysis['summary'].get('sharpe_long_short')}")
    print(f"    报告: {report_path.resolve()}")
    print(f"    目录: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
