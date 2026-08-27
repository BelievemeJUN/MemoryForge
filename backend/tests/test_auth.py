"""API Key 认证单元测试（P0-A-1 / P1-G-1）。直接测 FastAPI 依赖，无需起服务。"""
import pytest
from fastapi import HTTPException

from auth import get_current_user, require_user


async def _expect_401(key: str):
    with pytest.raises(HTTPException) as ei:
        await require_user(key)
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
    uid = await require_user("devkey1")
    assert uid == "42"
    assert get_current_user() == "42"  # 认证上下文已写入


@pytest.mark.asyncio
async def test_empty_config_rejects_all(monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    await _expect_401("devkey1")
