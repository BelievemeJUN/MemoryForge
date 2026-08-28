"""认证：API Key 登录 → JWT（HS256）动态令牌（P0-A-1 + 本轮升级）。

设计（面试可讲）：
  - echomind 的债：user_id 直接是请求参数，任何人可冒充。生产里 user_id 绝不能信请求参数。
  - 演进故事：API Key 是「长期静态凭证」（类比密码，客户端私藏）；登录用它换取
    「短时动态令牌」JWT（类比 session token）——带签名（防篡改）、带过期（限时有效）、
    带 jti（可吊销）。服务端只从认证上下文取 user_id，忽略请求参数里的 user_id。
  - JWT 用标准库手写（HS256）：header.payload.signature，base64url + HMAC-SHA256，
    不依赖第三方库——面试可现场讲清三段结构。
  - 兼容层：仍接受 `X-API-Key`（向后兼容，方便 curl 演示/测试）。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from contextvars import ContextVar

from dotenv import load_dotenv
from fastapi import Header, HTTPException

load_dotenv()

_current_user_ctx: ContextVar[str] = ContextVar("current_user", default="")

# JWT 配置（生产务必用环境变量注入强随机密钥）
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me-in-prod")
JWT_EXPIRES_SECONDS = int(os.getenv("JWT_EXPIRES_SECONDS", "3600"))


def _load_api_keys() -> dict[str, str]:
    """解析 API_KEYS -> {api_key: user_id}（登录凭证，仍为静态映射）。"""
    mapping: dict[str, str] = {}
    for item in os.getenv("API_KEYS", "").split(","):
        if ":" in item:
            key, uid = item.split(":", 1)
            mapping[key.strip()] = uid.strip()
    return mapping


# ---------- JWT（手写 HS256，标准库实现） ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(user_id: str, expires_in: int | None = None) -> tuple[str, int]:
    """签发 JWT。返回 (token, 有效秒数)。payload 含 sub/iat/exp/jti。"""
    expires_in = expires_in or JWT_EXPIRES_SECONDS
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid4().hex,
    }
    seg = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = hmac.new(JWT_SECRET.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64url(sig)}", expires_in


def decode_token(token: str) -> dict:
    """校验签名 + 过期，返回 payload。任何不合法都抛 ValueError。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("令牌格式错误")
    header_b, payload_b, sig_b = parts
    expected = hmac.new(
        JWT_SECRET.encode(), f"{header_b}.{payload_b}".encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_unb64url(sig_b), expected):
        raise ValueError("签名无效（令牌可能被篡改）")
    payload = json.loads(_unb64url(payload_b))
    if int(payload.get("exp", 0)) < time.time():
        raise ValueError("令牌已过期")
    return payload


# ---------- FastAPI 依赖 ----------

async def require_user(
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> str:
    """FastAPI 依赖：优先 `Authorization: Bearer <jwt>`，兼容 `X-API-Key`。

    校验通过后返回 user_id 并写入当前请求上下文（端点只信它）。
    """
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
        try:
            payload = decode_token(token)
        except ValueError as e:
            raise HTTPException(status_code=401, detail=str(e))
        uid = payload.get("sub", "")
        if not uid:
            raise HTTPException(status_code=401, detail="令牌缺少用户标识")
        _current_user_ctx.set(uid)
        return uid
    if x_api_key:
        keys = _load_api_keys()
        uid = keys.get(x_api_key)
        if uid is not None:
            _current_user_ctx.set(uid)
            return uid
    raise HTTPException(status_code=401, detail="缺少有效凭证（Bearer JWT 或 X-API-Key）")


def get_current_user() -> str:
    """取当前请求的 user_id（无则空串）。"""
    return _current_user_ctx.get()

