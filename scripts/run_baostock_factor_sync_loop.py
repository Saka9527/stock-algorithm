# -*- coding: utf-8 -*-
"""
factor_data_wide 同步守护：每轮独立子进程，超时杀进程并续跑。

  python scripts/run_baostock_factor_sync_loop.py
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

from multi_factor.baostock.factor_wide_sync import BaostockFactorWideSync

LOG_PATH = PROJECT_ROOT / "data" / "baostock_factor_sync.log"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_baostock_factor_wide.py"


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
    p = argparse.ArgumentParser(description="factor_data_wide 子进程循环同步")
    p.add_argument("--config", default="", help="baostock 配置路径")
    p.add_argument("--ifind-config", default="", help="ifind 配置路径")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--stocks-per-round", type=int, default=300)
    p.add_argument("--round-timeout", type=int, default=1800, help="单轮超时秒")
    p.add_argument("--sleep", type=float, default=10.0)
    p.add_argument("--min-remaining", type=int, default=500)
    p.add_argument("--max-rounds", type=int, default=0)
    return p.parse_args()


def _pending(sync: BaostockFactorWideSync) -> int:
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
        _log(f"[factor-loop] round timeout ({args.round_timeout}s), killed child")
        return -9


def main():
    args = parse_args()
    sync = BaostockFactorWideSync(
        baostock_config_path=args.config or None,
        ifind_config_path=args.ifind_config or None,
    )

    round_no = 0
    while True:
        round_no += 1
        if args.max_rounds and round_no > args.max_rounds:
            _log(f"[factor-loop] max_rounds={args.max_rounds}, exit")
            break

        status = sync.status()
        if status["api_remaining"] < args.min_remaining:
            _log(f"[factor-loop] quota low remaining={status['api_remaining']}, exit")
            break

        try:
            pending = _pending(sync)
        except Exception as exc:
            _log(f"[factor-loop] plan failed: {exc}")
            time.sleep(args.sleep)
            continue

        _log(
            f"[factor-loop] round {round_no} pending={pending} "
            f"wide={status['wide_stocks']}stocks/{status['wide_rows']}rows "
            f"api={status['api_requests']}/{status['api_limit']} skip={status['skip_codes']}"
        )
        if pending == 0:
            _log("[factor-loop] all done")
            break

        rc = _run_round(args)
        _log(f"[factor-loop] round {round_no} done rc={rc} {json.dumps(sync.status(), ensure_ascii=False)}")

        time.sleep(args.sleep)

    _log(f"[factor-loop] finished {json.dumps(sync.status(), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
