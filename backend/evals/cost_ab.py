"""成本级 A/B 对比实验：验证路由（确定性优先）省了多少成本。

对照组：同一批代码题，每题都额外调一次 LLM-as-judge（模拟"无验证路由"，验证全走 judge）
实验组：验证路由（有显式期望输出 → 只做确定性比对，不触发 judge）
成本降低 % = judge_token / (执行 token + judge token)

真实 LLM + 沙箱跑，输出可比对的两组成本。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

from verifier.judge import llm_judge  # noqa: E402
from workflow.exec_loop import get_exec_graph  # noqa: E402

N = int(os.getenv("COST_AB_N", "12"))  # 对比题目数（答案级前 N 道）


async def main():
    cases_path = os.path.join(os.path.dirname(__file__), "cases", "answer_cases.yaml")
    cases = (yaml.safe_load(open(cases_path, encoding="utf-8")) or [])[:N]

    graph = get_exec_graph()
    exec_tokens, judge_tokens, execs, passed = 0, 0, 0, 0
    print(f"=== 成本 A/B 对比：{len(cases)} 道答案级题 ===")
    for c in cases:
        # 实验组：验证路由（确定性比对，不 judge）
        r = await graph.ainvoke(
            {"task": c["task"], "tests": c["tests"], "max_attempts": c.get("max_attempts", 3)}
        )
        exec_tokens += int(r.get("tokens", 0) or 0)
        execs += int(r.get("executions", 1) or 1)
        if r.get("passed"):
            passed += 1

        # 对照组：模拟无验证路由——每题额外调一次 judge（多一次 LLM 推理）
        from chat.model_router import build_model  # lazy

        judge_model = build_model(c["task"])
        _p, _reason, jt = await llm_judge(
            judge_model, c["task"], r.get("plan", ""),
            r.get("stdout", ""), r.get("stderr", ""),
        )
        judge_tokens += int(jt or 0)

    control = exec_tokens + judge_tokens
    save_pct = (judge_tokens / control * 100) if control else 0
    print(f"执行（验证路由）token: {exec_tokens}（{execs} 次沙箱执行）")
    print(f"额外 judge token: {judge_tokens}（无验证路由才会产生）")
    print(f"对照组总 token（全 judge）: {control}")
    print(f"→ 验证路由使验证阶段成本降低 {save_pct:.1f}%")
    print(f"（同批题通过 {passed}/{len(cases)}，执行 token 未变，省的就是 judge 那次推理）")

    # 写结果到 report
    out = os.path.join(os.path.dirname(__file__), "cases", "cost_ab.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(
            f"cost_ab_n={len(cases)}\nexec_tokens={exec_tokens}\n"
            f"judge_tokens={judge_tokens}\ncontrol_tokens={control}\nsave_pct={save_pct:.1f}\n"
        )
    print(f"结果写入 {out}")


if __name__ == "__main__":
    asyncio.run(main())
