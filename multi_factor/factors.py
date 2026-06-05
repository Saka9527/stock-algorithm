# -*- coding: utf-8 -*-
"""PE、PB、ROE、动量因子定义与多因子合成。"""

from rqfactor import DELAY, Factor, RANK

# 价值：PE/PB 越低越好 -> 对原始值取负向排名
FACTOR_PE = -RANK(Factor("pe_ratio"))
FACTOR_PB = -RANK(Factor("pb_ratio_ttm"))
# 质量：ROE 越高越好
FACTOR_ROE = RANK(Factor("return_on_equity_ttm"))
# 动量：20 日收益率，越高越好
FACTOR_MOMENTUM = RANK(Factor("close") / DELAY(Factor("close"), 20) - 1)

SINGLE_FACTORS = {
    "pe": FACTOR_PE,
    "pb": FACTOR_PB,
    "roe": FACTOR_ROE,
    "momentum": FACTOR_MOMENTUM,
}


def build_composite_factor(weights: dict | None = None):
    """按权重线性合成多因子得分（已统一为「越大越好」）。"""
    weights = weights or {"pe": 0.25, "pb": 0.25, "roe": 0.25, "momentum": 0.25}
    total_w = sum(weights.values())
    composite = None
    for name, w in weights.items():
        if w <= 0:
            continue
        term = (w / total_w) * SINGLE_FACTORS[name]
        composite = term if composite is None else composite + term
    if composite is None:
        raise ValueError("至少需要一个因子权重")
    return composite
