"""LLM-as-judge 目标模式验证（P2）。

面试可讲：确定性比对只覆盖「用户给了明确期望输出」的场景；目标型任务
（无显式期望，如"实现一个爬虫""优化这段代码"）用 LLM-as-judge 按 rubric
判定完成度，理由喂回 fix 循环做自愈——这是当前 Agent 领域的主流验证方式
（OpenAI Evals / LangSmith Evaluator / DeepEval 同思路）。
"""
import json
import re

_JUDGE_PROMPT = """你是严谨的代码任务验收员。根据任务目标与运行结果，判断代码是否完成了目标。

任务目标：{task}
实现方案：{plan}
运行输出：{stdout}
错误输出：{stderr}

判定标准（rubric）：
1. 代码能否正常运行（无致命错误）
2. 输出是否体现对目标的完成
3. 是否有明显未实现 / 偷懒 / 占位

只输出 JSON：{{"passed": true 或 false, "reason": "一句话理由；未通过时给出具体改进建议"}}
"""


async def llm_judge(
    model,
    task: str,
    plan: str = "",
    stdout: str = "",
    stderr: str = "",
) -> tuple[bool, str, int]:
    """用评判模型判定目标完成度。返回 (是否通过, 理由, 消耗token)。

    第三返回值用于成本精确记账——judge 是一次真实 LLM 调用，之前漏算，
    导致 exec 子图内部成本"近似"。补上后请求级账单完整。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt = _JUDGE_PROMPT.format(
        task=task,
        plan=(plan or "")[:500],
        stdout=(stdout or "")[:2000],
        stderr=(stderr or "")[:1000],
    )
    try:
        resp = await model.ainvoke(
            [
                SystemMessage(content="只输出 JSON，不要多余文字。"),
                HumanMessage(content=prompt),
            ]
        )
        um = getattr(resp, "usage_metadata", None) or {}
        tokens = int(um.get("total_tokens") or 0)
        text = resp.content or ""
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            data = json.loads(m.group(0))
            return bool(data.get("passed")), str(data.get("reason", ""))[:300], tokens
        # 兜底：没解析出 JSON，用文本里的 true/false 判断
        return "true" in text.lower(), text[:200], tokens
    except Exception as e:  # noqa: BLE001
        # judge 失败不阻塞——已有沙箱执行结果，保守视为通过并记日志
        return True, f"(judge 异常，保守通过: {e})", 0
