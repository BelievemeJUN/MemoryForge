"""安全策略：静态代码审查（M1，分层防御的第二道防线）

定位（诚实，面试必讲）：
  - 真正的隔离靠 Docker（第一道防线）：容器 + 资源配额 + 断网 + 去权。
  - 本模块是「友好拦截 + 可量化拦截率」的第二道防线，防的是：
      1. 用户/LLM 的失误（误删、读敏感文件）
      2. 明显恶意（rm -rf、反弹 shell、读 /etc/shadow）
  - 不承诺防「对抗性逃逸」——那需要 Kata/gVisor/VM（见 PLAN_V2 §5，这是已知边界不是盲区）。

实现：AST 静态分析为主（可读、可测、可量化），子串黑名单兜底（覆盖 AST 盲区）。
"""
import ast
from dataclasses import dataclass, field


# —— 禁止导入的模块（根模块名）——
BLOCKED_IMPORTS = frozenset({
    "subprocess", "socket", "shutil", "ctypes", "fcntl", "pty",
    "telnetlib", "ftplib", "smtplib", "http", "urllib", "requests",
    "multiprocessing",
})

# —— 危险的内置函数/全局调用 ——
# 注：open 不在其中——文件访问安全由下方「路径越界检查」管（允许 /work、/tmp 前缀），
# 无条件拦 open 会误伤正常文件读写（如 /tmp 临时文件）。
BLOCKED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "getattr", "setattr",
    "globals", "locals", "vars", "input",
})

# —— 危险的对象方法/属性（结合所属模块判断，见 _is_blocked_attr）——
BLOCKED_ATTRS = frozenset({
    "system", "popen", "Popen", "run", "check_output", "check_call", "call",
    "rmtree", "remove", "unlink", "rmdir", "kill", "terminate", "send_signal",
    "mkfs", "mount", "unmount", "chmod", "chown", "execv", "spawn", "fork",
})

# —— 危险代码片段兜底（子串，覆盖 AST 抓不到的字符串/shell 场景）——
BLOCKED_SUBSTRINGS = (
    "rm -rf", "mkfs.", "dd if=", ":(){ :|:& };:", "shutdown", "reboot",
    "/etc/passwd", "/etc/shadow", "/root/", "chmod 777", "chmod -R",
    "base64", "eval(", "exec(",  # 混淆手段常见特征
)

# —— 允许读取的路径前缀（文件越界检查，open 的目标必须在此范围内）——
ALLOWED_READ_PREFIXES = ("/work", "/tmp")


@dataclass
class ScanResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.violations) if self.violations else ""


def _call_name(node: ast.AST) -> str | None:
    """把调用表达式还原成可读名字，如 os.system / subprocess.run"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _is_blocked_attr(name: str) -> bool:
    """判断 '模块.属性' 式调用是否命中危险属性，且对象属于敏感模块。
    例：os.system / Path.unlink / subprocess.run 命中；pandas.plot 不命中。
    """
    if not name or "." not in name:
        return False
    head, _, attr = name.rpartition(".")
    if attr not in BLOCKED_ATTRS:
        return False
    root = head.split(".")[0]
    return root in BLOCKED_IMPORTS or head in ("os", "pathlib", "Path")


def scan_code(code: str, max_length: int = 20_000) -> ScanResult:
    """静态审查一段 Python 代码，返回是否放行 + 违规清单。"""
    violations: list[str] = []

    # 0. 长度上限
    if len(code) > max_length:
        violations.append(f"代码超长: {len(code)} > {max_length}")

    # 1. 子串兜底（快速、覆盖 AST 盲区）
    for frag in BLOCKED_SUBSTRINGS:
        if frag in code:
            violations.append(f"包含危险片段: {frag!r}")

    # 2. AST 审查
    try:
        tree = ast.parse(code)
    except SyntaxError:
        # 语法错误不拦截——交给容器内解释器如实报错（属"运行失败"而非"安全违规"）
        return ScanResult(allowed=True, violations=violations)

    for node in ast.walk(tree):
        # 2.1 import 检查
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    violations.append(f"禁止导入模块: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BLOCKED_IMPORTS:
                violations.append(f"禁止导入模块: {node.module}")

        # 2.2 危险调用
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in BLOCKED_CALLS:
                violations.append(f"禁止调用: {name}")
            elif _is_blocked_attr(name):
                violations.append(f"禁止调用: {name}")

        # 2.3 文件越界（open 的路径必须在允许前缀内）
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "open"
            and node.args
        ):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                path = arg.value
                if not path.startswith(ALLOWED_READ_PREFIXES):
                    violations.append(f"文件越界访问: {path!r}")

    return ScanResult(allowed=not violations, violations=violations)
