"""记忆容量上限 + 单条删除测试（P2）。需 Milvus 在线。"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("SKIP_MILVUS_TESTS", "") == "1", reason="Milvus 未启动"
)


@pytest.mark.asyncio
async def test_count_list_prune_empty_user_safe():
    from milvus_client import get_milvus_client

    mc = await get_milvus_client()
    uid = int(uuid.uuid4().int % 10**6)  # 不存在的用户
    assert await mc.count_memories(uid) == 0
    assert await mc.list_memories(uid, limit=10) == []
    assert await mc.prune_to_capacity(uid, max_count=10) == 0  # 未超限不删
    assert await mc.delete_memories(uid, []) == 0  # 空列表安全


@pytest.mark.asyncio
async def test_delete_nonexistent_memory_returns_zero():
    from milvus_client import get_milvus_client

    mc = await get_milvus_client()
    uid = int(uuid.uuid4().int % 10**6)
    # 不存在的 id → 0（不报错）
    assert await mc.delete_memories(uid, [99999999]) == 0
