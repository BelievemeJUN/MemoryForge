"""hidden test 运行与结果分类（M3-1）。

设计（面试可讲）：
  - 「确定性验证」：在沙箱里跑代码，按期望输出比对，不靠 LLM 自评。
  - 结果分类 4 档：passed / failed / timeout / error——自愈循环按分类给
    LLM 不同的反馈信号（比如 timeout 要提示可能是死循环，error 要带 traceback）。
  - 每条 hidden test 是一条期望输出（stdout 比对），模式可选 exact/fuzzy/float。
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from sandbox.executor import DockerExecutor
from .assertions import compare

logger = logging.getLogger(__name__)


@dataclass
class HiddenTest:
    expected: str          # 期望 stdout（strip 后比对）
    mode: str = "exact"    # exact | fuzzy | float
    desc: str = ""         # 说明（如"样例 1"）


@dataclass
class TestResult:
    passed: bool
    category: str          # passed | failed | timeout | error
    detail: str = ""


def run_code_with_tests(
    code: str,
    tests: list[HiddenTest],
    executor: Optional[DockerExecutor] = None,
) -> TestResult:
    """在沙箱执行代码并跑 hidden tests，返回分类结果。"""
    # 默认用基础镜像（轻量稳定）；模板镜像由端到端/独立测试验证
    executor = executor or DockerExecutor(image="python:3.12-slim")

    # 1. 先跑一次沙箱拿 stdout（多个 test 共用一次执行，省容器开销）
    result = executor.run_python(code)

    if result.timed_out:
        return TestResult(False, "timeout", "代码执行超时（可能死循环或复杂度过高）")
    if result.error:
        return TestResult(False, "error", f"执行系统错误: {result.error}")
    if result.exit_code != 0:
        return TestResult(
            False,
            "error",
            f"代码运行失败（退出码 {result.exit_code}）\nstderr:\n{result.stderr}",
        )

    # 2. 逐条比对期望输出
    for t in tests:
        ok, why = compare(t.mode, result.stdout, t.expected)
        if not ok:
            label = f"[{t.desc}] " if t.desc else ""
            return TestResult(False, "failed", f"{label}{why}")

    return TestResult(True, "passed", f"全部 {len(tests)} 条测试通过")
