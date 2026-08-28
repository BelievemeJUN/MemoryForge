"""模型路由单测（P2）。纯逻辑，无需服务。"""
from chat.model_router import estimate_complexity, resolve_model


def test_simple_task():
    assert estimate_complexity("计算1加到100的和") == "simple"
    assert estimate_complexity("1+1等于几") == "simple"
    assert estimate_complexity("6乘以7是多少") == "simple"


def test_complex_task_keywords():
    assert estimate_complexity("实现一个爬虫抓取网页") == "complex"
    assert estimate_complexity("写一个类实现二叉树") == "complex"
    assert estimate_complexity("优化这段代码的性能") == "complex"


def test_long_task_is_complex():
    long = "请实现一个完整的命令行工具，支持子命令、参数解析、配置文件、日志输出，并且要有单元测试覆盖"
    assert estimate_complexity(long) == "complex"


def test_resolve_model_uses_env(monkeypatch):
    monkeypatch.setenv("SIMPLE_MODEL", "deepseek-chat")
    monkeypatch.setenv("COMPLEX_MODEL", "deepseek-reasoner")
    assert resolve_model("1+1") == "deepseek-chat"
    assert resolve_model("实现一个爬虫") == "deepseek-reasoner"
