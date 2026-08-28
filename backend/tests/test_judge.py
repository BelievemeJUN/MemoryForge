"""LLM-as-judge 测试（P2）。用假模型验证解析/兜底/异常安全，无需真实 LLM。"""
import pytest

from verifier.judge import llm_judge


class _FakeModel:
    def __init__(self, text):
        self.text = text

    async def ainvoke(self, messages):
        return type("R", (), {"content": self.text})()


@pytest.mark.asyncio
async def test_judge_parses_passed():
    m = _FakeModel('{"passed": true, "reason": "输出正确"}')
    passed, reason = await llm_judge(m, "任务", "方案", "stdout", "")
    assert passed is True and reason == "输出正确"


@pytest.mark.asyncio
async def test_judge_parses_failed():
    m = _FakeModel('{"passed": false, "reason": "缺少核心逻辑"}')
    passed, reason = await llm_judge(m, "任务", "", "stdout", "")
    assert passed is False and "核心" in reason


@pytest.mark.asyncio
async def test_judge_fallback_on_bad_json():
    m = _FakeModel("... 输出 true 表示通过 ...")
    passed, _ = await llm_judge(m, "任务", "", "", "")
    assert passed is True


@pytest.mark.asyncio
async def test_judge_exception_safe():
    class Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("model down")

    passed, reason = await llm_judge(Boom(), "任务", "", "", "")
    assert passed is True  # judge 异常保守通过，不阻塞主流程
    assert "judge" in reason
