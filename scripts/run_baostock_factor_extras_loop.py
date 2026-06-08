# -*- coding: utf-8 -*-
"""
factor_data_wide 扩展字段同步守护：每轮独立子进程，超时杀进程并续跑。

  python scripts/run_baostock_factor_extras_loop.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from multi_factor.baostock.factor_wide_extras_sync import FactorWideExtrasSync

LOG_PATH = PROJECT_ROOT / "data" / "baostock_factor_extras_sync.log"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_baostock_factor_extras.py"


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_args():
    p = argparse.ArgumentParser(description="factor_data_wide 扩展字段子进程循环同步")
    p.add_argument("--config", default="")
    p.add_argument("--ifind-config", default="")
    p.add_argument("--batch-size", type=int, default=80)
    p.add_argument("--stocks-per-round", type=int, default=200)
    p.add_argument("--round-timeout", type=int, default=2400)
    p.add_argument("--sleep", type=float, default=15.0)
    p.add_argument("--min-remaining", type=int, default=500)
    p.add_argument("--max-rounds", type=int, default=0)
    return p.parse_args()


def _pending(sync: FactorWideExtrasSync) -> int:
    from multi_factor.baostock.client import BaostockClient
    from multi_factor.baostock.quota import DailyRequestQuota

    quota = DailyRequestQuota(sync.store, sync.bs_cfg.daily_api_limit)
    client = BaostockClient(quota)
    with client.session():
        plan = sync.build_plan(client)
    return len(plan.stocks)


def _run_round(args) -> int:
    cmd = [
        sys.executable,
        "-u",
        str(SYNC_SCRIPT),
        "--batch-size",
        str(args.batch_size),
        "--max-stocks",
        str(args.stocks_per_round),
    ]
    if args.config:
        cmd.extend(["--config", args.config])
    if args.ifind_config:
        cmd.extend(["--ifind-config", args.ifind_config])
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT))
    try:
        return proc.wait(timeout=args.round_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=30)
        _log(f"[extras-loop] round timeout ({args.round_timeout}s), killed child")
        return -9


def main():
    args = parse_args()
    sync = FactorWideExtrasSync(
        baostock_config_path=args.config or None,
        ifind_config_path=args.ifind_config or None,
    )

    round_no = 0
    while True:
        round_no += 1
        if args.max_rounds and round_no > args.max_rounds:
            _log(f"[extras-loop] max_rounds={args.max_rounds}, exit")
            break

        status = sync.status()
        if status["api_remaining"] < args.min_remaining:
            _log(f"[extras-loop] quota low remaining={status['api_remaining']}, exit")
            break

        try:
            pending = _pending(sync)
        except Exception as exc:
            _log(f"[extras-loop] plan failed: {exc}")
            time.sleep(args.sleep)
            continue

        _log(
            f"[extras-loop] round {round_no} pending={pending} "
            f"null_extras={status['rows_with_null_extras_recent']} "
            f"api={status['api_requests']}/{status['api_limit']}"
        )
        if pending == 0:
            _log("[extras-loop] all done")
            break

        rc = _run_round(args)
        _log(f"[extras-loop] round {round_no} done rc={rc} {json.dumps(sync.status(), ensure_ascii=False)}")

        time.sleep(args.sleep)

    _log(f"[extras-loop] finished {json.dumps(sync.status(), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
