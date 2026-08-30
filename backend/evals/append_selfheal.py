"""循环级扩充：selfheal_cases.yaml 8 → 25 道（8 easy + 10 medium + 7 hard）。

新增题带明显 bug 的 pre_code（off-by-one / 边界 / 算法选错 / 死循环 / 逻辑反向），
测 fix 自愈在中等/高难度 bug 上的表现。
"""
import os

import yaml

HERE = os.path.dirname(__file__)
CASES = os.path.join(HERE, "cases")

# (id, task, pre_code, expected, mode, desc, difficulty)
NEW = [
    ("heal_even_squares", "输出 1 到 10 之间偶数的平方组成的列表",
     "out = [x * x for x in range(1, 11)]\nprint(out)",
     "[4, 16, 36, 64, 100]", "fuzzy", "偶数平方", "medium"),
    ("heal_char_freq", "统计 'banana' 中每个字符出现次数，按字符排序输出形如 a:3 b:1 n:2",
     "s = 'banana'\nfrom collections import Counter\nc = Counter(s)\nprint(c['a'], c['b'], c['n'])",
     "a:3\nb:1\nn:2", "fuzzy", "字符频率", "medium"),
    ("heal_second_max", "找出列表 [3, 7, 2, 9, 5] 中的第二大值并输出",
     "a = [3, 7, 2, 9, 5]\nprint(max(a))",
     "7", "exact", "第二大", "medium"),
    ("heal_binary_search", "在有序列表 [1, 3, 5, 7, 9] 中用二分查找数字 7 的索引并输出",
     "a = [1, 3, 5, 7, 9]\nt = 7\nlo, hi = 0, len(a)\nwhile lo < hi:\n    m = (lo + hi) // 2\n    if a[m] < t:\n        lo = m\n    else:\n        hi = m\nprint(lo)",
     "3", "exact", "二分索引", "medium"),
    ("heal_merge_sorted", "合并两个有序列表 [1, 3, 5] 和 [2, 4, 6] 并输出合并后有序结果",
     "a, b = [1, 3, 5], [2, 4, 6]\nprint(a + b)",
     "[1, 2, 3, 4, 5, 6]", "fuzzy", "合并有序", "medium"),
    ("heal_fizzbuzz", "输出 1 到 15 的 FizzBuzz：3 的倍数输出 Fizz、5 的倍数输出 Buzz、同时输出 FizzBuzz，每行一个",
     "for i in range(1, 16):\n    if i % 3 == 0:\n        print('Fizz')\n    else:\n        print(i)",
     "1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz", "fuzzy", "FizzBuzz", "medium"),
    ("heal_remove_spaces", "去掉字符串 'a b c' 中的所有空格并输出",
     "s = 'a b c'\nprint(s.replace(' ', '', 1))",
     "abc", "exact", "去空格", "medium"),
    ("heal_word_freq", "找出句子 'apple banana apple cherry banana apple' 中出现次数最多的单词并输出",
     "s = 'apple banana apple cherry banana apple'\nprint(s.split()[0])",
     "apple", "exact", "词频最高", "medium"),
    ("heal_leap_1900", "判断 1900 是否为闰年（能被4整除但不能被100整除，或能被400整除），输出 True 或 False",
     "y = 1900\nprint(y % 4 == 0)",
     "False", "exact", "1900 非闰年", "medium"),
    ("heal_count_vowels", "统计 'hello' 中元音字母（a/e/i/o/u）的个数并输出",
     "s = 'hello'\nv = set('aeiouh')\nprint(sum(1 for c in s if c in v))",
     "2", "exact", "元音数", "medium"),
    ("heal_fib_dp", "用动态规划计算斐波那契数列第 30 项并输出",
     "def fib(n):\n    if n < 2:\n        return n\n    return fib(n - 1) + fib(n - 2)\nprint(fib(30))",
     "832040", "exact", "fib(30) DP", "hard"),
    ("heal_coin_change", "用硬币面值 [1, 3, 4] 凑出金额 10，输出所需的最少硬币个数",
     "coins = [1, 3, 4]\namt = 10\ncnt = 0\nfor c in sorted(coins, reverse=True):\n    cnt += amt // c\n    amt %= c\nprint(cnt)",
     "3", "exact", "贪心非最优(4+4+1+1=4, 最优3+3+4=3)", "hard"),
    ("heal_max_subarray", "求数组 [-2,1,-3,4,-1,2,1,-5,4] 的最大连续子数组和并输出",
     "a = [-2, 1, -3, 4, -1, 2, 1, -5, 4]\nprint(sum(x for x in a if x > 0))",
     "6", "exact", "最大子序和", "hard"),
    ("heal_longest_palindrome", "找出字符串 'babad' 的最长回文子串的长度并输出",
     "s = 'babad'\nprint(1 if s[0] == s[-1] else 0)",
     "3", "exact", "最长回文", "hard"),
    ("heal_two_sum", "在 [2,7,11,15] 中找出两个和为 9 的元素的下标并输出 [a,b]",
     "a = [2, 7, 11, 15]\nt = 9\nfor i in range(len(a)):\n    for j in range(i, len(a)):\n        if a[i] + a[j] == t and i != j:\n            print([i, j])",
     "[0, 1]", "fuzzy", "两数之和", "hard"),
    ("heal_edit_distance", "计算把字符串 'horse' 变成 'ros' 的最小编辑距离并输出",
     "a, b = 'horse', 'ros'\nprint(abs(len(a) - len(b)))",
     "3", "exact", "编辑距离", "hard"),
    ("heal_nqueens", "输出 8 皇后问题有多少种互不攻击的摆法",
     "def ok(b, r, c):\n    for i in range(r):\n        if b[i] == c or abs(b[i] - c) == r - i:\n            return False\n    return True\ncnt = 0\nfor a in range(8):\n    for b in range(8):\n        for c in range(8):\n            for d in range(8):\n                for e in range(8):\n                    for f in range(8):\n                        for g in range(8):\n                            for h in range(8):\n                                board = [a, b, c, d, e, f, g, h]\n                                if len(set(board)) == 8:\n                                    cnt += 1\nprint(cnt)",
     "92", "exact", "八皇后", "hard"),
]


def main():
    path = os.path.join(CASES, "selfheal_cases.yaml")
    cases = yaml.safe_load(open(path, encoding="utf-8")) or []
    # 现有 8 道标 easy
    for c in cases:
        c["difficulty"] = "easy"
    for cid, task, pre, expected, mode, desc, diff in NEW:
        cases.append(
            {
                "id": cid,
                "task": task,
                "pre_code": pre,
                "tests": [{"expected": expected, "mode": mode, "desc": desc}],
                "max_attempts": 3,
                "difficulty": diff,
            }
        )
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cases, f, allow_unicode=True, sort_keys=False)
    n = {"easy": 0, "medium": 0, "hard": 0}
    for c in cases:
        n[c.get("difficulty", "easy")] += 1
    print(f"循环级 {len(cases)} 题：easy {n['easy']} / medium {n['medium']} / hard {n['hard']}")


if __name__ == "__main__":
    main()
