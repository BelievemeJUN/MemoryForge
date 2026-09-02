"""记忆检索配置（意图→MemoryProfile）单测：钳制 / 兜底 / 短路，纯逻辑无需服务。"""
import pytest

from chat.graph import (
    MemoryProfile,
    _clamp_k,
    _default_memory_profile,
    _resolve_memory_profile,
)


def test_clamp_k_bounds():
    assert _clamp_k(3) == 3
    assert _clamp_k(99) == 5      # 越上界 → 钳到 5
    assert _clamp_k(-2) == 0      # 越下界 → 钳到 0
    assert _clamp_k("abc") == 0   # 非数字 → 0
    assert _clamp_k(None) == 0
    assert _clamp_k(1, 0, 1) == 1  # summary 限 0-1


def test_default_profile_by_intent():
    code = _default_memory_profile("code")
    assert code["semantic"] == 3 and code["episodic"] == 2 and code["procedural"] == 3
    mem = _default_memory_profile("memory")
    assert mem["summary"] == 1 and mem["episodic"] == 3
    chat = _default_memory_profile("chat")
    assert chat["procedural"] == 0  # 普通对话兜底不查程序记忆


def test_resolve_profile_normal():
    class R:
        intent = "chat"
        memory = MemoryProfile(use_memory=True, semantic=3, episodic=3)

    p = _resolve_memory_profile(R(), "chat")
    assert p["semantic"] == 3 and p["episodic"] == 3 and p["procedural"] == 0


def test_resolve_profile_clamps_bad_k():
    # pydantic 层会拦截非 int 类型；这里测「合法但越界」的数字（99/-3）→ 钳制
    class R:
        intent = "chat"
        memory = MemoryProfile(use_memory=True, semantic=99, episodic=-3)

    p = _resolve_memory_profile(R(), "chat")
    assert p["semantic"] == 5 and p["episodic"] == 0 and p["procedural"] == 0


def test_resolve_profile_missing_falls_back():
    """memory 字段缺失/结构异常 → 降级硬编码兜底（不崩）。"""
    class R:
        intent = "code"
        memory = None

    p = _resolve_memory_profile(R(), "code")
    assert p == _default_memory_profile("code")


def test_resolve_profile_use_memory_false():
    class R:
        intent = "chat"
        memory = MemoryProfile(use_memory=False)

    p = _resolve_memory_profile(R(), "chat")
    assert p["use_memory"] is False


@pytest.mark.asyncio
async def test_retrieve_skips_when_no_memory(monkeypatch):
    """use_memory=false → 不触发 Milvus（含 import），直接空（省一次向量检索）。"""
    from chat.graph import _retrieve_memories

    import milvus_client

    def _boom(*a, **k):
        raise AssertionError("use_memory=false 不应触发 Milvus 检索")

    monkeypatch.setattr(milvus_client, "get_milvus_client", _boom)
    assert await _retrieve_memories("1", "你好", {"use_memory": False}) == []
