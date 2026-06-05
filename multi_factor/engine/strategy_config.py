# -*- coding: utf-8 -*-
"""策略与回测参数配置（dataclass，可直接序列化/CLI 映射）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from multi_factor import config as project_config

RebalanceFreq = Literal["daily", "weekly", "monthly"]
UniversePool = Literal["all_a", "csi300", "csi500", "csi1000"]
WeightMode = Literal["equal", "custom"]


@dataclass
class FactorSpec:
    """单个因子定义。"""

    code: str  # factor_base_info.factor_code / factor_data_wide 列，如 PE_TTM
    weight: float = 1.0
    # 若为 None，则从 factor_base_info.sort_type 推断（asc=值越小越好）
    ascending: bool | None = None


@dataclass
class StrategyConfig:
    """多因子策略 + 回测完整参数。"""

    start: str = project_config.START_DATE
    end: str = project_config.END_DATE
    ifind_config_path: str = str(project_config.IFIND_CONFIG_PATH)

    # 因子列表与合成
    factors: list[FactorSpec] = field(default_factory=list)
    weight_mode: WeightMode = "equal"
    # 行业 / 市值中性化（需数据支持；市值默认用 MARKET_VALUE 因子）
    industry_neutral: bool = False
    industry_factor_code: str = "SW_INDUSTRY"  # 无数据时自动跳过
    cap_neutral: bool = False
    cap_factor_code: str = "MARKET_VALUE"

    # 股票池与持仓
    universe: UniversePool = "all_a"
    top_n: int = 30
    rebalance_freq: RebalanceFreq = "daily"

    # 交易过滤
    exclude_st: bool = True
    exclude_suspended: bool = True
    exclude_new_days: int = 60  # 上市不足 N 日剔除；0=不剔除
    exclude_limit: bool = True
    limit_threshold: float = 0.095  # 涨跌停幅度（主板约 10%）

    # 账户
    initial_cash: float = 1_000_000.0
    target_gross_exposure: float = 0.95

    # A 股交易成本（用户指定）
    buy_commission: float = 0.0003  # 买入 0.03%
    sell_commission: float = 0.0013  # 卖出 0.13%
    slippage: float = 0.001  # 滑点 0.1%
    min_commission: float = 5.0

    # T+1：信号日收盘生成，下一交易日收盘成交
    t_plus_one: bool = True

    # 基准（沪深300）
    benchmark_code: str = "000300.SH"

    # 单因子分析
    ic_period: int = 1
    quantile_groups: int = 5
    run_single_factor_analysis: bool = True

    # 输出
    output_dir: Path = field(default_factory=lambda: project_config.OUTPUT_DIR / "engine_report")

    def normalized_factor_weights(self) -> dict[str, float]:
        """返回 factor_code -> 权重。"""
        if not self.factors:
            return {}
        if self.weight_mode == "equal":
            w = 1.0 / len(self.factors)
            return {f.code.upper(): w for f in self.factors}
        total = sum(f.weight for f in self.factors)
        if total <= 0:
            raise ValueError("自定义权重之和必须 > 0")
        return {f.code.upper(): f.weight / total for f in self.factors}


# 指数代码映射（同花顺 / Blader）
UNIVERSE_INDEX_CODES: dict[str, str] = {
    "all_a": "",
    "csi300": "000300.SH",
    "csi500": "000905.SH",
    "csi1000": "000852.SH",
}

DEFAULT_FACTORS: list[FactorSpec] = [
    FactorSpec("PE_TTM", weight=0.25, ascending=False),
    FactorSpec("PB", weight=0.25, ascending=False),
    FactorSpec("ROE_TTM", weight=0.25, ascending=True),
    FactorSpec("MOMENTUM_20", weight=0.25, ascending=True),
]
