"""用户画像常驻注入（chat 链路）测试：治本——闲聊也能了解用户偏好。"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


@pytest.mark.asyncio
async def test_get_user_profile_invalid_id_returns_empty():
    """非数字 user_id → 不连 PG，直接空串（不崩）。"""
    from chat.graph import _get_user_profile

    assert await _get_user_profile("abc") == ""
    assert await _get_user_profile("") == ""


@pytest.mark.asyncio
async def test_get_user_profile_graceful_on_pg_failure(monkeypatch):
    """PG 拉画像失败 → 空串降级，不阻塞对话。"""
    from chat.graph import _get_user_profile

    import postgresql_client

    async def _boom(*a, **k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(postgresql_client, "get_postgresql_client", _boom)
    assert await _get_user_profile("1") == ""


@pytest.mark.asyncio
async def test_chat_node_injects_profile_as_first_system(monkeypatch):
    """画像应作为第一条 system 注入（在记忆注入之前），模型能据此个性化寒暄。"""
    from chat.graph import _chat_node

    captured = {}

    class FakeModel:
        def invoke(self, msgs):
            captured["msgs"] = msgs
            return AIMessage(content="记得你！你在准备面试～")

    monkeypatch.setattr(
        "chat.graph.build_chat_model", lambda: FakeModel()
    )

    async def _fake_profile(user_id):
        return "用户正在准备 AI 应用开发面试，偏好简洁、结构化回答，喜欢大白话。"

    async def _no_memories(user_id, query, profile):
        return []

    monkeypatch.setattr("chat.graph._get_user_profile", _fake_profile)
    monkeypatch.setattr("chat.graph._retrieve_memories", _no_memories)

    state = {"user_id": "1", "messages": [HumanMessage(content="你好呀，最近怎么样？")]}
    out = await _chat_node(state)

    msgs = captured["msgs"]
    assert isinstance(msgs[0], SystemMessage)
    assert "正在准备 AI 应用开发面试" in msgs[0].content  # 画像在最前
    assert any(isinstance(m, HumanMessage) for m in msgs)  # 历史仍保留
    assert out["messages"][-1].content == "记得你！你在准备面试～"
