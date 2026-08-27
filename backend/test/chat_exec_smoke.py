"""M2-4 执行子图冒烟：意图=code → 执行子图(plan→write→execute) → 沙箱跑 → 返回结果。

直接运行：
    ./.venv/bin/python backend/test/chat_exec_smoke.py

真实链路：LLM 意图判断(1 次) + 规划(1 次) + 写码(1 次) + Docker 沙箱执行。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import HumanMessage  # noqa: E402

from chat.graph import build_graph  # noqa: E402


async def main():
    graph = build_graph()
    r = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content="帮我写一个计算斐波那契数列第 N 项的 Python 函数，并输出第 10 项的值"
                )
            ],
            "user_id": "1",
            "thread_id": "t1",
        }
    )
    intent = r.get("intent")
    reply = r["messages"][-1].content
    print("意图:", intent)
    print("回复:\n", reply)
    ok = intent == "code" and "55" in reply
    print("\n验证:", "✅" if ok else "❌", "(期望 intent=code 且输出含 55)")


if __name__ == "__main__":
    asyncio.run(main())
