"""数据导出测试（P2 合规，GDPR 导出权）。需 PG/Milvus/Redis 在线。

注意：PG 客户端是全局单例，连接池绑定首个事件循环——所有断言必须在
同一个测试函数内完成（pytest-asyncio 每测试一个新 loop，跨测试复用会崩）。
"""
import os
import uuid

import pytest


@pytest.mark.asyncio
async def test_export_structure_and_symmetric():
    from data_governance import delete_user_all, export_user_all
    from postgresql_client import get_postgresql_client

    # 1) 结构完整（即使是不存在的用户）
    uid = int(uuid.uuid4().int % 10**6)
    data = await export_user_all(uid)
    assert data["user_id"] == uid
    assert "postgresql" in data and "conversations" in data["postgresql"]
    assert "milvus" in data and "memories" in data["milvus"]
    assert "redis" in data and "daily_token_usage" in data["redis"]

    # 2) 造真实数据 → 导出有内容 → 删除 → 再导出为空（对称性）
    pg = await get_postgresql_client()
    await pg.add_conversation_message(uid, "t_export", "human", "hello export test")
    data = await export_user_all(uid)
    assert len(data["postgresql"]["conversations"]) >= 1
    assert any("hello export test" in c["content"] for c in data["postgresql"]["conversations"])

    stats = await delete_user_all(uid)
    assert stats["postgresql"]["raw_conversations"] >= 1
    after = await export_user_all(uid)
    assert after["postgresql"]["conversations"] == []
