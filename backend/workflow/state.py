"""执行子图状态（M2-4）。

与对话状态(ChatState)分离：执行子图只管「一个任务」的推进，字段各自独立，
M3 会在 code 之后加 verify/fix 的计数与反馈字段。
"""
from typing import TypedDict


class ExecState(TypedDict, total=False):
    task: str               # 用户需求（从最新消息提取）
    plan: str               # LLM 生成的实现方案
    code: str               # LLM 生成的代码
    stdout: str             # 沙箱执行 stdout
    stderr: str             # 沙箱执行 stderr
    exit_code: int          # 退出码
    timed_out: bool         # 是否超时
    security_blocked: bool  # 是否被安全策略拦截
    error: str              # 系统级错误
    # ---- M3 自愈 ----
    tests: list             # list[dict]，hidden test（expected/mode/desc）
    attempts: int           # 当前尝试轮数（从 1 起）
    max_attempts: int       # 熔断阈值（默认 3）
    feedback: str           # 上一轮验证失败反馈（喂给 fix）
    passed: bool            # 是否通过
    final: str              # 最终结果文本
    # ---- M5 成本记账 ----
    tokens: int             # 累计消耗的 LLM token（plan/write/fix 各节点累加）
    # ---- P0-1b 记忆个性化 ----
    prefs: str              # 用户程序性记忆（编码偏好），由 chat 图检索后传入
    # ---- P0-A-2 多租户 ----
    user_id: str            # 请求用户（由 chat 图从认证上下文传入，用于解析配额）
