"""版本化、内容寻址的处理缓存。"""

from ._failure import CacheFailure
from ._model import (
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheIdentity,
    CacheLookup,
    CacheNamespace,
    CacheObservation,
    CacheOutcome,
    CachePublication,
    CacheResolution,
)
from ._repository import CacheClaim, CacheRepository

__all__ = [
    "CacheClaim",
    "CacheEntrySpec",
    "CacheFailure",
    "CacheIdentity",
    "CacheLookup",
    "CacheNamespace",
    "CacheObservation",
    "CacheOutcome",
    "CachePublication",
    "CacheRepository",
    "CacheResolution",
    "CachedPayloadInvalid",
]
