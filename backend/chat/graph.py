"""LangGraph 对话总图（M2-1 最小版）。

这是「弃用 create_agent 后自己手写对话层」的起点。
M2-1 只做一个节点 chat（LLM 直接回复），先验证状态机骨架。
后续按 M2-2..M2-5 逐步加：流式 → 意图判断 → 读工具节点 → 执行子图。
"""
import logging
import os
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import RemoveMessage
from pydantic import BaseModel, Field

from .llm import build_chat_model
from .state import ChatState

logger = logging.getLogger(__name__)
load_dotenv()

# ===== M2-3 意图判断 =====
INTENT_PROMPT = (
    "你是意图分类器。根据用户最新一条消息判断意图，只输出 JSON 对象，字段：\n"
    '- "intent": 必填，取值为 chat / code / kb / memory 之一\n'
    "  - chat: 普通对话/闲聊/提问，不需要执行代码，不需要查资料\n"
    "  - code: 要求写代码、运行、调试、算结果（需要执行代码）\n"
    "  - kb: 想查知识库/内部文档/资料\n"
    "  - memory: 想回忆之前对话/个人偏好/习惯\n"
    '- "reasoning": 判断理由，一句话\n'
    "只输出 JSON，不要其他文字。"
)


class Intent(BaseModel):
    intent: Literal["chat", "code", "kb", "memory"]
    reasoning: str = Field(description="判断理由")


def _usage_tokens(resp) -> int:
    """P2-K：从 LLM 响应提取 total_tokens（成本记账）。

    DeepSeek 的 usage 在 usage_metadata；结构化输出（Intent 对象）可能在
    response_metadata.token_usage，两层都试。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    tokens = int(um.get("total_tokens") or 0)
    if not tokens:
        rm = getattr(resp, "response_metadata", None) or {}
        tokens = int((rm.get("token_usage") or {}).get("total_tokens") or 0)
    return tokens


# P2 长上下文压缩：消息超过该阈值时保留最近 N 条，删掉早期（保近舍远）
MAX_CONTEXT_MESSAGES = int(os.getenv("MAX_CONTEXT_MESSAGES", "20"))


async def _compress_node(state: ChatState) -> dict[str, Any]:
    """P2 长上下文压缩：消息过多时保留最近 N 条，删掉早期（保近舍远）。

    面试可讲：长对话会撑爆上下文窗口 + 烧钱；在意图判断前先压缩历史——
    用 RemoveMessage 删早期消息（控窗口 + 省 token）。可升级：早期消息用 LLM
    摘要成一条 summary 保留大意（代价是额外一次调用）。
    """
    msgs = state["messages"]
    if len(msgs) <= MAX_CONTEXT_MESSAGES + 2:
        return {}
    keep = MAX_CONTEXT_MESSAGES
    removes = [
        RemoveMessage(id=m.id)
        for m in msgs[: len(msgs) - keep]
        if getattr(m, "id", None)
    ]
    if removes:
        logger.info("上下文压缩: 删 %d 条早期消息，保留最近 %d 条", len(removes), keep)
    return {"messages": removes}


async def _retrieve_chat_memories(user_id: str, query: str) -> str:
    """P2：普通对话也注入长期记忆（摘要/语义/情节）。

    之前记忆只在 exec（程序记忆）和 read（记忆意图）生效，普通闲聊不查——
    这里补上：让「长期记忆」在对话主链路真正生效，不再只等用户主动问。
    """
    try:
        from milvus_client import get_milvus_client  # lazy

        mc = await get_milvus_client()
        uid = int(user_id) if str(user_id).isdigit() else 1
        memories = await mc.hybrid_retrieval_memories(
            query, user_id=uid, summary_k=1, semantic_k=3, episodic_k=3, procedural_k=0
        )
        parts = []
        for mtype in ("summary", "semantic", "episodic"):
            for m in memories.get(mtype, [])[:3]:
                if isinstance(m, dict) and m.get("content"):
                    parts.append(m["content"])
        return "\n".join(parts)[:800]
    except Exception:  # noqa: BLE001
        return ""


async def _chat_node(state: ChatState) -> dict[str, Any]:
    """LLM 回复节点：历史（+ P2 记忆注入）→ 模型 → assistant 消息。

    关键点：state["messages"] 里已含历史（add_messages 累积），所以模型
    能看到多轮上下文——这就是最简单的「短期记忆」。
    P2-K：普通对话也查记忆（全场景个性化）；本节点也记账 token（之前漏算）。
    """
    memories = await _retrieve_chat_memories(
        state.get("user_id", ""), state["messages"][-1].content
    )
    model = build_chat_model()
    msgs = state["messages"]
    if memories:
        sys = SystemMessage(
            content="以下是该用户的历史记忆（辅助理解用户，不要直接复述）：\n" + memories
        )
        msgs = [sys, *msgs]
    response = model.invoke(msgs)
    return {
        "messages": [response],
        "tokens": state.get("tokens", 0) + _usage_tokens(response),
    }


def _intent_node(state: ChatState) -> dict[str, Any]:
    """M2-3 意图判断节点：用 LLM 结构化输出判断用户意图，写入 state。

    面试可讲：比起关键词匹配，用 with_structured_output 让模型返回
    结构化 JSON（intent + reasoning），意图判断更鲁棒、可解释。
    """
    model = build_chat_model().with_structured_output(Intent, method="json_mode")
    latest = state["messages"][-1]
    result = model.invoke([SystemMessage(content=INTENT_PROMPT), latest])
    logger.info("意图判断: %s (%s)", result.intent, result.reasoning)
    return {
        "intent": result.intent,
        "tokens": state.get("tokens", 0) + _usage_tokens(result),
    }


def _route(state: ChatState) -> Literal["chat", "exec", "read"]:
    """M2-3 条件路由：根据意图分发。exec 走执行子图，read 走读工具节点。"""
    intent = state.get("intent", "chat")
    if intent == "code":
        return "exec"
    if intent in ("kb", "memory"):
        return "read"
    return "chat"


def _format_exec_result(r: dict) -> str:
    """把执行子图的结果整理成给用户看的文本。"""
    if r.get("security_blocked"):
        return "⚠️ 代码被安全策略拦截：\n" + (r.get("stderr") or "")
    lines = []
    if r.get("timed_out"):
        lines.append("⏱️ 代码执行超时，已被强制终止。")
    if r.get("error"):
        lines.append("❌ 执行出错：" + r["error"])
    if r.get("stdout"):
        lines.append("**运行输出：**\n```\n" + r["stdout"].rstrip() + "\n```")
    if r.get("stderr"):
        lines.append("**stderr：**\n```\n" + r["stderr"].rstrip() + "\n```")
    if r.get("exit_code") is not None and not r.get("timed_out"):
        lines.append(f"（退出码 {r['exit_code']}）")
    return "\n".join(lines)


async def _retrieve_user_prefs(user_id: str, task: str) -> str:
    """P0-1b：检索用户的程序性记忆（编码偏好），用于个性化代码生成。

    Milvus 不可用/无记忆时降级为空字符串（不阻塞写代码）。
    """
    try:
        from milvus_client import get_milvus_client  # lazy

        mc = await get_milvus_client()
        uid = int(user_id) if str(user_id).isdigit() else 1
        memories = await mc.hybrid_retrieval_memories(
            task, user_id=uid, summary_k=0, semantic_k=0, episodic_k=0, procedural_k=3
        )
        prefs = [
            m["content"]
            for m in memories.get("procedural", [])[:3]
            if isinstance(m, dict) and m.get("content")
        ]
        return "\n".join(prefs)
    except Exception:  # noqa: BLE001
        return ""


def _is_goal_task(task: str) -> bool:
    """P2：判断是否目标型任务（无显式期望输出，需 LLM-as-judge 验证）。

    启发式：含目标性动词（实现/写一个/优化/爬虫/分析…）且不含明确输出要求
    （输出/结果/等于/应该是/只返回）。
    面试可讲：这就是「验证路由」的第一跳——能给出明确期望的走确定性验证，
    目标型的走 judge，各取所长。
    """
    goal_keywords = ("实现", "写一个", "写个", "优化", "重构", "爬虫", "分析", "开发", "完成")
    if not any(k in task for k in goal_keywords):
        return False
    explicit = ("输出", "结果", "等于", "应该是", "期望", "只返回")
    return not any(k in task for k in explicit)


async def _exec_node(state: ChatState) -> dict[str, Any]:
    """M2-4/M3/P0-1b/P2：用户请求交给执行子图（plan→write→execute→verify→fix）。

    对话场景无 hidden test：verify 走「路由」——目标型任务（goal_mode）用 LLM-as-judge
    判定完成度并自愈，否则直接展示执行结果。检索编码偏好做个性化。
    """
    from workflow.exec_loop import get_exec_graph  # lazy，避免顶层互相依赖

    task = state["messages"][-1].content
    prefs = await _retrieve_user_prefs(state.get("user_id", ""), task)
    result = await get_exec_graph().ainvoke(
        {
            "task": task,
            "prefs": prefs,
            "tests": [],
            "max_attempts": 3,
            "user_id": state.get("user_id", ""),  # P0-A-2：透传用户供配额解析
            "goal_mode": _is_goal_task(task),     # P2：目标型任务 → judge 验证
        }
    )
    reply = result.get("final") or _format_exec_result(result)
    return {
        "messages": [AIMessage(content=reply)],
        # P2-K：把 exec 子图内部累计的 token 带回对话状态（成本口径完整）
        "tokens": state.get("tokens", 0) + int(result.get("tokens", 0) or 0),
    }


def _format_parents(parents) -> str:
    """知识库父块列表 → 文本。"""
    if not parents:
        return "知识库中未检索到相关内容。"
    lines = ["📚 **知识库检索结果：**\n"]
    for i, p in enumerate(parents[:5], 1):
        text = p.page_content if hasattr(p, "page_content") else str(p)
        lines.append(f"**{i}.** {text[:300]}")
    return "\n".join(lines)


def _format_memories(memories: dict) -> str:
    """记忆检索结果 → 文本。"""
    if not memories:
        return "记忆库中未检索到相关内容。"
    lines = ["🧠 **记忆检索结果：**\n"]
    for mtype, items in memories.items():
        if not items:
            continue
        lines.append(f"**[{mtype}]**")
        for item in items[:3]:
            text = item.page_content if hasattr(item, "page_content") else str(item)
            lines.append(f"  - {text[:200]}")
    return "\n".join(lines)


async def _read_node(state: ChatState) -> dict[str, Any]:
    """M2-3 后半：根据意图检索知识库/记忆，把结果拼成回复。

    面试可讲：read 是 async 节点——检索要调 Milvus/PostgreSQL 的 async
    接口，LangGraph 支持 sync/async 混合节点，图整体用 ainvoke 驱动。
    """
    intent = state.get("intent", "kb")
    query = state["messages"][-1].content
    user_id = int(state.get("user_id", "1") or 1)
    try:
        from milvus_client import get_milvus_client
        from postgresql_client import get_postgresql_client

        milvus = await get_milvus_client()
        if intent == "memory":
            memories = await milvus.hybrid_retrieval_memories(
                query,
                user_id=user_id,
                summary_k=1,
                semantic_k=3,
                episodic_k=3,
                procedural_k=2,
            )
            text = _format_memories(memories)
        else:
            kb_id = state.get("knowledge_base_id") or "默认知识库"
            parent_ids = await milvus.hybrid_retrieval_knowledge_base(
                query, knowledge_base_id=kb_id, top_k=3, user_id=user_id
            )
            if not parent_ids:
                text = "知识库中未检索到相关内容（可能知识库为空或没有相关文档）。"
            else:
                pg = await get_postgresql_client()
                parents = await pg.get_parents(
                    knowledge_base_id=kb_id, parent_ids=parent_ids, user_id=user_id
                )
                text = _format_parents(parents)
    except Exception as e:  # noqa: BLE001
        logger.exception("读工具节点检索失败")
        text = f"❌ 检索失败：{e}"
    return {"messages": [AIMessage(content=text)]}


def build_graph(checkpointer=None):
    """构建并编译对话总图。

    M4-1：传入 Redis checkpointer 后，图自动按 thread_id 存档/恢复会话历史，
    多轮对话不再需要手动传 history，且会话间隔离。
    """
    g = StateGraph(ChatState)
    # P2 上下文压缩：START 后先压缩历史（保近舍远），再意图判断
    g.add_node("compress", _compress_node)
    # 意图判断 → 条件路由 → 对应节点
    g.add_node("intent", _intent_node)
    g.add_node("chat", _chat_node)
    g.add_node("exec", _exec_node)     # M2-4 执行子图入口
    g.add_node("read", _read_node)     # M2-3 后半：读工具节点（知识库/记忆检索）
    g.add_edge(START, "compress")
    g.add_edge("compress", "intent")
    g.add_conditional_edges(
        "intent", _route, {"chat": "chat", "exec": "exec", "read": "read"}
    )
    for n in ("chat", "exec", "read"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)
