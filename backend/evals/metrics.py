"""评测四层指标 + bootstrap 置信区间（M5-1，方法论核心）。

面试可讲（大白话）：
  - 四层指标分别回答：能不能做对（用例）、自愈有没有用（循环）、安不安全（安全）、
    贵不贵（成本）。
  - bootstrap 置信区间：评测样本少（比如 10 题）时，单看通过率 70% 不可靠，
    用「重复抽样」估计 95% 置信区间（如 40%~90%），诚实呈现不确定性——
    这是 deepresearch 已验证的方法论平移，形成「方法论连续性」故事。
"""
import random
import statistics
from dataclasses import dataclass, field


# ---------- bootstrap 置信区间 ----------

def bootstrap_ci(samples: list[float], n_boot: int = 1000, alpha: float = 0.05, seed: int = 42) -> tuple[float, float]:
    """对样本均值做 bootstrap 置信区间（默认 95% CI）。

    samples: 每样本一个 0~1 值（如 1=通过/0=失败）。
    返回 (下界, 上界)。
    """
    if not samples:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(samples)
    means = [statistics.mean(samples[rng.randrange(n)] for _ in range(n)) for _ in range(n_boot)]
    means.sort()
    lo = means[int(n_boot * alpha / 2)]
    hi = means[int(n_boot * (1 - alpha / 2))]
    return (lo, hi)


def _ratio(results: list[dict], key: str) -> dict:
    """计算某布尔指标的比例 + CI + 样本数。"""
    vals = [1.0 if r.get(key) else 0.0 for r in results]
    n = len(vals)
    mean = statistics.mean(vals) if vals else 0.0
    lo, hi = bootstrap_ci(vals)
    return {"value": round(mean, 4), "ci": (round(lo, 4), round(hi, 4)), "n": n}


@dataclass
class EvalReport:
    """评测报告：五层指标聚合（答案/检索/循环/安全/成本）。"""

    answer: dict = field(default_factory=dict)     # 答案级（代码题整体正确率）
    retrieval: dict = field(default_factory=dict)  # 检索级（RAG 知识库召回）
    loop: dict = field(default_factory=dict)       # 循环级（自愈）
    security: dict = field(default_factory=dict)   # 安全级
    cost: dict = field(default_factory=dict)       # 成本级

    def to_markdown(self) -> str:
        def row(name, d):
            ci = f" [{d['ci'][0]}~{d['ci'][1]}]" if d.get("ci") else ""
            return f"| {name} | {d.get('value', '-')}{ci} | {d.get('n', '-')} |\n"

        lines = ["# CodeMind 评测报告", ""]
        lines.append("## 答案级（能不能做对）")
        lines.append("| 指标 | 值 [95%CI] | 样本 |")
        lines.append("|---|---|---|")
        lines += [row(k, v) for k, v in self.answer.items()]
        lines.append("\n## 检索级（RAG 知识库召回）")
        lines.append("| 指标 | 值 [95%CI] | 样本 |")
        lines.append("|---|---|---|")
        lines += [row(k, v) for k, v in self.retrieval.items()]
        lines.append("\n## 循环级（自愈有没有用）")
        lines.append("| 指标 | 值 [95%CI] | 样本 |")
        lines.append("|---|---|---|")
        lines += [row(k, v) for k, v in self.loop.items()]
        lines.append("\n## 安全级（安不安全）")
        lines.append("| 指标 | 值 [95%CI] | 样本 |")
        lines.append("|---|---|---|")
        lines += [row(k, v) for k, v in self.security.items()]
        lines.append("\n## 成本级（贵不贵）")
        lines.append("| 指标 | 值 | 样本 |")
        lines.append("|---|---|---|")
        lines += [row(k, v) for k, v in self.cost.items()]
        return "\n".join(lines)


# ---------- 五层指标计算 ----------


def _code_results(results: list[dict]) -> list[dict]:
    """只保留代码题（答案/循环级与成本统计用）；过滤检索题与安全题。"""
    return [r for r in results if not r.get("is_retrieval") and not r.get("is_security")]


def compute_case_metrics(results: list[dict]) -> dict:
    """答案级：代码题通过率 / 运行正确率 / 超时率。"""
    code = _code_results(results)
    return {
        "通过率": _ratio(code, "passed"),
        "运行正确率": _ratio(code, "ran_ok"),   # 代码能跑（非编译/语法错误）
        "超时率": _ratio(code, "timed_out"),
    }


def compute_retrieval_metrics(results: list[dict]) -> dict:
    """检索级：RAG 知识库召回——query 在 top_k 内是否召回含期望关键词的父块。"""
    retr = [r for r in results if r.get("is_retrieval")]
    return {
        "召回率 recall@top_k": _ratio(retr, "passed"),
        "平均召回父块数": {
            "value": round(statistics.mean([r.get("retrieved", 0) for r in retr]), 2) if retr else 0,
            "ci": (), "n": len(retr),
        },
    }


def compute_loop_metrics(results: list[dict]) -> dict:
    """循环级：自愈成功率 / 平均重试轮数 / 一次通过率。"""
    code = _code_results(results)
    passed = [r for r in code if r.get("passed")]
    attempts = [r.get("attempts", 1) for r in code]
    one_shot = [r for r in code if r.get("passed") and r.get("attempts", 1) == 1]
    return {
        "自愈成功率": _ratio(code, "self_healed"),      # 首轮失败但最终通过
        "一次通过率": _ratio(code, "passed_one_shot"),
        "平均尝试轮数": {
            "value": round(statistics.mean(attempts), 2) if attempts else 0,
            "ci": (), "n": len(attempts),
        },
    }


def compute_security_metrics(results: list[dict]) -> dict:
    """安全级：静态拦截（危险命令/库） + 资源滥用拦截（配额兜底）。"""
    static = [r for r in results if r.get("is_static")]
    res = [r for r in results if r.get("is_resource")]
    return {
        "危险命令拦截率": _ratio(static, "blocked"),
        "逃逸拦截率": _ratio(static, "blocked"),
        "资源滥用拦截率": _ratio(res, "resource_killed"),
    }


def compute_cost_metrics(results: list[dict]) -> dict:
    """成本级：总 token / 总执行次数 / 单题平均成本（token，只统计代码题）。"""
    code = _code_results(results)
    total_tokens = sum(r.get("tokens", 0) for r in code)
    total_execs = sum(r.get("executions", 0) for r in code)
    n = len(code)
    return {
        "总 token": {"value": total_tokens, "ci": (), "n": n},
        "总执行次数": {"value": total_execs, "ci": (), "n": n},
        "单题平均 token": {"value": round(total_tokens / n, 1) if n else 0, "ci": (), "n": n},
    }
