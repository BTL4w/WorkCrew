"""One lazy, bounded Redis command pool per API/worker process."""

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config import Settings
from app.modules.optimization.adapters.redis_cache import DisabledReadCache, RedisReadCache
from app.modules.optimization.application.cache_ports import ReadCachePort


@dataclass
class RedisRuntime:
    client: Redis | None
    namespace: str
    read_cache: ReadCachePort

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def create_redis_runtime(settings: Settings) -> RedisRuntime:
    namespace = f"{settings.environment}:{settings.redis_namespace}"
    if not settings.read_cache_enabled or settings.redis_url is None:
        return RedisRuntime(None, namespace, DisabledReadCache())
    url = urlsplit(settings.redis_url.get_secret_value())
    client = Redis(
        host=url.hostname or "localhost",
        port=url.port or 6379,
        db=0,
        username=unquote(url.username) if url.username is not None else None,
        password=unquote(url.password) if url.password is not None else None,
        ssl=url.scheme == "rediss",
        decode_responses=False,
        socket_timeout=0.025,
        socket_connect_timeout=0.025,
        max_connections=20,
        retry=Retry(NoBackoff(), 0),
        protocol=2,
    )
    return RedisRuntime(client, namespace, RedisReadCache(client, namespace))
