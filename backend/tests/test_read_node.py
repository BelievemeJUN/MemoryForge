"""read 节点升级测试：检索事实为主 + 画像/记忆注入（事实优先）、memory 吃 profile。"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage


def _fake_model(captured):
    class FakeModel:
        def invoke(self, msgs):
            captured["msgs"] = msgs
            return AIMessage(
                content="根据检索内容组织：用户偏好相关。",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

    return FakeModel()


def _hit_memory_results():
    return {"semantic": [{"id": "1", "content": "用户去年做过爬虫项目，用 requests"}]}


@pytest.mark.asyncio
async def test_read_memory_uses_intent_profile(monkeypatch):
    """memory 意图检索配额吃意图给的 profile（不再硬编码 1/3/3/2）。"""
    from chat.graph import _read_node

    captured = {"hybrid_kwargs": None, "msgs": None}

    class FakeMilvus:
        async def hybrid_retrieval_memories(self, query, **kw):
            captured["hybrid_kwargs"] = kw
            return _hit_memory_results()

    async def _fake_pg_conn(*a, **k):
        return None

    import milvus_client, postgresql_client

    monkeypatch.setattr(milvus_client, "get_milvus_client", lambda: _await(FakeMilvus()))
    monkeypatch.setattr(postgresql_client, "get_postgresql_client", _fake_pg_conn)
    monkeypatch.setattr("chat.graph.build_chat_model", lambda: _fake_model(captured))

    async def _fake_profile(user_id):
        return "用户小朱，喜欢简洁结构化回答"

    monkeypatch.setattr("chat.graph._get_user_profile", _fake_profile)

    state = {
        "intent": "memory",
        "memory": {"use_memory": True, "summary": 0, "semantic": 4, "episodic": 5, "procedural": 0},
        "user_id": "1",
        "messages": [HumanMessage(content="我之前说过什么关于爬虫的吗？")],
    }
    out = await _read_node(state)

    # 配额来自 profile 而非硬编码
    kw = captured["hybrid_kwargs"]
    assert kw["semantic_k"] == 4 and kw["episodic_k"] == 5 and kw["procedural_k"] == 0
    # 事实权威 system 在首位，画像与事实次之，query 最后
    msgs = captured["msgs"]
    assert "不得遗漏、增删" in msgs[0].content      # READ_GENERATE_SYSTEM（事实权威）
    assert "用户小朱" in msgs[1].content             # 画像注入
    assert "记忆检索结果" in msgs[2].content or "爬虫" in msgs[2].content  # 事实主体
    assert isinstance(msgs[-1], HumanMessage)
    assert out["messages"][-1].content == "根据检索内容组织：用户偏好相关。"


@pytest.mark.asyncio
async def test_read_memory_empty_skips_llm(monkeypatch):
    """memory 检索为空 → 直接返回提示，不调 LLM。"""
    from chat.graph import _read_node

    class FakeMilvus:
        async def hybrid_retrieval_memories(self, query, **kw):
            return {}  # 空 → _format_memories 返回"未检索到"

    import milvus_client

    monkeypatch.setattr(milvus_client, "get_milvus_client", lambda: _await(FakeMilvus()))

    def _boom(*a, **k):
        raise AssertionError("检索为空不应调 LLM")

    monkeypatch.setattr("chat.graph.build_chat_model", _boom)

    state = {
        "intent": "memory",
        "user_id": "1",
        "messages": [HumanMessage(content="我聊过什么？")],
    }
    out = await _read_node(state)
    assert "未检索到" in out["messages"][-1].content


@pytest.mark.asyncio
async def test_read_kb_empty_skips_llm(monkeypatch):
    """kb 检索为空 → 返回知识库未找到提示，不调 LLM。"""
    from chat.graph import _read_node

    class FakeMilvus:
        async def hybrid_retrieval_knowledge_base(self, query, **kw):
            return []

    import milvus_client

    monkeypatch.setattr(milvus_client, "get_milvus_client", lambda: _await(FakeMilvus()))

    def _boom(*a, **k):
        raise AssertionError("检索为空不应调 LLM")

    monkeypatch.setattr("chat.graph.build_chat_model", _boom)

    state = {
        "intent": "kb",
        "user_id": "1",
        "knowledge_base_id": "默认知识库",
        "messages": [HumanMessage(content="查一下 QPS 优化方案")],
    }
    out = await _read_node(state)
    assert "未检索到相关内容" in out["messages"][-1].content


def _await(v):
    class _A:
        def __init__(self, vv):
            self.v = vv

        def __await__(self):
            yield
            return self.v

    return _A(v)
