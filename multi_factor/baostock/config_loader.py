# -*- coding: utf-8 -*-
"""加载 BaoStock 同步配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from multi_factor import config as project_config

TABLE_DAILY = "stock_daily_qfq"
TABLE_QUOTA = "api_quota"
TABLE_SYNC_STATE = "stock_sync_state"


@dataclass
class BaostockConfig:
    db_url: str
    years: float = 3.0
    daily_api_limit: int = 40_000
    use_full_universe: bool = True
    tables: dict[str, str] = field(default_factory=dict)

    def table(self, key: str) -> str:
        defaults = {
            "daily": TABLE_DAILY,
            "quota": TABLE_QUOTA,
            "sync_state": TABLE_SYNC_STATE,
        }
        return self.tables.get(key) or defaults[key]


def _build_db_url(db: dict) -> str:
    url = (db.get("url") or "").strip()
    if url:
        if url.startswith("jdbc:mysql://"):
            rest = url[len("jdbc:mysql://") :]
            host_part, _, _query = rest.partition("?")
            host_port, dbname = host_part.split("/", 1) if "/" in host_part else (host_part, "blader")
            user = os.environ.get("BLADER_DB_USER") or db.get("username", "")
            password = os.environ.get("BLADER_DB_PASSWORD") or db.get("password", "")
            if not user or not password:
                raise ValueError("JDBC URL 需配合 database.username/password 或 BLADER_DB_* 环境变量")
            return (
                f"mysql+pymysql://{quote_plus(user)}:{quote_plus(password)}"
                f"@{host_port}/{dbname}?charset=utf8mb4"
            )
        return url

    user = os.environ.get("BLADER_DB_USER") or db.get("username")
    password = os.environ.get("BLADER_DB_PASSWORD") or db.get("password")
    host = db.get("host", "127.0.0.1")
    port = int(db.get("port", 3306))
    name = db.get("name") or db.get("database") or "blader"
    if not user or not password:
        return ""
    return (
        f"mysql+pymysql://{quote_plus(str(user))}:{quote_plus(str(password))}"
        f"@{host}:{port}/{name}?charset=utf8mb4"
    )


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_baostock_config(path: str | Path | None = None) -> BaostockConfig:
    path = Path(path or project_config.PROJECT_ROOT / "config" / "baostock_config.yaml")
    raw: dict = {}

    if path.exists():
        raw = _load_yaml(path)
    else:
        for alt in (
            project_config.PROJECT_ROOT / "config" / "ifind_config.yaml",
            project_config.PROJECT_ROOT / "config" / "baostock_config.example.yaml",
        ):
            if alt.exists():
                raw = _load_yaml(alt)
                if alt.name == "ifind_config.yaml":
                    raw = {"database": raw.get("database") or {}, "sync": raw.get("sync") or {}}
                break
        else:
            raise FileNotFoundError(
                f"未找到 {path}，请复制 config/baostock_config.example.yaml 并填写 MySQL 连接"
            )

    db = raw.get("database") or {}
    sync = raw.get("sync") or {}
    db_url = _build_db_url(db)
    if not db_url:
        raise ValueError(
            "database.url 或 host/username/password 未配置，"
            "可设置环境变量 BLADER_DB_USER / BLADER_DB_PASSWORD"
        )

    return BaostockConfig(
        db_url=db_url,
        years=float(sync.get("years", 3.0)),
        daily_api_limit=int(sync.get("daily_api_limit", 40_000)),
        use_full_universe=bool(sync.get("use_full_universe", True)),
        tables=raw.get("tables") or {},
    )
