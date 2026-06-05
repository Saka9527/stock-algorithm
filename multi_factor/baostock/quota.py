# -*- coding: utf-8 -*-
"""BaoStock 每日 API 请求配额统计与硬限制。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text

from multi_factor.baostock.db import BaostockStore


@dataclass
class QuotaSnapshot:
    quota_date: str
    request_count: int
    daily_limit: int
    remaining: int


class DailyRequestQuota:
    """按自然日累计请求次数，落库 api_quota。"""

    def __init__(self, store: BaostockStore, daily_limit: int = 40_000):
        self.store = store
        self.daily_limit = daily_limit
        self._quota_date = date.today().isoformat()
        self._count = self._load_count()

    def _load_count(self) -> int:
        sql = text(
            f"SELECT request_count, daily_limit FROM `{self.store.quota}` "
            f"WHERE quota_date = :d"
        )
        with self.store.engine.connect() as conn:
            row = conn.execute(sql, {"d": self._quota_date}).fetchone()
            if row:
                self.daily_limit = int(row[1])
                return int(row[0])

        with self.store.engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO `{self.store.quota}` "
                    f"(quota_date, request_count, daily_limit, updated_at) "
                    f"VALUES (:d, 0, :limit, :ts)"
                ),
                {
                    "d": self._quota_date,
                    "limit": self.daily_limit,
                    "ts": datetime.now(),
                },
            )
        return 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self._count)

    def snapshot(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            quota_date=self._quota_date,
            request_count=self._count,
            daily_limit=self.daily_limit,
            remaining=self.remaining,
        )

    def ensure_available(self, needed: int = 1) -> None:
        if self._count + needed > self.daily_limit:
            raise RuntimeError(
                f"BaoStock 今日 API 请求已达上限: {self._count}/{self.daily_limit}，"
                f"尚需 {needed} 次，剩余 {self.remaining} 次。"
                f"请明日再跑或提高 daily_api_limit（需符合平台规则）。"
            )

    def consume(self, n: int = 1, *, persist: bool = True) -> None:
        if n <= 0:
            return
        self.ensure_available(n)
        self._count += n
        if persist:
            self._persist()

    def _persist(self) -> None:
        sql = text(
            f"UPDATE `{self.store.quota}` SET request_count = :c, daily_limit = :limit, "
            f"updated_at = :ts WHERE quota_date = :d"
        )
        with self.store.engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "c": self._count,
                    "limit": self.daily_limit,
                    "ts": datetime.now(),
                    "d": self._quota_date,
                },
            )
