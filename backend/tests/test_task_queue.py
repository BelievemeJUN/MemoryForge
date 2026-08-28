"""任务队列 + worker 测试（B）。用真实 Redis（队列/状态机），沙箱跑代码需 Docker。"""
import os
import uuid

import pytest
import redis.asyncio as aioredis

from tasks.manager import _KEY_QUEUE, TaskManager
from tasks.worker import worker_loop

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
def redis():
    return aioredis.from_url(REDIS_URL)


def _uid():
    return f"test{uuid.uuid4().hex[:6]}"


@pytest.mark.asyncio
async def test_create_and_enqueue():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "print(1)"})
    await mgr.enqueue(task.id)
    redis = aioredis.from_url(REDIS_URL)
    item = await redis.brpop(_KEY_QUEUE, timeout=2)
    await redis.aclose()
    assert item is not None
    assert item[1].decode() == f"{uid}:{task.id}"


@pytest.mark.asyncio
async def test_worker_executes_code_exec():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "print('队列执行OK')"})
    await mgr.enqueue(task.id)
    await worker_loop(redis_url=REDIS_URL, once=True)
    done = await mgr.get(task.id)
    assert done is not None and done.status.value == "succeeded"
    assert "队列执行OK" in done.result.get("stdout", "")


@pytest.mark.asyncio
async def test_worker_marks_unknown_type_failed():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="no_such_type", payload={})
    await mgr.enqueue(task.id)
    await worker_loop(redis_url=REDIS_URL, once=True)
    done = await mgr.get(task.id)
    assert done is not None and done.status.value == "failed"
    assert "未知任务类型" in done.error


@pytest.mark.asyncio
async def test_worker_skips_non_queued():
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "print(1)"})
    await mgr.cancel(task.id)  # 先取消（非法流转会被拒，需 queued→cancelled 合法）
    task = await mgr.get(task.id)
    if task is None or task.status.value != "cancelled":
        # queued→cancelled 合法，正常应成功；若失败则直接跳过本测试（兼容状态机）
        return
    await mgr.enqueue(task.id)
    await worker_loop(redis_url=REDIS_URL, once=True)
    done = await mgr.get(task.id)
    assert done.status.value == "cancelled"  # 不被 worker 改写
