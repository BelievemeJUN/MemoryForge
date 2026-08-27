"""执行子图：plan → write → execute（M2-4 雏形）。

verify/fix（自愈）在 M3 加。本版本：LLM 规划 → LLM 写码 → 沙箱执行 → 返回结果。

面试可讲：
  - 子图是 LangGraph 的嵌套图，作为 chat 总图的 exec 节点挂载。
  - 三个节点职责单一（规划/写码/执行），M3 会在链上插入 verify 和 fix 循环。
"""
import logging
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from chat.llm import build_chat_model
from sandbox.concurrency import sandbox_slot
from sandbox.executor import DockerExecutor
from sandbox.limits import resolve_limits
from verifier.assertions import compare
from .state import ExecState

logger = logging.getLogger(__name__)


def _usage_tokens(resp) -> int:
    """从 LLM 响应提取 total_tokens（成本记账用，M5）。

    DeepSeek 的 usage 在 langchain 的 usage_metadata 属性（不是 response_metadata）。
    """
    um = getattr(resp, "usage_metadata", None) or {}
    return int(um.get("total_tokens") or 0)

PLAN_PROMPT = (
    "你是代码实现规划器。用户想实现一个功能，请给出简短实现方案"
    "（步骤 + 用到的算法/库）。控制在 150 字内，不要写代码。"
)
WRITE_PROMPT = (
    "你是 Python 工程师。根据下面的需求和方案，输出完整可运行的 Python 代码。\n"
    "要求：\n"
    "1. 只输出代码，不要解释、不要 markdown 代码块标记\n"
    "2. 代码要能独立运行（不依赖未提供的文件/网络）\n"
    "3. 不要使用被禁止的模块（subprocess/socket/os.system/shutil 等）\n"
    "4. 关键逻辑加中文注释\n"
    "\n需求：{task}\n方案：{plan}"
    "{prefs}"
)


FIX_PROMPT = (
    "你是代码修复工程师。上一版代码没有通过测试，请修复它。\n"
    "要求：\n"
    "1. 只输出修复后的完整 Python 代码，不要解释、不要 markdown 代码块标记\n"
    "2. 注意错误类型：超时可能是死循环/复杂度过高；运行错误要看 traceback\n"
    "3. 不要使用被禁止的模块（subprocess/socket/os.system/shutil 等）\n"
    "\n需求：{task}\n上一版代码：\n{code}\n测试失败信息：\n{feedback}"
)

def _start_router(state: ExecState) -> Literal["plan", "execute"]:
    """起始路由：已有 code（修复/评测场景）直接 execute，否则先 plan→write。"""
    return "execute" if state.get("code") else "plan"


def _plan_node(state: ExecState) -> dict[str, Any]:
    """规划：LLM 给出实现方案（不进沙箱，省资源）。"""
    model = build_chat_model()
    resp = model.invoke(
        [SystemMessage(content=PLAN_PROMPT), HumanMessage(content=state["task"])]
    )
    logger.info("执行子图 plan: %s", resp.content[:80])
    return {
        "plan": resp.content,
        "tokens": state.get("tokens", 0) + _usage_tokens(resp),
    }


def _write_node(state: ExecState) -> dict[str, Any]:
    """写码：LLM 根据方案生成完整 Python 代码。

    P0-1b：若检索到用户编码偏好（程序性记忆），注入提示词做个性化。
    """
    model = build_chat_model()
    prefs_block = ""
    if state.get("prefs"):
        prefs_block = f"\n\n用户编码偏好（请尽量遵循）：\n{state['prefs']}"
    prompt = WRITE_PROMPT.format(
        task=state["task"], plan=state["plan"], prefs=prefs_block
    )
    resp = model.invoke([HumanMessage(content=prompt)])
    return {"code": resp.content, "tokens": state.get("tokens", 0) + _usage_tokens(resp)}


def _execute_node(state: ExecState) -> dict[str, Any]:
    """执行：Docker 沙箱跑代码（M1 引擎），拿 stdout/stderr/退出码/超时。

    P0-A-2：配额按用户解析（resolve_limits），不同租户不同资源上限。
    P0-D-1/P2：先申请沙箱名额（Redis 分布式，全局+每用户），并发满直接拒绝。
    P2：执行审计日志（谁在何时执行了什么代码 + 结果），配合 request_id 全链可查。
    """
    user_id = state.get("user_id", "")
    executor = DockerExecutor(limits=resolve_limits(user_id))
    with sandbox_slot(user_id) as ok:
        if not ok:
            return {
                "error": "沙箱并发已满，请稍后重试",
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "timed_out": False,
                "security_blocked": False,
            }
        result = executor.run_python(state["code"])
    # P2：沙箱执行审计（结构化 JSON 日志，request_id 由 observability 自动带上）
    logger.info(
        "sandbox_exec user=%s code_len=%d exit=%s timeout=%s blocked=%s dur=%.2fs",
        user_id,
        len(state.get("code", "")),
        result.exit_code,
        result.timed_out,
        result.security_blocked,
        result.duration,
    )
    # P2/I：审计落库 PostgreSQL（同步短连接，失败只降级日志）
    from sandbox.audit import write_audit  # lazy，避免顶层依赖 psycopg

    write_audit(
        user_id=user_id,
        code_len=len(state.get("code", "")),
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        security_blocked=result.security_blocked,
        duration=result.duration,
        error=result.error or "",
    )
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "security_blocked": result.security_blocked,
        "error": result.error,
    }


def _verify_node(state: ExecState) -> dict[str, Any]:
    """验证：基于 execute 的 stdout 做确定性比对，并准备通过/熔断文案。

    关键：不重复跑沙箱——用 execute 已经产出的 stdout/exit_code/timed_out。
    """
    attempts = state.get("attempts", 1)
    max_attempts = state.get("max_attempts", 3)

    # 没有 hidden tests → 对话场景，直接展示执行结果（等同 M2-4）
    if not state.get("tests"):
        return {"passed": True, "attempts": attempts, "final": _format_output(state)}

    # 确定性分类
    if state.get("timed_out"):
        detail = "代码执行超时（可能死循环或复杂度过高）"
    elif state.get("error"):
        detail = f"执行系统错误: {state['error']}"
    elif state.get("exit_code") != 0:
        detail = f"代码运行失败（退出码 {state['exit_code']}）\n{state.get('stderr', '')}"
    else:
        fail: str | None = None
        for t in state["tests"]:
            ok, why = compare(t.get("mode", "exact"), state.get("stdout", ""), t["expected"])
            if not ok:
                fail = f"[{t.get('desc', '')}] {why}".strip()
                break
        if fail is None:
            return {
                "passed": True,
                "attempts": attempts,
                "feedback": "",
                "final": (
                    f"✅ 测试通过（第 {attempts} 轮尝试）\n\n"
                    f"**运行输出：**\n```\n{state.get('stdout', '').rstrip()}\n```"
                ),
            }
        detail = fail

    # 失败路径：未超预算 → 给 fix 反馈；超预算 → 熔断（诚实收尾）
    if attempts >= max_attempts:
        return {
            "passed": False,
            "attempts": attempts,
            "final": (
                f"❌ 经过 {attempts} 轮尝试仍未通过测试（已达重试预算），已熔断。\n\n"
                f"最后一次失败原因：\n{detail}"
            ),
        }
    return {"passed": False, "attempts": attempts, "feedback": detail}


def _after_verify(state: ExecState) -> Literal["fix", "__end__"]:
    """条件路由：通过或熔断 → END；否则 → fix（继续自愈循环）。"""
    if state.get("passed"):
        return "__end__"
    if state.get("attempts", 1) >= state.get("max_attempts", 3):
        return "__end__"
    return "fix"


def _fix_node(state: ExecState) -> dict[str, Any]:
    """修复：把确定性验证的失败反馈喂回 LLM 修代码。

    硬计数：attempts 由状态机控制并加 1，超过预算在 verify 熔断——
    不依赖 LLM 自觉（deepresearch 教训：软约束会被无视）。
    """
    model = build_chat_model()
    prompt = FIX_PROMPT.format(
        task=state["task"], code=state["code"], feedback=state["feedback"]
    )
    resp = model.invoke([HumanMessage(content=prompt)])
    return {
        "code": resp.content,
        "attempts": state.get("attempts", 1) + 1,
        "tokens": state.get("tokens", 0) + _usage_tokens(resp),
    }


def _format_output(state: ExecState) -> str:
    """无 hidden test 时的输出文案（等同 M2-4 的展示）。"""
    if state.get("security_blocked"):
        return "⚠️ 代码被安全策略拦截：\n" + (state.get("stderr") or "")
    lines = []
    if state.get("timed_out"):
        lines.append("⏱️ 代码执行超时，已被强制终止。")
    if state.get("error"):
        lines.append("❌ 执行出错：" + state["error"])
    if state.get("stdout"):
        lines.append("**运行输出：**\n```\n" + state["stdout"].rstrip() + "\n```")
    if state.get("stderr"):
        lines.append("**stderr：**\n```\n" + state["stderr"].rstrip() + "\n```")
    if state.get("exit_code") is not None and not state.get("timed_out"):
        lines.append(f"（退出码 {state['exit_code']}）")
    return "\n".join(lines)


_exec_graph = None


def get_exec_graph():
    """构建并缓存执行子图（单例）。"""
    global _exec_graph
    if _exec_graph is None:
        g = StateGraph(ExecState)
        g.add_node("plan", _plan_node)
        g.add_node("write", _write_node)
        g.add_node("execute", _execute_node)
        g.add_node("verify", _verify_node)
        g.add_node("fix", _fix_node)
        # 已有 code（修复/评测）直接 execute；否则 plan→write
        g.add_conditional_edges(
            START,
            _start_router,
            {"plan": "plan", "execute": "execute"},
        )
        g.add_edge("plan", "write")
        g.add_edge("write", "execute")
        g.add_edge("execute", "verify")
        # 自愈循环：通过/熔断 → END；否则 → fix → execute
        g.add_conditional_edges(
            "verify",
            _after_verify,
            {"fix": "fix", "__end__": END},
        )
        g.add_edge("fix", "execute")
        _exec_graph = g.compile()
    return _exec_graph
