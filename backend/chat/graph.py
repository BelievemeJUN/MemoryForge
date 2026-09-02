"""LangGraph 对话总图（M2-1 最小版）。

这是「弃用 create_agent 后自己手写对话层」的起点。
M2-1 只做一个节点 chat（LLM 直接回复），先验证状态机骨架。
后续按 M2-2..M2-5 逐步加：流式 → 意图判断 → 读工具节点 → 执行子图。
"""
import logging
import os
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import RemoveMessage
from pydantic import BaseModel, Field

from .llm import build_chat_model
from .state import ChatState

logger = logging.getLogger(__name__)
load_dotenv()

# ===== M2-3 意图判断（路由 + 记忆配置联合决策） =====
INTENT_PROMPT = (
    "你是意图分类器。根据用户最新一条消息判断意图，只输出 JSON 对象，字段：\n"
    '- "intent": 必填，取值为 chat / code / kb / memory / task 之一\n'
    "  - chat: 普通对话/闲聊/提问，不需要执行代码，不需要查资料\n"
    "  - code: 要求写代码、运行、调试、算结果（需要执行代码）\n"
    "  - kb: 想查知识库/内部文档/资料\n"
    "  - memory: 想回忆之前对话/个人偏好/习惯\n"
    '  - task: 要求后台执行/排队跑/批量任务/异步处理，或查询后台任务状态（任务号）\n'
    '- "memory": 记忆检索配置对象，字段 use_memory(bool)、summary(0-1)、semantic(0-5)、episodic(0-5)、procedural(0-5)\n'
    "  记忆配置规则（按本条消息需要什么记忆给配额，0=不需要，越需要越大）：\n"
    "  - 纯问候/寒暄（无实质内容，如你好）→ use_memory=false（用户画像已由系统注入，无需向量召回）\n"
    "  - 闲聊但涉及近况/背景/偏好（如最近怎么样/上次那个还在做吗）→ summary 1 + semantic 1-2\n"
    "  - 普通对话/需理解用户背景 → semantic 2-3；涉及长期任务可加 summary 1\n"
    "  - 事实/知识/偏好问题 → semantic 3-4\n"
    "  - 回忆历史（之前/上次/当时…）→ episodic 3-4\n"
    "  - 方法/流程/怎么做 → procedural 3-4（可加 semantic 2）\n"
    "  - code 写代码 → semantic 3 + episodic 2 + procedural 3（写码需用户习惯+同类任务）\n"
    '  - kb/task 场景通常 use_memory=false（知识库/后台任务不依赖个人记忆）\n'
    '- "reasoning": 判断理由，一句话\n'
    "只输出 JSON，不要其他文字。"
)


class MemoryProfile(BaseModel):
    """记忆检索配置：意图判断一并产出——本条消息需要哪种记忆、各几条。

    设计要点：向量检索召回的是「与当前 query 语义相近」的记忆条目；而
    用户画像这类浓缩偏好是常驻通道（系统注入，不走向量检索），所以寒暄
    类消息 use_memory=false 并不等于“失忆”——画像仍在上下文里。
    """

    use_memory: bool = True
    summary: int = 0      # 0-1
    semantic: int = 0     # 0-5
    episodic: int = 0     # 0-5
    procedural: int = 0   # 0-5


class Intent(BaseModel):
    intent: Literal["chat", "code", "kb", "memory", "task"]
    memory: MemoryProfile = Field(
        default_factory=MemoryProfile, description="记忆检索配置"
    )
    reasoning: str = Field(description="判断理由")


# ===== B：任务队列接入对话（TaskRequest 结构化抽取，_task_node 执行） =====
TASK_PROMPT = (
    "你是后台任务助手。根据用户最新消息解析意图，只输出 JSON 对象，字段：\n"
    '- "action": 必填，create（要求后台执行代码/跑任务/批量处理）、status（查询单个任务状态）或 list（列出我的任务）\n'
    '- "code": 要后台执行的 Python 代码（action=create 时；从代码块或描述中提取，没有则空串）\n'
    '- "task_id": 要查询的任务号（action=status 时；用户给的任务号，没有则空串）\n'
    "只输出 JSON，不要其他文字。"
)


class TaskRequest(BaseModel):
    action: Literal["create", "status", "list"]
    code: str = Field(default="", description="要后台执行的 Python 代码")
    task_id: str = Field(default="", description="要查询的任务号")
    reasoning: str = Field(default="", description="判断理由")


def _usage_tokens(resp, node: str = "llm") -> int:
    """P2-K：从 LLM 响应提取 total_tokens 并上报 OpenTelemetry GenAI 指标。

    DeepSeek 的 usage 在 usage_metadata；结构化输出（Intent 对象）可能在
    response_metadata.token_usage，两层都试。
    """
    from usage_metrics import record_llm_usage  # lazy，避免顶层依赖 OTel

    return record_llm_usage(node, resp)


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


def _clamp_k(v: Any, lo: int = 0, hi: int = 5) -> int:
    """把记忆配额钳制到 [lo, hi]；非数字/越界归 0 或边界（防模型乱填）。"""
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return lo


def _default_memory_profile(intent: str) -> dict:
    """LLM 记忆配置缺失/解析失败时的硬编码兜底（按场景）。"""
    if intent == "code":
        return {"use_memory": True, "summary": 0, "semantic": 3, "episodic": 2, "procedural": 3}
    if intent == "memory":
        return {"use_memory": True, "summary": 1, "semantic": 3, "episodic": 3, "procedural": 2}
    return {"use_memory": True, "summary": 1, "semantic": 3, "episodic": 3, "procedural": 0}


def _resolve_memory_profile(result: Any, intent: str) -> dict:
    """把 LLM 输出的 MemoryProfile 解析并钳制到合法范围；结构缺失/异常 → 硬编码兜底。"""
    m = getattr(result, "memory", None)
    if not isinstance(m, MemoryProfile):
        return _default_memory_profile(intent)
    return {
        "use_memory": bool(getattr(m, "use_memory", True)),
        "summary": _clamp_k(getattr(m, "summary", 0), 0, 1),
        "semantic": _clamp_k(getattr(m, "semantic", 0)),
        "episodic": _clamp_k(getattr(m, "episodic", 0)),
        "procedural": _clamp_k(getattr(m, "procedural", 0)),
    }


async def _retrieve_memories(user_id: str, query: str, profile: dict) -> list[dict]:
    """按意图给出的记忆配置检索，返回 [{"type", "content"}, ...]（固定类型顺序）。

    use_memory=false → 不做向量检索（纯问候无实质 query，画像已走常驻注入，
    向量召回该 query 语义相近条目收益低）；Milvus 不可用降级空列表。
    """
    if not profile.get("use_memory", True):
        return []
    try:
        from milvus_client import get_milvus_client  # lazy

        mc = await get_milvus_client()
        uid = int(user_id) if str(user_id).isdigit() else 1
        memories = await mc.hybrid_retrieval_memories(
            query, user_id=uid,
            summary_k=_clamp_k(profile.get("summary", 0), 0, 1),
            semantic_k=_clamp_k(profile.get("semantic", 0)),
            episodic_k=_clamp_k(profile.get("episodic", 0)),
            procedural_k=_clamp_k(profile.get("procedural", 0)),
        )
        out = []
        for mtype in ("summary", "semantic", "episodic", "procedural"):
            want = _clamp_k(profile.get(mtype, 0))
            for m in memories.get(mtype, [])[:want]:
                if isinstance(m, dict) and m.get("content"):
                    out.append({"type": mtype, "content": m["content"]})
        return out
    except Exception:  # noqa: BLE001
        return []


async def _get_user_profile(user_id: str) -> str:
    """拉取用户画像（PG users.user_profile，Redis 缓存 1h 防频繁读库）。

    画像 = 跨会话沉淀的浓缩偏好（“用户喜欢简洁、正在准备 AI 面试”），
    它不应靠“当前 query 向量召回”（寒暄 query 无语义召回差），而应作为
    每次对话的常驻上下文——这才是“闲聊也能了解用户偏好”的正确通道。
    PG/Redis 不可用或无画像 → 返回空串（不阻塞对话）。
    """
    if not str(user_id).isdigit():
        return ""
    try:
        from postgresql_client import get_postgresql_client  # lazy

        pg = await get_postgresql_client()
        profile = await pg.get_user_profile(int(user_id))
        return (profile or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _chat_node(state: ChatState) -> dict[str, Any]:
    """LLM 回复节点：画像（常驻）→ 记忆注入（可选）→ 历史 → 模型 → assistant。

    关键点：state["messages"] 里已含历史（add_messages 累积），所以模型
    能看到多轮上下文——这就是最简单的「短期记忆」。
    画像入图（治本）：寒暄/闲聊也能了解用户偏好，不依赖向量检索召回；
    P2-K：本节点记账 token（之前漏算）。
    """
    profile = state.get("memory") or _default_memory_profile("chat")
    items = await _retrieve_memories(
        state.get("user_id", ""), state["messages"][-1].content, profile
    )
    model = build_chat_model()
    sys_blocks: list[SystemMessage] = []

    user_profile = await _get_user_profile(state.get("user_id", ""))
    if user_profile:
        sys_blocks.append(
            SystemMessage(
                content="用户画像（个性化参考：了解用户背景与偏好风格，不要直接复述，回答要贴合其偏好）：\n"
                + user_profile[:600]
            )
        )
    if items:
        memories = "\n".join(i["content"] for i in items)[:800]
        sys_blocks.append(
            SystemMessage(
                content="以下是该用户的历史记忆（辅助理解用户，不要直接复述）：\n" + memories
            )
        )

    msgs = [*sys_blocks, *state["messages"]] if sys_blocks else state["messages"]
    response = model.invoke(msgs)
    return {
        "messages": [response],
        "tokens": state.get("tokens", 0) + _usage_tokens(response, "chat"),
    }


def _intent_node(state: ChatState) -> dict[str, Any]:
    """M2-3 意图判断节点：路由意图 + 记忆检索配置一并产出，写入 state。

    一次结构化输出同时解决「去哪个节点」和「需要向量召回哪些记忆」——
    避免每个场景硬编码记忆 k 值。寒暄类 use_memory=false 不是“失忆”：
    用户画像走常驻注入通道（_chat_node 里拉 PG），不依赖向量检索。
    模型偶发漏填/乱填 memory 时，_resolve_memory_profile 钳制 + 硬编码兜底。
    """
    model = build_chat_model().with_structured_output(Intent, method="json_mode")
    latest = state["messages"][-1]
    result = model.invoke([SystemMessage(content=INTENT_PROMPT), latest])
    memory = _resolve_memory_profile(result, result.intent)
    logger.info("意图判断: %s memory=%s (%s)", result.intent, memory, result.reasoning)
    return {
        "intent": result.intent,
        "memory": memory,
        "tokens": state.get("tokens", 0) + _usage_tokens(result, "intent"),
    }


def _route(state: ChatState) -> Literal["chat", "exec", "read", "task"]:
    """M2-3 条件路由：根据意图分发。exec 走执行子图，read 走读工具节点，task 走任务队列节点。"""
    intent = state.get("intent", "chat")
    if intent == "code":
        return "exec"
    if intent in ("kb", "memory"):
        return "read"
    if intent == "task":
        return "task"
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


def _build_history_context(messages: list) -> str:
    """从对话历史提取「用户此前的需求描述」，拼进写码任务，避免前文需求丢失。

    场景：用户可能先花几轮描述需求（爬什么、存 CSV、要限速），最后才说"帮我写代码"——
    若只把最新一条当 task，前文需求全丢。这里取最新指令之前的最近 N 条用户消息
    作为历史上下文，exec 写码时一并可见。无历史/不可用返回空串。
    """
    history = [m for m in messages[:-1] if isinstance(m, HumanMessage)]
    recent = history[-6:]
    if not recent:
        return ""
    lines = "\n".join(
        f"- {str(m.content).strip()}" for m in recent if getattr(m, "content", None)
    )
    return f"用户在此前的对话中描述过以下需求/背景（写代码时须一并满足）：\n{lines}"


_CODE_LABELS = {
    "summary": "对话总纲",
    "semantic": "该用户对这类任务的相关事实/偏好",
    "episodic": "过往同类任务",
    "procedural": "编码习惯",
}


def _format_code_context(items: list[dict]) -> str:
    """把检索到的记忆格式化为给写码提示词的 prefs（带类型标注，便于模型区分用途）。"""
    return "\n".join(
        f"[{_CODE_LABELS.get(i['type'], i['type'])}] {i['content']}" for i in items
    )


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

    latest = state["messages"][-1].content          # 最新指令（目标模式判定用）
    context = _build_history_context(state["messages"])  # 此前的需求描述（写码一并可见）
    full_task = f"{context}\n\n【本次指令】{latest}" if context else latest
    profile = state.get("memory") or _default_memory_profile("code")
    items = await _retrieve_memories(state.get("user_id", ""), full_task, profile)
    prefs = _format_code_context(items)
    # 用户画像也喂给写码/修复：写码不是对着陌生人写（风格/注释语言/背景偏好）
    profile_text = await _get_user_profile(state.get("user_id", ""))
    result = await get_exec_graph().ainvoke(
        {
            "task": full_task,                       # 完整任务：历史需求 + 最新指令
            "prefs": prefs,
            "profile": profile_text,
            "tests": [],
            "max_attempts": 3,
            "user_id": state.get("user_id", ""),  # P0-A-2：透传用户供配额解析
            "goal_mode": _is_goal_task(latest),     # P2：目标判定仍看最新指令
        }
    )
    reply = result.get("final") or _format_exec_result(result)
    return {
        "messages": [AIMessage(content=reply)],
        # P2-K：把 exec 子图内部累计的 token 带回对话状态（成本口径完整）
        "tokens": state.get("tokens", 0) + int(result.get("tokens", 0) or 0),
    }


async def _task_status_text(mgr, task_id: str) -> str:
    """B：把任务状态/结果拼成给用户看的文本。核心逻辑，可单测（不碰 LLM）。"""
    if not task_id:
        return "⚠️ 需要任务号。格式：查任务 <任务号>"
    task = await mgr.get(task_id)
    if task is None:
        return f"❌ 找不到任务 `{task_id}`（可能不属于你或已删除）。"
    s = task.status.value
    lines = [f"任务 `{task_id}` 状态：**{s}**"]
    if s == "succeeded":
        out = (task.result.get("stdout") or "").strip()
        lines.append("```\n" + out + "\n```" if out else "（无输出）")
    elif s == "failed":
        lines.append("失败原因：" + (task.error or "未知"))
    elif s == "running":
        lines.append("⏳ 执行中，稍后再查…")
    elif s == "queued":
        lines.append("🕐 排队中，稍后再查…")
    elif s == "cancelled":
        lines.append("已取消。")
    return "\n".join(lines)


async def _apply_task_request(mgr, req: TaskRequest) -> str:
    """B：执行解析出的任务请求（create → 入队；status → 查询；list → 我的任务列表）。

    核心逻辑与 LLM 分离，可单测。
    """
    if req.action == "status":
        return await _task_status_text(mgr, req.task_id)
    if req.action == "list":
        tasks = await mgr.list(limit=10)
        if not tasks:
            return "📭 你还没有任务。说「帮我后台跑个任务：<代码>」即可提交。"
        marks = {
            "succeeded": "✅", "failed": "❌", "running": "⏳",
            "queued": "🕐", "cancelled": "⛔",
        }
        lines = ["📋 **我的最近任务：**\n"]
        for t in tasks:
            lines.append(
                f"{marks.get(t.status.value, '•')} `{t.id}` · {t.task_type} · **{t.status.value}**"
            )
        return "\n".join(lines)
    if not req.code:
        return "⚠️ 没识别到要运行的代码。请用代码块说明，或说「帮我后台跑：<代码>」。"
    task = await mgr.create(task_type="code_exec", payload={"code": req.code})
    await mgr.enqueue(task.id)
    return (
        f"✅ 已提交后台任务（任务号 `{task.id}`）。"
        f"执行完成后可用「查任务 {task.id}」获取结果。"
    )


async def _task_node(state: ChatState) -> dict[str, Any]:
    """B：任务队列节点。LLM 结构化抽取（create/status+code/task_id）→ 入队/查状态。

    面试可讲：对话图的「异步出口」——长耗时任务不阻塞对话，返回 task_id
    由后台 worker 消费，再通过对话「查任务 <id>」轮询，形成闭环。
    """
    from tasks.manager import TaskManager  # lazy

    model = build_chat_model().with_structured_output(TaskRequest, method="json_mode")
    latest = state["messages"][-1]
    req = model.invoke([SystemMessage(content=TASK_PROMPT), latest])
    logger.info("任务节点解析: %s (%s)", req.action, req.reasoning)
    mgr = TaskManager(user_id=state.get("user_id", ""))
    text = await _apply_task_request(mgr, req)
    return {
        "messages": [AIMessage(content=text)],
        "tokens": state.get("tokens", 0) + _usage_tokens(req, "task"),
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
    g.add_node("task", _task_node)     # B：任务队列节点（后台执行/查状态）
    g.add_edge(START, "compress")
    g.add_edge("compress", "intent")
    g.add_conditional_edges(
        "intent", _route, {"chat": "chat", "exec": "exec", "read": "read", "task": "task"}
    )
    for n in ("chat", "exec", "read", "task"):
        g.add_edge(n, END)
    return g.compile(checkpointer=checkpointer)
