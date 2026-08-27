"""沙箱配额解析单元测试（P0-A-2 / P1-G-1）。纯逻辑，无需服务。"""
from sandbox.limits import resolve_limits


def test_default_limits():
    l = resolve_limits("")
    assert l.mem_limit == "256m"
    assert l.pids_limit == 64
    assert l.timeout == 30.0


def test_per_user_override(monkeypatch):
    monkeypatch.setenv("QUOTA_USER_42_MEM", "128m")
    monkeypatch.setenv("QUOTA_USER_42_PIDS", "32")
    monkeypatch.setenv("QUOTA_USER_42_TIMEOUT", "5")
    l = resolve_limits("42")
    assert l.mem_limit == "128m"
    assert l.pids_limit == 32
    assert l.timeout == 5.0


def test_other_user_untouched(monkeypatch):
    monkeypatch.setenv("QUOTA_USER_42_MEM", "128m")
    assert resolve_limits("7").mem_limit == "256m"


def test_unknown_field_ignored(monkeypatch):
    monkeypatch.setenv("QUOTA_USER_42_NOPE", "abc")
    l = resolve_limits("42")
    assert l.mem_limit == "256m"  # 未知字段不影响
