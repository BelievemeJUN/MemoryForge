"""沙箱引擎集成测试（M1 / P1-G-1）。需要 Docker daemon。

覆盖：正常代码 / 语法错误 / 危险代码拦截 / 死循环超时强杀。
"""
import pytest

from sandbox.executor import DockerExecutor
from sandbox.limits import SandboxLimits


@pytest.fixture(scope="module")
def ex():
    # 显式用基础镜像：CI runner 无需本地模板镜像，聚焦沙箱引擎本身
    return DockerExecutor(image="python:3.12-slim")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_code(ex):
    r = await ex.arun_python("print('hello sandbox')\nprint(1 + 1)")
    assert "hello sandbox" in r.stdout and "2" in r.stdout
    assert r.exit_code == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_syntax_error_reported(ex):
    r = await ex.arun_python("def f(:")
    assert not r.security_blocked
    assert r.exit_code != 0
    assert "SyntaxError" in r.stderr


@pytest.mark.integration
@pytest.mark.asyncio
async def test_os_system_blocked(ex):
    r = await ex.arun_python("import os\nos.system('rm -rf /')")
    assert r.security_blocked and "[安全拦截]" in r.stderr


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subprocess_blocked(ex):
    r = await ex.arun_python("import subprocess\nsubprocess.run(['ls'])")
    assert r.security_blocked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_passwd_read_blocked(ex):
    r = await ex.arun_python("open('/etc/passwd').read()")
    assert r.security_blocked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_eval_blocked(ex):
    r = await ex.arun_python("print(eval('1+1'))")
    assert r.security_blocked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_kill():
    short = DockerExecutor(image="python:3.12-slim", limits=SandboxLimits(timeout=5.0))
    r = await short.arun_python("while True: pass")
    assert r.timed_out
