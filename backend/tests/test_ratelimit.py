"""限流测试（P2-H）。需 Redis（默认 6380）。"""
import uuid

import pytest

from ratelimit import RateLimiter, USER_MAX


@pytest.mark.integration
@pytest.mark.asyncio
async def test_under_limit_allowed():
    rl = RateLimiter()
    uid = f"rl-ok-{uuid.uuid4().hex[:8]}"
    ok, wait = await rl.check_user(uid)
    assert ok and wait == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_exceed_limit_rejected():
    rl = RateLimiter()
    uid = f"rl-over-{uuid.uuid4().hex[:8]}"
    last_ok = True
    for _ in range(USER_MAX + 2):
        ok, _ = await rl.check_user(uid)
        last_ok = ok
    assert last_ok is False  # 超限后被拒


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ip_limit_scope():
    rl = RateLimiter()
    ip = f"10.0.0.{uuid.uuid4().hex[:2]}"
    ok, _ = await rl.check_ip(ip)
    assert ok  # IP 与 user 是独立 key，互不影响
