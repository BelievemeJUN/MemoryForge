"""输出比对策略（M3-1）。

面试可讲：比对不是「一刀切」——exact 适合精确匹配（如题号），
fuzzy 容忍空白/大小写差异（LLM 输出的常见噪声），float 用浮点容差
（数值计算任务避免 0.1+0.2 这类误差误判）。每种模式都有明确适用场景。
"""
import math


def compare(mode: str, actual: str, expected: str) -> tuple[bool, str]:
    """按模式比对 stdout，返回 (是否通过, 说明)。"""
    mode = (mode or "exact").lower()
    if mode == "exact":
        return _exact(actual, expected)
    if mode == "fuzzy":
        return _fuzzy(actual, expected)
    if mode == "float":
        return _float(actual, expected)
    raise ValueError(f"未知比对模式: {mode}")


def _exact(actual: str, expected: str) -> tuple[bool, str]:
    a, e = actual.strip(), expected.strip()
    if a == e:
        return True, ""
    return False, f"exact 不匹配\n  期望: {e!r}\n  实际: {a!r}"


def _fuzzy(actual: str, expected: str) -> tuple[bool, str]:
    """忽略首尾空白与大小写（保留内部空白差异）。"""
    a, e = actual.strip().lower(), expected.strip().lower()
    if a == e:
        return True, ""
    return False, f"fuzzy 不匹配\n  期望: {e!r}\n  实际: {a!r}"


def _extract_numbers(text: str) -> list[float]:
    """提取文本中的数字（含小数/负数/科学计数）。"""
    import re

    pat = re.compile(r"[-+]?\d+\.?\d*(?:[eE][-+]?\d+)?")
    return [float(m) for m in pat.findall(text)]


def _float(actual: str, expected: str, rel_tol: float = 1e-6) -> tuple[bool, str]:
    """浮点容差：提取双方数字序列逐一比对。"""
    a_nums, e_nums = _extract_numbers(actual), _extract_numbers(expected)
    if len(a_nums) != len(e_nums):
        return False, (
            f"float 数字个数不一致\n  期望 {len(e_nums)} 个: {e_nums}\n"
            f"  实际 {len(a_nums)} 个: {a_nums}"
        )
    for i, (a, e) in enumerate(zip(a_nums, e_nums)):
        if not math.isclose(a, e, rel_tol=rel_tol, abs_tol=1e-9):
            return False, f"float 第 {i + 1} 个数不匹配: 期望 {e}, 实际 {a}"
    return True, ""
