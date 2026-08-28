"""模型路由（P2）：按任务复杂度选模型——简单走便宜，复杂走贵。

面试可讲：不是所有任务都用最强模型——简单计算用便宜快的，复杂实现用强模型，
成本与质量平衡（成本优化的常见手段）。
  - 启发式判断复杂度（任务长度 + 目标性关键词）；
  - env 可配 SIMPLE_MODEL / COMPLEX_MODEL（默认都 deepseek-chat，可切换 reasoner）；
  - plan / write / fix / judge 都用路由后的模型。
"""
import os

from chat.llm import build_chat_model

# 复杂度启发式关键词（含则视为复杂任务）
_COMPLEX_KEYWORDS = (
    "实现", "优化", "重构", "爬虫", "算法", "数据结构", "可视化",
    "并发", "多线程", "类", "设计", "框架", "接口", "数据库", "系统",
)


def estimate_complexity(task: str = "", plan: str = "") -> str:
    """启发式：任务长度 + 关键词 → simple / complex。"""
    text = (task or "") + " " + (plan or "")
    if len(task or "") > 60 or any(k in text for k in _COMPLEX_KEYWORDS):
        return "complex"
    return "simple"


def resolve_model(task: str = "", plan: str = "") -> str:
    """按复杂度返回模型名（env 可配 SIMPLE_MODEL / COMPLEX_MODEL）。"""
    if estimate_complexity(task, plan) == "complex":
        return os.getenv("COMPLEX_MODEL", "deepseek-chat")
    return os.getenv("SIMPLE_MODEL", "deepseek-chat")


def build_model(task: str = "", plan: str = ""):
    """按复杂度构造模型（plan / write / fix / judge 用）。"""
    return build_chat_model(model=resolve_model(task, plan))
