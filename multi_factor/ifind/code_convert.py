# -*- coding: utf-8 -*-
"""iFinD / 米筐 证券代码互转。"""

import re

_THS_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)
_RQ_RE = re.compile(r"^(\d{6})\.(XSHG|XSHE|XBJG)$", re.IGNORECASE)

_EXCHANGE_MAP = {
    "SH": "XSHG",
    "SZ": "XSHE",
    "BJ": "XBJG",
}
_RQ_TO_THS = {v: k for k, v in _EXCHANGE_MAP.items()}


def ths_to_rq(code: str) -> str:
    """600000.SH -> 600000.XSHG"""
    code = str(code).strip().upper()
    m = _THS_RE.match(code)
    if not m:
        return code
    num, exch = m.group(1), m.group(2).upper()
    return f"{num}.{_EXCHANGE_MAP[exch]}"


def rq_to_ths(code: str) -> str:
    """600000.XSHG -> 600000.SH"""
    code = str(code).strip().upper()
    m = _RQ_RE.match(code)
    if not m:
        return code
    num, exch = m.group(1), m.group(2).upper()
    return f"{num}.{_RQ_TO_THS[exch]}"


def normalize_code(code: str, target: str = "rq") -> str:
    target = target.lower()
    code = str(code).strip().upper()
    if _RQ_RE.match(code):
        return code if target == "rq" else rq_to_ths(code)
    if _THS_RE.match(code):
        return ths_to_rq(code) if target == "rq" else code
    if len(code) == 6 and code.isdigit():
        # 默认上交所；深市请使用带后缀代码
        return f"{code}.XSHG" if target == "rq" else f"{code}.SH"
    return code


def normalize_codes(codes, target: str = "rq"):
    return [normalize_code(c, target) for c in codes]
