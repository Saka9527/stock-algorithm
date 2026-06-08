# -*- coding: utf-8 -*-
"""factor_data_wide 宽表：factor_code 与列映射、读写辅助。"""

from __future__ import annotations

import json
from typing import Any

from multi_factor.ifind.code_convert import normalize_code, rq_to_ths

# factor_calc_data.factor_code -> factor_data_wide 物理列（非 JSON）
WIDE_COLUMN_BY_FACTOR_CODE: dict[str, str] = {
    "PE_TTM": "pe_ttm",
    "PB": "pb_mrq",
    "PB_MRQ": "pb_mrq",
    "PS_TTM": "ps_ttm",
    "PCF_NCF_TTM": "pcf_ncf_ttm",
    "ROE_TTM": "roe_ttm",
    "MOMENTUM_20": "mom_20d",
    "MOMENTUM": "mom_20d",
    "MAIN_NET_INFLOW_RATIO": "net_inflow_5d",
    "MAIN_NET_INFLOW": "net_inflow_5d",
    "CURRENT_MV": "float_cap",
    "MARKET_VALUE": "total_cap",
}

# 无专用列时写入 factor_ext_json
EXT_FACTOR_CODES: frozenset[str] = frozenset(
    {
        "PE",
        "PEG_LYR",
        "ROE",
        "ROE_YOY",
        "GROSS_MARGIN",
        "NET_MARGIN",
    }
)

WIDE_TABLE = "factor_data_wide"
WIDE_DEDICATED_COLUMNS: frozenset[str] = frozenset(WIDE_COLUMN_BY_FACTOR_CODE.values())


def wide_column_for_factor(factor_code: str) -> str | None:
    code = str(factor_code).upper()
    if code in WIDE_COLUMN_BY_FACTOR_CODE:
        return WIDE_COLUMN_BY_FACTOR_CODE[code]
    if code.startswith("MOMENTUM_"):
        return "mom_20d" if code in ("MOMENTUM_20", "MOMENTUM") else None
    return None


def is_ext_factor(factor_code: str) -> bool:
    code = str(factor_code).upper()
    return code in EXT_FACTOR_CODES and code not in WIDE_COLUMN_BY_FACTOR_CODE


def storage_stock_code(code: str) -> str:
    """宽表统一 THS 代码（与 stock_daily_qfq 一致）。"""
    return rq_to_ths(normalize_code(code, "rq"))


def long_rows_to_wide_records(rows: list[dict]) -> list[dict]:
    """
    将 factor_calc_data 长表行聚合为 factor_data_wide 行。
    输入行需含 stock_code, data_date, factor_code, factor_value。
    """
    bucket: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        fc = str(row.get("factor_code", "")).upper()
        if not fc:
            continue
        sym = storage_stock_code(str(row["stock_code"]))
        dt = str(row["data_date"])[:10]
        key = (dt, sym)
        rec = bucket.setdefault(
            key,
            {"data_date": dt, "stock_code": sym, "factor_ext_json": None},
        )
        val = row.get("factor_value")
        if val is None:
            continue
        col = wide_column_for_factor(fc)
        if col:
            rec[col] = val
        elif is_ext_factor(fc) or fc not in WIDE_COLUMN_BY_FACTOR_CODE:
            ext = rec.get("_ext") or {}
            ext[fc] = val
            rec["_ext"] = ext
    out: list[dict] = []
    for rec in bucket.values():
        ext = rec.pop("_ext", None)
        if ext:
            rec["factor_ext_json"] = json.dumps(ext, ensure_ascii=False)
        out.append(rec)
    return out


def factor_codes_with_columns() -> list[str]:
    codes = list(WIDE_COLUMN_BY_FACTOR_CODE.keys())
    codes.extend(sorted(EXT_FACTOR_CODES))
    return sorted(set(codes))


_WIDE_UPSERT_COLS = (
    "close",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "pcf_ncf_ttm",
    "roe_ttm",
    "mom_20d",
    "turnover_20d",
    "net_inflow_5d",
    "float_cap",
    "total_cap",
)


def normalize_wide_record(rec: dict) -> dict:
    """补齐宽表 upsert 参数字段（未赋值列用 None）。"""
    out = {
        "data_date": rec["data_date"],
        "stock_code": rec["stock_code"],
        "factor_ext_json": rec.get("factor_ext_json"),
    }
    for col in _WIDE_UPSERT_COLS:
        out[col] = rec.get(col)
    return out


def _wide_upsert_sql() -> str:
    col_list = ", ".join(f"`{c}`" for c in _WIDE_UPSERT_COLS)
    val_list = ", ".join(f":{c}" for c in _WIDE_UPSERT_COLS)
    updates = [f"`{c}` = COALESCE(VALUES(`{c}`), `{c}`)" for c in _WIDE_UPSERT_COLS]
    updates.append(
        "factor_ext_json = JSON_MERGE_PATCH("
        "COALESCE(factor_ext_json, JSON_OBJECT()), "
        "COALESCE(CAST(VALUES(factor_ext_json) AS JSON), JSON_OBJECT())"
        ")"
    )
    updates.append("update_time = NOW()")
    return f"""
    INSERT INTO `{WIDE_TABLE}` (
      data_date, stock_code, {col_list}, factor_ext_json, update_time
    ) VALUES (
      :data_date, :stock_code, {val_list}, :factor_ext_json, NOW()
    )
    ON DUPLICATE KEY UPDATE {", ".join(updates)}
    """


_BAOSTOCK_OVERWRITE_COLS = ("close", "pe_ttm", "pb_mrq", "ps_ttm", "pcf_ncf_ttm")
_BAOSTOCK_EXTRAS_OVERWRITE_COLS = (
    "float_cap",
    "total_cap",
    "is_st",
    "is_suspended",
    "turnover_20d",
)


def upsert_wide_extras_overwrite(engine, records: list[dict], chunk_size: int = 1000) -> int:
    """BaoStock 扩展字段增量覆盖写入 factor_data_wide。"""
    if not records:
        return 0
    col_list = ", ".join(f"`{c}`" for c in _BAOSTOCK_EXTRAS_OVERWRITE_COLS)
    val_list = ", ".join(f":{c}" for c in _BAOSTOCK_EXTRAS_OVERWRITE_COLS)
    updates = [f"`{c}` = VALUES(`{c}`)" for c in _BAOSTOCK_EXTRAS_OVERWRITE_COLS]
    updates.append("update_time = NOW()")
    sql = f"""
    INSERT INTO `{WIDE_TABLE}` (
      data_date, stock_code, {col_list}, update_time
    ) VALUES (
      :data_date, :stock_code, {val_list}, NOW()
    )
    ON DUPLICATE KEY UPDATE {", ".join(updates)}
    """
    from sqlalchemy import text

    n = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i : i + chunk_size]
        with engine.begin() as conn:
            conn.execute(text(sql), chunk)
        n += len(chunk)
    return n


def upsert_wide_baostock_overwrite(engine, records: list[dict], chunk_size: int = 1000) -> int:
    """BaoStock 增量覆盖：指定列用新值覆盖（NULL 也写入）。"""
    if not records:
        return 0
    col_list = ", ".join(f"`{c}`" for c in _BAOSTOCK_OVERWRITE_COLS)
    val_list = ", ".join(f":{c}" for c in _BAOSTOCK_OVERWRITE_COLS)
    updates = [f"`{c}` = VALUES(`{c}`)" for c in _BAOSTOCK_OVERWRITE_COLS]
    updates.append("update_time = NOW()")
    sql = f"""
    INSERT INTO `{WIDE_TABLE}` (
      data_date, stock_code, {col_list}, update_time
    ) VALUES (
      :data_date, :stock_code, {val_list}, NOW()
    )
    ON DUPLICATE KEY UPDATE {", ".join(updates)}
    """
    from sqlalchemy import text

    n = 0
    for i in range(0, len(records), chunk_size):
        chunk = records[i : i + chunk_size]
        with engine.begin() as conn:
            conn.execute(text(sql), chunk)
        n += len(chunk)
    return n


def upsert_wide_records(engine, records: list[dict], chunk_size: int = 2000) -> int:
    if not records:
        return 0
    sql = _wide_upsert_sql()
    params = [normalize_wide_record(r) for r in records]
    n = 0
    from sqlalchemy import text

    for i in range(0, len(params), chunk_size):
        chunk = params[i : i + chunk_size]
        with engine.begin() as conn:
            conn.execute(text(sql), chunk)
        n += len(chunk)
    return n
