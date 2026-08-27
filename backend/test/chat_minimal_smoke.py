"""M2-1 最小对话图冒烟测试。

直接运行（无需 pytest）：
    ./.venv/bin/python backend/test/chat_minimal_smoke.py

验证两件事（对应 M2-1 验收）：
  1. LangGraph 状态机骨架能跑通：invoke → 节点执行 → 返回 assistant 消息
  2. 状态累积（短期记忆）：第二轮把历史传进去，模型能回忆第一轮内容
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from chat.graph import build_graph  # noqa: E402

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


async def main():
    graph = build_graph()

    print("=== 第一轮：打招呼 ===")
    r1 = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="你好！请用一句话介绍你自己，并记住我的名字叫小明。")],
            "user_id": "1",
            "thread_id": "t1",
        }
    )
    ai1 = r1["messages"][-1]
    check(
        "返回 assistant 消息",
        isinstance(ai1, AIMessage) and len(ai1.content) > 0,
        repr(ai1.content[:60]),
    )
    print("   AI:", ai1.content[:100])

    print("=== 第二轮：回忆类问题 → 意图路由到 memory（read 节点）===")
    history = [*r1["messages"], HumanMessage(content="我叫什么名字？")]  # 手动传递历史
    r2 = await graph.ainvoke(
        {"messages": history, "user_id": "1", "thread_id": "t1"}
    )
    intent2 = r2.get("intent")
    ai2 = r2["messages"][-1]
    check(
        "回忆类问题路由到 memory（记忆检索）",
        intent2 == "memory" and "记忆" in ai2.content,
        f"intent={intent2}, reply={ai2.content[:40]!r}",
    )
    print("   AI:", ai2.content[:100])

    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
