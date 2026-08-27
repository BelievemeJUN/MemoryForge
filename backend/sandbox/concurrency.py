"""沙箱并发控制（P0-D-1）：防一个用户瞬间起一堆容器打垮 Docker daemon。

设计（面试可讲）：
  - 双层信号量：全局上限（SANDBOX_MAX_GLOBAL，默认 4）+ 每用户上限
    （SANDBOX_MAX_PER_USER，默认 2）。恶意用户只能占自己那 2 个名额，
    也挤不掉其他租户（全局 4 兜底整体资源）。
  - 非阻塞尝试：拿不到直接「系统繁忙」拒绝，不排队不阻塞主线程——
    宁可诚实拒绝，也不让请求堆积拖垮 daemon。
  - 计数在「启动容器前」检查：静态审查先跑（不占名额），
    审查失败/白名单外的根本不进容器，不消耗并发额度。
"""
import os
import threading
from contextlib import contextmanager

GLOBAL_MAX = int(os.getenv("SANDBOX_MAX_GLOBAL", "4"))
PER_USER_MAX = int(os.getenv("SANDBOX_MAX_PER_USER", "2"))

_global_sem = threading.Semaphore(GLOBAL_MAX)
_user_sems: dict[str, threading.Semaphore] = {}
_user_guard = threading.Lock()


def _user_sem(user_id: str) -> threading.Semaphore:
    with _user_guard:
        sem = _user_sems.get(user_id)
        if sem is None:
            sem = threading.Semaphore(PER_USER_MAX)
            _user_sems[user_id] = sem
        return sem


def try_acquire(user_id: str = "") -> bool:
    """非阻塞申请沙箱名额（全局 + 每用户同时通过才成功）。"""
    if not _global_sem.acquire(blocking=False):
        return False
    if not _user_sem(user_id).acquire(blocking=False):
        _global_sem.release()  # 每用户满了也要还回全局名额
        return False
    return True


def release(user_id: str = "") -> None:
    """归还沙箱名额（与 try_acquire 成对）。"""
    _user_sem(user_id).release()
    _global_sem.release()


@contextmanager
def sandbox_slot(user_id: str = ""):
    """上下文管理器：acquired=False 表示并发已满（调用方应直接拒绝执行）。"""
    acquired = try_acquire(user_id)
    try:
        yield acquired
    finally:
        if acquired:
            release(user_id)
