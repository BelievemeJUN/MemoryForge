#!/bin/sh
# 容器内 Python 执行入口（M1）
# 由 DockerExecutor 以 /bin/sh 调用，无需可执行位。
# 职责：清空潜在敏感环境变量 + 关掉字节码缓存 + 执行目标脚本。
set -u

# 1) 清空敏感环境变量（双保险：即使宿主误透传了密钥，容器内也拿不到）
unset DASHSCOPE_API_KEY TOKEN SECRET PASSWORD API_KEY OPENAI_API_KEY \
      DASHSCOPE_TOKEN 2>/dev/null || true

# 2) 关闭字节码缓存（工作目录只读，避免 __pycache__ 写失败）
export PYTHONDONTWRITEBYTECODE=1
# 3) 无缓冲输出（保证 print 立即可见，不丢 stdout）
export PYTHONUNBUFFERED=1

# 4) 执行目标脚本
exec python "$@"
