# -*- coding: utf-8 -*-
"""指数成分股：DB / BaoStock / 中证指数官网。"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.request import urlopen

import pandas as pd

from multi_factor.ifind.code_convert import normalize_codes
from multi_factor.engine.strategy_config import UNIVERSE_INDEX_CODES

if TYPE_CHECKING:
    from multi_factor.ifind.provider import IFindDataProvider

logger = logging.getLogger(__name__)

# 指数 THS 代码 -> 数据源
BAOSTOCK_INDEX_API: dict[str, str] = {
    "000300.SH": "hs300",
    "000905.SH": "zz500",
}

# 中证指数官网 cons 文件代码（无 .SH 后缀）
CSINDEX_CONS_CODE: dict[str, str] = {
    "000300.SH": "000300",
    "000905.SH": "000905",
    "000852.SH": "000852",
}

CSINDEX_CONS_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{code}cons.xls"
)

_MASK_CACHE: dict[tuple[str, str, str], pd.DataFrame] = {}


def pool_to_index_code(pool: str) -> str:
    return UNIVERSE_INDEX_CODES.get(pool, "")


def _norm_date(s) -> pd.Timestamp:
    return pd.to_datetime(s).normalize()


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _month_end_dates(trading_dates: pd.DatetimeIndex) -> list[pd.Timestamp]:
    if trading_dates.empty:
        return []
    s = pd.Series(trading_dates, index=trading_dates)
    return [pd.Timestamp(d) for d in s.groupby(s.index.to_period("M")).max().tolist()]


def _parse_csindex_codes(df: pd.DataFrame) -> tuple[pd.Timestamp, list[str]]:
    if df.empty:
        raise ValueError("中证指数成分文件为空")
    date_col = df.columns[0]
    code_col = next(c for c in df.columns if "Constituent Code" in str(c))
    ex_col = next(
        c for c in df.columns if ("交易所" in str(c) or str(c).startswith("Exchange")) and "Eng" not in str(c)
    )
    raw_date = str(df.iloc[0][date_col]).replace("-", "")[:8]
    as_of = pd.to_datetime(raw_date).normalize()
    codes: list[str] = []
    for _, row in df.iterrows():
        num = str(int(row[code_col])).zfill(6)
        exch = str(row[ex_col])
        suffix = "SH" if "上海" in exch or "Shanghai" in exch else "SZ"
        codes.append(f"{num}.{suffix}")
    return as_of, sorted(set(codes))


def fetch_csindex_members(index_code: str) -> tuple[pd.Timestamp, list[str]]:
    cons = CSINDEX_CONS_CODE.get(index_code.upper())
    if not cons:
        raise ValueError(f"未配置中证指数 cons 代码: {index_code}")
    url = CSINDEX_CONS_URL.format(code=cons)
    with urlopen(url, timeout=30) as resp:
        data = resp.read()
    df = pd.read_excel(BytesIO(data))
    return _parse_csindex_codes(df)


def fetch_baostock_members(index_code: str, as_of: str) -> tuple[pd.Timestamp, list[str]]:
    import baostock as bs

    from multi_factor.baostock.code_convert import bs_to_ths

    api = BAOSTOCK_INDEX_API.get(index_code.upper())
    if not api:
        raise ValueError(f"BaoStock 不支持指数: {index_code}")
    date_s = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"BaoStock login 失败: {lg.error_msg}")
    try:
        fn = bs.query_hs300_stocks if api == "hs300" else bs.query_zz500_stocks
        rs = fn(date=date_s)
        if rs.error_code != "0":
            raise RuntimeError(f"BaoStock 成分查询失败: {rs.error_msg}")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            raise ValueError(f"BaoStock 成分为空: {index_code} {date_s}")
        update_date = pd.to_datetime(rows[0][0]).normalize()
        codes = sorted({bs_to_ths(r[1]) for r in rows})
        return update_date, codes
    finally:
        bs.logout()


def fetch_members_snapshot(index_code: str, as_of: str) -> tuple[pd.Timestamp, list[str]]:
    """按指数选择 BaoStock 或中证官网拉取单日成分快照。"""
    code = index_code.upper()
    if code in BAOSTOCK_INDEX_API:
        try:
            return fetch_baostock_members(code, as_of)
        except Exception as ex:
            logger.warning("BaoStock 成分失败，回退中证官网: %s %s", code, ex)
    if code in CSINDEX_CONS_CODE:
        dt, members = fetch_csindex_members(code)
        return dt, members
    raise ValueError(f"不支持的指数代码: {index_code}")


def _codes_to_daily_mask(
    snapshots: list[tuple[pd.Timestamp, list[str]]],
    trading_dates: pd.DatetimeIndex,
    code_target: str = "rq",
) -> pd.DataFrame:
    if not snapshots:
        return pd.DataFrame(index=trading_dates)
    events = sorted(
        [(pd.Timestamp(dt).normalize(), normalize_codes(codes, code_target)) for dt, codes in snapshots],
        key=lambda x: x[0],
    )
    all_codes = sorted({c for _, codes in events for c in codes})
    out = pd.DataFrame(False, index=trading_dates, columns=all_codes)
    for dt in trading_dates:
        applicable = [item for item in events if item[0] <= dt]
        if not applicable:
            continue
        active = set(applicable[-1][1])
        cols = [c for c in active if c in out.columns]
        if cols:
            out.loc[dt, cols] = True
    return out


def _load_mask_from_db(
    provider: "IFindDataProvider",
    index_code: str,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame | None:
    if "index_members" not in provider.cfg.tables:
        return None
    try:
        c = provider.cfg.cols("index_members")
        t = provider.cfg.table("index_members")
        sql = f"""
        SELECT `{c.col('date')}` AS dt, `{c.col('code')}` AS sym
        FROM `{t}`
        WHERE `{c.col('index_code')}` = :idx
        """
        extra = provider.cfg.filter_sql("index_members")
        if extra:
            sql += f" AND ({extra})"
        df = provider._query_sql(sql, params={"idx": index_code})
        if df.empty:
            return None
        df["dt"] = pd.to_datetime(df["dt"]).dt.normalize()
        df["sym"] = normalize_codes(df["sym"].astype(str), "rq")
        cols = sorted(df["sym"].unique())
        out = pd.DataFrame(False, index=trading_dates, columns=cols)
        for dt, grp in df.groupby("dt"):
            if dt in out.index:
                syms = [s for s in grp["sym"].tolist() if s in out.columns]
                if syms:
                    out.loc[dt, syms] = True
        return out.ffill().fillna(False)
    except Exception as ex:
        logger.warning("读取 index_members 表失败: %s", ex)
        return None


def _snapshot_anchor_dates(trading_dates: pd.DatetimeIndex) -> list[pd.Timestamp]:
    anchors = set(_month_end_dates(trading_dates))
    if len(trading_dates):
        anchors.add(pd.Timestamp(trading_dates[0]))
        prev_month = (trading_dates[0].to_period("M") - 1).to_timestamp(how="end").normalize()
        anchors.add(prev_month)
    return sorted(anchors)


def _load_mask_from_remote(
    index_code: str,
    trading_dates: pd.DatetimeIndex,
) -> pd.DataFrame:
    anchor_dates = _snapshot_anchor_dates(trading_dates)
    if not anchor_dates:
        return pd.DataFrame(index=trading_dates)
    snapshots: list[tuple[pd.Timestamp, list[str]]] = []
    for dt in anchor_dates:
        try:
            snap_dt, codes = fetch_members_snapshot(index_code, dt.strftime("%Y-%m-%d"))
            snapshots.append((snap_dt, codes))
        except Exception as ex:
            logger.warning("拉取成分失败 %s %s: %s", index_code, dt.date(), ex)
    if not snapshots and index_code.upper() in CSINDEX_CONS_CODE:
        snap_dt, codes = fetch_csindex_members(index_code)
        snapshots.append((snap_dt, codes))
    return _codes_to_daily_mask(snapshots, trading_dates)


def load_index_members_mask(
    provider: "IFindDataProvider",
    index_code: str,
    trading_dates: pd.DatetimeIndex,
    *,
    start: str = "",
    end: str = "",
) -> pd.DataFrame:
    """返回成分股日频布尔矩阵（index=交易日, columns=股票 RQ）。"""
    if not index_code or trading_dates is None or len(trading_dates) == 0:
        return pd.DataFrame(index=trading_dates)

    cache_key = (index_code.upper(), str(trading_dates[0].date()), str(trading_dates[-1].date()))
    if cache_key in _MASK_CACHE:
        return _MASK_CACHE[cache_key]

    mask = _load_mask_from_db(provider, index_code, trading_dates)
    if mask is None or not mask.any().any():
        mask = _load_mask_from_remote(index_code, trading_dates)

    _MASK_CACHE[cache_key] = mask
    return mask


def upsert_members(
    engine,
    table: str,
    index_code: str,
    trade_date: str,
    stock_codes: list[str],
    source: str,
    chunk_size: int = 1000,
) -> int:
    from sqlalchemy import text

    if not stock_codes:
        return 0
    sql = f"""
    INSERT INTO `{table}` (trade_date, index_code, stock_code, source)
    VALUES (:trade_date, :index_code, :stock_code, :source)
    ON DUPLICATE KEY UPDATE source = VALUES(source), update_time = NOW()
    """
    rows = [
        {
            "trade_date": _sql_date(trade_date),
            "index_code": index_code.upper(),
            "stock_code": code,
            "source": source,
        }
        for code in stock_codes
    ]
    n = 0
    with engine.begin() as conn:
        for i in range(0, len(rows), chunk_size):
            conn.execute(text(sql), rows[i : i + chunk_size])
            n += len(rows[i : i + chunk_size])
    return n
