"""处理缓存拥有的不可变通用值。"""

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REASON_CODE = re.compile(r"cache\.[a-z][a-z0-9_]{0,63}")
_PayloadT = TypeVar("_PayloadT")


class CacheNamespace(str, Enum):
    """处理缓存的四个独立命名空间。"""

    TRANSCRIPT = "transcript"
    TRANSCRIPTION_SHARD = "transcription_shard"
    TOPIC_REVIEW = "topic_review"
    SUBTITLE_OPTIMIZATION = "subtitle_optimization"


class CacheOutcome(str, Enum):
    """一次处理缓存操作的封闭结果。"""

    HIT = "hit"
    MISS = "miss"
    CORRUPT_QUARANTINED = "corrupt_quarantined"
    WRITE_PUBLISHED = "write_published"
    WRITE_ALREADY_PRESENT = "write_already_present"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"


class CachedPayloadInvalid(ValueError):
    """业务 codec 明确拒绝缓存 payload。"""

    def __init__(self, reason_code: str = "cache.payload_invalid") -> None:
        if (
            not isinstance(reason_code, str)
            or _REASON_CODE.fullmatch(reason_code) is None
        ):
            raise ValueError("缓存 payload 原因码格式不合法")
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True, init=False)
class CacheIdentity:
    """业务模块提供结果影响事实后形成的版本化内容身份。"""

    namespace: CacheNamespace
    identity_schema_version: str
    algorithm_version: str
    payload_schema_version: str
    adapter_id: str
    model_id: str
    configuration_fingerprint: str
    digest: str
    input_digest: str

    def __new__(cls, *_args: object, **_kwargs: object) -> "CacheIdentity":
        raise TypeError("CacheIdentity 必须由 CacheIdentity.create() 创建")

    @classmethod
    def create(
        cls,
        *,
        namespace: CacheNamespace,
        identity_schema_version: str,
        algorithm_version: str,
        payload_schema_version: str,
        adapter_id: str,
        model_id: str,
        configuration_fingerprint: str,
        result_inputs: Mapping[str, object],
    ) -> "CacheIdentity":
        """规范化业务拥有的结果影响输入并计算 SHA-256 身份。"""
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("缓存身份必须使用 CacheNamespace")
        for value, field_name in (
            (identity_schema_version, "身份 schema 版本"),
            (algorithm_version, "算法版本"),
            (payload_schema_version, "payload schema 版本"),
        ):
            _require_version(value, field=field_name)
        for value, field_name in (
            (adapter_id, "Adapter 标识"),
            (model_id, "模型标识"),
        ):
            if (
                not isinstance(value, str)
                or _IDENTIFIER.fullmatch(value) is None
                or value.startswith("/")
                or "://" in value
            ):
                raise ValueError(f"{field_name}格式不合法")
        if (
            not isinstance(configuration_fingerprint, str)
            or _SHA256.fullmatch(configuration_fingerprint) is None
        ):
            raise ValueError("配置指纹必须是小写 SHA-256")
        if not isinstance(result_inputs, Mapping):
            raise TypeError("结果影响输入必须是 JSON 对象")

        inputs = _json_snapshot(result_inputs)
        input_bytes = _canonical_json(inputs)
        document = _canonical_json(
            {
                "adapter_id": adapter_id,
                "algorithm_version": algorithm_version,
                "configuration_fingerprint": configuration_fingerprint,
                "identity_schema_version": identity_schema_version,
                "inputs": inputs,
                "model_id": model_id,
                "namespace": namespace.value,
                "payload_schema_version": payload_schema_version,
            }
        )

        instance = object.__new__(cls)
        object.__setattr__(instance, "namespace", namespace)
        object.__setattr__(
            instance,
            "identity_schema_version",
            identity_schema_version,
        )
        object.__setattr__(instance, "algorithm_version", algorithm_version)
        object.__setattr__(
            instance,
            "payload_schema_version",
            payload_schema_version,
        )
        object.__setattr__(instance, "adapter_id", adapter_id)
        object.__setattr__(instance, "model_id", model_id)
        object.__setattr__(
            instance,
            "configuration_fingerprint",
            configuration_fingerprint,
        )
        object.__setattr__(
            instance,
            "digest",
            hashlib.sha256(document).hexdigest(),
        )
        object.__setattr__(
            instance,
            "input_digest",
            hashlib.sha256(input_bytes).hexdigest(),
        )
        return instance


@dataclass(frozen=True, slots=True)
class CacheEntrySpec(Generic[_PayloadT]):
    """业务模块拥有的 payload 编解码与校验规则。"""

    identity: CacheIdentity
    encode: Callable[[_PayloadT], object] = field(
        repr=False,
        compare=False,
    )
    decode: Callable[[object], _PayloadT] = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CacheIdentity):
            raise TypeError("缓存条目规格必须绑定 CacheIdentity")
        if not callable(self.encode) or not callable(self.decode):
            raise TypeError("缓存 payload 编解码器必须可调用")


@dataclass(frozen=True, slots=True)
class CacheObservation:
    """可交给运行诊断的单次缓存事实。"""

    namespace: CacheNamespace
    outcome: CacheOutcome
    singleflight_wait_ms: int | None = None
    reason_code: str | None = None
    quarantine_digest_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class CacheLookup(Generic[_PayloadT]):
    """一次查询的业务值与脱敏观察事实。"""

    value: _PayloadT | None
    observation: CacheObservation

    @property
    def hit(self) -> bool:
        return self.observation.outcome is CacheOutcome.HIT


@dataclass(frozen=True, slots=True)
class CachePublication:
    """一次不可变发布的观察结果。"""

    observation: CacheObservation


@dataclass(frozen=True, slots=True)
class CacheResolution(Generic[_PayloadT]):
    """查询或计算得到的完整、已校验业务结果。"""

    value: _PayloadT
    from_cache: bool
    observations: tuple[CacheObservation, ...]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_version(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field}格式不合法")
    return value


def _json_snapshot(value: object) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("缓存身份 JSON 不允许非有限数值")
        return value
    if isinstance(value, Mapping):
        snapshot: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("缓存身份 JSON 对象键必须是字符串")
            snapshot[key] = _json_snapshot(item)
        return snapshot
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        return [_json_snapshot(item) for item in value]
    raise TypeError("缓存身份只接受 JSON 值")
