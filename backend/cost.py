"""成本记账与配额（P2-K）：per-user token 预算 + 熔断。

面试可讲：
  - M5 已有「评测级」token 记账（exec 子图内部精确累计）；这里补「运行时 per-user」记账。
  - Redis INCRBY + 次日 00:00 过期：成本按天累计，第二天自动清零（看板口径清晰）。
  - 两层成本口径：请求级（chat/intent/exec 最终回复的 usage）+ 评测级（exec 子图内部）
    ——诚实讲：请求级是近似，评测级才精确，生产应统一为「一次请求一个 usage 账单」。
"""
import os
import time

import redis.asyncio as aioredis

# 每用户每日 token 预算（超限熔断拒绝新请求）
DAILY_BUDGET = int(os.getenv("COST_DAILY_BUDGET_PER_USER", "100000"))


class CostTracker:
    """Redis 持久化的 per-user 成本记账器。"""

    def __init__(self, redis_url: str = ""):
        self.redis = aioredis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6380/0")
        )

    def _key(self, user_id: str) -> str:
        return f"cost:{time.strftime('%Y%m%d')}:{user_id}"

    async def get_usage(self, user_id: str) -> int:
        """当日已用 token 数。"""
        v = await self.redis.get(self._key(user_id))
        return int(v) if v else 0

    async def add_usage(self, user_id: str, tokens: int) -> int:
        """累加 token 并设置次日 00:00 过期。返回累计值。"""
        if tokens <= 0:
            return await self.get_usage(user_id)
        key = self._key(user_id)
        tomorrow = int(time.time() + 86400)
        pipe = self.redis.pipeline()
        pipe.incrby(key, tokens)
        pipe.expireat(key, tomorrow)
        vals = await pipe.execute()
        return int(vals[0])

    async def check_budget(self, user_id: str, est_tokens: int = 0) -> tuple[bool, int]:
        """请求前预算检查：已用 + 预估 <= 预算才放行。返回 (允许?, 预计累计值)。"""
        used = await self.get_usage(user_id)
        total = used + est_tokens
        return total <= DAILY_BUDGET, total
