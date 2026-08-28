"""记忆 top_k 权重设计测试（P2 方案A）。纯逻辑，无需服务。

验证两件事：
  1) 每类型保底 1 条（四类都有覆盖）；
  2) type_weight 真的生效——竞争名额时，权重让 semantic 优先；
     无权重时（对照组）结果不同，证明权重不是摆设。
"""
from memory_manager import MemoryManager


def _mem(mid, mtype, score=0.5, importance=0.5, last_access=None, content="m"):
    return {
        "id": mid,
        "memory_type": mtype,
        "score": score,
        "importance": importance,
        "last_access_at": last_access,
        "content": content,
    }


def _call(memory_dict, configs, type_weights=None, total_cap=None):
    mm = MemoryManager.__new__(MemoryManager)  # 跳过 __init__，只测排序逻辑
    return mm.get_the_top_k_memories(
        memory_dict=memory_dict,
        memory_configs=configs,
        type_weights=type_weights,
        total_cap=total_cap,
    )


def test_each_type_guaranteed():
    """每类型至少保底 1 条，不会某类型全灭。"""
    memory_dict = {
        "semantic": [_mem("s1", "semantic", 0.9)],
        "episodic": [_mem("e1", "episodic", 0.9)],
        "summary": [_mem("sum1", "summary", 0.9)],
        "procedural": [_mem("p1", "procedural", 0.9)],
    }
    configs = {t: {"k": 1} for t in memory_dict}
    r = _call(memory_dict, configs)
    assert all(len(r[t]) == 1 for t in memory_dict), r


def test_type_weight_decides_competition():
    """权重生效（关键）：总名额封顶 3 < 配额和 4 → 真实竞争。

    保底后剩 1 个名额，两个类型各 1 个候选竞争：
      - semantic 候选 base=0.625 × 权重1.3 = 0.8125 → 胜出
      - episodic 候选 base=0.67  × 权重1.0 = 0.67
    无权重对照组：0.625 < 0.67 → episodic 胜出 → 证明权重真的起作用。
    """
    memory_dict = {
        "semantic": [
            _mem("s_top", "semantic", 0.9),   # 保底
            _mem("s_comp", "semantic", 0.5),  # 竞争池
        ],
        "episodic": [
            _mem("e_top", "episodic", 0.95),  # 保底
            _mem("e_comp", "episodic", 0.6),  # 竞争池
        ],
    }
    configs = {"semantic": {"k": 2}, "episodic": {"k": 2}}
    # 带权重：semantic 竞争胜出
    r = _call(memory_dict, configs, total_cap=3)
    ids = [m["id"] for lst in r.values() for m in lst]
    assert "s_comp" in ids, f"权重应让 semantic 胜出，实际: {ids}"
    assert "e_comp" not in ids

    # 对照组（权重全 1.0）：episodic base 更高 → episodic 胜出
    r2 = _call(
        memory_dict,
        configs,
        type_weights={"semantic": 1.0, "episodic": 1.0},
        total_cap=3,
    )
    ids2 = [m["id"] for lst in r2.values() for m in lst]
    assert "e_comp" in ids2 and "s_comp" not in ids2, f"无权重应 episodic 胜出: {ids2}"
