"""LangGraph 流式 → SSE 封装（M2-2 起，M2-5 修复）。

面试可讲：
  - `astream(stream_mode=["messages", "updates"])`：
      - messages → (AIMessageChunk, metadata) 增量 token，metadata 带节点名
      - updates  → 每步的 state 增量（节点返回的完整消息）
  - 我们只把「用户可见的 chat 节点」token 流出去——内部节点（如意图判断的
    结构化输出 JSON）绝不让前端看到（内部状态不泄露）。
  - exec/read 节点返回的是完整消息（非 LLM 逐字流），通过 updates 补发。
"""
import json
from typing import AsyncGenerator

from langchain_core.messages import AIMessage

# 内部节点名（其 LLM 输出不应流给用户）
_INTERNAL_NODES = {"intent", "task"}


def _sse(data: dict) -> str:
    """序列化一个 SSE 事件（data: + JSON + 空行）。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_chat(
    graph, state: dict, config: dict, collect: list | None = None, tokens: list | None = None
) -> AsyncGenerator[str, None]:
    """把对话图的事件流翻译成 SSE 文本事件。

    事件类型：
      {"type":"status",  "content":"thinking"}  开始
      {"type":"token",   "content":"..."}        用户可见文本（增量或补发）
      {"type":"done"}                            结束

    collect: 可选，用于收集最终 AI 回复内容（P0-1 对话入库用）。
    tokens: 可选，P2-K 收集请求级累计 token（各节点记账后写回 state）。
    """
    yield _sse({"type": "status", "content": "thinking"})
    sent = False  # 是否已发出用户可见内容

    async for mode, data in graph.astream(
        state, config, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            chunk, metadata = data
            node = metadata.get("langgraph_node", "")
            if node in _INTERNAL_NODES:
                continue  # 内部节点输出不流给用户
            if chunk and getattr(chunk, "content", None):
                sent = True
                yield _sse({"type": "token", "content": chunk.content})
        elif mode == "updates":
            # exec/read 等节点返回的完整 AIMessage → 补发（若无打字机流）
            for node_name, update in data.items():
                # LangGraph：节点返回空 dict（如 compress 未触发）时 update 为 None → 跳过
                if not update:
                    continue
                # P2-K：收集节点写回的 token 记账（取最终值）
                if tokens is not None and update.get("tokens") is not None:
                    tokens.append(int(update["tokens"]))
                msgs = update.get("messages")
                if msgs:
                    last = msgs[-1]
                    if hasattr(last, "content") and last.content and not sent:
                        sent = True
                        yield _sse({"type": "token", "content": last.content})
                    # P0-1：收集最终 AI 回复（对话入库）
                    if collect is not None and isinstance(last, AIMessage) and last.content:
                        collect.append(last.content)

    yield _sse({"type": "done", "tokens": tokens[-1] if tokens else 0})
