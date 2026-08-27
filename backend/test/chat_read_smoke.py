"""M2-3 后半：读工具节点冒烟（知识库/记忆检索接入 LangGraph）。

直接运行：
    ./.venv/bin/python backend/test/chat_read_smoke.py

验证：intent=kb → read 节点（async）→ 调 Milvus/PostgreSQL 检索 → 返回。
知识库当前为空 → 返回友好提示，证明「图 + async 节点 + 检索调用」链路通。
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
                HumanMessage(content="我的知识库里有什么关于报销流程的资料吗")
            ],
            "user_id": "1",
            "thread_id": "t1",
        }
    )
    intent = r.get("intent")
    reply = r["messages"][-1].content
    print("意图:", intent)
    print("回复:", reply[:200])
    ok = intent == "kb" and "检索" in reply
    print("\n验证:", "✅" if ok else "❌", "(期望 intent=kb 且 read 节点给出检索结果)")


if __name__ == "__main__":
    asyncio.run(main())
