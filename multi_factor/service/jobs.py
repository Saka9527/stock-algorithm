# -*- coding: utf-8 -*-
"""异步任务管理（内存队列，单进程）。"""

from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.factor_performance_jobs import FactorPerformanceJobRunner
from multi_factor.service.runner import BacktestRequest, BacktestResult, run_pipeline
from multi_factor.service import engine_service


@dataclass
class JobRecord:
    job_id: str
    status: str  # pending | running | succeeded | failed
    created_at: str
    finished_at: str | None = None
    request: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None


class JobManager:
    def __init__(self):
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def submit(self, req: BacktestRequest) -> str:
        job_id = uuid.uuid4().hex
        record = JobRecord(
            job_id=job_id,
            status="pending",
            created_at=datetime.utcnow().isoformat() + "Z",
            request={
                "job_type": "engine" if req.use_engine and req.source == "ifind" else "legacy",
                "source": req.source,
                "start": req.start,
                "end": req.end,
                "top_n": req.top_n,
                "use_engine": req.use_engine,
                "skip_factor_analysis": req.skip_factor_analysis,
                "skip_backtest": req.skip_backtest,
                "scores_only": req.scores_only,
            },
        )
        with self._lock:
            self._jobs[job_id] = record

        thread = threading.Thread(target=self._run, args=(job_id, req), daemon=True)
        thread.start()
        return job_id

    def submit_engine(self, body: dict) -> str:
        """提交新版多因子引擎异步任务。"""
        job_id = uuid.uuid4().hex
        record = JobRecord(
            job_id=job_id,
            status="pending",
            created_at=datetime.utcnow().isoformat() + "Z",
            request={"job_type": "engine", **body},
        )
        with self._lock:
            self._jobs[job_id] = record
        thread = threading.Thread(
            target=self._run_engine, args=(job_id, body), daemon=True
        )
        thread.start()
        return job_id

    def submit_factor_performance(self, body: dict) -> str:
        """提交因子绩效离线预热任务。"""
        job_id = uuid.uuid4().hex
        record = JobRecord(
            job_id=job_id,
            status="pending",
            created_at=datetime.utcnow().isoformat() + "Z",
            request={"job_type": "factor_performance", **body},
        )
        with self._lock:
            self._jobs[job_id] = record
        thread = threading.Thread(
            target=self._run_factor_performance, args=(job_id, body), daemon=True
        )
        thread.start()
        return job_id

    def _run_engine(self, job_id: str, body: dict) -> None:
        with self._lock:
            self._jobs[job_id].status = "running"
        try:
            payload = engine_service.run_engine_backtest(body)
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "succeeded"
                rec.result = payload
                rec.finished_at = datetime.utcnow().isoformat() + "Z"
        except Exception:
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "failed"
                rec.error = traceback.format_exc()
                rec.finished_at = datetime.utcnow().isoformat() + "Z"

    def _run(self, job_id: str, req: BacktestRequest) -> None:
        with self._lock:
            self._jobs[job_id].status = "running"
        try:
            bt: BacktestResult = run_pipeline(req)
            payload = {
                "summary": bt.summary,
                "factor_scores_shape": list(bt.factor_scores_shape)
                if bt.factor_scores_shape
                else None,
                "output_dir": bt.output_dir,
                "message": bt.message,
            }
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "succeeded"
                rec.result = payload
                rec.finished_at = datetime.utcnow().isoformat() + "Z"
        except Exception:
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "failed"
                rec.error = traceback.format_exc()
                rec.finished_at = datetime.utcnow().isoformat() + "Z"

    def _run_factor_performance(self, job_id: str, body: dict) -> None:
        with self._lock:
            self._jobs[job_id].status = "running"
        try:
            cfg = load_ifind_config(body.get("ifind_config") or None)
            runner = FactorPerformanceJobRunner(cfg)

            factor_codes = body.get("factor_codes")
            if not factor_codes and body.get("factor_code"):
                factor_codes = [body["factor_code"]]

            payload = runner.run_batch(
                factor_codes=factor_codes,
                start=body["start"],
                end=body["end"],
                period=int(body.get("period", 1)),
                quantiles=int(body.get("quantiles", 5)),
                top_pct=float(body.get("top_pct", 0.2)),
            )
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "succeeded"
                rec.result = payload
                rec.finished_at = datetime.utcnow().isoformat() + "Z"
        except Exception:
            with self._lock:
                rec = self._jobs[job_id]
                rec.status = "failed"
                rec.error = traceback.format_exc()
                rec.finished_at = datetime.utcnow().isoformat() + "Z"

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)


job_manager = JobManager()
