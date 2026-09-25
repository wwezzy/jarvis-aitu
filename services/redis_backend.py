"""One Redis command surface for native Redis and the legacy Upstash REST API.

No memory emulation: absence or failure is reported by the feature using Redis.
"""
import os


class NativeAsyncRedis:
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def eval(self, script, *, keys, args):
        return await self.client.eval(script, len(keys), *keys, *args)


def backend_name():
    if os.getenv("REDIS_URL", "").strip():
        return "redis"
    if os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN"):
        return "upstash_rest"
    return "absent"


def create_redis(*, sync=False):
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        if not url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        if sync:
            from redis import Redis
        else:
            from redis.asyncio import Redis
        client = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5,
                                socket_timeout=5, health_check_interval=30)
        return client if sync else NativeAsyncRedis(client)
    url, token = os.getenv("UPSTASH_REDIS_REST_URL"), os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        if sync:
            from upstash_redis import Redis
        else:
            from upstash_redis.asyncio import Redis
        return Redis(url=url, token=token)
    return None


async def health(client):
    if client is None:
        return {"backend": "absent", "healthy": False, "optional": True}
    try:
        await client.ping()
        return {"backend": backend_name(), "healthy": True}
    except Exception as exc:
        return {"backend": backend_name(), "healthy": False, "error": type(exc).__name__}
