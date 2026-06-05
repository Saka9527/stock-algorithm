# -*- coding: utf-8 -*-
"""策略回测结果落库：配置、净值、热力图、持仓分析、交易、因子归因。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from multi_factor.engine.backtest_analytics import (
    build_nav_series,
    compute_factor_attribution,
    compute_holding_pnl_top10,
    compute_monthly_returns,
    monthly_heatmap_eligible,
    return_overview_from_perf,
    risk_metrics_from_perf,
)
from multi_factor.engine.strategy_config import StrategyConfig
from multi_factor.ifind.config_loader import IFindConfig


MAX_BACKTEST_HISTORY = 20


def _sql_date(s: str) -> str:
    s = str(s).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


class BacktestStorage:
    """回测结果持久化（保留最近 20 次）。"""

    def __init__(self, cfg: IFindConfig):
        if not cfg.db_url:
            raise ValueError("回测落库需要配置 database，请在 ifind_config.yaml 中设置")
        self.cfg = cfg
        self.engine = create_engine(cfg.db_url, pool_pre_ping=True)
        self.project_root = Path(__file__).resolve().parent.parent.parent

    def _run_sql_script(self, relative_path: str) -> None:
        path = self.project_root / relative_path
        if not path.exists():
            return
        content = path.read_text(encoding="utf-8")
        stmts: list[str] = []
        buf: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
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

    def ensure_tables(self) -> None:
        self._run_sql_script("scripts/sql/backtest_create_tables.sql")

    def _config_to_json(self, cfg: StrategyConfig) -> dict[str, Any]:
        return {
            "start": cfg.start,
            "end": cfg.end,
            "universe": cfg.universe,
            "top_n": cfg.top_n,
            "rebalance_freq": cfg.rebalance_freq,
            "weight_mode": cfg.weight_mode,
            "industry_neutral": cfg.industry_neutral,
            "cap_neutral": cfg.cap_neutral,
            "initial_cash": cfg.initial_cash,
            "buy_commission": cfg.buy_commission,
            "sell_commission": cfg.sell_commission,
            "slippage": cfg.slippage,
            "exclude_st": cfg.exclude_st,
            "exclude_suspended": cfg.exclude_suspended,
            "exclude_new_days": cfg.exclude_new_days,
            "exclude_limit": cfg.exclude_limit,
            "target_gross_exposure": cfg.target_gross_exposure,
            "benchmark_code": cfg.benchmark_code,
            "factors": [
                {"code": f.code, "weight": f.weight, "ascending": f.ascending}
                for f in cfg.factors
            ],
        }

    def save_run(
        self,
        cfg: StrategyConfig,
        bt_result: dict[str, Any],
        perf: dict[str, Any],
        factor_analyses: dict[str, Any],
        returns_df,
        output_dir: Path,
        run_id: str | None = None,
    ) -> str:
        """保存一次完整回测结果，返回 run_id。"""
        self.ensure_tables()
        run_id = run_id or str(uuid.uuid4())
        now = datetime.now()

        heatmap_ok, heatmap_note = monthly_heatmap_eligible(cfg.start, cfg.end)
        config_json = self._config_to_json(cfg)
        factors_json = config_json["factors"]

        run_params = {
            "run_id": run_id,
            "start_date": _sql_date(cfg.start),
            "end_date": _sql_date(cfg.end),
            "universe": cfg.universe,
            "top_n": cfg.top_n,
            "rebalance_freq": cfg.rebalance_freq,
            "weight_mode": cfg.weight_mode,
            "industry_neutral": int(cfg.industry_neutral),
            "cap_neutral": int(cfg.cap_neutral),
            "initial_cash": cfg.initial_cash,
            "buy_commission": cfg.buy_commission,
            "sell_commission": cfg.sell_commission,
            "slippage": cfg.slippage,
            "factors_json": json.dumps(factors_json, ensure_ascii=False),
            "config_json": json.dumps(config_json, ensure_ascii=False),
            "total_return": perf.get("total_return"),
            "annualized_return": perf.get("annualized_return"),
            "benchmark_total_return": perf.get("benchmark_total_return"),
            "benchmark_annualized_return": perf.get("benchmark_annualized_return"),
            "excess_return": perf.get("excess_return"),
            "annualized_excess_return": perf.get("annualized_excess_return"),
            "alpha": perf.get("alpha"),
            "beta": perf.get("beta"),
            "max_drawdown": perf.get("max_drawdown"),
            "sharpe_ratio": perf.get("sharpe_ratio"),
            "calmar_ratio": perf.get("calmar_ratio"),
            "win_rate": perf.get("win_rate"),
            "profit_loss_ratio": perf.get("profit_loss_ratio"),
            "volatility": perf.get("volatility"),
            "information_ratio": perf.get("information_ratio"),
            "trading_days": perf.get("trading_days"),
            "monthly_heatmap_available": int(heatmap_ok),
            "monthly_heatmap_note": heatmap_note if not heatmap_ok else None,
            "output_dir": str(output_dir),
            "created_at": now,
        }

        nav_rows = build_nav_series(
            bt_result["portfolio"],
            bt_result["benchmark_returns"],
            cfg.initial_cash,
        )
        monthly_rows = (
            compute_monthly_returns(bt_result["strategy_returns"]) if heatmap_ok else []
        )
        holding = compute_holding_pnl_top10(
            bt_result["positions"], returns_df, bt_result["portfolio"]
        )
        factor_attr = compute_factor_attribution(
            factor_analyses, cfg, perf.get("total_return", 0.0)
        )
        stock_trades = bt_result.get("stock_trades", [])

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO backtest_run (
                      run_id, start_date, end_date, universe, top_n, rebalance_freq,
                      weight_mode, industry_neutral, cap_neutral,
                      initial_cash, buy_commission, sell_commission, slippage,
                      factors_json, config_json,
                      total_return, annualized_return, benchmark_total_return,
                      benchmark_annualized_return, excess_return, annualized_excess_return,
                      alpha, beta, max_drawdown, sharpe_ratio, calmar_ratio,
                      win_rate, profit_loss_ratio, volatility, information_ratio, trading_days,
                      monthly_heatmap_available, monthly_heatmap_note, output_dir, created_at
                    ) VALUES (
                      :run_id, :start_date, :end_date, :universe, :top_n, :rebalance_freq,
                      :weight_mode, :industry_neutral, :cap_neutral,
                      :initial_cash, :buy_commission, :sell_commission, :slippage,
                      CAST(:factors_json AS JSON), CAST(:config_json AS JSON),
                      :total_return, :annualized_return, :benchmark_total_return,
                      :benchmark_annualized_return, :excess_return, :annualized_excess_return,
                      :alpha, :beta, :max_drawdown, :sharpe_ratio, :calmar_ratio,
                      :win_rate, :profit_loss_ratio, :volatility, :information_ratio, :trading_days,
                      :monthly_heatmap_available, :monthly_heatmap_note, :output_dir, :created_at
                    )
                    """
                ),
                run_params,
            )

            if nav_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO backtest_nav (
                          run_id, trade_date, strategy_equity, strategy_nav,
                          benchmark_nav, excess_nav, daily_return
                        ) VALUES (
                          :run_id, :trade_date, :strategy_equity, :strategy_nav,
                          :benchmark_nav, :excess_nav, :daily_return
                        )
                        """
                    ),
                    [{"run_id": run_id, **r} for r in nav_rows],
                )

            if monthly_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO backtest_monthly_return (
                          run_id, year_num, month_num, return_pct
                        ) VALUES (
                          :run_id, :year, :month, :return_pct
                        )
                        """
                    ),
                    [
                        {
                            "run_id": run_id,
                            "year": r["year"],
                            "month": r["month"],
                            "return_pct": r["return_pct"],
                        }
                        for r in monthly_rows
                    ],
                )

            holding_rows = holding["profit_top10"] + holding["loss_top10"]
            if holding_rows:
                conn.execute(
                    text(
                        """
                        INSERT INTO backtest_holding_pnl (
                          run_id, stock_code, total_pnl, total_return, rank_type, rank_num
                        ) VALUES (
                          :run_id, :stock_code, :total_pnl, :total_return, :rank_type, :rank_num
                        )
                        """
                    ),
                    [{"run_id": run_id, **r} for r in holding_rows],
                )

            if factor_attr:
                conn.execute(
                    text(
                        """
                        INSERT INTO backtest_factor_attribution (
                          run_id, factor_code, factor_weight, contribution_pct, contribution_ret
                        ) VALUES (
                          :run_id, :factor_code, :factor_weight, :contribution_pct, :contribution_ret
                        )
                        """
                    ),
                    [{"run_id": run_id, **r} for r in factor_attr],
                )

            if stock_trades:
                conn.execute(
                    text(
                        """
                        INSERT INTO backtest_trade (
                          run_id, trade_date, signal_date, action, stock_code,
                          quantity, price, weight_delta, equity_after
                        ) VALUES (
                          :run_id, :trade_date, :signal_date, :action, :stock_code,
                          :quantity, :price, :weight_delta, :equity_after
                        )
                        """
                    ),
                    [{"run_id": run_id, **t} for t in stock_trades],
                )

            self._trim_old_runs(conn)

        return run_id

    def _trim_old_runs(self, conn) -> None:
        """保留最近 MAX_BACKTEST_HISTORY 次回测记录。"""
        conn.execute(
            text(
                f"""
                UPDATE backtest_run SET is_deleted = 1
                WHERE run_id IN (
                  SELECT run_id FROM (
                    SELECT run_id FROM backtest_run
                    WHERE is_deleted = 0
                    ORDER BY created_at DESC
                    LIMIT 999 OFFSET {MAX_BACKTEST_HISTORY}
                  ) t
                )
                """
            )
        )

    def list_runs(self, limit: int = MAX_BACKTEST_HISTORY) -> list[dict[str, Any]]:
        """历史回测记录列表。"""
        self.ensure_tables()
        sql = """
        SELECT run_id, start_date, end_date, universe, top_n, rebalance_freq,
               total_return, annualized_return, excess_return, max_drawdown,
               sharpe_ratio, monthly_heatmap_available, created_at
        FROM backtest_run
        WHERE is_deleted = 0
        ORDER BY created_at DESC
        LIMIT :limit
        """
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"limit": limit}).mappings().all()
        return [self._serialize_row(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.ensure_tables()
        sql = "SELECT * FROM backtest_run WHERE run_id = :run_id AND is_deleted = 0"
        with self.engine.connect() as conn:
            row = conn.execute(text(sql), {"run_id": run_id}).mappings().first()
        if not row:
            return None
        return self._serialize_row(row)

    def get_full_report(self, run_id: str) -> dict[str, Any] | None:
        """获取完整历史回测报告。"""
        run = self.get_run(run_id)
        if not run:
            return None

        perf_fields = {
            "total_return", "annualized_return", "benchmark_total_return",
            "benchmark_annualized_return", "excess_return", "annualized_excess_return",
            "alpha", "beta", "max_drawdown", "sharpe_ratio", "calmar_ratio",
            "win_rate", "profit_loss_ratio", "volatility", "information_ratio", "trading_days",
        }
        perf = {k: run.get(k) for k in perf_fields}

        return {
            "run": run,
            "config": run.get("config_json") or {},
            "return_overview": return_overview_from_perf(perf),
            "risk_metrics": risk_metrics_from_perf(perf),
            "nav_curve": self.get_nav(run_id),
            "monthly_heatmap": self.get_monthly_heatmap(run_id),
            "holding_analysis": self.get_holding_analysis(run_id),
            "factor_attribution": self.get_factor_attribution(run_id),
            "trades": self.get_trades(run_id),
        }

    def get_nav(self, run_id: str, limit: int | None = None) -> list[dict]:
        sql = """
        SELECT trade_date, strategy_equity, strategy_nav, benchmark_nav, excess_nav, daily_return
        FROM backtest_nav
        WHERE run_id = :run_id AND is_deleted = 0
        ORDER BY trade_date
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"run_id": run_id}).mappings().all()
        return [self._serialize_row(r) for r in rows]

    def get_monthly_heatmap(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            return {"available": False, "note": "回测记录不存在", "data": []}
        available = bool(run.get("monthly_heatmap_available"))
        if not available:
            return {
                "available": False,
                "note": run.get("monthly_heatmap_note") or "无法展示月度收益热力图",
                "data": [],
            }
        sql = """
        SELECT year_num, month_num, return_pct
        FROM backtest_monthly_return
        WHERE run_id = :run_id AND is_deleted = 0
        ORDER BY year_num, month_num
        """
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"run_id": run_id}).mappings().all()
        data = [
            {
                "year": int(r["year_num"]),
                "month": int(r["month_num"]),
                "return_pct": float(r["return_pct"]),
            }
            for r in rows
        ]
        return {"available": True, "note": "", "data": data}

    def get_holding_analysis(self, run_id: str) -> dict[str, list]:
        sql = """
        SELECT stock_code, total_pnl, total_return, rank_type, rank_num
        FROM backtest_holding_pnl
        WHERE run_id = :run_id AND is_deleted = 0
        ORDER BY rank_type, rank_num
        """
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"run_id": run_id}).mappings().all()
        profit, loss = [], []
        for r in rows:
            item = self._serialize_row(r)
            if item["rank_type"] == "profit":
                profit.append(item)
            else:
                loss.append(item)
        return {"profit_top10": profit, "loss_top10": loss}

    def get_factor_attribution(self, run_id: str) -> list[dict]:
        sql = """
        SELECT factor_code, factor_weight, contribution_pct, contribution_ret
        FROM backtest_factor_attribution
        WHERE run_id = :run_id AND is_deleted = 0
        ORDER BY contribution_pct DESC
        """
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"run_id": run_id}).mappings().all()
        return [self._serialize_row(r) for r in rows]

    def get_trades(self, run_id: str, limit: int | None = None) -> list[dict]:
        sql = """
        SELECT trade_date, signal_date, action, stock_code, quantity, price,
               weight_delta, equity_after
        FROM backtest_trade
        WHERE run_id = :run_id AND is_deleted = 0
        ORDER BY trade_date, stock_code
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), {"run_id": run_id}).mappings().all()
        return [self._serialize_row(r) for r in rows]

    @staticmethod
    def _serialize_row(row) -> dict[str, Any]:
        out = {}
        for k, v in dict(row).items():
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat() if hasattr(v, "hour") else str(v)
            elif isinstance(v, (bytes, bytearray)):
                out[k] = v.decode("utf-8")
            elif k.endswith("_json") and isinstance(v, str):
                try:
                    out[k] = json.loads(v)
                except json.JSONDecodeError:
                    out[k] = v
            else:
                out[k] = v
        return out
