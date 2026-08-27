"""输出比对策略单元测试（M3-1 / P1-G-1）。纯逻辑，无需服务。"""
import pytest

from verifier.assertions import compare


def test_exact():
    ok, _ = compare("exact", "abc\n", "abc")
    assert ok
    assert not compare("exact", "abc", "abd")[0]


def test_fuzzy_ignores_case_whitespace():
    assert compare("fuzzy", "  Hello World  ", "hello world")[0]
    assert not compare("fuzzy", "a", "b")[0]


def test_float_tolerance():
    assert compare("float", "结果: 3.14159", "3.14159")[0]
    assert compare("float", "0.3000000001", "0.3")[0]  # 浮点容差
    assert not compare("float", "1 2", "1")[0]  # 数字个数不一致


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        compare("nope", "a", "b")
