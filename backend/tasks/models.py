"""任务状态机数据模型（M4-2）。"""
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """任务状态（状态机合法流转见 TaskManager._ALLOWED_TRANSITIONS）。"""

    QUEUED = "queued"        # 排队中（刚创建）
    RUNNING = "running"      # 执行中
    SUCCEEDED = "succeeded"  # 成功（终态）
    FAILED = "failed"        # 失败（终态）
    CANCELLED = "cancelled"  # 已取消（终态）


@dataclass
class Task:
    """一个任务。存储在 Redis hash：task:{id}。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_type: str = "code_exec"     # 任务类型（扩展用）
    status: TaskStatus = TaskStatus.QUEUED
    payload: dict[str, Any] = field(default_factory=dict)  # 任务内容
    result: dict[str, Any] = field(default_factory=dict)   # 结果
    error: str = ""                  # 失败原因
    retries: int = 0                 # 已重试次数（worker 失败重试用）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_type": self.task_type,
            "status": self.status.value,
            "payload": self.payload,
            "result": self.result,
            "error": self.error,
            "retries": self.retries,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d["id"],
            task_type=d.get("task_type", "code_exec"),
            status=TaskStatus(d["status"]),
            payload=d.get("payload", {}),
            result=d.get("result", {}),
            error=d.get("error", ""),
            retries=int(d.get("retries", 0) or 0),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
        )
