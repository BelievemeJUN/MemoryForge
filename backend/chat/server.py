"""独立 FastAPI 服务：沙箱对话 SSE 端点（M2-2 起，M4-1 会话隔离，P0-1 记忆）。

运行：
    ./.venv/bin/uvicorn chat.server:app --host 0.0.0.0 --port 8020

设计说明：
  - 独立 app，不依赖 echomind 的 main.py。
  - M4-1：lifespan 里用 async with 持有 PostgreSQL checkpoint（会话隔离）。
  - P0-1a：对话后写 PostgreSQL raw_conversations；lifespan 挂后台记忆提取任务。
  - /api/sandbox/chat 返回 text/event-stream（SSE）。
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from auth import require_user
from cost import CostTracker
from ratelimit import RateLimiter
from .checkpointer import create_checkpointer_cm
from .graph import build_graph
from .llm import build_chat_model
from .streaming import stream_chat
from observability import get_request_id, set_request_id, setup_logging

# P0-E：结构化 JSON 日志（自动带 request_id）
setup_logging()
logger = logging.getLogger(__name__)


async def _memory_extraction_loop():
    """P0-1a：后台记忆提取（echomind 机制）。

    定时扫描未摘要对话 → LLM 提取记忆/画像 → 入库（Milvus 记忆集合）。
    惰性 import：不拖慢 server 启动（第一次跑才加载 Milvus/记忆链路）。
    """
    while True:
        try:
            from auto_store_memory_from_psql import run_compression_task  # lazy

            await run_compression_task(model=build_chat_model())
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆提取任务失败（不影响对话）: %s", e)
        await asyncio.sleep(180)  # 每 3 分钟扫一次


@asynccontextmanager
async def lifespan(app: FastAPI):
    # M4-1：PostgreSQL checkpoint（async with 保证连接生命周期正确）
    async with create_checkpointer_cm() as saver:
        await saver.setup()
        app.state.graph = build_graph(checkpointer=saver)
        # P0-1a：后台记忆提取任务
        app.state.memory_task = asyncio.create_task(_memory_extraction_loop())
        # P2-H/K：限流 + 成本记账器（Redis 连接复用）
        app.state.limiter = RateLimiter()
        app.state.cost = CostTracker()
        yield
        app.state.memory_task.cancel()


app = FastAPI(title="CodeMind Chat Sandbox", version="0.3.1", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """P0-E：每个请求分配/透传 request_id，写入 ContextVar（日志自动带上）并回传响应头。"""
    rid = set_request_id(request.headers.get("X-Request-ID"))
    try:
        response = await call_next(request)
    finally:
        pass
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def rate_limit_middleware(request, call_next):
    """P2-H：per-IP 限流（未认证洪水防线）；user 级限流在端点内做（认证后才有 user_id）。"""
    ip = request.client.host if request.client else "unknown"
    ok, wait = await app.state.limiter.check_ip(ip)
    if not ok:
        return JSONResponse(
            status_code=429,
            content={"detail": f"请求过于频繁，请 {wait}s 后重试"},
            headers={"Retry-After": str(wait)},
        )
    return await call_next(request)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    thread_id: str = "default"


async def _persist_conversation(user_id: str, thread_id: str, user_msg: str, ai_reply: str):
    """P0-1a：对话写入 PostgreSQL raw_conversations（记忆提取的数据源）。"""
    try:
        from postgresql_client import get_postgresql_client  # lazy

        pg = await get_postgresql_client()
        uid = int(user_id) if user_id.isdigit() else 1
        await pg.add_conversation_message(uid, thread_id, "human", user_msg)
        await pg.add_conversation_message(uid, thread_id, "ai", ai_reply)
    except Exception as e:  # noqa: BLE001
        logger.warning("对话入库失败（不影响对话）: %s", e)


@app.post("/api/sandbox/chat")
async def chat(req: ChatRequest, user_id: str = Depends(require_user)):
    """对话端点。user_id 从 API Key 认证解出（不再信任请求参数）。

    P2-H：per-user 限流（防单用户烧 token / 占沙箱）。
    P2-K：请求前预算检查（超限熔断）+ 请求结束后按实际 token 记账。
    """
    # P2-H：per-user 限流
    ok, wait = await app.state.limiter.check_user(user_id)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请 {wait}s 后重试",
            headers={"Retry-After": str(wait)},
        )
    # P2-K：预算检查（预估 2000 token；实际以流结束记账为准）
    ok_budget, _ = await app.state.cost.check_budget(user_id, est_tokens=2000)
    if not ok_budget:
        raise HTTPException(status_code=429, detail="当日 token 预算已用完，请明日再试")

    graph = app.state.graph
    state = {
        "messages": [HumanMessage(content=req.message)],
        "user_id": user_id,  # 认证上下文（可信）
        "thread_id": req.thread_id,
    }
    config = {"configurable": {"thread_id": req.thread_id}}
    collected: list = []
    collected_tokens: list = []  # P2-K：请求级 token 累计（流结束记账用）

    async def event_gen():
        try:
            async for ev in stream_chat(
                graph, state, config, collect=collected, tokens=collected_tokens
            ):
                yield ev
        finally:
            # P0-1a：流结束后把对话写入 raw_conversations（用可信 user_id）
            if collected:
                await _persist_conversation(
                    user_id, req.thread_id, req.message, collected[-1]
                )
            # P2-K：按实际 token 记账到 per-user 日预算
            if collected_tokens:
                await app.state.cost.add_usage(user_id, collected_tokens[-1])

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    """P0-E：依赖探活（PG/Redis/Milvus/Docker），返回各依赖状态。"""
    from health import check_dependencies  # lazy，避免启动时加载重依赖

    deps = await check_dependencies()
    ok = all(d.get("ok") for d in deps.values())
    return {"status": "ok" if ok else "degraded", "dependencies": deps}
