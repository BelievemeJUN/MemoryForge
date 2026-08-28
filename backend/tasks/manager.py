"""任务状态机管理（M4-2）：Redis 存储，状态合法流转 + 可查可取消。

存储设计（面试可讲）：
  - task:{id}  -> Redis Hash（任务本身，字段可热更新）
  - tasks:all  -> Redis ZSet（按创建时间排序的任务索引，用于列表/分页）

状态机（合法流转，非法流转拒绝）：
  queued → running → succeeded | failed
     |___________ cancelled（queued/running 可取消）____________|
"""
import json
import logging
import os
import time
from typing import Optional

import redis.asyncio as aioredis
from dotenv import load_dotenv

from .models import Task, TaskStatus

logger = logging.getLogger(__name__)
load_dotenv()

# 合法状态流转（from: allowed next）
_ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RUNNING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

_KEY_TASK = "task:{}"
_KEY_INDEX = "tasks:all"
# B：全局 FIFO 队列（worker 消费）。value = "{user_id}:{task_id}"，worker 据此路由回用户命名空间。
_KEY_QUEUE = "tasks:queue"


def _key_user_prefix(user_id: str) -> tuple[str, str]:
    """P0-A-2：多租户 key 命名空间。空 user 保持旧 key（向后兼容测试）。

    user_id="42" -> ("task:42:{}", "tasks:42:all")；user_id="" -> 旧 key。
    """
    if not user_id:
        return _KEY_TASK, _KEY_INDEX
    return f"task:{user_id}:{{}}", f"tasks:{user_id}:all"


class TaskManager:
    """基于 Redis 的任务管理器（每请求新建，Redis 客户端线程安全）。

    P0-A-2：构造时指定 user_id，任务 key/索引全部带用户前缀，
    不同租户的任务天然隔离、list 只列自己。
    """

    def __init__(self, redis_url: Optional[str] = None, user_id: str = ""):
        self.redis = aioredis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6380/0")
        )
        self.user_id = user_id
        self._key_task, self._key_index = _key_user_prefix(user_id)

    # ---------- 写操作 ----------

    async def create(self, task_type: str = "code_exec", payload: dict | None = None) -> Task:
        """创建任务（queued），返回 Task（含 id）。"""
        task = Task(task_type=task_type, payload=payload or {})
        mapping = task.to_dict()
        # Redis hash 只存字符串：dict 字段序列化
        mapping["payload"] = json.dumps(mapping["payload"], ensure_ascii=False)
        mapping["result"] = json.dumps(mapping["result"], ensure_ascii=False)
        pipe = self.redis.pipeline()
        pipe.hset(self._key_task.format(task.id), mapping=mapping)
        pipe.zadd(self._key_index, {task.id: task.created_at})
        await pipe.execute()
        return task

    async def enqueue(self, task_id: str) -> None:
        """B：任务入队（FIFO）。value 带用户前缀，worker 按此路由回对应命名空间。

        LPUSH + worker BRPOP → 天然 FIFO；多 worker 可横向扩展（每个任务只出队一次）。
        """
        await self.redis.lpush(_KEY_QUEUE, f"{self.user_id}:{task_id}")

    async def transition(self, task_id: str, to: TaskStatus, *, result=None, error: str = "") -> Optional[Task]:
        """状态流转：合法才允许。成功/失败可带结果或错误。"""
        task = await self.get(task_id)
        if task is None:
            return None
        if to not in _ALLOWED_TRANSITIONS.get(task.status, set()):
            logger.warning("非法状态流转: %s -> %s", task.status, to)
            return task  # 返回原状态，不改

        updates = {"status": to.value, "updated_at": time.time()}
        if result is not None:
            updates["result"] = json.dumps(result, ensure_ascii=False)
        if error:
            updates["error"] = error
        await self.redis.hset(self._key_task.format(task_id), mapping=updates)
        return await self.get(task_id)

    async def cancel(self, task_id: str) -> Optional[Task]:
        """取消任务（queued/running 可取消）。"""
        return await self.transition(task_id, TaskStatus.CANCELLED, error="用户取消")

    # ---------- 读操作 ----------

    async def get(self, task_id: str) -> Optional[Task]:
        d = await self.redis.hgetall(self._key_task.format(task_id))
        if not d:
            return None
        decoded = {k.decode(): (v.decode() if isinstance(v, bytes) else v) for k, v in d.items()}
        for field_name in ("result", "payload"):
            if field_name in decoded:
                try:
                    decoded[field_name] = json.loads(decoded[field_name])
                except json.JSONDecodeError:
                    pass
        return Task.from_dict(decoded)

    async def list(self, limit: int = 50) -> list[Task]:
        """按创建时间倒序列出**当前用户**的任务（带 id 索引）。"""
        ids = await self.redis.zrevrange(self._key_index, 0, limit - 1)
        tasks = []
        for tid in ids:
            t = await self.get(tid.decode())
            if t:
                tasks.append(t)
        return tasks
