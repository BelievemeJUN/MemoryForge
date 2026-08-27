"""沙箱安全回归自动化（P1-G-2）：每次沙箱改动自动跑全量安全用例。

数据源：backend/evals/cases/security_cases.yaml
  - expect_blocked          → 静态拦截（AST 审查直接拦）
  - expect_resource_killed  → 资源配额兜底（pids/mem/超时/tmpfs）
"""
import os

import pytest
import yaml

from sandbox.executor import DockerExecutor
from sandbox.limits import SandboxLimits

_CASES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "evals", "cases", "security_cases.yaml"
)

with open(_CASES_PATH, encoding="utf-8") as _f:
    _CASES = yaml.safe_load(_f) or []


def _make_case(case):
    async def _run():
        ex = DockerExecutor(limits=SandboxLimits(timeout=10.0))
        r = await ex.arun_python(case["danger_code"])
        if case.get("expect_blocked"):
            assert r.security_blocked, (
                f"{case['id']} 应被静态拦截，实际未拦: {r.stderr or r.stdout}"
            )
        if case.get("expect_resource_killed"):
            # 双重防御都算防住：静态拦截（如 os.fork）或资源配额兜底（超时/异常/非零退出）
            defended = (
                r.security_blocked
                or r.timed_out
                or bool(r.error)
                or r.exit_code not in (0, None)
            )
            assert defended, (
                f"{case['id']} 应被资源配额/静态拦截防住，实际未受限: "
                f"exit={r.exit_code} timed_out={r.timed_out} blocked={r.security_blocked}"
            )

    _run.__name__ = f"test_{case['id']}"
    return pytest.mark.integration(pytest.mark.asyncio(_run))


for _c in _CASES:
    globals()[f"test_{_c['id']}"] = _make_case(_c)


def test_security_case_manifest_loaded():
    """数据源本身应非空且结构完整（元测试）。"""
    assert len(_CASES) >= 10, "security_cases.yaml 用例数异常"
    for c in _CASES:
        assert c.get("id") and c.get("danger_code"), f"用例结构缺失: {c}"
