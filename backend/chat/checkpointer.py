"""LangGraph checkpoint（M4-1 会话隔离）。

大白话：checkpoint = 给每个会话一个独立的「存档点」。AI 每轮对话结束自动存档，
下一轮带着自己的 thread_id 来，自动从存档恢复历史——不用手动传 history，两个
会话也互不串门（还了 echomind「thread_id 直接用 user_id」的债）。

技术：AsyncRedisSaver（Redis 作为存档后端，langgraph-checkpoint-redis）。
注意：图里有 async 节点，必须用 AsyncRedisSaver + ainvoke/astream。
"""
"""LangGraph checkpoint（M4-1 会话隔离）。

方案演进：最初用 Redis Stack（echomind README 要求），但 redis-stack 的
RedisAI/RedisGears 模块在本 WSL 环境加载失败，checkpoint 恢复历史时连接被关
→ 改用 PostgreSQL（AsyncPostgresSaver）：更稳、不依赖 Redis 模块，面试可讲
「checkpoint 存 PostgreSQL，与业务数据同库」。

关键：from_conn_string 返回 async 上下文管理器，连接生命周期必须绑定在
async with 块内（官方推荐）——手动 __aenter__ 会导致连接被提前关闭。
"""
import os

from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

load_dotenv()


def create_checkpointer_cm():
    """返回 AsyncPostgresSaver 的 async 上下文管理器。

    用法：
        async with create_checkpointer_cm() as saver:
            await saver.setup()          # 建表（幂等）
            graph = build_graph(checkpointer=saver)
            ...使用 graph...
    """
    dsn = os.getenv("DATABASE_URL")
    return AsyncPostgresSaver.from_conn_string(dsn)
