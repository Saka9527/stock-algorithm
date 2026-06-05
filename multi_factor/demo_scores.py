# -*- coding: utf-8 -*-
"""无 RQDatac 时基于 RQAlpha 行情生成演示用因子得分（仅动量 proxy）。"""

import numpy as np
import pandas as pd
from rqalpha import run_func
from rqalpha.apis import *


def _collect_momentum_scores(start: str, end: str, stock_count: int = 80) -> pd.DataFrame:
    store = {}

    def init(ctx):
        ctx._rows = []
        ins = all_instruments("CS", start)
        ctx.stocks = ins.order_book_id.head(stock_count).tolist()
        update_universe(ctx.stocks)
        subscribe(ctx.stocks)
        ctx.window = 20

    def handle_bar(ctx, bar_dict):
        dt = ctx.now.date()
        for obid in ctx.stocks:
            if obid not in bar_dict:
                continue
            bars = history_bars(obid, ctx.window + 1, "1d", "close")
            if bars is None or len(bars) < ctx.window + 1:
                continue
            mom = float(bars[-1] / bars[0] - 1.0)
            store.setdefault(dt, {})[obid] = mom

    cfg = {
        "base": {
            "start_date": start,
            "end_date": end,
            "accounts": {"stock": 100000},
            "frequency": "1d",
        },
        "mod": {"sys_analyser": {"enabled": False}},
    }
    run_func(init=init, handle_bar=handle_bar, config=cfg)

    panel = pd.DataFrame(store).T.sort_index()
    panel.index = pd.to_datetime(panel.index)
    return panel.rank(axis=1, pct=True)


def _to_rq_date(s: str) -> str:
    s = s.replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def generate_demo_scores(start_date: str, end_date: str, save_path) -> pd.DataFrame:
    """生成演示得分矩阵并保存。"""
    df = _collect_momentum_scores(_to_rq_date(start_date), _to_rq_date(end_date))
    save_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(save_path)
    return df
