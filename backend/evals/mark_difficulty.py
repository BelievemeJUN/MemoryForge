"""答案级难度标注：answer_cases.yaml 92 题启发式分 easy/medium（hard 在 hard_cases.yaml）。"""
import os

import yaml

HERE = os.path.dirname(__file__)
CASES = os.path.join(HERE, "cases")

# 命中任一关键词 → medium（涉及算法/结构处理）；否则 easy（基础运算/简单操作）
_MEDIUM_KW = [
    "查找", "二分", "第二大", "最长", "去重", "词频", "排序", "合并",
    "递归", "转置", "Fizz", "旋转", "重排", "频率", "anagram", "回文",
    "质数", "最大公约", "最小公倍", "完全数", "出现次数",
]


def main():
    path = os.path.join(CASES, "answer_cases.yaml")
    cases = yaml.safe_load(open(path, encoding="utf-8")) or []
    n = {"easy": 0, "medium": 0}
    for c in cases:
        task = c.get("task", "")
        diff = "medium" if any(k in task for k in _MEDIUM_KW) else "easy"
        c["difficulty"] = diff
        n[diff] += 1
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cases, f, allow_unicode=True, sort_keys=False)
    print(f"答案级基础 {len(cases)} 题：easy {n['easy']} / medium {n['medium']}（+hard_cases 25 道 hard）")


if __name__ == "__main__":
    main()
