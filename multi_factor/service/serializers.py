# -*- coding: utf-8 -*-
"""Pandas / NumPy -> JSON 可序列化结构。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _sanitize_scalar(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        if math.isnan(v) or math.isfinite(v) is False:
            return None
        return float(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    if isinstance(v, (list, tuple)):
        return [_sanitize_scalar(x) for x in v]
    return v


def dataframe_to_records(df: pd.DataFrame) -> list[dict]:
    out = df.reset_index()
    records = out.to_dict(orient="records")
    for row in records:
        for k, v in list(row.items()):
            row[k] = _sanitize_scalar(v)
    return records


def series_to_records(s: pd.Series) -> list[dict]:
    return dataframe_to_records(s.to_frame(name=s.name or "value"))


def panel_to_split_json(df: pd.DataFrame) -> dict:
    """宽表 -> {dates, symbols, values}，便于前端/其他服务消费。"""
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    symbols = [str(c) for c in df.columns]
    values = []
    for _, row in df.iterrows():
        values.append([_sanitize_scalar(v) for v in row.tolist()])
    return {"dates": dates, "symbols": symbols, "values": values}


def summary_dict_to_json(summary: dict) -> dict:
    return {k: _sanitize_scalar(v) for k, v in summary.items()}
