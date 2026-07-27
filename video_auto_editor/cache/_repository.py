"""处理缓存仓库门面与内存 Adapter。"""

from collections.abc import Callable
from datetime import datetime, timezone
from threading import Lock, RLock
from time import monotonic
from typing import Any, Protocol, TypeVar, cast

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.workspace import ManagedDirectoryCapability

from ._envelope import (
    _CorruptEnvelope,
    _decode_envelope,
    _encode_envelope,
    _prepare_payload,
    _PreparedPayload,
)
from ._filesystem import _FileSystemStore
from ._model import (
    CacheEntrySpec,
    CacheIdentity,
    CacheLookup,
    CacheObservation,
    CacheOutcome,
    CachePublication,
    CacheResolution,
    _require_version,
)

_PayloadT = TypeVar("_PayloadT")
_ResultT = TypeVar("_ResultT")
_Clock = Callable[[], datetime]


class _Store(Protocol):
    def read(self, identity: CacheIdentity) -> bytes | None:
        ...

    def publish(self, identity: CacheIdentity, envelope: bytes) -> bool:
        ...

    def quarantine(
        self,
        identity: CacheIdentity,
        reason_code: str,
    ) -> None:
        ...

    def with_claim(
        self,
        identity: CacheIdentity,
        cancellation: CancellationToken,
        effect: Callable[[int], _ResultT],
    ) -> _ResultT:
        ...


class _MemoryStore:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], bytes] = {}
        self._claims: dict[tuple[str, str], Lock] = {}
        self._quarantine: list[tuple[CacheIdentity, str, bytes]] = []
        self._guard = RLock()

    def read(self, identity: CacheIdentity) -> bytes | None:
        with self._guard:
            return self._entries.get(_key(identity))

    def publish(self, identity: CacheIdentity, envelope: bytes) -> bool:
        with self._guard:
            key = _key(identity)
            if key in self._entries:
                return False
            self._entries[key] = envelope
            return True

    def quarantine(
        self,
        identity: CacheIdentity,
        reason_code: str,
    ) -> None:
        with self._guard:
            raw = self._entries.pop(_key(identity))
            self._quarantine.append((identity, reason_code, raw))

    def with_claim(
        self,
        identity: CacheIdentity,
        cancellation: CancellationToken,
        effect: Callable[[int], _ResultT],
    ) -> _ResultT:
        with self._guard:
            claim = self._claims.setdefault(_key(identity), Lock())
        started = monotonic()
        while True:
            cancellation.raise_if_cancelled()
            if claim.acquire(timeout=0.01):
                break
        waited = max(0, round((monotonic() - started) * 1000))
        try:
            cancellation.raise_if_cancelled()
            return effect(waited)
        finally:
            claim.release()


class CacheClaim:
    """只在仓库独占 claim 回调期间有效的查询与发布能力。"""

    _active: bool
    _identity: CacheIdentity
    _publish_allowed: bool
    _queried: bool
    _repository: "CacheRepository"
    _wait_ms: int

    __slots__ = (
        "_active",
        "_identity",
        "_publish_allowed",
        "_queried",
        "_repository",
        "_wait_ms",
    )

    def __new__(
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "CacheClaim":
        raise TypeError("CacheClaim 只能由 CacheRepository 签发")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("CacheClaim 不能由缓存仓库之外的模块实现")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("CacheClaim 不可修改")

    def lookup(
        self,
        spec: CacheEntrySpec[_PayloadT],
    ) -> CacheLookup[_PayloadT]:
        self._assert_spec(spec)
        result = self._repository._lookup_locked(spec, self._wait_ms)
        object.__setattr__(self, "_queried", True)
        object.__setattr__(self, "_publish_allowed", not result.hit)
        return result

    def publish(
        self,
        spec: CacheEntrySpec[_PayloadT],
        value: _PayloadT,
    ) -> CachePublication:
        self._assert_spec(spec)
        prepared = _prepare_payload(spec, value)
        return self._publish_prepared(
            cast(CacheEntrySpec[object], spec),
            cast(_PreparedPayload[object], prepared),
        )

    def _publish_prepared(
        self,
        spec: CacheEntrySpec[object],
        prepared: _PreparedPayload[object],
    ) -> CachePublication:
        self._assert_spec(spec)
        if not self._queried:
            raise RuntimeError("取得缓存 claim 后必须先重新查询")
        if not self._publish_allowed:
            return CachePublication(
                CacheObservation(
                    namespace=spec.identity.namespace,
                    outcome=CacheOutcome.WRITE_ALREADY_PRESENT,
                    singleflight_wait_ms=self._wait_ms,
                )
            )
        publication = self._repository._publish_locked(
            spec.identity,
            prepared,
            self._wait_ms,
        )
        object.__setattr__(self, "_publish_allowed", False)
        return publication

    def _assert_spec(self, spec: CacheEntrySpec[Any]) -> None:
        if not self._active:
            raise RuntimeError("缓存 claim 作用域已经结束")
        if not isinstance(spec, CacheEntrySpec):
            raise TypeError("缓存 claim 必须使用 CacheEntrySpec")
        if spec.identity != self._identity:
            raise ValueError("缓存 claim 只能操作同一内容身份")

    def _close(self) -> None:
        object.__setattr__(self, "_active", False)


class CacheRepository:
    """隐藏存储差异并强制 claim 后重查的共享缓存仓库。"""

    _application_version: str
    _clock: _Clock
    _store: _Store

    __slots__ = ("_application_version", "_clock", "_store")

    def __new__(
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "CacheRepository":
        raise TypeError("CacheRepository 只能由工厂方法创建")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("CacheRepository 不能由缓存模块之外实现")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("CacheRepository 不可修改")

    @classmethod
    def in_memory(
        cls,
        *,
        application_version: str,
        clock: _Clock | None = None,
    ) -> "CacheRepository":
        version = _require_version(
            application_version,
            field="生产程序版本",
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "_store", _MemoryStore())
        object.__setattr__(instance, "_application_version", version)
        object.__setattr__(
            instance,
            "_clock",
            clock or (lambda: datetime.now(timezone.utc)),
        )
        return instance

    @classmethod
    def initialize(
        cls,
        cache_directory: ManagedDirectoryCapability,
        *,
        application_version: str,
        clock: _Clock | None = None,
    ) -> "CacheRepository":
        version = _require_version(
            application_version,
            field="生产程序版本",
        )
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_store",
            _FileSystemStore(cache_directory),
        )
        object.__setattr__(instance, "_application_version", version)
        object.__setattr__(
            instance,
            "_clock",
            clock or (lambda: datetime.now(timezone.utc)),
        )
        return instance

    def lookup(
        self,
        spec: CacheEntrySpec[_PayloadT],
        *,
        cancellation: CancellationToken,
    ) -> CacheLookup[_PayloadT]:
        _validate_request(spec, cancellation)
        try:
            return self._lookup_unlocked(spec)
        except _CorruptEnvelope:
            pass
        return self.claim(
            spec.identity,
            cancellation=cancellation,
            effect=lambda claim: claim.lookup(spec),
        )

    def claim(
        self,
        identity: CacheIdentity,
        *,
        cancellation: CancellationToken,
        effect: Callable[[CacheClaim], _ResultT],
    ) -> _ResultT:
        if not isinstance(identity, CacheIdentity):
            raise TypeError("缓存 claim 必须绑定 CacheIdentity")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("缓存 claim 必须绑定 CancellationToken")
        if not callable(effect):
            raise TypeError("缓存 claim 效果必须可调用")

        def execute(wait_ms: int) -> _ResultT:
            claim = object.__new__(CacheClaim)
            object.__setattr__(claim, "_repository", self)
            object.__setattr__(claim, "_identity", identity)
            object.__setattr__(claim, "_wait_ms", wait_ms)
            object.__setattr__(claim, "_active", True)
            object.__setattr__(claim, "_queried", False)
            object.__setattr__(claim, "_publish_allowed", False)
            try:
                return effect(claim)
            finally:
                claim._close()

        return self._store.with_claim(identity, cancellation, execute)

    def resolve(
        self,
        spec: CacheEntrySpec[_PayloadT],
        *,
        cancellation: CancellationToken,
        compute: Callable[[], _PayloadT],
    ) -> CacheResolution[_PayloadT]:
        _validate_request(spec, cancellation)
        if not callable(compute):
            raise TypeError("缓存计算必须可调用")
        first = self.lookup(spec, cancellation=cancellation)
        if first.hit:
            return CacheResolution(
                value=cast(_PayloadT, first.value),
                from_cache=True,
                observations=(first.observation,),
            )

        def resolve_under_claim(
            claim: CacheClaim,
        ) -> CacheResolution[_PayloadT]:
            second = claim.lookup(spec)
            observations = [first.observation, second.observation]
            if second.hit:
                return CacheResolution(
                    value=cast(_PayloadT, second.value),
                    from_cache=True,
                    observations=tuple(observations),
                )
            cancellation.raise_if_cancelled()
            computed = compute()
            cancellation.raise_if_cancelled()
            prepared = _prepare_payload(spec, computed)
            cancellation.raise_if_cancelled()
            publication = claim._publish_prepared(
                cast(CacheEntrySpec[object], spec),
                cast(_PreparedPayload[object], prepared),
            )
            observations.append(publication.observation)
            cancellation.raise_if_cancelled()
            return CacheResolution(
                value=prepared.value,
                from_cache=False,
                observations=tuple(observations),
            )

        return self.claim(
            spec.identity,
            cancellation=cancellation,
            effect=resolve_under_claim,
        )

    def _lookup_unlocked(
        self,
        spec: CacheEntrySpec[_PayloadT],
    ) -> CacheLookup[_PayloadT]:
        raw = self._store.read(spec.identity)
        if raw is None:
            return _lookup_result(spec.identity, CacheOutcome.MISS)
        value = _decode_envelope(raw, spec)
        return _lookup_result(
            spec.identity,
            CacheOutcome.HIT,
            value=value,
        )

    def _lookup_locked(
        self,
        spec: CacheEntrySpec[_PayloadT],
        wait_ms: int,
    ) -> CacheLookup[_PayloadT]:
        raw = self._store.read(spec.identity)
        if raw is None:
            return _lookup_result(
                spec.identity,
                CacheOutcome.MISS,
                wait_ms=wait_ms,
            )
        try:
            value = _decode_envelope(raw, spec)
        except _CorruptEnvelope as corrupt:
            reason_code = corrupt.reason_code
        else:
            return _lookup_result(
                spec.identity,
                CacheOutcome.HIT,
                value=value,
                wait_ms=wait_ms,
            )
        self._store.quarantine(
            spec.identity,
            reason_code,
        )
        return _lookup_result(
            spec.identity,
            CacheOutcome.CORRUPT_QUARANTINED,
            wait_ms=wait_ms,
            reason_code=reason_code,
        )

    def _publish_locked(
        self,
        identity: CacheIdentity,
        payload: _PreparedPayload[object],
        wait_ms: int,
    ) -> CachePublication:
        envelope = _encode_envelope(
            identity,
            payload,
            application_version=self._application_version,
            created_at=self._clock(),
        )
        published = self._store.publish(identity, envelope)
        return CachePublication(
            CacheObservation(
                namespace=identity.namespace,
                outcome=(
                    CacheOutcome.WRITE_PUBLISHED
                    if published
                    else CacheOutcome.WRITE_ALREADY_PRESENT
                ),
                singleflight_wait_ms=wait_ms,
            )
        )


def _lookup_result(
    identity: CacheIdentity,
    outcome: CacheOutcome,
    *,
    value: _PayloadT | None = None,
    wait_ms: int | None = None,
    reason_code: str | None = None,
) -> CacheLookup[_PayloadT]:
    return CacheLookup(
        value=value,
        observation=CacheObservation(
            namespace=identity.namespace,
            outcome=outcome,
            singleflight_wait_ms=wait_ms,
            reason_code=reason_code,
            quarantine_digest_prefix=(
                f"sha256:{identity.digest[:16]}"
                if outcome is CacheOutcome.CORRUPT_QUARANTINED
                else None
            ),
        ),
    )


def _validate_request(
    spec: CacheEntrySpec[Any],
    cancellation: CancellationToken,
) -> None:
    if not isinstance(spec, CacheEntrySpec):
        raise TypeError("缓存仓库必须使用 CacheEntrySpec")
    if not isinstance(cancellation, CancellationToken):
        raise TypeError("缓存仓库必须绑定 CancellationToken")
    cancellation.raise_if_cancelled()


def _key(identity: CacheIdentity) -> tuple[str, str]:
    return identity.namespace.value, identity.digest
