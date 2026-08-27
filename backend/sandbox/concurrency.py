"""沙箱并发控制（P0-D-1 进程内 → P2 分布式）：防一个用户/多实例瞬间起一堆容器打垮 Docker。

设计（面试可讲）：
  - 双层上限：全局（SANDBOX_MAX_GLOBAL，默认 4）+ 每用户（SANDBOX_MAX_PER_USER，默认 2）。
  - **Redis 分布式信号量**：多 uvicorn worker / 多实例共享同一份计数，
    不再各自为政（进程内 Semaphore 在多实例下上限会失效）。
  - 原子性用 Lua 脚本（GET+INCR 一步完成，无竞态）；槽位带 TTL（默认 60s），
    进程崩溃后槽位自动释放，不留死锁。
  - 回退：Redis 不可用时降级到进程内 Semaphore（本地无服务也能跑测试）。
"""
import os
import threading
from contextlib import contextmanager

try:  # redis 同步客户端（executor.run_python 在 to_thread 里跑）
    import redis as _redis
except ImportError:  # pragma: no cover
    _redis = None

GLOBAL_MAX = int(os.getenv("SANDBOX_MAX_GLOBAL", "4"))
PER_USER_MAX = int(os.getenv("SANDBOX_MAX_PER_USER", "2"))
_SLOT_TTL = int(os.getenv("SANDBOX_SLOT_TTL", "60"))

# Lua：原子获取一个槽位（GET+INCR+EXPIRE 一步，无竞态）
_ACQUIRE_LUA = """
local k, max = KEYS[1], tonumber(ARGV[1])
local cur = tonumber(redis.call('GET', k) or '0')
if cur >= max then return 0 end
redis.call('INCR', k)
redis.call('EXPIRE', k, ARGV[2])
return 1
"""
# Lua：原子释放一个槽位
_RELEASE_LUA = """
local k = KEYS[1]
local cur = tonumber(redis.call('GET', k) or '0')
if cur > 0 then redis.call('DECR', k) end
return cur
"""


class _RedisCounter:
    """Redis 原子计数（连接一次，脚本预注册）。"""

    def __init__(self):
        self._client = None
        self._acquire = None
        self._release = None
        if _redis is not None:
            try:
                c = _redis.from_url(
                    os.getenv("REDIS_URL", "redis://localhost:6380/0"),
                    socket_connect_timeout=2,
                )
                c.ping()
                self._client = c
                self._acquire = c.register_script(_ACQUIRE_LUA)
                self._release = c.register_script(_RELEASE_LUA)
            except Exception:  # noqa: BLE001  Redis 不可用 → 走本地回退
                self._client = None

    def available(self) -> bool:
        return self._client is not None

    def acquire_one(self, key: str, limit: int) -> bool:
        return bool(self._acquire(keys=[key], args=[limit, _SLOT_TTL]))

    def release_one(self, key: str) -> None:
        self._release(keys=[key])


_counter = _RedisCounter()

# ---- 本地回退（Redis 不可用）----
_fallback_global = threading.Semaphore(GLOBAL_MAX)
_fallback_users: dict[str, threading.Semaphore] = {}
_fallback_guard = threading.Lock()


def _fallback_user(user_id: str) -> threading.Semaphore:
    with _fallback_guard:
        sem = _fallback_users.get(user_id)
        if sem is None:
            sem = threading.Semaphore(PER_USER_MAX)
            _fallback_users[user_id] = sem
        return sem


def try_acquire(user_id: str = "") -> bool:
    """非阻塞申请沙箱名额（全局 + 每用户同时通过才成功）。"""
    if _counter.available():
        if not _counter.acquire_one("sbx:global", GLOBAL_MAX):
            return False
        if not _counter.acquire_one(f"sbx:user:{user_id}", PER_USER_MAX):
            _counter.release_one("sbx:global")  # 每用户满则还回全局名额
            return False
        return True
    # 本地回退
    if not _fallback_global.acquire(blocking=False):
        return False
    if not _fallback_user(user_id).acquire(blocking=False):
        _fallback_global.release()
        return False
    return True


def release(user_id: str = "") -> None:
    """归还沙箱名额（与 try_acquire 成对）。"""
    if _counter.available():
        _counter.release_one(f"sbx:user:{user_id}")
        _counter.release_one("sbx:global")
    else:
        _fallback_user(user_id).release()
        _fallback_global.release()


@contextmanager
def sandbox_slot(user_id: str = ""):
    """上下文管理器：acquired=False 表示并发已满（调用方应直接拒绝执行）。"""
    acquired = try_acquire(user_id)
    try:
        yield acquired
    finally:
        if acquired:
            release(user_id)
