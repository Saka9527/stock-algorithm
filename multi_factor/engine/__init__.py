# -*- coding: utf-8 -*-
"""多因子策略引擎：数据、合成、回测、单因子分析、绩效与报告。"""

from multi_factor.engine.pipeline import run_engine_pipeline
from multi_factor.engine.strategy_config import FactorSpec, StrategyConfig

__all__ = ["FactorSpec", "StrategyConfig", "run_engine_pipeline"]
