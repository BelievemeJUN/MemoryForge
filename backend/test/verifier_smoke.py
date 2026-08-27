"""M3-1 verifier 模块冒烟：输出比对 + hidden test 运行分类。

直接运行：
    ./.venv/bin/python backend/test/verifier_smoke.py

验证：
  1. 正确代码 → passed
  2. 错误代码 → failed（输出不匹配）
  3. 死循环代码 → timeout
  4. float 容差比对
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from verifier.assertions import compare  # noqa: E402
from verifier.test_runner import HiddenTest, run_code_with_tests  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✅ {name}")
    else:
        FAIL += 1
        print(f"❌ {name}  | {detail}")


def main():
    print("=== 1. 比对模式 ===")
    ok, why = compare("exact", "  hello\n", "hello")
    check("exact 忽略首尾空白", ok, why)
    ok, why = compare("fuzzy", "  Hello World\n", "hello world")
    check("fuzzy 忽略大小写空白", ok, why)
    ok, why = compare("float", "结果是 0.30000000000000004", "0.3")
    check("float 容差（0.1+0.2 不误判）", ok, why)

    print("=== 2. hidden test 沙箱运行 ===")
    good = "print('55')"
    r = run_code_with_tests(good, [HiddenTest(expected="55")])
    check("正确代码 passed", r.passed and r.category == "passed", r.detail)

    bad = "print('54')"
    r = run_code_with_tests(bad, [HiddenTest(expected="55")])
    check("错误输出 failed", (not r.passed) and r.category == "failed", r.detail)

    loop = "while True: pass"
    r = run_code_with_tests(loop, [HiddenTest(expected="55")])
    check("死循环 timeout", (not r.passed) and r.category == "timeout", r.detail)

    # 多测试：任一失败即失败
    multi = run_code_with_tests(
        "print('1')\nprint('2')",
        [HiddenTest(expected="1"), HiddenTest(expected="2"), HiddenTest(expected="3")],
    )
    check("多测试任一失败即 failed", (not multi.passed) and multi.category == "failed")

    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
