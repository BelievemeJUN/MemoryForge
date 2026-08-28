"""上下文压缩单测（P2）。纯逻辑，无需服务。"""
import pytest
from langchain_core.messages import HumanMessage

from chat.graph import _compress_node


@pytest.mark.asyncio
async def test_compress_removes_early():
    msgs = [HumanMessage(content=f"m{i}", id=f"id{i}") for i in range(25)]
    r = await _compress_node({"messages": msgs})
    removed = sorted(x.id for x in r["messages"])
    assert len(removed) == 5 and removed[0] == "id0"


@pytest.mark.asyncio
async def test_no_compress_under_threshold():
    msgs = [HumanMessage(content="x", id=f"id{i}") for i in range(10)]
    assert await _compress_node({"messages": msgs}) == {}


@pytest.mark.asyncio
async def test_skip_msgs_without_id():
    class NoId:
        content = "x"

    # LangChain 消息通常自动带 id；这里用无 id 属性的对象验证跳过逻辑
    msgs = [NoId() for _ in range(25)]
    r = await _compress_node({"messages": msgs})
    assert r.get("messages", []) == []  # 无 id → 没有可移除项
