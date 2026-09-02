"""exec 链路注入用户画像（写码 + 修复）测试：不依赖 LLM/Docker。"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage


def _fake_model_with(captured):
    """返回记录最后一次 invoke 消息的假模型。"""

    class FakeModel:
        def invoke(self, msgs):
            captured["prompt"] = msgs[0].content
            return AIMessage(
                content="print('ok')",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    return FakeModel()


def test_write_node_injects_profile(monkeypatch):
    from workflow import exec_loop as m

    captured = {}
    monkeypatch.setattr(m, "build_model", lambda task: _fake_model_with(captured))
    state = {
        "task": "打印1到10",
        "plan": "用 for 循环",
        "prefs": "[编码习惯] 用户习惯用中文注释",
        "profile": "用户小朱在准备 AI 面试，喜欢简洁代码、中文注释",
    }
    result = m._write_node(state)
    assert result["code"] == "print('ok')"
    p = captured["prompt"]
    assert "用户小朱在准备 AI 面试" in p          # 画像注入
    assert "用户编码偏好（请尽量遵循）" in p       # prefs 保留
    assert "打印1到10" in p                       # task 在


def test_write_node_skips_empty_profile(monkeypatch):
    from workflow import exec_loop as m

    captured = {}
    monkeypatch.setattr(m, "build_model", lambda task: _fake_model_with(captured))
    m._write_node({"task": "t", "plan": "p", "prefs": "", "profile": ""})
    assert "用户画像" not in captured["prompt"]     # 无画像不注入、format 不崩


def test_fix_node_injects_profile(monkeypatch):
    from workflow import exec_loop as m

    captured = {}
    monkeypatch.setattr(m, "build_model", lambda task: _fake_model_with(captured))
    state = {
        "task": "打印1到10",
        "code": "print(1)",
        "feedback": "期望输出 1..10",
        "profile": "用户偏好 Python 而非 shell",
    }
    out = m._fix_node(state)
    assert out["code"] == "print('ok')"
    assert out["attempts"] == 2
    assert "用户偏好 Python 而非 shell" in captured["prompt"]
    assert "打印1到10" in captured["prompt"]


def test_fix_node_skips_empty_profile(monkeypatch):
    from workflow import exec_loop as m

    captured = {}
    monkeypatch.setattr(m, "build_model", lambda task: _fake_model_with(captured))
    m._fix_node({"task": "t", "code": "c", "feedback": "f", "profile": ""})
    assert "用户画像" not in captured["prompt"]


@pytest.mark.asyncio
async def test_exec_node_passes_profile_to_subgraph(monkeypatch):
    """对话 exec 节点：把拉到的用户画像传进 exec 子图。"""
    from chat.graph import _exec_node

    received = {}

    class FakeExecGraph:
        async def ainvoke(self, state, **kw):
            received.update(state)
            return {"final": "ok", "tokens": 7}

    monkeypatch.setattr("workflow.exec_loop.get_exec_graph", lambda: FakeExecGraph())

    async def _fake_profile(user_id):
        return "用户小朱，准备 AI 面试，偏好中文注释"

    async def _no_memories(user_id, query, profile):
        return []

    monkeypatch.setattr("chat.graph._get_user_profile", _fake_profile)
    monkeypatch.setattr("chat.graph._retrieve_memories", _no_memories)

    state = {"user_id": "1", "messages": [HumanMessage(content="写个函数算斐波那契")]}
    out = await _exec_node(state)
    assert received["profile"] == "用户小朱，准备 AI 面试，偏好中文注释"
    assert received["task"]  # 完整任务透传
    assert out["messages"][-1].content == "ok"
    assert out["tokens"] == 7
