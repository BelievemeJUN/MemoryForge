"""数据合规（P2）：删除用户全部数据（GDPR 类删除权）。

面试可讲：多租户项目必须能「删掉某个用户的所有痕迹」——数据散落在
PG（对话/画像/知识库）、Milvus（记忆/知识库向量）、Redis（成本/限流/记忆触摸/任务），
一处都不能漏，否则删除权是假的。
"""
import logging
import os

logger = logging.getLogger(__name__)


async def delete_user_all(user_id: int) -> dict:
    """删除某用户在 PG/Milvus/Redis 的全部数据，返回各存储删除统计。"""
    stats: dict = {}

    # 1) PostgreSQL：对话 / 画像 / 知识库（级联）
    from postgresql_client import get_postgresql_client  # lazy

    pg = await get_postgresql_client()
    stats["postgresql"] = await pg.delete_user_data(user_id)

    # 2) Milvus：记忆 + 知识库向量（Milvus 不可用时降级，不阻塞其他存储删除）
    try:
        from milvus_client import get_milvus_client  # lazy

        mc = await get_milvus_client()
        stats["milvus"] = await mc.delete_user_all(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Milvus 不可用，跳过向量删除: %s", e)
        stats["milvus"] = {"memory": -1, "knowledge": -1, "skipped": str(e)}

    # 3) Redis：成本 / 限流 / 记忆触摸 / 画像缓存 / 任务
    import redis.asyncio as aioredis  # lazy

    r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6380/0"))
    patterns = (
        f"cost:*:{user_id}",
        f"rl:user:{user_id}",
        f"mem_touch:{user_id}",
        f"user_profile:{user_id}",
        f"task:{user_id}:*",
        f"tasks:{user_id}:*",
    )
    deleted_keys = 0
    for pat in patterns:
        async for key in r.scan_iter(match=pat):
            await r.delete(key)
            deleted_keys += 1
    stats["redis_keys"] = deleted_keys

    logger.info("数据合规: 用户 %s 全部数据已删除 %s", user_id, stats)
    return stats
