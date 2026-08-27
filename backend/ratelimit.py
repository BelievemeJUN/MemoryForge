"""请求级限流（P2-H）：Redis 滑动窗口。

面试可讲：
  - 为什么用滑动窗口不是固定窗口：固定窗口在边界会有"两倍突刺"（前窗口尾 + 后窗口头
    各打满），滑动窗口按真实时间窗口计数，更平滑。
  - Redis ZSET 实现：成员 = 请求时间戳，score = 时间戳；每请求先清掉窗口外成员再计数，
    均摊 O(1)。
  - 双层：per-IP（防未认证洪水）+ per-user（认证后按用户，防单用户烧钱）。
"""
import os
import time

import redis.asyncio as aioredis

WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))       # 窗口（秒）
IP_MAX = int(os.getenv("RATE_LIMIT_IP_MAX", "120"))      # 每 IP 每分钟
USER_MAX = int(os.getenv("RATE_LIMIT_USER_MAX", "30"))   # 每用户每分钟


class RateLimiter:
    """基于 Redis 的滑动窗口限流器（每请求新建，客户端线程安全）。"""

    def __init__(self, redis_url: str = ""):
        self.redis = aioredis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6380/0")
        )

    async def _hit(self, key: str, limit: int) -> tuple[bool, int]:
        """滑动窗口核心：清窗口外 → 计数 → 写入。返回 (是否放行, 建议等待秒数)。"""
        now = time.time()
        start = now - WINDOW
        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, 0, start)
        pipe.zcard(key)
        pipe.zadd(key, {str(now): now})
        pipe.expire(key, WINDOW + 5)
        _, count, _, _ = await pipe.execute()
        if count <= limit:
            return True, 0
        # 超限：估算窗口内最早请求还需多久"滑出窗口"
        oldest = await self.redis.zrange(key, 0, 0, withscores=True)
        wait = int(WINDOW - (now - oldest[0][1])) if oldest else WINDOW
        return False, max(1, wait)

    async def check_ip(self, ip: str) -> tuple[bool, int]:
        """按 IP 限流（中间件用，未认证也能拦洪水）。"""
        return await self._hit(f"rl:ip:{ip}", IP_MAX)

    async def check_user(self, user_id: str) -> tuple[bool, int]:
        """按用户限流（认证后，端点内检查）。"""
        return await self._hit(f"rl:user:{user_id}", USER_MAX)
