"""M1 沙箱引擎冒烟测试。

直接运行本脚本即可（无需 pytest）：
    ./.venv/bin/python backend/test/sandbox_smoke.py

验证四件事（对应 M1 验收标准）：
  1. 正常代码：能拿到 stdout / 退出码
  2. 语法错误：如实报错（运行失败，非安全拦截）
  3. 危险代码：被静态审查拦截（security_blocked=True）
  4. 死循环：被宿主侧超时强杀（timed_out=True）

输出格式统一：用 run_* 包装每个用例，成功 ✅ / 失败 ❌。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sandbox.executor import DockerExecutor          # noqa: E402
from sandbox.limits import SandboxLimits             # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    mark = "✅" if cond else "❌"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"{mark} {name}" + (f"  | {detail}" if detail else ""))


def show(result) -> str:
    return (
        f"exit={result.exit_code} timed_out={result.timed_out} "
        f"blocked={result.security_blocked} dur={result.duration:.2f}s "
        f"stdout={result.stdout[:80]!r} stderr={result.stderr[:80]!r}"
    )


async def main():
    # 显式用基础镜像：冒烟测沙箱引擎本身，轻量稳定（模板镜像走端到端验证）
    ex = DockerExecutor(image="python:3.12-slim")
    short = DockerExecutor(image="python:3.12-slim", limits=SandboxLimits(timeout=5.0))

    print("=== 1. 正常代码 ===")
    r = await ex.arun_python("print('hello sandbox')\nprint(1 + 1)")
    check("正常代码拿到输出", "hello sandbox" in r.stdout and "2" in r.stdout, show(r))
    check("正常代码退出码 0", r.exit_code == 0, show(r))

    print("=== 2. 语法错误 ===")
    r = await ex.arun_python("def f(:")
    check(
        "语法错误如实报错(非拦截)",
        not r.security_blocked and r.exit_code != 0 and "SyntaxError" in r.stderr,
        show(r),
    )

    print("=== 3. 危险代码被拦 ===")
    r = await ex.arun_python("import os\nos.system('rm -rf /')")
    check("os.system 被拦", r.security_blocked and "[安全拦截]" in r.stderr, show(r))

    r = await ex.arun_python("import subprocess\nsubprocess.run(['ls'])")
    check("subprocess 被拦", r.security_blocked, show(r))

    r = await ex.arun_python("open('/etc/passwd').read()")
    check("文件越界被拦", r.security_blocked, show(r))

    r = await ex.arun_python("print(eval('1+1'))")
    check("eval 被拦", r.security_blocked, show(r))

    print("=== 4. 死循环超时强杀 ===")
    r = await short.arun_python("while True: pass")
    check("死循环超时强杀", r.timed_out, show(r))

    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
