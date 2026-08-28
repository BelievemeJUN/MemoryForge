"""成本记账与配额（P2-K）：per-user token 预算 + 熔断 + 趋势。

面试可讲：
  - M5 已有「评测级」token 记账（exec 子图内部精确累计）；这里补「运行时 per-user」记账。
  - Redis INCRBY + 次日 00:00 过期：成本按天累计，第二天自动清零（看板口径清晰）。
  - 两层成本口径：请求级（chat/intent/exec 最终回复的 usage）+ 评测级（exec 子图内部）
    ——本轮把 verify 的 judge LLM 调用也纳入 exec 记账，请求级账单更精确。
  - 趋势：每日记账时把「当日累计」快照写进 hist key（31 天 TTL），
    面板据此画近 7 日曲线——成本从「当前值」升级为「可回顾的曲线」。
"""
import datetime
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
        total = int(vals[0])
        # 趋势快照：当日累计写进 hist key（保留 31 天，供面板画曲线）
        today = time.strftime("%Y%m%d")
        hist_key = f"cost:hist:{user_id}:{today}"
        hp = self.redis.pipeline()
        hp.set(hist_key, total)
        hp.expire(hist_key, 31 * 86400)
        await hp.execute()
        return total

    async def history(self, user_id: str, days: int = 7) -> dict[str, int]:
        """近 N 天每日成本（含今天）。返回 {YYYY-MM-DD: token数}。"""
        out: dict[str, int] = {}
        today = datetime.date.today()
        for i in range(days - 1, -1, -1):
            d = today - datetime.timedelta(days=i)
            v = await self.redis.get(f"cost:hist:{user_id}:{d.strftime('%Y%m%d')}")
            out[d.strftime("%Y-%m-%d")] = int(v) if v else 0
        return out

    async def check_budget(self, user_id: str, est_tokens: int = 0) -> tuple[bool, int]:
        """请求前预算检查：已用 + 预估 <= 预算才放行。返回 (允许?, 预计累计值)。"""
        used = await self.get_usage(user_id)
        total = used + est_tokens
        return total <= DAILY_BUDGET, total
