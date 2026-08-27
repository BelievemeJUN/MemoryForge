"""任务管理器集成测试（M4-2 / P0-A-2 / P1-G-1）。需要 Redis（默认 6380）。"""
import pytest

from tasks.manager import TaskManager
from tasks.models import TaskStatus


@pytest.mark.integration
@pytest.mark.asyncio
async def test_namespace_isolation():
    a = TaskManager(user_id="42")
    b = TaskManager(user_id="7")
    ta = await a.create(payload={"code": "print(1)"})
    tb = await b.create(payload={"code": "print(2)"})
    la = [t.id for t in await a.list()]
    lb = [t.id for t in await b.list()]
    # 任务互不可见（命名空间隔离）
    assert tb.id not in la and ta.id not in lb
    # 跨租户读不到
    assert await a.get(tb.id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_legal_transition():
    m = TaskManager(user_id="tran")
    t = await m.create()
    r = await m.transition(t.id, TaskStatus.RUNNING)
    assert r.status == TaskStatus.RUNNING
    r2 = await m.transition(t.id, TaskStatus.SUCCEEDED, result={"stdout": "ok"})
    assert r2.status == TaskStatus.SUCCEEDED
    assert r2.result == {"stdout": "ok"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_illegal_transition_rejected():
    m = TaskManager(user_id="tran2")
    t = await m.create()
    # 合法走到 SUCCEEDED（QUEUED→RUNNING→SUCCEEDED）
    await m.transition(t.id, TaskStatus.RUNNING)
    await m.transition(t.id, TaskStatus.SUCCEEDED)
    # SUCCEEDED 后非法流转 → 保持原状态（状态机拒绝）
    r = await m.transition(t.id, TaskStatus.CANCELLED)
    assert r.status == TaskStatus.SUCCEEDED
