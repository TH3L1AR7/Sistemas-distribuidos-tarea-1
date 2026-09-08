import os
import redis.asyncio as redis

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

_redis: redis.Redis | None = None


async def init():
    global _redis
    _redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


async def close():
    if _redis:
        await _redis.aclose()


async def get(key: str) -> str | None:
    return await _redis.get(key)


async def set(key: str, value: str, ttl: int):
    await _redis.set(key, value, ex=ttl)