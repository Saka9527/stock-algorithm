# -*- coding: utf-8 -*-
"""凌晨定时任务：因子绩效预热、缓存水位更新、默认回测可选预热。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text

from multi_factor.archive.daily_parquet import DailyParquetArchive
from multi_factor.archive.factor_parquet import FactorParquetArchive, dedicated_factor_codes
from multi_factor.cache.redis_cache import get_cache
from multi_factor.ifind.config_loader import IFindConfig, load_ifind_config
from multi_factor.ifind.factor_performance_jobs import FactorPerformanceJobRunner
from multi_factor.ifind.provider import IFindDataProvider
from multi_factor.service.backtest_validation import build_backtest_request, query_db_data_range


class PipelineJobLogger:
    def __init__(self, cfg: IFindConfig):
        if not cfg.db_url:
            raise ValueError("pipeline_job_log 需要 database 配置")
        self.engine = create_engine(cfg.db_url, pool_pre_ping=True)

    def ensure_table(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent.parent
        sql_path = root / "scripts" / "sql" / "pipeline_job_log_create_tables.sql"
        if not sql_path.exists():
            return
        content = sql_path.read_text(encoding="utf-8")
        stmts: list[str] = []
        buf: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if stripped.upper().startswith("USE "):
                continue
            buf.append(line)
            if stripped.endswith(";"):
                stmts.append("\n".join(buf).rstrip().rstrip(";"))
                buf = []
        if buf:
            stmts.append("\n".join(buf).rstrip().rstrip(";"))
        with self.engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))

    def start(self, job_type: str, params: dict) -> int:
        self.ensure_table()
        sql = """
        INSERT INTO pipeline_job_log (job_type, job_status, params_json, started_at)
        VALUES (:job_type, 'running', CAST(:params_json AS JSON), :started_at)
        """
        with self.engine.begin() as conn:
            res = conn.execute(
                text(sql),
                {
                    "job_type": job_type,
                    "params_json": json.dumps(params, ensure_ascii=False),
                    "started_at": datetime.now(),
                },
            )
            return int(res.lastrowid)

    def finish(
        self,
        job_id: int,
        status: str,
        result: dict | None = None,
        error: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        finished = datetime.now()
        success = len((result or {}).get("success", [])) if result else 0
        failed = len((result or {}).get("failed", [])) if result else 0
        sql = """
        UPDATE pipeline_job_log SET
          job_status = :status,
          start_date = :start_date,
          end_date = :end_date,
          result_json = CAST(:result_json AS JSON),
          success_count = :success_count,
          failed_count = :failed_count,
          error_message = :error_message,
          finished_at = :finished_at,
          duration_sec = TIMESTAMPDIFF(SECOND, started_at, :finished_at)
        WHERE id = :id
        """
        with self.engine.begin() as conn:
            conn.execute(
                text(sql),
                {
                    "id": job_id,
                    "status": status,
                    "start_date": start_date,
                    "end_date": end_date,
                    "result_json": json.dumps(result or {}, ensure_ascii=False, default=str),
                    "success_count": success,
                    "failed_count": failed,
                    "error_message": error,
                    "finished_at": finished,
                },
            )


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def resolve_nightly_window(cfg: IFindConfig, years: float | None = None) -> dict[str, Any]:
    years = years if years is not None else cfg.performance.default_years
    return query_db_data_range(cfg, years=years)


def run_daily_parquet_archive(
    cfg: IFindConfig | None = None,
    years: float | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    """增量更新每日行情 Parquet 归档。"""
    from pathlib import Path

    from multi_factor import config as project_config

    cfg = cfg or load_ifind_config()
    span = resolve_nightly_window(cfg, years=years)
    start, end = span["start"], span["end"]
    provider = IFindDataProvider(cfg)
    root = Path(cfg.parquet_archive.dir)
    if not root.is_absolute():
        root = project_config.PROJECT_ROOT / root
    archive = DailyParquetArchive(root)
    trading = [d.strftime("%Y%m%d") for d in provider.get_trading_dates(start, end, prefer_parquet=False)]
    result = archive.build_range(
        provider, start, end, incremental=incremental, trading_dates=trading
    )
    cache = get_cache(cfg.redis.as_dict() if cfg.redis.host else None)
    cache.set_json(
        "meta:parquet_watermark",
        {"start": start, "end": end, "files": len(archive.list_dates()), "updated_at": datetime.now().isoformat()},
        ttl=86400 * 7,
    )
    return {"window": span, **result}


def run_factor_parquet_archive(
    cfg: IFindConfig | None = None,
    factor_codes: list[str] | None = None,
    years: float | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    """增量更新因子宽表 Parquet 归档。"""
    from pathlib import Path

    from multi_factor import config as project_config

    cfg = cfg or load_ifind_config()
    span = resolve_nightly_window(cfg, years=years)
    start, end = span["start"], span["end"]
    fq = cfg.parquet_archive.factor
    if not fq.enabled:
        return {"skipped": True, "reason": "factor parquet disabled"}
    provider = IFindDataProvider(cfg)
    root = Path(fq.dir)
    if not root.is_absolute():
        root = project_config.PROJECT_ROOT / root
    archive = FactorParquetArchive(root, read_workers=cfg.parquet_archive.read_workers)
    codes = factor_codes or dedicated_factor_codes()
    details = []
    for fc in codes:
        details.append(archive.build_range(provider, fc, start, end, incremental=incremental))
    cache = get_cache(cfg.redis.as_dict() if cfg.redis.host else None)
    cache.set_json(
        "meta:factor_parquet_watermark",
        {
            "start": start,
            "end": end,
            "factors": codes,
            "updated_at": datetime.now().isoformat(),
        },
        ttl=86400 * 7,
    )
    return {"window": span, "factors": codes, "details": details}


def run_factor_performance_nightly(
    cfg: IFindConfig | None = None,
    factor_codes: list[str] | None = None,
    years: float | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """凌晨因子绩效全量预热：写 factor_performance_* + 更新 Redis 水位。"""
    cfg = cfg or load_ifind_config()
    span = resolve_nightly_window(cfg, years=years)
    start, end = span["start"], span["end"]
    logger = PipelineJobLogger(cfg)
    job_id = logger.start(
        "factor_performance",
        {"start": start, "end": end, "years": years, "workers": workers},
    )
    try:
        runner = FactorPerformanceJobRunner(cfg)
        result = runner.run_batch(
            factor_codes=factor_codes,
            start=start,
            end=end,
            workers=workers,
        )
        cache = get_cache(cfg.redis.as_dict() if cfg.redis.host else None)
        cache.set_json(
            "meta:factor_perf_watermark",
            {
                "start": start,
                "end": end,
                "updated_at": datetime.now().isoformat(),
                "success": len(result.get("success", [])),
                "failed": len(result.get("failed", [])),
            },
            ttl=86400 * 7,
        )
        logger.finish(job_id, "succeeded", result, start_date=_sql_date(start), end_date=_sql_date(end))
        return {"job_id": job_id, "window": span, **result}
    except Exception as ex:
        logger.finish(job_id, "failed", error=str(ex), start_date=_sql_date(start), end_date=_sql_date(end))
        raise


def run_nightly_pipeline(
    cfg: IFindConfig | None = None,
    *,
    factor_codes: list[str] | None = None,
    years: float | None = None,
    workers: int | None = None,
    warmup_backtest: bool = False,
    backtest_factors: list[str] | None = None,
) -> dict[str, Any]:
    """完整凌晨流水线。"""
    cfg = cfg or load_ifind_config()
    out: dict[str, Any] = {"started_at": datetime.now().isoformat()}

    provider = IFindDataProvider(cfg)
    s, e = provider.query_data_date_range()
    cache = get_cache(cfg.redis.as_dict() if cfg.redis.host else None)
    cache.set_json("meta:daily_watermark", {"start": s, "end": e, "updated_at": datetime.now().isoformat()}, ttl=86400)

    if cfg.parquet_archive.enabled:
        out["parquet_archive"] = run_daily_parquet_archive(cfg, years=years, incremental=True)

    if cfg.parquet_archive.factor.enabled:
        out["factor_parquet_archive"] = run_factor_parquet_archive(
            cfg, factor_codes=factor_codes, years=years, incremental=True
        )

    out["factor_performance"] = run_factor_performance_nightly(
        cfg, factor_codes=factor_codes, years=years, workers=workers
    )

    if warmup_backtest:
        from multi_factor.service.backtest_validation import validate_backtest_chain

        codes = backtest_factors or ["PE_TTM", "ROE_TTM"]
        bt_results = []
        for code in codes:
            body = build_backtest_request(
                {
                    "factor_code": code,
                    "use_full_data_range": True,
                    "data_years": years or cfg.performance.default_years,
                    "top_n": 20,
                    "rebalance_freq": "monthly",
                    "run_single_factor_analysis": False,
                    "output_subdir": f"nightly/{code.lower()}",
                }
            )
            bt_results.append({"factor": code, "summary": validate_backtest_chain(body)["summary"]})
        out["backtest_warmup"] = bt_results

    out["finished_at"] = datetime.now().isoformat()
    return out


def seconds_until_hour(hour: int, minute: int = 0) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
