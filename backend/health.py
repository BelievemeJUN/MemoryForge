"""健康检查：依赖探活（P0-E）。

设计（面试可讲）：
  - /health 不只是"进程活着"，而是探活四个依赖：PostgreSQL / Redis / Milvus / Docker。
  - 用**一次性连接**而非缓存单例——避免跨事件循环的经典坑（deepresearch 教训：
    缓存单例在 health 探活里会 Event loop is closed）。
  - 每个依赖独立 try/except + 超时，不互相拖累。
"""
import asyncio
import logging
import os

import psycopg
import redis.asyncio as aioredis
from dotenv import load_dotenv
from pymilvus import AsyncMilvusClient

logger = logging.getLogger(__name__)
load_dotenv()


async def _check_postgres() -> dict:
    try:
        conn = await psycopg.AsyncConnection.connect(
            os.getenv("DATABASE_URL"), connect_timeout=3
        )
        cur = await conn.execute("SELECT 1")
        await cur.fetchone()
        await conn.close()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:100]}


async def _check_redis() -> dict:
    try:
        r = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6380/0"))
        await r.ping()
        await r.aclose()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:100]}


async def _check_milvus() -> dict:
    c = None
    try:
        c = AsyncMilvusClient(uri=os.getenv("Milvus_url"), token=os.getenv("Token"))
        await c.list_collections()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:100]}
    finally:
        if c is not None:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass


async def _check_docker() -> dict:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:100]}


async def check_dependencies() -> dict:
    """并行探活四个依赖，返回 {名称: 状态}。"""
    checks = {
        "postgresql": _check_postgres(),
        "redis": _check_redis(),
        "milvus": _check_milvus(),
        "docker": _check_docker(),
    }
    results = {}
    for name, coro in checks.items():
        try:
            results[name] = await asyncio.wait_for(coro, timeout=5)
        except asyncio.TimeoutError:
            results[name] = {"ok": False, "error": "timeout"}
    return results
