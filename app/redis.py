from redis.asyncio import Redis
redis_client: Redis | None = None
from app.config import settings

def init_redis(url=settings.RADIS_URL):
    global redis_client
    if not redis_client:
        redis_client = Redis.from_url(url, encoding="utf-8", decode_responses=True)
    return redis_client

def get_redis() -> Redis:
    if not redis_client:
        raise RuntimeError("Redis not initialized")
    return redis_client
