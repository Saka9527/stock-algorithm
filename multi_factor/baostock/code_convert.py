# -*- coding: utf-8 -*-
"""BaoStock 代码 (sh.600000) 与 THS 风格 (600000.SH) 互转。"""

import re

_BS_RE = re.compile(r"^(sh|sz|bj)\.(\d{6})$", re.IGNORECASE)
_THS_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)

_BS_TO_THS = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def bs_to_ths(code: str) -> str:
    """sh.600000 -> 600000.SH"""
    code = str(code).strip()
    m = _BS_RE.match(code)
    if not m:
        return code.upper()
    exch, num = m.group(1).upper(), m.group(2)
    return f"{num}.{_BS_TO_THS[exch]}"


def ths_to_bs(code: str) -> str:
    """600000.SH -> sh.600000"""
    code = str(code).strip().upper()
    m = _THS_RE.match(code)
    if not m:
        return code.lower()
    num, exch = m.group(1), m.group(2).upper()
    return f"{exch.lower()[:2] if exch != 'BJ' else 'bj'}.{num}"


def normalize_bs(code: str) -> str:
    """统一为 sh.600000 小写交易所前缀。"""
    if _BS_RE.match(code):
        m = _BS_RE.match(code)
        return f"{m.group(1).lower()}.{m.group(2)}"
    return ths_to_bs(code)
