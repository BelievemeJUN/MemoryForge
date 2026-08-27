"""DockerExecutor —— 一次性容器沙箱执行引擎（M1，核心）

分层防御第一道防线：Docker 资源级隔离。
每个任务一个全新容器，用完即毁（remove），默认断网（--network=none），
rootfs 只读（仅 /tmp 可写且限量），非 root 用户，去掉全部 Linux capabilities。

面试可讲：
  - 为什么用「一次性容器 + 只读 rootfs」：代码想改容器？根本没地方写。
  - 为什么 detach + watchdog 强杀：容器内 timeout 可能被绕过，宿主侧超时才是底线。
  - 为什么静态审查放执行前：速度快、可测试、可量化（对应评测的「安全级拦截率」）。
"""
import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import docker

from .limits import SandboxLimits, DEFAULT_LIMITS
from .security import scan_code

logger = logging.getLogger(__name__)

# P0-D-1：白名单收窄 daemon 攻击面
#   - 镜像白名单：只允许预审过的沙箱镜像，防调用方拉任意镜像（含带宿主挂载/提权的恶意镜像）
#   - 网络模式白名单：只允许断网（none），防意外开放网络
_ALLOWED_IMAGES = frozenset({"python:3.12-slim"})
_ALLOWED_NETWORK_MODES = frozenset({"none"})


# seccomp profile（M6 深化）：显式禁掉逃逸/提权 syscall
_SECCOMP_PROFILE = os.path.join(os.path.dirname(__file__), "seccomp.json")


def _seccomp_opt() -> str:
    """返回 seccomp security_opt 值。

    注意：docker-py 需把 profile 的 JSON 内容内联传入（不是路径），
    daemon 会直接解析该值为 seccomp profile。
    """
    with open(_SECCOMP_PROFILE, "r", encoding="utf-8") as f:
        return f"seccomp={f.read()}"


@dataclass
class ExecutionResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration: float = 0.0
    security_blocked: bool = False
    violations: list = field(default_factory=list)
    error: Optional[str] = None  # 系统级错误（Docker 不可用/镜像缺失等）


class DockerExecutor:
    """基于一次性 Docker 容器的代码执行器（同步核心 + async 包装）。"""

    def __init__(
        self,
        image: str = "python:3.12-slim",
        limits: SandboxLimits = DEFAULT_LIMITS,
        network_mode: str = "none",
    ):
        # P0-D-1：白名单校验（构造期拒绝，快速失败）
        if image not in _ALLOWED_IMAGES:
            raise ValueError(f"镜像不在白名单: {image}")
        if network_mode not in _ALLOWED_NETWORK_MODES:
            raise ValueError(f"网络模式不在白名单: {network_mode}")
        self.image = image
        self.limits = limits
        self.network_mode = network_mode
        self._client: Optional[docker.DockerClient] = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ---------- 对外 API ----------

    async def arun_python(self, code: str) -> ExecutionResult:
        """异步入口：静态审查 + 容器执行，供 FastAPI 直接 await。"""
        return await asyncio.to_thread(self.run_python, code)

    def run_python(self, code: str) -> ExecutionResult:
        """同步核心：静态审查 -> 起容器 -> 等待/超时 -> 收集输出 -> 清理。"""
        # 0. 静态安全审查（第二道防线，先于容器，省资源且可量化）
        scan = scan_code(code, max_length=self.limits.max_code_length)
        if not scan.allowed:
            return ExecutionResult(
                security_blocked=True,
                violations=scan.violations,
                stderr=f"[安全拦截] {scan.reason}",
            )

        # 1. 把代码写进宿主机临时工作目录
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="codemind_") as workdir:
            script = os.path.join(workdir, "main.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(code)
            # bind mount 权限坑：容器内 nobody(uid 65534) 需能读目录和文件。
            # 宿主机 TemporaryDirectory 默认 0700/0600，容器内映射 uid 不匹配会 Permission denied。
            os.chmod(workdir, 0o755)
            os.chmod(script, 0o644)

            # 容器内执行入口脚本目录（也以只读挂载）
            scripts_dir = os.path.join(os.path.dirname(__file__), "scripts")

            # 2. 启动一次性容器
            container = None
            try:
                container = self.client.containers.run(
                    image=self.image,
                    command=["/bin/sh", "/scripts/run_python.sh", "/work/main.py"],
                    detach=True,
                    network_mode=self.network_mode,           # 默认断网
                    mem_limit=self.limits.mem_limit,
                    nano_cpus=self.limits.nano_cpus,
                    pids_limit=self.limits.pids_limit,
                    read_only=True,                           # rootfs 只读
                    # 仅 /tmp 可写且限量（Docker tmpfs 需显式 size 选项）
                    tmpfs={"/tmp": f"rw,size={self.limits.disk_limit}"},
                    volumes={
                        workdir: {"bind": "/work", "mode": "ro"},
                        scripts_dir: {"bind": "/scripts", "mode": "ro"},
                    },
                    working_dir="/work",
                    user="nobody",                            # 非 root
                    cap_drop=["ALL"],                         # 去掉全部能力
                    # 禁提权 + 自定义 seccomp（禁 ptrace/mount/pivot_root 等逃逸 syscall）
                    security_opt=[
                        "no-new-privileges:true",
                        _seccomp_opt(),
                    ],
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("容器启动失败")
                return ExecutionResult(
                    error=f"容器启动失败: {e}", duration=time.monotonic() - start
                )

            # 3. 等待退出 + 宿主侧超时强杀
            timed_out = False
            exit_code: Optional[int] = None
            try:
                wait_res = container.wait(timeout=self.limits.timeout)
                exit_code = wait_res.get("StatusCode")
            except Exception:  # noqa: BLE001  # 超时等异常
                timed_out = True
                logger.warning("沙箱执行超时(%.1fs)，强杀容器 %s", self.limits.timeout, container.id)
                try:
                    container.kill()
                except Exception:  # noqa: BLE001
                    pass

            # 4. 收集输出 + 清理
            stdout = stderr = ""
            try:
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass

        # 5. 截断 + 返回
        duration = time.monotonic() - start
        return ExecutionResult(
            stdout=self._truncate(stdout),
            stderr=self._truncate(stderr),
            exit_code=exit_code,
            timed_out=timed_out,
            duration=duration,
        )

    def _truncate(self, text: str) -> str:
        """按字节和行数双重截断输出，防刷屏/内存放大。"""
        if not text:
            return text
        raw = text.encode("utf-8", errors="replace")
        if len(raw) > self.limits.max_output_bytes:
            text = raw[: self.limits.max_output_bytes].decode("utf-8", errors="replace")
            text += "\n...[输出已截断]..."
        lines = text.splitlines()
        if len(lines) > self.limits.max_output_lines:
            text = "\n".join(lines[: self.limits.max_output_lines])
            text += f"\n...[共 {len(lines)} 行，仅显示前 {self.limits.max_output_lines} 行]..."
        return text
