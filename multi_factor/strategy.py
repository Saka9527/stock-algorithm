# -*- coding: utf-8 -*-
"""RQAlpha 多因子选股策略：月度调仓、等权持仓。"""

import pickle
from pathlib import Path

import pandas as pd
from rqalpha.apis import *

from multi_factor import config


def _load_factor_scores(path: Path) -> pd.DataFrame:
    with open(path, "rb") as f:
        return pickle.load(f)


def _select_top_stocks(scores_row: pd.Series, top_n: int) -> list:
    valid = scores_row.dropna()
    if valid.empty:
        return []
    return valid.nlargest(top_n).index.tolist()


def init(context):
    scores_path = getattr(context, "factor_scores_path", None)
    if scores_path is None:
        scores_path = str(config.FACTOR_SCORES_PATH)
    context.factor_scores = _load_factor_scores(Path(scores_path))
    context.top_n = getattr(context, "top_n", config.TOP_N)
    context.target_weight = config.TARGET_GROSS_EXPOSURE / max(context.top_n, 1)

    all_ids = context.factor_scores.columns.tolist()
    update_universe(all_ids)
    subscribe(all_ids)

    scheduler.run_monthly(rebalance, tradingday=config.REBALANCE_TRADINGDAY)
    logger.info(
        "多因子策略初始化: top_n={}, 得分矩阵 {} x {}".format(
            context.top_n,
            context.factor_scores.shape[0],
            context.factor_scores.shape[1],
        )
    )


def rebalance(context, bar_dict):
    dt = pd.Timestamp(context.now.date())
    scores = context.factor_scores
    if dt not in scores.index:
        idx = scores.index[scores.index <= dt]
        if len(idx) == 0:
            return
        dt = idx[-1]

    row = scores.loc[dt]
    targets = _select_top_stocks(row, context.top_n)
    if not targets:
        return

    target_set = set(targets)
    positions = get_positions()
    if isinstance(positions, dict):
        held = list(positions.keys())
    else:
        held = [p.order_book_id for p in positions]
    for obid in held:
        if obid not in target_set:
            order_target_percent(obid, 0)

    for obid in targets:
        if is_suspended(obid):
            continue
        if obid not in bar_dict or not is_valid_price(bar_dict[obid].close):
            continue
        order_target_percent(obid, context.target_weight)

    logger.info("{} 调仓完成, 持仓 {} 只".format(context.now.date(), len(targets)))


def handle_bar(context, bar_dict):
    pass


def get_backtest_config(
    start_date: str | None = None,
    end_date: str | None = None,
    factor_scores_path: str | None = None,
) -> dict:
    """构建 RQAlpha 回测配置（含滑点、手续费、基准）。"""
    start_date = start_date or config.START_DATE
    end_date = end_date or config.END_DATE
    scores_path = factor_scores_path or str(config.FACTOR_SCORES_PATH)

    return {
        "base": {
            "start_date": start_date,
            "end_date": end_date,
            "benchmark": config.BENCHMARK,
            "accounts": {"stock": config.INITIAL_CASH},
            "frequency": "1d",
        },
        "extra": {
            "log_level": "info",
            "context_vars": {
                "factor_scores_path": scores_path,
                "top_n": config.TOP_N,
            },
        },
        "mod": {
            "sys_simulation": {
                "enabled": True,
                "slippage_model": config.SLIPPAGE_MODEL,
                "slippage": config.SLIPPAGE,
            },
            "sys_transaction_cost": {
                "enabled": True,
                "commission_multiplier": config.COMMISSION_MULTIPLIER,
                "stock_min_commission": config.STOCK_MIN_COMMISSION,
            },
            "sys_analyser": {
                "enabled": True,
                "plot": False,
                "benchmark": config.BENCHMARK,
                "record": True,
                "strategy_name": "multi_factor",
                "output_file": str(config.OUTPUT_DIR / "backtest_result.pkl"),
                "report_save_path": str(config.BACKTEST_REPORT_DIR),
            },
            "sys_scheduler": {
                "enabled": True,
            },
        },
    }
