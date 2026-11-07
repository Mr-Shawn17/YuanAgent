"""设备令牌存取：Windows 凭据管理器（keyring），环境变量兜底。

优先级：环境变量 YUAN_RUNNER_TOKEN > keyring > 交互输入（set-token 命令写入）。
"""

from __future__ import annotations

import os

SERVICE_NAME = "YuanAgentRunner"
ACCOUNT_NAME = "device-token"
ENV_NAME = "YUAN_RUNNER_TOKEN"


def get_token() -> str:
    """读取设备令牌；无则返回空串（由调用方提示用户先 set-token）。"""
    env = os.environ.get(ENV_NAME, "").strip()
    if env:
        return env
    try:
        import keyring

        value = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME) or ""
        return value.strip()
    except Exception:
        return ""


def set_token(token: str) -> None:
    """保存令牌到系统凭据库。"""
    token = token.strip()
    if not token:
        raise ValueError("令牌不能为空")
    import keyring

    keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, token)


def clear_token() -> bool:
    """删除已存令牌；不存在时返回 False。"""
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        return True
    except Exception:
        return False


def token_status() -> str:
    """诊断用：返回令牌来源说明。"""
    if os.environ.get(ENV_NAME, "").strip():
        return f"环境变量 {ENV_NAME}"
    if get_token():
        return "系统凭据库"
    return "未配置"


__all__ = [
    "ACCOUNT_NAME",
    "ENV_NAME",
    "SERVICE_NAME",
    "clear_token",
    "get_token",
    "set_token",
    "token_status",
]
