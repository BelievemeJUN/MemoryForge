"""pytest 共享环境（P1-G-1）。

- 把 backend/ 加入 sys.path（测试统一用 `from sandbox...` / `from tasks...` 导入）
- 清理持久 terminal 可能残留的环境变量（防 load_dotenv 读到旧值导致串测）
"""
import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BACKEND)

# 清掉常见污染变量（各测试各自 load_dotenv / setenv）
for _k in (
    "user", "password", "host", "port", "db_name",
    "DATABASE_URL", "REDIS_URL", "EMBEDDING_API_KEY",
):
    os.environ.pop(_k, None)
