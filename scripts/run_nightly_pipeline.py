# -*- coding: utf-8 -*-
"""
凌晨定时流水线：因子绩效预热 + Redis 水位 + 可选回测预热。

用法:
  # 立即执行一次
  python scripts/run_nightly_pipeline.py --run-now

  # 守护进程：每天凌晨 2:00 执行
  python scripts/run_nightly_pipeline.py --schedule-hour 2 --schedule-minute 0

  # 仅因子绩效，4 并发
  python scripts/run_nightly_pipeline.py --run-now --workers 4 --skip-backtest-warmup
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.service.nightly_jobs import run_nightly_pipeline, seconds_until_hour

LOG_PATH = PROJECT_ROOT / "data" / "nightly_pipeline.log"


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_args():
    p = argparse.ArgumentParser(description="凌晨因子绩效/回测预热流水线")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--run-now", action="store_true", help="立即执行，不等待定时")
    p.add_argument("--schedule-hour", type=int, default=2, help="每日执行小时，默认凌晨2点")
    p.add_argument("--schedule-minute", type=int, default=0, help="每日执行分钟")
    p.add_argument("--data-years", type=float, default=0, help="数据年数，0=配置默认3年")
    p.add_argument("--workers", type=int, default=0, help="因子绩效并发数，0=配置默认")
    p.add_argument("--factor-code", action="append", default=[], help="指定因子，可多次传入")
    p.add_argument("--skip-backtest-warmup", action="store_true", help="跳过回测预热")
    p.add_argument("--backtest-factor", action="append", default=[], help="回测预热因子列表")
    return p.parse_args()


def _execute(args) -> dict:
    cfg = load_ifind_config(args.ifind_config or None)
    years = args.data_years or None
    workers = args.workers or None
    factor_codes = args.factor_code or None
    return run_nightly_pipeline(
        cfg,
        factor_codes=factor_codes,
        years=years,
        workers=workers,
        warmup_backtest=not args.skip_backtest_warmup,
        backtest_factors=args.backtest_factor or None,
    )


def main():
    args = parse_args()

    def _once():
        _log("nightly pipeline start")
        try:
            result = _execute(args)
            _log(
                f"nightly pipeline done: perf_ok={len(result.get('factor_performance', {}).get('success', []))} "
                f"perf_fail={len(result.get('factor_performance', {}).get('failed', []))}"
            )
            out = PROJECT_ROOT / "data" / "nightly_pipeline_last.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        except Exception as ex:
            _log(f"nightly pipeline failed: {ex}")
            raise

    if args.run_now:
        _once()
        return

    _log(f"scheduler armed: daily {args.schedule_hour:02d}:{args.schedule_minute:02d}")
    while True:
        wait_sec = seconds_until_hour(args.schedule_hour, args.schedule_minute)
        _log(f"sleep {wait_sec:.0f}s until next run")
        time.sleep(wait_sec)
        try:
            _once()
        except Exception:
            pass
        time.sleep(60)


if __name__ == "__main__":
    main()
