"""LLM 批量生成评测题（扩题库到 100 题）。

流程：LLM 生成不重复题目（task + expected + solution）→ 本地独立进程跑 solution
      校验 expected 是否准确 → 通过才转成 case，输出 gen_cases.yaml。

安全：生成的 solution 用独立 subprocess + 超时运行，提示词禁止危险模块；
      这是本地校验脚本，信任计算题代码。
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

from chat.llm import build_chat_model  # noqa: E402

GEN_PROMPT = """你是编程题出题专家。生成 {n} 道**不重复**、**简单到中等**的 Python 编程题，用于评测"代码执行 Agent"。

硬性要求：
1. 题目只涉及纯计算/字符串/列表/字典/排序/简单算法，用 Python 标准库即可完成
2. 题目必须**输出确定性结果**（用 print 输出一个明确答案），不能是"实现类"开放题
3. **禁止**使用 os/subprocess/socket/eval/exec/__import__/open/网络 等任何危险或 IO 能力
4. 每道题的 expected 必须是 solution 跑出来的**精确输出字符串**（不要有多余空格）
5. 不要和这些主题重复：{existing}
6. 难度分布：约一半"基础"（加减乘除/字符串简单操作），一半"中等"（排序/去重/词频/嵌套结构）

只输出 JSON 数组（不要任何其他文字），每项：
{{"task": "给 Agent 的需求描述（中文，含具体数据）", "expected": "期望输出（solution 跑出的精确结果）", "solution": "参考 Python 代码（完整可运行）"}}
"""


def run_solution(code: str, timeout: float = 5.0):
    """独立进程跑 solution，返回 (exit_code, stdout, stderr)。"""
    with tempfile.TemporaryDirectory(prefix="casecheck_") as d:
        script = os.path.join(d, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run(
                [sys.executable, script],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=d,
            )
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "TIMEOUT"


def mode_of(expected: str) -> str:
    if expected.count(".") == 1 and expected.replace(".", "").replace("-", "").isdigit():
        return "float"
    if expected.replace("[", "").replace("]", "").replace(",", "").replace(" ", "").isdigit():
        return "fuzzy"  # 列表
    return "exact"


async def generate_cases(n: int) -> list[dict]:
    from langchain_core.messages import HumanMessage, SystemMessage

    existing = [c["id"] for c in yaml.safe_load(open(cases_path, encoding="utf-8"))]
    model = build_chat_model()
    prompt = GEN_PROMPT.format(n=n, existing=", ".join(existing[:20]))
    resp = await model.ainvoke(
        [SystemMessage(content="你是出题助手，只输出 JSON。"), HumanMessage(content=prompt)]
    )
    text = resp.content or ""
    # 提取 JSON 数组
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("LLM 未输出 JSON 数组")
    return json.loads(text[start : end + 1])


async def main():
    n = int(os.getenv("GEN_COUNT", "42"))
    raw = await generate_cases(n)
    ok, fail = [], []
    for item in raw:
        task, expected, solution = item.get("task"), item.get("expected"), item.get("solution")
        if not (task and expected and solution):
            fail.append({"reason": "字段缺失", **item})
            continue
        rc, out, err = run_solution(solution)
        if rc != 0 or out.rstrip() != expected.strip():
            fail.append({"reason": f"期望不符 rc={rc} err={err[:60]}", "expected": expected, "got": out.strip()[:60]})
            continue
        ok.append(
            {
                "id": f"gen_{uuid.uuid4().hex[:8]}",
                "task": task,
                "tests": [{"expected": expected.strip(), "mode": mode_of(expected.strip()), "desc": "LLM 生成题"}],
                "max_attempts": 3,
            }
        )
    # 追加到 gen_cases.yaml
    out_path = os.path.join(os.path.dirname(__file__), "cases", "gen_cases.yaml")
    existing = []
    if os.path.exists(out_path):
        existing = yaml.safe_load(open(out_path, encoding="utf-8")) or []
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing + ok, f, allow_unicode=True, sort_keys=False)
    print(f"生成 {len(raw)} 道 → 校验通过 {len(ok)} 道，失败 {len(fail)} 道")
    for x in fail[:5]:
        print("  失败:", x.get("reason", ""), "|", str(x.get("task", ""))[:40])


cases_path = os.path.join(os.path.dirname(__file__), "cases", "main_cases.yaml")

if __name__ == "__main__":
    asyncio.run(main())
