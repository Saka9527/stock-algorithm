# -*- coding: utf-8 -*-
"""RQDatac 初始化与股票池工具。"""

import os

import rqdatac


def _read_windows_user_env(name: str) -> str | None:
    """Cursor/IDE 子进程有时拿不到用户级环境变量，从注册表补读。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value or None
    except OSError:
        return None


def init_rqdatac():
    """从环境变量、Windows 用户环境或 ~/.rqdatac 配置初始化 RQDatac。"""
    if rqdatac.initialized():
        return

    username = os.environ.get("RQDATAC_USERNAME") or os.environ.get("RQDATA_USER")
    password = os.environ.get("RQDATAC_PASSWORD") or os.environ.get("RQDATA_PASS")
    uri = (
        os.environ.get("RQDATAC2_CONF")
        or os.environ.get("RQDATAC_CONF")
        or os.environ.get("RQDATAC_URI")
        or _read_windows_user_env("RQDATAC2_CONF")
        or _read_windows_user_env("RQDATAC_CONF")
    )

    if uri:
        rqdatac.init(uri)
        return
    if username and password:
        rqdatac.init(username, password)
        return

    rqdatac.init()


def verify_rqdatac_connection() -> None:
    """登录后试调一次接口，尽早暴露账号/权限问题。"""
    init_rqdatac()
    try:
        rqdatac.index_components("000300.XSHG", "20230101")
    except Exception as e:
        err = str(e)
        hint = (
            "RQDatac 已连接但接口调用失败。常见原因：\n"
            "  1) 该账号为网站登录号，未开通 RQData API 或未设置 API 密码；\n"
            "  2) 需在米筐用户中心申请 RQData 许可，并使用 API 专用账号密码或 RQDATAC2_CONF URI；\n"
            "  3) 试用/权限不足导致部分接口不可用。\n"
            f"原始错误: {err}"
        )
        raise RuntimeError(hint) from e


def get_universe(index_code: str, date: str) -> list:
    init_rqdatac()
    return list(rqdatac.index_components(index_code, date))
