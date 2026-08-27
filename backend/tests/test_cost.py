"""成本记账/配额测试（P2-K）。需 Redis（默认 6380）。"""
import uuid

import pytest

from cost import CostTracker, DAILY_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_and_get_usage():
    c = CostTracker()
    uid = f"cost-{uuid.uuid4().hex[:8]}"
    await c.add_usage(uid, 100)
    await c.add_usage(uid, 50)
    assert await c.get_usage(uid) == 150


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_allow_and_reject():
    c = CostTracker()
    uid = f"budget-{uuid.uuid4().hex[:8]}"
    # 预算内：放行
    ok, total = await c.check_budget(uid, 5000)
    assert ok
    # 累计超预算：拒绝
    await c.add_usage(uid, DAILY_BUDGET)
    ok2, total2 = await c.check_budget(uid, 1000)
    assert not ok2
    assert total2 > DAILY_BUDGET


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_zero_noop():
    c = CostTracker()
    uid = f"cost0-{uuid.uuid4().hex[:8]}"
    await c.add_usage(uid, 0)
    assert await c.get_usage(uid) == 0
