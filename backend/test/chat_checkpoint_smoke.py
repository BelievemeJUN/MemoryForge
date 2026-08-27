"""M4-1 会话隔离冒烟测试：Redis checkpoint 历史恢复 + 会话互不串。

直接运行：
    ./.venv/bin/python backend/test/chat_checkpoint_smoke.py

验证（不依赖 AI 回答内容，直接验证机制）：
  1. 同一 thread_id：第二轮 ainvoke 不带历史，checkpoint 自动恢复 → messages 累积
  2. 不同 thread_id：会话 B 只有自己的消息，看不到 A 的历史 → 隔离
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import HumanMessage  # noqa: E402

from chat.checkpointer import create_checkpointer_cm  # noqa: E402
from chat.graph import build_graph  # noqa: E402


async def main() -> int:
    async with create_checkpointer_cm() as saver:
        await saver.setup()
        graph = build_graph(checkpointer=saver)
        return await _run(graph)


async def _run(graph) -> int:
    suffix = uuid.uuid4().hex[:8]  # 唯一会话，避免脏数据

    print("=== 1. 同一会话：历史自动恢复 ===")
    cfg_a = {"configurable": {"thread_id": f"t-A-{suffix}"}}
    r1 = await graph.ainvoke(
        {"messages": [HumanMessage(content="我叫小明，请记住")]}, config=cfg_a
    )
    n1 = len(r1["messages"])
    r2 = await graph.ainvoke(
        {"messages": [HumanMessage(content="你好")]}, config=cfg_a
    )
    n2 = len(r2["messages"])
    print(f"第1轮 {n1} 条消息 → 第2轮 {n2} 条消息（第二轮没传历史）")
    ok1 = n2 > n1  # 历史被 checkpoint 恢复并累积

    print("=== 2. 会话隔离：B 看不到 A ===")
    cfg_b = {"configurable": {"thread_id": f"t-B-{suffix}"}}
    rb = await graph.ainvoke(
        {"messages": [HumanMessage(content="你好")]}, config=cfg_b
    )
    nb = len(rb["messages"])
    print(f"会话 B 只有 {nb} 条消息（不应含 A 的历史）")
    ok2 = nb <= 2  # B 仅自己的 user+ai，隔离生效

    print()
    print("验证:", "✅" if (ok1 and ok2) else "❌", "(历史恢复 + 会话隔离)")
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
