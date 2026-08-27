"""轻量 API Key 认证（P0-A-1）。

设计（面试可讲）：
  - echomind 的债：user_id 直接是请求参数，任何人可冒充。生产里 user_id 绝不能信请求参数。
  - 这里给一个最小但正确的模型：客户端带 `X-API-Key` 头，服务端映射到用户（user_id），
    端点**只从认证上下文取 user_id**（忽略请求里传的 user_id）。
  - API_KEYS 环境变量格式: "key1:user1,key2:user2"（演示用；生产应接用户表 + JWT）。
"""
import os
from contextvars import ContextVar

from dotenv import load_dotenv
from fastapi import Header, HTTPException

load_dotenv()

_current_user_ctx: ContextVar[str] = ContextVar("current_user", default="")


def _load_api_keys() -> dict[str, str]:
    """解析 API_KEYS -> {api_key: user_id}。"""
    mapping: dict[str, str] = {}
    for item in os.getenv("API_KEYS", "").split(","):
        if ":" in item:
            key, uid = item.split(":", 1)
            mapping[key.strip()] = uid.strip()
    return mapping


async def require_user(x_api_key: str = Header(default="")) -> str:
    """FastAPI 依赖：校验 X-API-Key，返回 user_id 并写入当前上下文。

    请求头参数名 x_api_key 对应 `X-API-Key`。
    """
    keys = _load_api_keys()
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key")
    user_id = keys.get(x_api_key)
    if user_id is None:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    _current_user_ctx.set(user_id)
    return user_id


def get_current_user() -> str:
    """取当前请求的 user_id（无则空串）。"""
    return _current_user_ctx.get()
