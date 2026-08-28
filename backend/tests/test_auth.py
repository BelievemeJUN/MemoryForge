"""API Key 认证单元测试（P0-A-1 / P1-G-1）。直接测 FastAPI 依赖，无需起服务。"""
import pytest
from fastapi import HTTPException

from auth import get_current_user, require_user


async def _expect_401(key: str):
    with pytest.raises(HTTPException) as ei:
        await require_user(authorization="", x_api_key=key)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_key(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42,devkey2:7")
    await _expect_401("")


@pytest.mark.asyncio
async def test_invalid_key(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42,devkey2:7")
    await _expect_401("wrong-key")


@pytest.mark.asyncio
async def test_valid_key_maps_user(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42,devkey2:7")
    uid = await require_user(authorization="", x_api_key="devkey1")
    assert uid == "42"
    assert get_current_user() == "42"  # 认证上下文已写入


@pytest.mark.asyncio
async def test_empty_config_rejects_all(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    await _expect_401("devkey1")


# ---------- JWT 动态令牌 ----------


@pytest.mark.asyncio
async def test_login_creates_token(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42")
    from auth import create_token, decode_token

    token, exp = create_token("42")
    assert exp > 0
    payload = decode_token(token)
    assert payload["sub"] == "42"
    assert "exp" in payload and "jti" in payload and "iat" in payload


@pytest.mark.asyncio
async def test_expired_token_rejected(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42")
    from auth import create_token, decode_token

    token, _ = create_token("42", expires_in=-1)  # 立即过期
    with pytest.raises(ValueError):
        decode_token(token)


@pytest.mark.asyncio
async def test_tampered_token_rejected(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42")
    from auth import create_token, decode_token

    token, _ = create_token("42")
    tampered = token[:-4] + "AAAA"  # 改签名尾部
    with pytest.raises(ValueError):
        decode_token(tampered)


@pytest.mark.asyncio
async def test_bearer_token_authenticates(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42")
    from auth import create_token, require_user

    token, _ = create_token("42")
    uid = await require_user(authorization=f"Bearer {token}")
    assert uid == "42"


@pytest.mark.asyncio
async def test_bad_bearer_rejected(monkeypatch):
    monkeypatch.setenv("API_KEYS", "devkey1:42")
    from auth import require_user

    with pytest.raises(HTTPException) as ei:
        await require_user(authorization="Bearer not.a.token")
    assert ei.value.status_code == 401
