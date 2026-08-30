"""题库重组：把 main_cases(58) + gen_cases(42) 拆分为五级结构。
- answer_cases.yaml   答案级（无 pre_code 的普通题）
- selfheal_cases.yaml 循环级（带 pre_code 的自愈题）
- security_cases.yaml 安全级（保留原有）
检索级 retrieval_cases.yaml 由 seed_retrieval_kb.py + 检索题库单独提供。
成本级：从以上所有执行统计 token，不单独建题。
"""
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
CASES = os.path.join(HERE, "cases")


def main():
    main_cases = yaml.safe_load(open(os.path.join(CASES, "main_cases.yaml"), encoding="utf-8")) or []
    gen_cases = yaml.safe_load(open(os.path.join(CASES, "gen_cases.yaml"), encoding="utf-8")) or []

    answer, selfheal = [], []
    for c in main_cases + gen_cases:
        (selfheal if c.get("pre_code") else answer).append(c)

    with open(os.path.join(CASES, "answer_cases.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(answer, f, allow_unicode=True, sort_keys=False)
    with open(os.path.join(CASES, "selfheal_cases.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(selfheal, f, allow_unicode=True, sort_keys=False)

    print(f"答案级 {len(answer)} 题 → answer_cases.yaml")
    print(f"循环级 {len(selfheal)} 题 → selfheal_cases.yaml")


if __name__ == "__main__":
    main()
