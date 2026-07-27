"""统一缓存 envelope 的严格编解码。"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar

from ._model import (
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheIdentity,
    _canonical_json,
    _json_snapshot,
    _require_version,
)

ENVELOPE_SCHEMA_VERSION = "processing-cache-envelope.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
)
_PayloadT = TypeVar("_PayloadT")


class _CorruptEnvelope(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("处理缓存条目已损坏")


class _DuplicateField(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _PreparedPayload(Generic[_PayloadT]):
    tree: object
    canonical: bytes
    value: _PayloadT


def _prepare_payload(
    spec: CacheEntrySpec[_PayloadT],
    value: _PayloadT,
) -> _PreparedPayload[_PayloadT]:
    tree = _json_snapshot(spec.encode(value))
    canonical = _canonical_json(tree)
    decoded_tree = _strict_json(canonical)
    decoded = spec.decode(decoded_tree)
    return _PreparedPayload(tree=tree, canonical=canonical, value=decoded)


def _encode_envelope(
    identity: CacheIdentity,
    payload: _PreparedPayload[object],
    *,
    application_version: str,
    created_at: datetime,
) -> bytes:
    created = _timestamp(created_at)
    envelope = {
        "algorithm_version": identity.algorithm_version,
        "created_at": created,
        "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
        "identity": {
            "input_sha256": identity.input_digest,
            "schema_version": identity.identity_schema_version,
            "sha256": identity.digest,
        },
        "namespace": identity.namespace.value,
        "payload": {
            "byte_length": len(payload.canonical),
            "schema_version": identity.payload_schema_version,
            "sha256": hashlib.sha256(payload.canonical).hexdigest(),
            "value": payload.tree,
        },
        "producer": {
            "adapter_id": identity.adapter_id,
            "application_version": application_version,
            "configuration_fingerprint": (
                identity.configuration_fingerprint
            ),
            "model_id": identity.model_id,
        },
    }
    return _canonical_json(envelope) + b"\n"


def _decode_envelope(
    raw: bytes,
    spec: CacheEntrySpec[_PayloadT],
) -> _PayloadT:
    try:
        envelope = _strict_json(raw)
    except (
        RecursionError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise _CorruptEnvelope("cache.json_invalid") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "algorithm_version",
        "created_at",
        "envelope_schema_version",
        "identity",
        "namespace",
        "payload",
        "producer",
    }:
        raise _CorruptEnvelope("cache.envelope_schema_invalid")

    identity = spec.identity
    if (
        envelope["envelope_schema_version"] != ENVELOPE_SCHEMA_VERSION
        or envelope["namespace"] != identity.namespace.value
    ):
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    if envelope["algorithm_version"] != identity.algorithm_version:
        raise _CorruptEnvelope("cache.algorithm_mismatch")
    if not _valid_timestamp(envelope["created_at"]):
        raise _CorruptEnvelope("cache.envelope_schema_invalid")

    identity_record = envelope["identity"]
    if not isinstance(identity_record, dict) or set(identity_record) != {
        "input_sha256",
        "schema_version",
        "sha256",
    }:
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    if (
        identity_record["schema_version"]
        != identity.identity_schema_version
        or identity_record["sha256"] != identity.digest
        or identity_record["input_sha256"] != identity.input_digest
    ):
        raise _CorruptEnvelope("cache.identity_mismatch")

    producer = envelope["producer"]
    if not isinstance(producer, dict) or set(producer) != {
        "adapter_id",
        "application_version",
        "configuration_fingerprint",
        "model_id",
    }:
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    if (
        producer["adapter_id"] != identity.adapter_id
        or producer["model_id"] != identity.model_id
        or producer["configuration_fingerprint"]
        != identity.configuration_fingerprint
        or not _valid_version(producer["application_version"])
    ):
        raise _CorruptEnvelope("cache.identity_mismatch")

    payload = envelope["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "byte_length",
        "schema_version",
        "sha256",
        "value",
    }:
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    if payload["schema_version"] != identity.payload_schema_version:
        raise _CorruptEnvelope("cache.payload_schema_mismatch")
    byte_length = payload["byte_length"]
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length < 0
    ):
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    payload_sha256 = payload["sha256"]
    if (
        not isinstance(payload_sha256, str)
        or _SHA256.fullmatch(payload_sha256) is None
    ):
        raise _CorruptEnvelope("cache.envelope_schema_invalid")
    try:
        canonical = _canonical_json(_json_snapshot(payload["value"]))
    except (RecursionError, TypeError, ValueError) as exc:
        raise _CorruptEnvelope("cache.payload_invalid") from exc
    if len(canonical) != byte_length:
        raise _CorruptEnvelope("cache.payload_length_mismatch")
    if hashlib.sha256(canonical).hexdigest() != payload_sha256:
        raise _CorruptEnvelope("cache.payload_digest_mismatch")
    try:
        return spec.decode(_strict_json(canonical))
    except CachedPayloadInvalid as exc:
        raise _CorruptEnvelope(exc.reason_code) from exc
    except RecursionError as exc:
        raise _CorruptEnvelope("cache.payload_invalid") from exc


def _strict_json(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_non_finite,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField(key)
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"不允许非有限 JSON 数值：{value}")


def _timestamp(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("缓存创建时间必须带时区")
    utc = value.astimezone(timezone.utc)
    return utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_timestamp(value: object) -> bool:
    if (
        not isinstance(value, str)
        or _TIMESTAMP.fullmatch(value) is None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_version(value: object) -> bool:
    try:
        _require_version(value, field="生产程序版本")
    except ValueError:
        return False
    return True
