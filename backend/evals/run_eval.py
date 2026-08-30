"""M5 评测运行器：跑题目集 → 四层指标 → 输出 markdown 报告。

用法：
    ./.venv/bin/python backend/evals/run_eval.py backend/evals/cases/demo_cases.yaml

面试可讲：
  - 普通题走「执行子图」（生成→沙箱→验证→自愈），统计通过率/自愈率/成本
  - 安全题不走 LLM 生成（那会让模型拒绝），直接构造危险代码打沙箱，统计拦截率
  - 所有比例指标带 bootstrap 置信区间，诚实呈现样本量小的不确定性
"""
import asyncio
import os
import sys
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evals.metrics import (  # noqa: E402
    EvalReport,
    compute_case_metrics,
    compute_cost_metrics,
    compute_loop_metrics,
    compute_retrieval_metrics,
    compute_security_metrics,
)
from sandbox.executor import DockerExecutor  # noqa: E402
from workflow.exec_loop import get_exec_graph  # noqa: E402


async def run_case(graph, case: dict) -> dict:
    """跑一道题，返回指标明细。"""
    # 检索级（RAG 知识库召回）：query → 混合检索 → 父块 → 判是否命中期望关键词
    if case.get("retrieval"):
        from milvus_client import get_milvus_client  # lazy
        from postgresql_client import get_postgresql_client  # lazy

        mc = await get_milvus_client()
        pg = await get_postgresql_client()
        uid = case.get("user_id", 1)
        parent_ids = await mc.hybrid_retrieval_knowledge_base(
            case["query"], case["kb_id"], case.get("top_k", 3), uid
        )
        texts = await pg.get_parents(parent_ids, case["kb_id"], uid) if parent_ids else []
        hit = any(case["expected_keyword"] in (t or "") for t in texts)
        return {
            "passed": hit,
            "is_retrieval": True,
            "retrieved": len(texts),
            "keyword": case.get("expected_keyword", ""),
            "attempts": 1,
            "self_healed": False,
            "passed_one_shot": hit,
            "executions": 0,
            "tokens": 0,
            "timed_out": False,
            "ran_ok": True,
            "blocked": False,
            "final": f"召回 {len(texts)} 父块, 命中={hit}",
        }

    executor = DockerExecutor()

    # 安全题（静态拦截）：直接构造危险代码打沙箱（不依赖 LLM 生成，避免模型拒绝）
    if case.get("expect_blocked"):
        result = executor.run_python(case["danger_code"])
        return {
            "passed": bool(result.security_blocked),
            "blocked": result.security_blocked,
            "is_security": True,
            "is_static": True,
            "is_resource": False,
            "resource_killed": False,
            "timed_out": False,
            "ran_ok": True,
            "attempts": 1,
            "self_healed": False,
            "passed_one_shot": False,
            "executions": 1,
            "tokens": 0,
            "final": (result.stderr or "")[:80],
        }

    # 安全题（资源滥用）：代码本身不危险，靠配额兜底（pids/mem/超时/磁盘）
    # 判定「被防住」：静态拦截 / 超时 / 非正常退出（OOM/进程限制/磁盘满）任一即可
    if case.get("expect_resource_killed"):
        result = executor.run_python(case["danger_code"])
        killed = (
            result.security_blocked
            or result.timed_out
            or (result.exit_code not in (0, None))
        )
        return {
            "passed": killed,
            "blocked": False,
            "is_security": True,
            "is_static": False,
            "is_resource": True,
            "resource_killed": killed,
            "timed_out": result.timed_out,
            "ran_ok": False,
            "attempts": 1,
            "self_healed": False,
            "passed_one_shot": False,
            "executions": 1,
            "tokens": 0,
            "final": f"exit={result.exit_code} timed_out={result.timed_out}",
        }

    # 普通题：执行子图（生成→沙箱→验证→自愈，硬预算熔断）
    # pre_code 存在时直接进执行（自愈评测：给初始错误代码，测 fix 能否修对）
    result = await graph.ainvoke(
        {
            "task": case["task"],
            "code": case.get("pre_code"),   # None 时 start_router 走 plan→write
            "tests": case["tests"],
            "max_attempts": case.get("max_attempts", 3),
        }
    )
    passed = result.get("passed", False)
    attempts = result.get("attempts", 1)
    return {
        "passed": passed,
        "blocked": False,
        "is_security": False,
        "is_static": False,
        "is_resource": False,
        "resource_killed": False,
        "timed_out": result.get("timed_out", False),
        "ran_ok": result.get("exit_code") == 0,
        "attempts": attempts,
        "self_healed": passed and attempts > 1,   # 首轮失败但最终通过
        "passed_one_shot": passed and attempts == 1,
        "executions": attempts,                    # 每次 execute 算一次沙箱执行
        "tokens": result.get("tokens", 0),
        "final": (result.get("final") or "")[:100],
    }


async def main(case_paths: list[str]) -> int:
    graph = get_exec_graph()
    cases = []
    for p in case_paths:
        cases.extend(yaml.safe_load(open(p, encoding="utf-8")) or [])

    print(f"=== 评测 {len(cases)} 题（{', '.join(p.rsplit('/', 1)[-1] for p in case_paths)}） ===")
    results = []
    for case in cases:
        r = await run_case(graph, case)
        results.append(r)
        mark = "✅" if r["passed"] else "❌"
        print(
            f"{mark} {case['id']:<14} attempts={r['attempts']} "
            f"tokens={r['tokens']} | {r['final'][:50].replace(chr(10), ' / ')}"
        )

    report = EvalReport(
        answer=compute_case_metrics(results),
        retrieval=compute_retrieval_metrics(results),
        loop=compute_loop_metrics(results),
        security=compute_security_metrics(results),
        cost=compute_cost_metrics(results),
    )
    md = report.to_markdown()
    out_path = os.path.join(os.path.dirname(case_paths[0]), "report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print()
    print(md)
    print(f"\n报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    default = [os.path.join(os.path.dirname(__file__), "cases", "demo_cases.yaml")]
    paths = sys.argv[1:] if len(sys.argv) > 1 else default
    sys.exit(asyncio.run(main(paths)))
