# -*- coding: utf-8 -*-
"""
BaoStock 同步守护：每轮启动独立子进程，超时自动杀进程并续跑。
不依赖 IDE 终端，适合长时间后台运行。

  python scripts/run_baostock_sync_loop.py
  powershell -File scripts/start_baostock_sync.ps1
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

from multi_factor.baostock.config_loader import load_baostock_config
from multi_factor.baostock.sync import BaostockDailySync

LOG_PATH = PROJECT_ROOT / "data" / "baostock_sync.log"
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_baostock_daily.py"


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_args():
    p = argparse.ArgumentParser(description="BaoStock 子进程循环同步")
    p.add_argument("--config", default="", help="配置文件路径")
    p.add_argument("--batch-size", type=int, default=100, help="每批股票数")
    p.add_argument("--stocks-per-round", type=int, default=300, help="每轮子进程最多同步股票数")
    p.add_argument("--round-timeout", type=int, default=900, help="单轮超时秒")
    p.add_argument("--sleep", type=float, default=5.0, help="轮次间隔秒")
    p.add_argument("--min-remaining", type=int, default=500, help="配额低于此值停止")
    p.add_argument("--max-rounds", type=int, default=0, help="最大轮数，0=不限")
    return p.parse_args()


def _pending(sync: BaostockDailySync) -> int:
    from multi_factor.baostock.client import BaostockClient
    from multi_factor.baostock.quota import DailyRequestQuota

    quota = DailyRequestQuota(sync.store, sync.cfg.daily_api_limit)
    client = BaostockClient(quota)
    with client.session():
        plan = sync.build_plan(client)
    return len(plan.stocks)


def _run_round(args, cfg_path: str) -> int:
    cmd = [
        sys.executable,
        "-u",
        str(SYNC_SCRIPT),
        "--batch-size",
        str(args.batch_size),
        "--max-stocks",
        str(args.stocks_per_round),
    ]
    if cfg_path:
        cmd.extend(["--config", cfg_path])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            timeout=args.round_timeout,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        _log(f"[loop] 子进程超时 ({args.round_timeout}s)，已终止，下轮继续")
        return -9


def main():
    args = parse_args()
    cfg = load_baostock_config(args.config or None)
    sync = BaostockDailySync(cfg)
    cfg_path = args.config or ""

    round_no = 0
    while True:
        round_no += 1
        if args.max_rounds and round_no > args.max_rounds:
            _log(f"[loop] 达到 max_rounds={args.max_rounds}，退出")
            break

        status = sync.quota_status()
        if status["api_remaining"] < args.min_remaining:
            _log(f"[loop] 配额不足 remaining={status['api_remaining']}，退出")
            break

        try:
            pending = _pending(sync)
        except Exception as exc:
            _log(f"[loop] 计划失败: {exc}")
            time.sleep(args.sleep)
            continue

        _log(
            f"[loop] 第 {round_no} 轮 pending={pending} "
            f"库内={status['stocks_in_db']}只/{status['rows_total']}行 "
            f"api={status['api_requests']}/{status['api_limit']} skip={status.get('skip_codes', 0)}"
        )
        if pending == 0:
            _log("[loop] 全部完成")
            break

        rc = _run_round(args, cfg_path)
        _log(f"[loop] 第 {round_no} 轮子进程结束 rc={rc} {sync.quota_status()}")

        time.sleep(args.sleep)

    _log(f"[loop] 结束 {json.dumps(sync.quota_status(), ensure_ascii=False)}")


if __name__ == "__main__":
    main()
