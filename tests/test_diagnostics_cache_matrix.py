import json
import re
from datetime import datetime, timezone

import pytest

from video_auto_editor.cache import CacheNamespace, CacheOutcome
from video_auto_editor.diagnostics import (
    DiagnosticCompletion,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    StageOutcome,
)
from video_auto_editor.diagnostics.collecting import (
    initialize as initialize_collecting_diagnostics,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    RunStage,
)
from video_auto_editor.runtime.identity import RunId


def _fixed_wall_clock():
    return datetime(
        2026,
        7,
        26,
        12,
        34,
        56,
        123000,
        tzinfo=timezone.utc,
    )


def _incrementing_monotonic_clock():
    value = 0.0

    def tick():
        nonlocal value
        value += 0.01
        return value

    return tick


@pytest.mark.parametrize(
    "digest_prefix",
    (
        "sha256:01234567",
        "sha256:0123456789abcdef",
    ),
)
def test_corrupt_cache_fact_accepts_only_bounded_digest_prefixes(
    digest_prefix,
):
    Facts.cache(
        CacheNamespace.TRANSCRIPT,
        CacheOutcome.CORRUPT_QUARANTINED,
        reason_code="cache.digest_mismatch",
        quarantine_digest_prefix=digest_prefix,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        (
            {},
            "稳定原因和有限摘要前缀",
        ),
        (
            {
                "reason_code": "包含不应持久化的正文",
                "quarantine_digest_prefix": "sha256:01234567",
            },
            "原因码格式不合法",
        ),
        (
            {
                "reason_code": "cache.digest_mismatch",
                "quarantine_digest_prefix": "sha256:0123456",
            },
            "摘要前缀格式不合法",
        ),
        (
            {
                "reason_code": "cache.digest_mismatch",
                "quarantine_digest_prefix": "sha256:0123456789abcdef0",
            },
            "摘要前缀格式不合法",
        ),
        (
            {
                "reason_code": "cache.digest_mismatch",
                "quarantine_digest_prefix": "sha256:" + "a" * 64,
            },
            "完整缓存身份摘要",
        ),
    ),
)
def test_corrupt_cache_fact_rejects_missing_or_sensitive_diagnostics(
    kwargs,
    message,
):
    with pytest.raises(ValueError, match=message):
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.CORRUPT_QUARANTINED,
            **kwargs,
        )


@pytest.mark.parametrize(
    "error_code",
    (None, ErrorCode.CONFIG_VALUE_INVALID),
)
def test_cache_infrastructure_failure_requires_its_stable_error_code(
    error_code,
):
    with pytest.raises(ValueError, match="必须关联稳定错误码"):
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.INFRASTRUCTURE_FAILED,
            error_code=error_code,
        )


def test_cache_diagnostics_cover_the_full_matrix_and_redact_sensitive_data():
    private_cache_path = (
        "/srv/private/cache/topic-review/customer-42.payload"
    )
    payload_canary = "CACHE_PAYLOAD_CANARY_客户课程正文_不可持久化"
    rejected_full_digest = "sha256:" + "a" * 64

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Facts.cache(
            CacheNamespace.TOPIC_REVIEW,
            CacheOutcome.HIT,
            path=private_cache_path,
        )
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        Facts.cache(
            CacheNamespace.TOPIC_REVIEW,
            CacheOutcome.HIT,
            payload=payload_canary,
        )
    with pytest.raises(ValueError, match="完整缓存身份摘要"):
        Facts.cache(
            CacheNamespace.TOPIC_REVIEW,
            CacheOutcome.CORRUPT_QUARANTINED,
            reason_code="cache.digest_mismatch",
            quarantine_digest_prefix=rejected_full_digest,
        )

    diagnostics = initialize_collecting_diagnostics(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_incrementing_monotonic_clock(),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    cache_scope = stage.scope(ErrorModule.TRANSCRIPTION)
    application_scope = stage.scope(ErrorModule.APPLICATION)
    digest_prefixes = {}
    expected_wait_totals = {}

    for namespace_index, namespace in enumerate(CacheNamespace):
        digest_length = 8 if namespace_index % 2 == 0 else 16
        digest_prefix = "sha256:" + (
            "0123456789abcdef"[:digest_length]
        )
        digest_prefixes[namespace] = digest_prefix
        expected_wait_totals[namespace] = (
            10 + namespace_index + 20 + namespace_index
        )

        for outcome in CacheOutcome:
            if outcome is CacheOutcome.CORRUPT_QUARANTINED:
                operation_kind = OperationKind.CACHE_QUARANTINE
                kwargs = {
                    "reason_code": "cache.digest_mismatch",
                    "quarantine_digest_prefix": digest_prefix,
                }
            elif outcome in {
                CacheOutcome.WRITE_PUBLISHED,
                CacheOutcome.WRITE_ALREADY_PRESENT,
            }:
                operation_kind = OperationKind.CACHE_WRITE
                kwargs = {}
            elif outcome is CacheOutcome.INFRASTRUCTURE_FAILED:
                operation_kind = OperationKind.CACHE_READ
                kwargs = {
                    "error_code": (
                        ErrorCode.CACHE_INFRASTRUCTURE_FAILED
                    )
                }
            else:
                operation_kind = OperationKind.CACHE_READ
                kwargs = {
                    "singleflight_wait_ms": (
                        10 + namespace_index
                        if outcome is CacheOutcome.HIT
                        else 20 + namespace_index
                    )
                }

            operation = cache_scope.start_operation(
                operation_kind,
                item_index=1,
                item_count=1,
            )
            operation.record(Facts.cache(namespace, outcome, **kwargs))
            operation.complete(
                (
                    OperationOutcome.FAILED
                    if outcome is CacheOutcome.INFRASTRUCTURE_FAILED
                    else OperationOutcome.SUCCEEDED
                ),
                attempt_count=1,
            )

    application_scope.record(
        Facts.interruption(InterruptionSignal.SIGTERM)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=24)
    finalization = diagnostics.finish(
        DiagnosticCompletion.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=0,
        )
    )
    snapshot = diagnostics.snapshot()
    assert snapshot.manifest is not None
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    manifest = json.loads(snapshot.manifest)
    cache_events = [
        event
        for event in events
        if event["event_code"] == "cache.observed"
    ]

    assert finalization.manifest_persisted is True
    assert len(cache_events) == len(CacheNamespace) * len(CacheOutcome)
    assert {
        (
            event["attributes"]["namespace"],
            event["attributes"]["outcome"],
        )
        for event in cache_events
    } == {
        (namespace.value, outcome.value)
        for namespace in CacheNamespace
        for outcome in CacheOutcome
    }

    for namespace in CacheNamespace:
        namespace_events = [
            event
            for event in cache_events
            if event["attributes"]["namespace"] == namespace.value
        ]
        corrupt_event = next(
            event
            for event in namespace_events
            if event["attributes"]["outcome"]
            == CacheOutcome.CORRUPT_QUARANTINED.value
        )
        infrastructure_event = next(
            event
            for event in namespace_events
            if event["attributes"]["outcome"]
            == CacheOutcome.INFRASTRUCTURE_FAILED.value
        )

        assert corrupt_event["level"] == "warning"
        assert corrupt_event["attributes"]["reason_code"] == (
            "cache.digest_mismatch"
        )
        assert corrupt_event["attributes"][
            "quarantine_digest_prefix"
        ] == digest_prefixes[namespace]
        assert re.fullmatch(
            r"sha256:[0-9a-f]{8,16}",
            corrupt_event["attributes"]["quarantine_digest_prefix"],
        )
        assert infrastructure_event["level"] == "error"
        assert infrastructure_event["attributes"]["error_code"] == (
            ErrorCode.CACHE_INFRASTRUCTURE_FAILED.value
        )
        assert all(
            event["level"] == "info"
            for event in namespace_events
            if (
                event is not corrupt_event
                and event is not infrastructure_event
            )
        )

        assert manifest["cache"]["namespaces"][namespace.value] == {
            "queries": 3,
            "hits": 1,
            "misses": 1,
            "corrupt_quarantined": 1,
            "writes_published": 1,
            "writes_already_present": 1,
            "infrastructure_failures": 1,
            "singleflight_wait_count": 2,
            "singleflight_wait_ms_total": (
                expected_wait_totals[namespace]
            ),
        }

    persisted = snapshot.events + snapshot.manifest
    assert private_cache_path.encode("utf-8") not in persisted
    assert payload_canary.encode("utf-8") not in persisted
    assert rejected_full_digest.encode("utf-8") not in persisted
