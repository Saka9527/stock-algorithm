# -*- coding: utf-8 -*-
"""加载 iFinD / Blader 数据映射配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import yaml

from multi_factor import config as project_config


@dataclass
class TableColumns:
    mapping: dict[str, str] = field(default_factory=dict)

    def col(self, logical: str) -> str:
        if logical not in self.mapping:
            raise KeyError(f"配置 columns 缺少字段: {logical}")
        return self.mapping[logical]


@dataclass
class RedisConfig:
    enabled: bool = True
    host: str = ""
    port: int = 6379
    password: str = ""
    database: int = 0
    key_prefix: str = "stock_algo:"
    socket_timeout: float = 5.0

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "password": self.password,
            "database": self.database,
            "key_prefix": self.key_prefix,
            "socket_timeout": self.socket_timeout,
        }


@dataclass
class FactorParquetConfig:
    """因子宽表 Parquet 本地归档。"""

    enabled: bool = True
    dir: str = "data/parquet/factor"
    prefer_read: bool = True
    min_coverage: float = 0.95
    use_redis: bool = False


@dataclass
class ParquetArchiveConfig:
    """每日行情 Parquet 本地归档。"""

    enabled: bool = True
    dir: str = "data/parquet/market"
    prefer_read: bool = True
    min_coverage: float = 0.95
    use_monthly_wide: bool = True
    read_workers: int = 8
    bundle_cache_ttl: int = 3600
    bundle_use_redis: bool = False
    factor: FactorParquetConfig = field(default_factory=FactorParquetConfig)


@dataclass
class PerformanceConfig:
    """因子绩效 / 回测预热默认参数。"""

    default_years: float = 3.0
    cache_ttl_report: int = 3600
    cache_ttl_panel: int = 86400
    batch_workers: int = 4
    series_chunk_size: int = 5000


@dataclass
class IFindConfig:
    db_url: str = ""
    csv_dir: str = ""
    schema: str = "legacy"  # legacy | blader
    tables: dict[str, str] = field(default_factory=dict)
    columns: dict[str, TableColumns] = field(default_factory=dict)
    factor_mapping: dict[str, str] = field(default_factory=dict)
    sql_filters: dict[str, str] = field(default_factory=dict)
    universe_mode: str = "index_members"  # index_members | factor_distinct | daily_distinct
    universe_index: str = "000300.SH"
    benchmark_index: str = "000300.SH"
    benchmark_rq: str = "000300.XSHG"
    code_format: str = "ths"
    momentum_window: int = 20
    redis: RedisConfig = field(default_factory=RedisConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    parquet_archive: ParquetArchiveConfig = field(default_factory=ParquetArchiveConfig)

    @property
    def code_target(self) -> str:
        return "ths" if self.code_format.lower() == "ths" else "rq"

    @property
    def is_blader(self) -> bool:
        return self.schema.lower() == "blader"

    def table(self, key: str) -> str:
        if key not in self.tables:
            raise KeyError(f"配置 tables 缺少: {key}")
        return self.tables[key]

    def cols(self, table_key: str) -> TableColumns:
        return self.columns[table_key]

    def filter_sql(self, table_key: str) -> str:
        return (self.sql_filters.get(table_key) or "").strip()


def _parse_columns(raw: dict[str, Any]) -> dict[str, TableColumns]:
    return {k: TableColumns(mapping=v) for k, v in (raw or {}).items()}


def _build_db_url(db: dict) -> str:
    url = (db.get("url") or "").strip()
    if url:
        if url.startswith("jdbc:mysql://"):
            # jdbc:mysql://host:port/db?params -> mysql+pymysql://...
            rest = url[len("jdbc:mysql://") :]
            host_part, _, query = rest.partition("?")
            if "/" in host_part:
                host_port, dbname = host_part.split("/", 1)
            else:
                host_port, dbname = host_part, "blader"
            user = os.environ.get("BLADER_DB_USER") or db.get("username", "")
            password = os.environ.get("BLADER_DB_PASSWORD") or db.get("password", "")
            if not user or not password:
                raise ValueError("JDBC URL 需配合 database.username/password 或环境变量 BLADER_DB_*")
            return (
                f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
                f"@{host_port}/{dbname}?charset=utf8mb4"
            )
        return url

    user = os.environ.get("BLADER_DB_USER") or db.get("username")
    password = os.environ.get("BLADER_DB_PASSWORD") or db.get("password")
    host = os.environ.get("BLADER_DB_HOST") or db.get("host", "127.0.0.1")
    port = int(os.environ.get("BLADER_DB_PORT") or db.get("port", 3306))
    name = os.environ.get("BLADER_DB_NAME") or db.get("name") or db.get("database") or "blader"
    if not user or not password:
        return ""
    return (
        f"mysql+pymysql://{quote_plus(str(user))}:{quote_plus(str(password))}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )


def load_ifind_config(path: str | Path | None = None) -> IFindConfig:
    path = Path(path or project_config.IFIND_CONFIG_PATH)
    if not path.exists():
        for alt in (
            project_config.PROJECT_ROOT / "config" / "ifind_config.blader.example.yaml",
            project_config.PROJECT_ROOT / "config" / "ifind_config.example.yaml",
        ):
            if alt.exists():
                raise FileNotFoundError(
                    f"未找到 {path}，请复制 {alt.name} 为 ifind_config.yaml 并填写数据库账号"
                )
        raise FileNotFoundError(f"未找到 iFinD 配置: {path}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    db = raw.get("database") or {}
    uni = raw.get("universe") or {}
    bench = raw.get("benchmark") or {}
    redis_raw = raw.get("redis") or {}
    perf_raw = raw.get("performance") or {}
    pq_raw = raw.get("parquet_archive") or {}
    redis_cfg = RedisConfig(
        enabled=bool(redis_raw.get("enabled", True)),
        host=str(os.environ.get("REDIS_HOST") or redis_raw.get("host") or ""),
        port=int(os.environ.get("REDIS_PORT") or redis_raw.get("port", 6379)),
        password=str(os.environ.get("REDIS_PASSWORD") or redis_raw.get("password") or ""),
        database=int(os.environ.get("REDIS_DATABASE") or redis_raw.get("database", redis_raw.get("db", 0))),
        key_prefix=str(redis_raw.get("key_prefix") or "stock_algo:"),
        socket_timeout=float(redis_raw.get("socket_timeout", 5)),
    )
    factor_raw = pq_raw.get("factor") or {}
    factor_cfg = FactorParquetConfig(
        enabled=bool(factor_raw.get("enabled", True)),
        dir=str(factor_raw.get("dir") or "data/parquet/factor"),
        prefer_read=bool(factor_raw.get("prefer_read", True)),
        min_coverage=float(factor_raw.get("min_coverage", 0.95)),
        use_redis=bool(factor_raw.get("use_redis", False)),
    )
    pq_cfg = ParquetArchiveConfig(
        enabled=bool(pq_raw.get("enabled", True)),
        dir=str(pq_raw.get("dir") or "data/parquet/market"),
        prefer_read=bool(pq_raw.get("prefer_read", True)),
        min_coverage=float(pq_raw.get("min_coverage", 0.95)),
        use_monthly_wide=bool(pq_raw.get("use_monthly_wide", True)),
        read_workers=int(pq_raw.get("read_workers", 8)),
        bundle_cache_ttl=int(pq_raw.get("bundle_cache_ttl", 3600)),
        bundle_use_redis=bool(pq_raw.get("bundle_use_redis", False)),
        factor=factor_cfg,
    )
    perf_cfg = PerformanceConfig(
        default_years=float(perf_raw.get("default_years", 3.0)),
        cache_ttl_report=int(perf_raw.get("cache_ttl_report", 3600)),
        cache_ttl_panel=int(perf_raw.get("cache_ttl_panel", 86400)),
        batch_workers=int(perf_raw.get("batch_workers", 4)),
        series_chunk_size=int(perf_raw.get("series_chunk_size", 5000)),
    )
    cfg = IFindConfig(
        db_url=_build_db_url(db),
        csv_dir=(raw.get("csv_dir") or "").strip(),
        schema=(raw.get("schema") or "legacy").lower(),
        tables=raw.get("tables") or {},
        columns=_parse_columns(raw.get("columns")),
        factor_mapping=raw.get("factor_mapping") or {},
        sql_filters=raw.get("sql_filters") or {},
        universe_mode=uni.get("mode") or raw.get("universe_mode") or "index_members",
        universe_index=uni.get("index_code", "000300.SH"),
        benchmark_index=bench.get("index_code", "000300.SH"),
        benchmark_rq=bench.get("rq_code", "000300.XSHG"),
        code_format=(raw.get("code_format") or "ths").lower(),
        momentum_window=int(raw.get("momentum_window", 20)),
        redis=redis_cfg,
        performance=perf_cfg,
        parquet_archive=pq_cfg,
    )
    if redis_cfg.host:
        from multi_factor.cache.redis_cache import reset_cache, get_cache

        reset_cache()
        get_cache(redis_cfg.as_dict())
    return cfg
