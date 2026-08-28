"""敏感信息脱敏（安全深化）：AI 生成的代码可能把密钥/令牌直接 print 出来。

面试可讲：六层防御挡的是「代码行为」，但密钥是「数据泄漏」——代码可以合法地
打印环境变量/读取配置文件，把真实密钥吐给用户。所以输出层还要一道正则脱敏：
命中常见密钥格式（OpenAI sk-、GitHub ghp_、AWS AKIA、JWT、key=xxx 等）就替换成占位符。
放在沙箱执行出口统一处理，对话/评测/worker 所有消费方都自动安全。

诚实边界：这是"友好拦截"——只能拦已知格式，不能承诺对抗性混淆；
真正的边界是密钥不落盘/不传进沙箱（环境隔离）。
"""
import re

# (正则, 替换) —— 替换可为字符串或可调用（接收 Match）
_PATTERNS: list[tuple[re.Pattern, object]] = [
    # OpenAI / 常见 LLM 密钥
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "<密钥已脱敏>"),
    # GitHub 个人令牌
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<GitHub令牌已脱敏>"),
    # AWS Access Key
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<AWS密钥已脱敏>"),
    # JWT（eyJ...三段）
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}"), "<JWT已脱敏>"),
    # 通用 key=value（api_key/token/secret/password...）——保留字段名便于定位
    (
        re.compile(r"(?i)(api[_-]?key|token|secret|passwd|password)\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{8,}"),
        lambda m: f"{m.group(1)}=<已脱敏>",
    ),
    # Authorization: Bearer xxx
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}"), "<Bearer令牌已脱敏>"),
]


def redact_secrets(text: str) -> str:
    """对文本做敏感信息脱敏；空文本原样返回。"""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
