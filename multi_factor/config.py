# -*- coding: utf-8 -*-
"""回测与因子研究全局配置。"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IFIND_CONFIG_PATH = Path(
    os.environ.get("IFIND_CONFIG_PATH") or (PROJECT_ROOT / "config" / "ifind_config.yaml")
)
IFIND_BLADER_EXAMPLE_PATH = PROJECT_ROOT / "config" / "ifind_config.blader.example.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output"
FACTOR_SCORES_PATH = OUTPUT_DIR / "factor_scores.pkl"
FACTOR_ANALYSIS_DIR = OUTPUT_DIR / "factor_analysis"
BACKTEST_REPORT_DIR = OUTPUT_DIR / "backtest_report"

# 回测区间与标的池
START_DATE = "20200102"
END_DATE = "20231229"
BENCHMARK = "000300.XSHG"
UNIVERSE_INDEX = "000300.XSHG"

# 账户与交易
INITIAL_CASH = 1_000_000
TOP_N = 30
REBALANCE_TRADINGDAY = 1  # 每月第 1 个交易日调仓
TARGET_GROSS_EXPOSURE = 0.95

# 滑点与手续费（对齐 RQAlpha sys_simulation / sys_transaction_cost）
SLIPPAGE_MODEL = "PriceRatioSlippage"
SLIPPAGE = 0.001
COMMISSION_MULTIPLIER = 1.0
STOCK_MIN_COMMISSION = 5

# 多因子合成权重（PE/PB 越低越好，ROE/动量越高越好，在 factors 中已做方向处理）
FACTOR_WEIGHTS = {
    "pe": 0.25,
    "pb": 0.25,
    "roe": 0.25,
    "momentum": 0.25,
}

# 因子检验
IC_PERIODS = [1, 5, 20]
QUANTILE_GROUPS = 5
FACTOR_ASCENDING = {
    "pe": False,
    "pb": False,
    "roe": True,
    "momentum": True,
    "composite": True,
}
