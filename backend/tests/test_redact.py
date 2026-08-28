"""敏感信息脱敏测试（安全深化）。纯函数，无需服务。"""
from redact import redact_secrets


def test_openai_key_redacted():
    out = redact_secrets("token=sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in out


def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.abcdefghijklmnop"
    out = redact_secrets(f"token {jwt}")
    assert jwt not in out


def test_github_token_redacted():
    tok = "ghp_" + "A" * 30
    out = redact_secrets(f"gh token: {tok}")
    assert "ghp_" not in out


def test_keyvalue_keeps_field_name():
    out = redact_secrets("api_key=superSecretValue123")
    assert "api_key=<已脱敏>" in out
    assert "superSecretValue" not in out


def test_aws_key_redacted():
    ak = "AKIA" + "B" * 16
    out = redact_secrets(f"aws key {ak}")
    assert ak not in out


def test_plain_text_untouched():
    text = "hello world 1+1=2, print('正常输出')"
    assert redact_secrets(text) == text


def test_empty_text():
    assert redact_secrets("") == ""
