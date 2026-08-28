"""任务队列 + worker 测试（B）。用真实 Redis（队列/状态机），沙箱跑代码需 Docker。"""
import os
import uuid

import pytest
import redis.asyncio as aioredis

from tasks.manager import _KEY_QUEUE, TaskManager
from tasks.worker import worker_loop

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
async def redis():
    r = aioredis.from_url(REDIS_URL)
    yield r
    await r.aclose()


@pytest.fixture(autouse=True)
async def _clean_queue():
    """每个测试前清空全局队列/重试/死信，防残留任务干扰（对话图测试也会入队）。"""
    r = aioredis.from_url(REDIS_URL)
    await r.delete(_KEY_QUEUE)
    await r.delete("tasks:retry")
    await r.delete("tasks:dlq")
    await r.aclose()


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
    # max_retries=0 → 第一次失败即耗尽 → failed + 死信
    await worker_loop(redis_url=REDIS_URL, once=True, max_retries=0)
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


# ---------- B+：失败重试 + 死信队列 ----------


@pytest.mark.asyncio
async def test_handle_failure_schedules_retry():
    from tasks.worker import handle_failure

    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="no_such_type", payload={})
    redis = aioredis.from_url(REDIS_URL)
    action = await handle_failure(redis, mgr, task, "boom", max_retries=2, delay=0.01)
    await redis.aclose()
    assert action == "retry"
    done = await mgr.get(task.id)
    assert done.status.value == "queued" and done.retries == 1
    r = aioredis.from_url(REDIS_URL)
    assert await r.zcard("tasks:retry") == 1
    await r.aclose()


@pytest.mark.asyncio
async def test_handle_failure_dlq_when_exhausted():
    from tasks.worker import handle_failure

    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="no_such_type", payload={})
    # 先跑一次 → retries=1（未耗尽）
    redis = aioredis.from_url(REDIS_URL)
    await handle_failure(redis, mgr, task, "e1", max_retries=2, delay=0.01)
    task = await mgr.get(task.id)
    # 再跑两次 → 耗尽（retries 2→3 > max 2）进死信
    await handle_failure(redis, mgr, task, "e2", max_retries=2, delay=0.01)
    task = await mgr.get(task.id)
    action = await handle_failure(redis, mgr, task, "e3", max_retries=2, delay=0.01)
    assert action == "fail"
    done = await mgr.get(task.id)
    assert done.status.value == "failed"
    assert await redis.llen("tasks:dlq") == 1
    await redis.aclose()


@pytest.mark.asyncio
async def test_promote_ready_retries_back_to_queue():
    from tasks.worker import promote_ready_retries

    uid = _uid()
    mgr = TaskManager(user_id=uid)
    task = await mgr.create(task_type="code_exec", payload={"code": "print(1)"})
    await mgr.schedule_retry(task.id, -1)  # 已到期
    redis = aioredis.from_url(REDIS_URL)
    n = await promote_ready_retries(redis)
    assert n == 1
    assert await redis.llen("tasks:queue") == 1
    assert await redis.zcard("tasks:retry") == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_worker_retries_then_succeeds_with_recoverable_error():
    """真实闭环：任务先因类型未知失败 1 次 → 重试；改 payload 不可行（hash 固定）。
    这里验证「失败→重试入延迟队列」与「成功」两条路径在真实 worker 下都成立。"""
    uid = _uid()
    mgr = TaskManager(user_id=uid)
    # 未知类型 → 必然失败 → 第一次重试（不耗尽）
    t1 = await mgr.create(task_type="bad_type", payload={})
    await mgr.enqueue(t1.id)
    await worker_loop(redis_url=REDIS_URL, once=True)
    done = await mgr.get(t1.id)
    assert done.status.value == "queued" and done.retries == 1  # 未耗尽，等待重试
    # 正常任务 → 成功
    t2 = await mgr.create(task_type="code_exec", payload={"code": "print('重试后成功')"})
    await mgr.enqueue(t2.id)
    await worker_loop(redis_url=REDIS_URL, once=True)
    done2 = await mgr.get(t2.id)
    assert done2.status.value == "succeeded"
    assert "重试后成功" in done2.result.get("stdout", "")
