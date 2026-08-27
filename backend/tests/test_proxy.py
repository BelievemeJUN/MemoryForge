"""白名单代理逻辑单测（P2 可选联网）。纯逻辑，无需服务。"""
from sandbox.proxy import is_allowed


def test_allowed_domains():
    assert is_allowed("api.deepseek.com")
    assert is_allowed("api.deepseek.com:443")
    assert is_allowed("github.com")


def test_rejected_domains():
    assert not is_allowed("evil.com")
    assert not is_allowed("example.com")
    assert not is_allowed("")


def test_subdomain_requires_explicit():
    # 子域需显式在白名单；files.pythonhosted.org 未配置 → 拒绝（默认安全）
    assert not is_allowed("files.pythonhosted.org")
