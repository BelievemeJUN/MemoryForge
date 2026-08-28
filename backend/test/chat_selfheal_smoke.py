"""M3-2 自愈循环冒烟测试。

直接运行：
    ./.venv/bin/python backend/test/chat_selfheal_smoke.py

验证两件事：
  1. 自愈成功：故意给错误代码（缺基线的 fib → RecursionError），fix 修到通过
  2. 硬熔断：max_attempts=1，第一轮失败即熔断，返回原因（诚实收尾）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workflow.exec_loop import get_exec_graph  # noqa: E402


def main():
    graph = get_exec_graph()

    print("=== 1. 自愈成功：错误代码 → fix 修到通过 ===")
    # verify 节点为 async（P2 目标模式 judge），统一用 ainvoke
    r = asyncio.run(graph.ainvoke(
        {
            "task": "实现斐波那契数列第 N 项并输出第 10 项的值",
            # 故意缺基线 → RecursionError，让 fix 有机会修复
            "code": "def fib(n):\n    return fib(n - 1) + fib(n - 2)\nprint(fib(10))",
            "tests": [{"expected": "55", "mode": "exact", "desc": "fib(10)"}],
            "max_attempts": 3,
        }
    ))
    print(f"passed={r.get('passed')} attempts={r.get('attempts')}")
    print("final:", r["final"][:300].replace("\n", " / "))
    ok1 = r.get("passed") and "55" in r["final"]
    print("验证:", "✅" if ok1 else "❌", "(期望自愈通过且输出 55)")

    print()
    print("=== 2. 硬熔断：max_attempts=1 第一轮失败即熔断 ===")
    r2 = asyncio.run(graph.ainvoke(
        {
            "task": "输出 100 个数字 7",
            "code": "print('7')",
            "tests": [{"expected": "7" * 100, "mode": "exact", "desc": "100个7"}],
            "max_attempts": 1,
        }
    ))
    print(f"passed={r2.get('passed')} attempts={r2.get('attempts')}")
    print("final:", r2["final"][:200].replace("\n", " / "))
    ok2 = (not r2.get("passed")) and "熔断" in r2["final"]
    print("验证:", "✅" if ok2 else "❌", "(期望熔断且说明原因)")

    print()
    print("总验证:", "✅ 全通过" if (ok1 and ok2) else "❌ 有失败")
    sys.exit(0 if (ok1 and ok2) else 1)


if __name__ == "__main__":
    main()
