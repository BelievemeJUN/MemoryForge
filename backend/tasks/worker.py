"""任务队列 worker（B）：后台消费 Redis 队列，执行 queued 任务并回写状态。

面试价值：长耗时任务（评测/批量执行）从请求线程拆出 → 请求立即返回 task_id，
worker 异步执行，前端轮询状态。Redis 队列天然支持多 worker 横向扩展。

运行：
    ./.venv/bin/python -m tasks.worker            # 常驻 worker（可起多个）
    ./.venv/bin/python -m tasks.worker --once     # 处理一个任务后退出（测试用）

流程：BRPOP tasks:queue → 解析 user_id:task_id → 按用户命名空间取任务 →
     transition queued→running → 执行 → transition running→succeeded/failed。
"""
import argparse
import asyncio
import logging
import os

import redis.asyncio as aioredis
from dotenv import load_dotenv

from .manager import _KEY_QUEUE, TaskManager
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


async def worker_loop(redis_url: str | None = None, once: bool = False):
    """消费循环：BRPOP 取任务 → 执行 → 回写状态（状态机保证合法流转）。"""
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6380/0")
    redis = aioredis.from_url(redis_url)
    processed = 0
    while True:
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
            await mgr.transition(task_id, TaskStatus.FAILED, error=str(e))
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
