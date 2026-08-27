"""沙箱并发上限单元测试（P0-D-1 / P1-G-1）。纯逻辑，无需服务。

注意：信号量是进程级共享（默认全局 4 / 每用户 2），pytest 串行执行，
每个 with 块用完自动归还，计数不跨测试残留。
"""
from sandbox.concurrency import sandbox_slot


def test_per_user_limit():
    # 默认每用户 2 个名额：第 3 个被拒
    with sandbox_slot("u1") as a, sandbox_slot("u1") as b, sandbox_slot("u1") as c:
        assert a and b
        assert not c


def test_global_limit():
    # 默认全局 4：4 个不同用户各占 1 后，第 5 个被拒
    with (
        sandbox_slot("g1") as a,
        sandbox_slot("g2") as b,
        sandbox_slot("g3") as c,
        sandbox_slot("g4") as d,
    ):
        assert a and b and c and d
        with sandbox_slot("g5") as e:
            assert not e


def test_release_recovers():
    with sandbox_slot("r1") as ok1:
        assert ok1
    with sandbox_slot("r1") as ok2:  # 释放后恢复
        assert ok2
