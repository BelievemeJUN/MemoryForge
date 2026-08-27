"""沙箱资源配额配置（M1）

所有限制最终落到 Docker 的 host_config 上。面试可讲：
「我不用『一刀切』配额，而是把内存/CPU/进程数/磁盘/超时/输出量拆成独立维度，
  每个维度都能单独收紧，并且都有对应的测试用例。」
"""
from dataclasses import dataclass, replace
import os
import re


@dataclass(frozen=True)
class SandboxLimits:
    # 内存上限（Docker mem_limit 格式：256m / 1g）
    mem_limit: str = "256m"
    # CPU 上限：nano_cpus，10^9 = 1 核。默认 0.5 核
    nano_cpus: int = 500_000_000
    # 进程数上限（防 fork 炸弹）
    pids_limit: int = 64
    # 可写临时区容量（tmpfs 挂载到 /tmp，rootfs 其余只读）
    disk_limit: str = "100m"
    # 执行超时（秒），超时由宿主机 watchdog 强杀
    timeout: float = 30.0
    # 输出截断：单次最多捕获的字节数
    max_output_bytes: int = 65_536
    # 输出截断：最多保留的行数
    max_output_lines: int = 500
    # 单次代码长度上限（字符）
    max_code_length: int = 20_000


# 默认配额（可直接替换成更严/更松的实例，便于按场景调整）
DEFAULT_LIMITS = SandboxLimits()

# ===== P0-A-2：按用户解析配额（多租户资源命名空间）=====
# 环境变量格式：QUOTA_USER_<user_id>_<FIELD>=value，例如：
#   QUOTA_USER_42_MEM=128m    # 用户 42 内存降到 128m
#   QUOTA_USER_42_PIDS=32     # 用户 42 进程数上限 32
_QUOTA_ENV_RE = re.compile(r"^QUOTA_USER_(?P<uid>.+)_(?P<field>[A-Z_]+)$")
_FIELD_MAP = {
    "MEM": "mem_limit",
    "CPU": "nano_cpus",
    "PIDS": "pids_limit",
    "TIMEOUT": "timeout",
    "DISK": "disk_limit",
    "MAXOUT": "max_output_bytes",
}


def resolve_limits(user_id: str = "") -> SandboxLimits:
    """按用户解析沙箱配额（默认 DEFAULT_LIMITS，可经 env 按用户覆盖）。

    演示单机多租户配额治理；生产可接配额表 / 网关下发。
    """
    if not user_id:
        return DEFAULT_LIMITS
    overrides: dict[str, object] = {}
    for key, val in os.environ.items():
        m = _QUOTA_ENV_RE.match(key)
        if not m or m.group("uid") != user_id:
            continue
        field = _FIELD_MAP.get(m.group("field"))
        if field is None:
            continue
        if field in ("nano_cpus", "pids_limit", "max_output_bytes"):
            overrides[field] = int(val)
        elif field == "timeout":
            overrides[field] = float(val)
        else:
            overrides[field] = val
    return replace(DEFAULT_LIMITS, **overrides) if overrides else DEFAULT_LIMITS
