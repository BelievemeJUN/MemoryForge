"""M2-3 意图判断 + 条件路由冒烟测试。

直接运行（无需 pytest）：
    ./.venv/bin/python backend/test/chat_intent_smoke.py

验证四类意图判断 + 路由是否正常（真实调用 LLM，约 8 次请求）。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from langchain_core.messages import HumanMessage  # noqa: E402

from chat.graph import build_graph  # noqa: E402

# (输入, 期望意图)
CASES = [
    ("你好呀，今天天气怎么样", "chat"),
    ("帮我写一个冒泡排序的 Python 代码", "code"),
    ("我的知识库里有哪些资料", "kb"),
    ("我上次问了你什么来着", "memory"),
]

PASS = 0
FAIL = 0


async def main():
    graph = build_graph()
    for msg, expect in CASES:
        r = await graph.ainvoke(
            {
                "messages": [HumanMessage(content=msg)],
                "user_id": "1",
                "thread_id": "t1",
            }
        )
        intent = r.get("intent")
        ok = intent == expect
        global PASS, FAIL
        if ok:
            PASS += 1
            print(f"✅ 意图={intent} (期望 {expect}) | 输入: {msg}")
        else:
            FAIL += 1
            print(f"❌ 意图={intent} (期望 {expect}) | 输入: {msg}")
        print(f"    回复: {r['messages'][-1].content[:60]}")

    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
