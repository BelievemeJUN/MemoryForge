"""任务队列 worker（B + B+）：后台消费 Redis 队列，失败重试 + 死信队列。

面试价值：长耗时任务（评测/批量执行）从请求线程拆出 → 请求立即返回 task_id，
worker 异步执行，前端轮询状态。Redis 队列天然支持多 worker 横向扩展。
B+ 深化：执行失败不直接放弃——重试（指数退避延迟队列），耗尽进死信队列(DLQ)，
人工排查/重放，不丢任务、不闪电风暴。

运行：
    ./.venv/bin/python -m tasks.worker            # 常驻 worker（可起多个）
    ./.venv/bin/python -m tasks.worker --once     # 处理一个任务后退出（测试用）

流程：promote 到期重试 → BRPOP tasks:queue → 解析 user_id:task_id →
     按用户命名空间取任务 → queued→running → 执行 → succeeded；
     失败 → retries 未满 → 回 queued + 延迟 ZSET；耗尽 → failed + DLQ。
"""
import argparse
import asyncio
import logging
import os
import time

import redis.asyncio as aioredis
from dotenv import load_dotenv

from .manager import _KEY_QUEUE, _KEY_RETRY, TaskManager
from .models import Task, TaskStatus

logger = logging.getLogger(__name__)
load_dotenv()


async def execute_task(task: Task) -> dict:
    """按任务类型分发执行。code_exec → Docker 沙箱跑代码（默认模板镜像，离线）。"""
    if task.task_type == "code_exec":
        from sandbox.executor import DockerExecutor  # lazy，不拖慢 worker 启动

        code = task.payload.get("code", "")
        res = await DockerExecutor().arun_python(code)  # 内部 asyncio.to_thread
        return {
            "stdout": res.stdout,
            "stderr": res.stderr,
            "exit_code": res.exit_code,
            "timed_out": res.timed_out,
            "security_blocked": res.security_blocked,
            "violations": res.violations,
            "duration": round(res.duration, 3),
        }
    raise ValueError(f"未知任务类型: {task.task_type}")


async def promote_ready_retries(redis) -> int:
    """把到期（score<=now）的重试项搬回主队列。返回搬移数量。"""
    now = time.time()
    ready = await redis.zrangebyscore(_KEY_RETRY, 0, now)
    if not ready:
        return 0
    pipe = redis.pipeline()
    for member in ready:
        pipe.lpush(_KEY_QUEUE, member)  # 与 enqueue 一致用 lpush（worker BRPOP=FIFO）
    pipe.zrem(_KEY_RETRY, *ready)
    await pipe.execute()
    logger.info("重试到期，搬回 %d 个任务", len(ready))
    return len(ready)


async def handle_failure(
    redis, mgr: TaskManager, task: Task, error: str, max_retries: int, delay: float = 0.0
) -> str:
    """失败处理：重试未满 → 回 queued + 延迟重试；耗尽 → failed + 死信。返回动作。"""
    retries = (task.retries or 0) + 1
    if retries <= max_retries:
        await mgr.transition(task.id, TaskStatus.QUEUED)
        # transition 不碰 retries 字段，单独写（避免状态机语义膨胀）
        await mgr.redis.hset(mgr._key_task.format(task.id), mapping={"retries": retries})
        backoff = delay or min(2 ** retries, 30)  # 指数退避：2s/4s/8s…封顶 30s
        await mgr.schedule_retry(task.id, backoff)
        logger.info("任务 %s 失败（第 %d 次），%.1fs 后重试: %s", task.id, retries, backoff, error[:80])
        return "retry"
    await mgr.transition(task.id, TaskStatus.FAILED, error=error)
    await mgr.push_dlq(task.id, error)
    logger.warning("任务 %s 重试耗尽（%d 次）进死信队列: %s", task.id, retries, error[:80])
    return "fail"


async def worker_loop(redis_url: str | None = None, once: bool = False, max_retries: int | None = None):
    """消费循环：promote 重试 → BRPOP 取任务 → 执行 → 回写状态（状态机保证合法流转）。"""
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6380/0")
    max_retries = max_retries if max_retries is not None else int(
        os.getenv("TASK_MAX_RETRIES", "2")
    )
    redis = aioredis.from_url(redis_url)
    processed = 0
    while True:
        await promote_ready_retries(redis)
        item = await redis.brpop(_KEY_QUEUE, timeout=2)
        if not item:
            if once:
                break
            continue
        raw = item[1]
        user_id, _, task_id = raw.decode().partition(":")
        mgr = TaskManager(redis_url=redis_url, user_id=user_id)
        task = await mgr.get(task_id)
        if task is None:
            logger.warning("任务不存在（可能已删）: %s", raw)
            continue
        if task.status != TaskStatus.QUEUED:
            logger.warning("任务状态异常，跳过: %s=%s", task_id, task.status)
            continue

        await mgr.transition(task_id, TaskStatus.RUNNING)
        try:
            result = await execute_task(task)
            await mgr.transition(task_id, TaskStatus.SUCCEEDED, result=result)
            logger.info("任务成功: %s (%s)", task_id, task.task_type)
        except Exception as e:  # noqa: BLE001
            logger.exception("任务执行失败: %s", task_id)
            await handle_failure(redis, mgr, task, str(e), max_retries)
        processed += 1
        if once:
            break
    await redis.aclose()
    return processed


def main():
    parser = argparse.ArgumentParser(description="MemoryForge 任务队列 worker")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出（测试用）")
    args = parser.parse_args()
    n = asyncio.run(worker_loop(once=args.once))
    if args.once:
        print(f"worker 处理完成: {n} 个任务")


if __name__ == "__main__":
    main()

