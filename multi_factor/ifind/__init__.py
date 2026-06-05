# -*- coding: utf-8 -*-
"""同花顺 iFinD 本地同步数据接入。"""

from multi_factor.ifind.config_loader import load_ifind_config
from multi_factor.ifind.provider import IFindDataProvider

__all__ = ["IFindDataProvider", "load_ifind_config"]
