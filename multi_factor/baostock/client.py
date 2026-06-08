# -*- coding: utf-8 -*-
"""带配额计数的 BaoStock 客户端封装。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import baostock as bs
import pandas as pd

from multi_factor.baostock.quota import DailyRequestQuota

# 前复权日 K 精简字段
K_FIELDS = "date,code,open,high,low,close,volume,amount,tradestatus"
ADJ_QFQ = "2"

# 估值/行情字段（不复权，与 BaoStock 日 K 指标一致）
VALUATION_FIELDS = "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM"
EXTRAS_FIELDS = "date,code,close,volume,turn,tradestatus,isST"
ADJ_NONE = "3"


def is_a_share_stock(code: str) -> bool:
    """排除指数、ETF 等，保留沪深 A 股。"""
    if not (code.startswith("sh.") or code.startswith("sz.")):
        return False
    num = code.split(".", 1)[1]
    if len(num) != 6 or not num.isdigit():
        return False
    if code.startswith("sh.000") or code.startswith("sz.399"):
        return False
    if code.startswith(("sh.6", "sh.688", "sh.689")):
        return True
    return code.startswith(("sz.0", "sz.2", "sz.3"))


class BaostockClient:
    """串行调用 BaoStock，每次 query/login/logout 计入配额。"""

    def __init__(self, quota: DailyRequestQuota):
        self.quota = quota
        self._logged_in = False

    def login(self) -> None:
        self.quota.ensure_available(1)
        lg = bs.login()
        self.quota.consume(1)
        if lg.error_code != "0":
            raise RuntimeError(f"BaoStock login 失败: {lg.error_code} {lg.error_msg}")
        self._logged_in = True

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            self.quota.ensure_available(1)
            bs.logout()
            self.quota.consume(1)
        finally:
            self._logged_in = False

    def relogin(self) -> None:
        """长任务中重建连接，避免 BaoStock 会话超时。"""
        self.logout()
        self.login()

    def _query(self, rs, *, retry: bool = True) -> None:
        self.quota.ensure_available(1)
        self.quota.consume(1)
        if rs.error_code == "0":
            return
        msg = f"{rs.error_code} {rs.error_msg}"
        if retry and self._logged_in:
            self.relogin()
            raise RuntimeError(f"BaoStock API 失败(将重试): {msg}")
        raise RuntimeError(f"BaoStock API 失败: {msg}")

    def query_trade_dates(self, start: str, end: str) -> list[str]:
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        self._query(rs)
        dates = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if row[1] == "1":
                dates.append(row[0])
        return dates

    def query_latest_trading_day(self, start: str, end: str) -> str:
        dates = self.query_trade_dates(start, end)
        if not dates:
            raise RuntimeError(f"区间内无交易日: {start} ~ {end}")
        return dates[-1]

    def query_all_stock(self, day: str) -> list[str]:
        """返回指定交易日仍在市的 A 股代码（sh./sz. 格式）。"""
        rs = bs.query_all_stock(day=day)
        self._query(rs)
        codes = []
        while rs.error_code == "0" and rs.next():
            code = rs.get_row_data()[0]
            if is_a_share_stock(code):
                codes.append(code)
        return sorted(set(codes))

    def query_stock_universe(self) -> list[str]:
        """一次拉取全部 A 股基本资料（含已退市），用于历史回填。"""
        rs = bs.query_stock_basic()
        self._query(rs)
        codes = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            code, _name, _ipo, _out, sec_type, _status = row[:6]
            if sec_type == "1" and is_a_share_stock(code):
                codes.append(code)
        return sorted(set(codes))

    def query_history_k_data_plus(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        last_exc: RuntimeError | None = None
        for attempt in range(2):
            rs = bs.query_history_k_data_plus(
                code,
                K_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=ADJ_QFQ,
            )
            try:
                self._query(rs, retry=(attempt == 0))
            except RuntimeError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                raise
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            fields = rs.fields if isinstance(rs.fields, list) else rs.fields.split(",")
            if not rows:
                return pd.DataFrame(columns=fields)
            return pd.DataFrame(rows, columns=fields)
        if last_exc:
            raise last_exc
        return pd.DataFrame()

    def query_extras_k_data_plus(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """日 K 扩展字段：换手率、停牌、ST（不复权）。"""
        last_exc: RuntimeError | None = None
        for attempt in range(2):
            rs = bs.query_history_k_data_plus(
                code,
                EXTRAS_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=ADJ_NONE,
            )
            try:
                self._query(rs, retry=(attempt == 0))
            except RuntimeError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                raise
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            fields = rs.fields if isinstance(rs.fields, list) else rs.fields.split(",")
            if not rows:
                return pd.DataFrame(columns=fields)
            return pd.DataFrame(rows, columns=fields)
        if last_exc:
            raise last_exc
        return pd.DataFrame()

    def query_profit_shares(self, code: str, year: int) -> pd.DataFrame:
        """季度股本：totalShare / liqaShare（股）。"""
        rs = bs.query_profit_data(code, str(year))
        self._query(rs)
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        fields = rs.fields if isinstance(rs.fields, list) else rs.fields.split(",")
        if not rows:
            return pd.DataFrame(columns=fields)
        return pd.DataFrame(rows, columns=fields)

    def query_valuation_k_data_plus(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        last_exc: RuntimeError | None = None
        for attempt in range(2):
            rs = bs.query_history_k_data_plus(
                code,
                VALUATION_FIELDS,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=ADJ_NONE,
            )
            try:
                self._query(rs, retry=(attempt == 0))
            except RuntimeError as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                raise
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            fields = rs.fields if isinstance(rs.fields, list) else rs.fields.split(",")
            if not rows:
                return pd.DataFrame(columns=fields)
            return pd.DataFrame(rows, columns=fields)
        if last_exc:
            raise last_exc
        return pd.DataFrame()

    @contextmanager
    def session(self) -> Iterator["BaostockClient"]:
        self.login()
        try:
            yield self
        finally:
            self.logout()
