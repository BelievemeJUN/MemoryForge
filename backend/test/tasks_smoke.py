"""M4-2 任务状态机冒烟测试。

直接运行：
    ./.venv/bin/python backend/test/tasks_smoke.py

验证：
  1. 创建 → queued
  2. 合法流转：queued→running→succeeded（可带结果）
  3. 非法流转拒绝：终态(succeeded) 不能再转
  4. 取消：queued → cancelled
  5. 列表查询
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tasks.manager import TaskManager  # noqa: E402
from tasks.models import TaskStatus  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name}  | {detail}")


async def main() -> int:
    mgr = TaskManager()

    print("=== 1. 创建任务 → queued ===")
    t = await mgr.create(payload={"code": "print('hello')"})
    check("创建状态 queued", t.status == TaskStatus.QUEUED, t.status.value)

    print("=== 2. 合法流转：queued→running→succeeded ===")
    r = await mgr.transition(t.id, TaskStatus.RUNNING)
    check("running", r.status == TaskStatus.RUNNING, r.status.value)
    r = await mgr.transition(t.id, TaskStatus.SUCCEEDED, result={"stdout": "hello"})
    check(
        "succeeded 且带结果",
        r.status == TaskStatus.SUCCEEDED and r.result.get("stdout") == "hello",
        f"{r.status.value} {r.result}",
    )

    print("=== 3. 非法流转拒绝 ===")
    r2 = await mgr.transition(t.id, TaskStatus.CANCELLED)
    check("终态后不能转 cancelled", r2.status == TaskStatus.SUCCEEDED, r2.status.value)

    print("=== 4. 取消 queued 任务 ===")
    t2 = await mgr.create(payload={"code": "sleep"})
    r3 = await mgr.cancel(t2.id)
    check("取消成功", r3.status == TaskStatus.CANCELLED, r3.status.value)
    r4 = await mgr.transition(t2.id, TaskStatus.RUNNING)
    check("cancelled 后不能转 running", r4.status == TaskStatus.CANCELLED, r4.status.value)

    print("=== 5. 列表查询 ===")
    tasks = await mgr.list(limit=10)
    check("列表数量 >= 2", len(tasks) >= 2, f"实际 {len(tasks)}")
    print("  最新任务:", [(x.id, x.status.value) for x in tasks[:3]])

    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
