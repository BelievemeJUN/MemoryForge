"""沙箱执行审计落库（P2/I）：谁在何时执行了什么代码 → PostgreSQL。

设计（面试可讲）：
  - 审计是合规与安全运营的基础（03 文档 I）：能回答「谁执行了什么、结果如何」。
  - 用同步 psycopg 一次性短连接：executor.run_python 跑在 to_thread（同步线程），
    异步客户端跨事件循环不可靠，同步短连接最稳。
  - 表不存在自动建（CREATE TABLE IF NOT EXISTS），无需迁移工具。
  - 失败只降级为 debug 日志，绝不影响代码执行主流程（审计是增强不是依赖）。
"""
import logging
import os

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_audit (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT,
    code_len INT,
    exit_code INT,
    timed_out BOOLEAN,
    security_blocked BOOLEAN,
    duration FLOAT,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
)
"""
_INSERT = """
INSERT INTO sandbox_audit
    (user_id, code_len, exit_code, timed_out, security_blocked, duration, error)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""


def write_audit(
    user_id: str = "",
    code_len: int = 0,
    exit_code: int | None = None,
    timed_out: bool = False,
    security_blocked: bool = False,
    duration: float = 0.0,
    error: str = "",
) -> bool:
    """写入一条沙箱执行审计（同步，失败降级不抛异常）。"""
    try:
        import psycopg

        dsn = os.getenv("DATABASE_URL", "")
        if not dsn:
            return False
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            conn.execute(_DDL)
            conn.execute(
                _INSERT,
                (
                    user_id,
                    code_len,
                    exit_code,
                    timed_out,
                    security_blocked,
                    round(duration, 3),
                    (error or "")[:500],
                ),
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("审计落库失败（不影响执行）: %s", e)
        return False
