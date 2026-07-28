import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from video_auto_editor.configuration import Configuration
from video_auto_editor.diagnostics import (
    CacheNamespace,
    CacheOutcome,
    CertifiedPlatform,
    DiagnosticPackageReadReason,
    DiagnosticPackageReader,
    DiagnosticPackageReadState,
    DiagnosticPackageSnapshot,
    ExternalDataCategory,
    ExternalRequestOutcome,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    PreflightOutcome,
    ProviderCapability,
    ProviderTransport,
    RecoveredNoticeKind,
    RetryKind,
    RunDiagnostics,
    RunOutcome,
    StageOutcome,
)
from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ErrorCode,
    ErrorModule,
    RemoteRequestId,
    RunStage,
)
from video_auto_editor.runtime.identity import OperationId, RunId


_RUN_ID = "run_11111111-1111-4111-8111-111111111111"
_OTHER_RUN_ID = "run_22222222-2222-4222-8222-222222222222"


def _in_memory_diagnostics():
    return RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=lambda: 0.0,
    )


def _event(
    event_code,
    sequence,
    *,
    run_id=_RUN_ID,
    extra_fields=None,
    attributes=None,
):
    event = {
        "schema_version": "run_event.v1",
        "timestamp": (
            "2026-07-26T12:34:57.373Z"
            if event_code == "run.completed"
            else "2026-07-26T12:34:56.123Z"
        ),
        "sequence": sequence,
        "run_id": run_id,
        "level": "info",
        "event_code": event_code,
        "stage": (
            "initialized"
            if event_code in {"run.initialized", "run.completed"}
            else "preflight"
        ),
        "module": "application",
        "message": {
            "run.initialized": "直播拆条运行已初始化。",
            "run.completed": "直播拆条运行已结束。",
            "stage.started": "直播拆条运行阶段已开始。",
            "stage.completed": "直播拆条运行阶段已完成。",
        }.get(event_code, "安全消息"),
        "attributes": (
            {
                "application_version": "4.7.0",
                "release": {"status": "unknown"},
            }
            if event_code == "run.initialized"
            else (
                {
                    "duration_ms": 1250,
                    "exit_code": 0,
                    "outcome": "succeeded",
                    "result_kind": {
                        "status": "available",
                        "value": "empty",
                    },
                }
                if event_code == "run.completed"
                else {}
            )
        ),
    }
    if attributes is not None:
        event["attributes"] = attributes
    if extra_fields is not None:
        event.update(extra_fields)
    return event


def _encode_events(events):
    return b"".join(
        json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for event in events
    )


def _safe_error(error_id="33333333-3333-4333-8333-333333333333"):
    return {
        "error_id": error_id,
        "error_code": "config.value_invalid",
        "category": "configuration",
        "stage": "preflight",
        "module": "configuration",
        "operation": {"status": "not_applicable"},
        "event_sequence": 1,
        "safe_message": "配置包含不合法的值。",
        "retryable_in_new_run": True,
        "operator_action": "fix_configuration",
        "diagnostics": {
            "field": "clip_policy.max_duration_seconds",
            "reason_code": "value.out_of_range",
        },
    }


def _manifest(
    events,
    *,
    event_log_overrides=None,
    extra_fields=None,
    mutate=None,
):
    event_log = {
        "path": "events.jsonl",
        "event_count": len(events.splitlines()),
        "byte_length": len(events),
        "sha256": "sha256:" + hashlib.sha256(events).hexdigest(),
    }
    if event_log_overrides is not None:
        event_log.update(event_log_overrides)
    manifest = {
        "schema_version": "run_manifest.v1",
        "identity": {
            "run_id": _RUN_ID,
            "application_version": "4.7.0",
            "release": {"status": "unknown"},
        },
        "lifecycle": {
            "started_at": "2026-07-26T12:34:56.123Z",
            "ended_at": "2026-07-26T12:34:57.373Z",
            "duration_ms": 1250,
            "outcome": "succeeded",
            "exit_code": 0,
            "result_kind": {"status": "available", "value": "empty"},
            "interruption": {"status": "not_applicable"},
        },
        "source": {"status": "not_observed"},
        "environment": {"status": "not_observed"},
        "configuration": {"status": "not_observed"},
        "stages": {
            "initialized": {"status": "not_started"},
            "preflight": {"status": "not_started"},
            "source_analysis": {"status": "not_started"},
            "transcription": {"status": "not_started"},
            "candidate_planning": {"status": "not_started"},
            "topic_review": {"status": "not_started"},
            "delivery_build": {"status": "not_started"},
            "delivery_verification": {"status": "not_started"},
            "publishing": {"status": "not_started"},
        },
        "operations": {"summaries": []},
        "retries_and_recovery": {
            "transport_retry": 0,
            "semantic_retry": 0,
            "coverage_recovery": 0,
        },
        "cache": {"status": "not_observed"},
        "external_services": {"status": "not_observed"},
        "delivery": {
            "build_state": "completed",
            "verification_state": "passed",
            "publication_state": "committed",
        },
        "notices": [],
        "errors": {
            "primary_error": {"status": "not_applicable"},
            "associated_errors": [],
            "recovery_incomplete": False,
        },
        "event_log": event_log,
    }
    if extra_fields is not None:
        manifest.update(extra_fields)
    if mutate is not None:
        mutate(manifest)
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _complete_snapshot():
    events = _encode_events(
        [_event("run.initialized", 1), _event("run.completed", 2)]
    )
    return DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))


def test_reader_accepts_a_complete_bound_package():
    result = DiagnosticPackageReader.read(_complete_snapshot())

    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID
    assert str(result.run_id) == _RUN_ID
    assert result.event_count == 2


def test_reader_rejects_a_forged_terminal_event_envelope():
    events = _encode_events(
        [
            _event("run.initialized", 1),
            {
                **_event("run.completed", 2),
                "stage": "publishing",
            },
        ]
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=events,
            manifest=_manifest(events),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID


def test_reader_rejects_a_forged_release_projection():
    sensitive_value = "/private/course/source.mp4"
    release = {
        "status": "present",
        "value": {"source_path": sensitive_value},
    }
    event_values = [
        _event("run.initialized", 1),
        _event("run.completed", 2),
    ]
    event_values[0]["attributes"]["release"] = release
    events = _encode_events(event_values)

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=events,
            manifest=_manifest(
                events,
                mutate=lambda manifest: manifest["identity"].update(
                    {"release": release}
                ),
            ),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    assert sensitive_value not in repr(result)


def test_reader_accepts_serialized_remote_request_id_diagnostics():
    monotonic_values = iter((0.0, 0.1, 0.2, 0.3))
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=lambda: next(monotonic_values),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    primary_error = stage.scope(ErrorModule.TRANSCRIPTION).record_failure(
        SimpleNamespace(
            error_code=ErrorCode.TRANSCRIPTION_RATE_LIMITED,
            diagnostics={
                "remote_request_id": RemoteRequestId.from_adapter(
                    "provider-request-id"
                ),
                "attempt": 1,
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=1)
    diagnostics.finish(RunOutcome.failed(primary_error))

    result = DiagnosticPackageReader.read(diagnostics.snapshot())

    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID


def test_reader_accepts_producer_aggregates_rebuilt_from_events():
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    scope.record(
        Facts.external_service(
            capability=ProviderCapability.TRANSCRIPTION,
            adapter_id="http_adapter",
            provider_id="provider",
            model_id="model",
            configuration_fingerprint="sha256:" + "a" * 64,
            endpoint_origin="https://[2001:0db8::1]:443",
            transport=ProviderTransport.REMOTE,
            allowed_data_categories=(
                ExternalDataCategory.AUDIO_SHARD,
            ),
        )
    )
    request = scope.start_operation(
        OperationKind.EXTERNAL_REQUEST,
        item_index=1,
        item_count=1,
    )
    request.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="provider.rate_limited",
        backoff_ms=25,
    )
    request.record(
        Facts.external_request(
            ProviderCapability.TRANSCRIPTION,
            ExternalRequestOutcome.SUCCEEDED,
            attempt_count=2,
            remote_request_id=RemoteRequestId.from_adapter(
                "provider-request-id"
            ),
            input_tokens=20,
            output_tokens=5,
        )
    )
    request.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    cache_write = scope.start_operation(
        OperationKind.CACHE_WRITE,
        item_index=1,
        item_count=1,
    )
    cache_write.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPTION_SHARD,
            CacheOutcome.WRITE_PUBLISHED,
            singleflight_wait_ms=7,
        )
    )
    cache_write.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    scope.record(
        Facts.recovered(
            RecoveredNoticeKind.TRANSPORT_RETRY_SUCCEEDED
        )
    )
    scope.record(
        Facts.transcription_execution(
            retry_count=1,
            recovery_count=0,
        )
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGINT)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGINT,
            cleanup_duration_ms=10,
        )
    )

    result = DiagnosticPackageReader.read(diagnostics.snapshot())

    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID


def test_reader_rebuilds_neutral_transcription_execution_aggregates():
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    scope.record(
        Facts.transcription_execution(
            retry_count=2,
            recovery_count=1,
        )
    )
    primary_error = scope.record_failure(
        SimpleNamespace(
            error_code=ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            diagnostics={"attempt": 3, "http_status": 503},
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)
    diagnostics.finish(RunOutcome.failed(primary_error))

    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    result = DiagnosticPackageReader.read(snapshot)

    assert manifest["retries_and_recovery"] == {
        "coverage_recovery": 1,
        "semantic_retry": 0,
        "transport_retry": 2,
    }
    assert [
        event["attributes"]
        for event in events
        if event["event_code"] == "transcription.execution_observed"
    ] == [{"recovery_count": 1, "retry_count": 2}]
    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID


def test_rejected_transcription_aggregate_does_not_pollute_retry_counts():
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    recovery = scope.start_operation(
        OperationKind.COVERAGE_RECOVERY,
        item_index=1,
        item_count=1,
    )
    recovery.schedule_retry(
        RetryKind.COVERAGE_RECOVERY,
        next_attempt=2,
        reason_code="coverage.gap_detected",
        backoff_ms=0,
    )
    recovery.complete(OperationOutcome.SUCCEEDED, attempt_count=2)

    with pytest.raises(ValueError, match="少于已记录内部操作"):
        scope.record(
            Facts.transcription_execution(
                retry_count=2,
                recovery_count=0,
            )
        )

    scope.record(
        Facts.transcription_execution(
            retry_count=0,
            recovery_count=1,
        )
    )
    primary_error = scope.record_failure(
        SimpleNamespace(
            error_code=ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            diagnostics={"attempt": 2, "http_status": 503},
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)
    diagnostics.finish(RunOutcome.failed(primary_error))
    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)

    assert manifest["retries_and_recovery"] == {
        "coverage_recovery": 1,
        "semantic_retry": 0,
        "transport_retry": 0,
    }
    assert (
        DiagnosticPackageReader.read(snapshot).state
        is DiagnosticPackageReadState.COMPLETE
    )


def test_transcription_aggregate_rejects_internal_work_after_whole_cache_hit():
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    cache = scope.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )
    cache.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.HIT,
        )
    )
    cache.complete(OperationOutcome.SUCCEEDED, attempt_count=1)

    with pytest.raises(ValueError, match="整场转写缓存命中"):
        scope.record(
            Facts.transcription_execution(
                retry_count=1,
                recovery_count=0,
            )
        )


@pytest.mark.parametrize(
    "order",
    ("hit_then_retry", "retry_then_hit"),
)
def test_whole_cache_hit_and_transcription_retry_conflict_is_rejected(
    order,
):
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)

    def record_hit():
        cache = scope.start_operation(
            OperationKind.CACHE_READ,
            item_index=1,
            item_count=1,
        )
        cache.record(
            Facts.cache(
                CacheNamespace.TRANSCRIPT,
                CacheOutcome.HIT,
            )
        )
        cache.complete(OperationOutcome.SUCCEEDED, attempt_count=1)

    def schedule_retry():
        request = scope.start_operation(
            OperationKind.EXTERNAL_REQUEST,
            item_index=1,
            item_count=1,
        )
        request.schedule_retry(
            RetryKind.TRANSPORT_RETRY,
            next_attempt=2,
            reason_code="provider.rate_limited",
            backoff_ms=0,
        )
        request.complete(OperationOutcome.SUCCEEDED, attempt_count=2)

    if order == "hit_then_retry":
        record_hit()
        with pytest.raises(ValueError, match="整场转写缓存命中"):
            schedule_retry()
    else:
        schedule_retry()
        with pytest.raises(ValueError, match="整场转写缓存命中"):
            record_hit()


def test_reader_rejects_internal_work_forged_after_whole_cache_hit():
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    cache = scope.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )
    cache.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.MISS,
        )
    )
    cache.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    scope.record(
        Facts.transcription_execution(
            retry_count=1,
            recovery_count=0,
        )
    )
    primary_error = scope.record_failure(
        SimpleNamespace(
            error_code=ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            diagnostics={"attempt": 2, "http_status": 503},
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)
    diagnostics.finish(RunOutcome.failed(primary_error))
    snapshot = diagnostics.snapshot()
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    cache_event = next(
        event
        for event in events
        if event["event_code"] == "cache.observed"
    )
    cache_event["attributes"]["outcome"] = "hit"
    forged_events = _encode_events(events)
    manifest = json.loads(snapshot.manifest)
    cache_stats = manifest["cache"]["namespaces"]["transcript"]
    cache_stats["hits"] = 1
    cache_stats["misses"] = 0
    manifest["event_log"].update(
        {
            "byte_length": len(forged_events),
            "sha256": (
                "sha256:" + hashlib.sha256(forged_events).hexdigest()
            ),
        }
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=forged_events,
            manifest=(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            ),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT


def test_reader_scopes_whole_transcript_hit_conflict_to_transcription_stage():
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    transcription = stage.scope(ErrorModule.TRANSCRIPTION)
    request = transcription.start_operation(
        OperationKind.EXTERNAL_REQUEST,
        item_index=1,
        item_count=1,
    )
    request.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="provider.rate_limited",
        backoff_ms=0,
    )
    request.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    transcription.record(
        Facts.transcription_execution(
            retry_count=1,
            recovery_count=0,
        )
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)

    stage = diagnostics.start_stage(RunStage.CANDIDATE_PLANNING)
    planning = stage.scope(ErrorModule.CLIP_PLANNING)
    cache = planning.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )
    cache.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.HIT,
        )
    )
    cache.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGINT)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGINT,
            cleanup_duration_ms=0,
        )
    )

    result = DiagnosticPackageReader.read(diagnostics.snapshot())

    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID


def test_producer_scopes_whole_transcript_hit_conflict_to_transcription_stage():
    diagnostics = _in_memory_diagnostics()
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    readiness = stage.scope(ErrorModule.READINESS)
    cache = readiness.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )
    cache.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            CacheOutcome.HIT,
        )
    )
    cache.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)

    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    transcription = stage.scope(ErrorModule.TRANSCRIPTION)
    request = transcription.start_operation(
        OperationKind.EXTERNAL_REQUEST,
        item_index=1,
        item_count=1,
    )
    request.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="provider.rate_limited",
        backoff_ms=0,
    )
    request.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    transcription.record(
        Facts.transcription_execution(
            retry_count=1,
            recovery_count=0,
        )
    )
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGINT)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGINT,
            cleanup_duration_ms=0,
        )
    )

    result = DiagnosticPackageReader.read(diagnostics.snapshot())

    assert result.state is DiagnosticPackageReadState.COMPLETE
    assert result.reason is DiagnosticPackageReadReason.VALID


@pytest.mark.parametrize("field", ["python_version", "font_family"])
def test_reader_rejects_forged_environment_projection(field):
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    stage.scope(ErrorModule.READINESS).record(
        Facts.environment(
            certified_platform=CertifiedPlatform.UBUNTU_24_04_AMD64,
            python_version=DetectedVersion.from_readiness("3.12.4"),
            ffmpeg_version=DetectedVersion.from_readiness("7.1.1"),
            ffprobe_version=DetectedVersion.from_readiness("7.1.1"),
            font_family="Noto Sans CJK SC",
            font_available=True,
            installation_fingerprint="sha256:" + "a" * 64,
            preflight_outcome=PreflightOutcome.SUCCEEDED,
        )
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGTERM)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=10,
        )
    )
    snapshot = diagnostics.snapshot()
    sensitive_value = "/private/course/source.mp4"
    event_values = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    environment_event = next(
        event
        for event in event_values
        if event["event_code"] == "environment.observed"
    )
    if field == "python_version":
        environment_event["attributes"]["python_version"] = sensitive_value
    else:
        environment_event["attributes"]["font"]["family"] = sensitive_value
    forged_events = _encode_events(event_values)
    manifest = json.loads(snapshot.manifest)
    if field == "python_version":
        manifest["environment"]["python_version"] = sensitive_value
    else:
        manifest["environment"]["font"]["family"] = sensitive_value
    manifest["event_log"].update(
        {
            "byte_length": len(forged_events),
            "sha256": (
                "sha256:" + hashlib.sha256(forged_events).hexdigest()
            ),
        }
    )
    forged_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=forged_events,
            manifest=forged_manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    assert sensitive_value not in repr(result)


@pytest.mark.parametrize(
    "field",
    [
        "transcription_provider",
        "text_model_provider",
        "transcription_model",
        "topic_review_model",
        "subtitle_optimization_model",
    ],
)
def test_reader_rejects_forged_configuration_provider_or_model(
    tmp_path,
    field,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    projection = Configuration.load(source).diagnostic_projection
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    stage.scope(ErrorModule.CONFIGURATION).record(
        Facts.configuration(projection)
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGTERM)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=10,
        )
    )
    snapshot = diagnostics.snapshot()
    event_values = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    configuration_event = next(
        event
        for event in event_values
        if event["event_code"] == "configuration.observed"
    )
    manifest = json.loads(snapshot.manifest)
    event_configuration = configuration_event["attributes"][
        "result_configuration"
    ]
    manifest_configuration = manifest["configuration"][
        "result_configuration"
    ]
    sensitive_value = "/private/course/source.mp4"
    if field in {"transcription_provider", "text_model_provider"}:
        event_configuration[field] = "other_provider"
        manifest_configuration[field] = "other_provider"
    elif field == "transcription_model":
        event_configuration[field] = sensitive_value
        manifest_configuration[field] = sensitive_value
    elif field == "topic_review_model":
        event_configuration["topic_review"]["model"] = sensitive_value
        manifest_configuration["topic_review"]["model"] = sensitive_value
    else:
        event_configuration["subtitle_optimization"][
            "model"
        ] = sensitive_value
        manifest_configuration["subtitle_optimization"][
            "model"
        ] = sensitive_value
    forged_events = _encode_events(event_values)
    manifest["event_log"].update(
        {
            "byte_length": len(forged_events),
            "sha256": (
                "sha256:" + hashlib.sha256(forged_events).hexdigest()
            ),
        }
    )
    forged_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=forged_events,
            manifest=forged_manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    assert sensitive_value not in repr(result)


def test_reader_rejects_a_forged_operation_attempt_chain():
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    operation = stage.scope(ErrorModule.TRANSCRIPTION).start_operation(
        OperationKind.TRANSCRIPTION_SHARD,
        item_index=1,
        item_count=1,
    )
    operation.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="provider.rate_limited",
        backoff_ms=25,
    )
    operation.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGTERM)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=10,
        )
    )
    snapshot = diagnostics.snapshot()
    event_values = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    for event in event_values:
        if event["event_code"] == "retry.scheduled":
            event["attributes"]["next_attempt"] = 99
        elif event["event_code"] == "operation.completed":
            event["attributes"]["attempt_count"] = 99
    forged_events = _encode_events(event_values)
    manifest = json.loads(snapshot.manifest)
    manifest["event_log"].update(
        {
            "byte_length": len(forged_events),
            "sha256": (
                "sha256:" + hashlib.sha256(forged_events).hexdigest()
            ),
        }
    )
    forged_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=forged_events,
            manifest=forged_manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID


@pytest.mark.parametrize(
    ("nested", "mutation"),
    [
        (True, "replace_parent"),
        (False, "inject_parent"),
        (False, "change_module"),
    ],
)
def test_reader_rejects_forged_operation_topology(nested, mutation):
    monotonic_value = 0.0

    def monotonic_clock():
        nonlocal monotonic_value
        monotonic_value += 0.1
        return monotonic_value

    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=lambda: datetime(
            2026,
            7,
            26,
            tzinfo=timezone.utc,
        ),
        monotonic_clock=monotonic_clock,
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    parent = (
        scope.start_operation(
            OperationKind.CANDIDATE_BATCH,
            item_index=1,
            item_count=1,
        )
        if nested
        else None
    )
    operation = scope.start_operation(
        OperationKind.TRANSCRIPTION_SHARD,
        item_index=1,
        item_count=1,
        parent=parent,
    )
    operation.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="provider.rate_limited",
        backoff_ms=25,
    )
    operation.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    if parent is not None:
        parent.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGTERM)
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=10,
        )
    )
    snapshot = diagnostics.snapshot()
    event_values = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    retry_event = next(
        event
        for event in event_values
        if event["event_code"] == "retry.scheduled"
    )
    if mutation == "change_module":
        retry_event["module"] = ErrorModule.CACHE.value
    else:
        retry_event["parent_operation_id"] = str(OperationId.new())
    forged_events = _encode_events(event_values)
    manifest = json.loads(snapshot.manifest)
    manifest["event_log"].update(
        {
            "byte_length": len(forged_events),
            "sha256": (
                "sha256:" + hashlib.sha256(forged_events).hexdigest()
            ),
        }
    )
    forged_manifest = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=forged_events,
            manifest=forged_manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID


def test_reader_marks_an_unterminated_tail_incomplete_without_echoing_it():
    snapshot = _complete_snapshot()
    sensitive_tail = '不应回显的课程正文'
    truncated = snapshot.events + (
        '{"message":"' + sensitive_tail
    ).encode("utf-8")

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=truncated,
            manifest=snapshot.manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.INCOMPLETE
    assert result.reason is DiagnosticPackageReadReason.EVENT_TAIL_TRUNCATED
    assert result.event_count == 2
    assert sensitive_tail not in repr(result)


def test_reader_marks_malformed_middle_json_corrupt():
    first = _encode_events([_event("run.initialized", 1)])
    terminal = _encode_events([_event("run.completed", 3)])
    events = first + b'{"broken":}\n' + terminal

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_JSON_INVALID


@pytest.mark.parametrize(
    ("invalid_line", "reason"),
    [
        (
            (
                '{"schema_version":"run_event.v1",'
                '"schema_version":"run_event.v1"}\n'
            ).encode(),
            DiagnosticPackageReadReason.EVENT_DUPLICATE_FIELD,
        ),
        (
            (
                '{"schema_version":"run_event.v1",'
                '"duration_ms":NaN}\n'
            ).encode(),
            DiagnosticPackageReadReason.EVENT_NON_FINITE_NUMBER,
        ),
    ],
)
def test_reader_rejects_non_strict_event_json(invalid_line, reason):
    events = (
        _encode_events([_event("run.initialized", 1)])
        + invalid_line
        + _encode_events([_event("run.completed", 3)])
    )

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is reason


def test_reader_closes_over_decoded_event_type_errors():
    sensitive_value = "不应回显的事件值"
    event_values = [
        _event("run.initialized", 1),
        _event("run.completed", 2),
    ]
    event_values[0]["level"] = [sensitive_value]
    events = _encode_events(event_values)

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=events,
            manifest=_manifest(events),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    assert sensitive_value not in repr(result)


def test_reader_closes_over_decoded_manifest_type_errors():
    snapshot = _complete_snapshot()
    sensitive_value = "不应回显的清单值"

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=_manifest(
                snapshot.events,
                mutate=lambda manifest: manifest["delivery"].update(
                    {"build_state": [sensitive_value]}
                ),
            ),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert (
        result.reason
        is DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    )
    assert sensitive_value not in repr(result)


def test_reader_closes_over_deep_successfully_decoded_json():
    events = b"[" * 800 + b"0" + b"]" * 800 + b"\n"

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=None)
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (
            _event("provider.debug_payload", 2),
            DiagnosticPackageReadReason.EVENT_CODE_UNKNOWN,
        ),
        (
            _event(
                "run.completed",
                2,
                extra_fields={"raw_response": "不允许"},
            ),
            DiagnosticPackageReadReason.EVENT_FIELD_UNKNOWN,
        ),
        (
            _event(
                "run.completed",
                2,
                attributes={
                    "duration_ms": 1250,
                    "exit_code": 2,
                    "outcome": "failed",
                    "result_kind": {"status": "not_applicable"},
                    "raw_response": "不允许",
                },
            ),
            DiagnosticPackageReadReason.EVENT_FIELD_UNKNOWN,
        ),
    ],
)
def test_reader_rejects_unknown_event_codes_and_fields(event, reason):
    events = _encode_events([_event("run.initialized", 1), event])

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is reason


@pytest.mark.parametrize(
    ("terminal", "reason"),
    [
        (
            _event("run.completed", 3),
            DiagnosticPackageReadReason.EVENT_SEQUENCE_MISMATCH,
        ),
        (
            _event("run.completed", 2, run_id=_OTHER_RUN_ID),
            DiagnosticPackageReadReason.EVENT_RUN_ID_MISMATCH,
        ),
    ],
)
def test_reader_rejects_event_identity_discontinuity(terminal, reason):
    events = _encode_events([_event("run.initialized", 1), terminal])

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is reason


@pytest.mark.parametrize(
    "events",
    [
        [
            _event("run.initialized", 1),
            _event("stage.started", 2, attributes={}),
            _event("run.completed", 3),
        ],
        [
            _event("run.initialized", 1),
            _event(
                "stage.completed",
                2,
                attributes={
                    "duration_ms": 10,
                    "outcome": "succeeded",
                    "work_item_count": 0,
                },
            ),
            _event("run.completed", 3),
        ],
    ],
)
def test_reader_rejects_unpaired_stage_boundaries(events):
    encoded = _encode_events(events)

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=encoded,
            manifest=_manifest(encoded),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID


def test_reader_marks_a_missing_terminal_event_incomplete():
    events = _encode_events([_event("run.initialized", 1)])

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=events, manifest=_manifest(events))
    )

    assert result.state is DiagnosticPackageReadState.INCOMPLETE
    assert result.reason is DiagnosticPackageReadReason.TERMINAL_EVENT_MISSING


def test_reader_marks_a_missing_manifest_incomplete():
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(events=snapshot.events, manifest=None)
    )

    assert result.state is DiagnosticPackageReadState.INCOMPLETE
    assert result.reason is DiagnosticPackageReadReason.MANIFEST_MISSING


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"event_count": 3},
            DiagnosticPackageReadReason.MANIFEST_EVENT_COUNT_MISMATCH,
        ),
        (
            {"byte_length": 1},
            DiagnosticPackageReadReason.MANIFEST_EVENT_BYTE_LENGTH_MISMATCH,
        ),
        (
            {"sha256": "sha256:" + "0" * 64},
            DiagnosticPackageReadReason.MANIFEST_EVENT_DIGEST_MISMATCH,
        ),
    ],
)
def test_reader_verifies_manifest_event_log_integrity(overrides, reason):
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=_manifest(
                snapshot.events,
                event_log_overrides=overrides,
            ),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is reason


def test_reader_rejects_unknown_manifest_fields():
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=_manifest(
                snapshot.events,
                extra_fields={"raw_course_content": "不允许"},
            ),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["stages"].pop("publishing"),
        lambda manifest: manifest["stages"]["preflight"].update(
            {"raw_detail": "不允许"}
        ),
        lambda manifest: manifest["retries_and_recovery"].pop(
            "semantic_retry"
        ),
        lambda manifest: manifest["delivery"].update(
            {"publication_state": "unknown_state"}
        ),
        lambda manifest: manifest["errors"].update(
            {"raw_exception": "不允许"}
        ),
        lambda manifest: manifest.update(
            {"configuration": {"status": "available", "raw": "不允许"}}
        ),
        lambda manifest: manifest.update(
            {"source": {"status": "available", "raw": "不允许"}}
        ),
        lambda manifest: manifest.update(
            {"external_services": {"status": "observed", "raw": "不允许"}}
        ),
    ],
)
def test_reader_rejects_missing_or_unknown_nested_manifest_fields(mutate):
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=_manifest(snapshot.events, mutate=mutate),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason in {
        DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN,
        DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest["stages"].update(
            {
                "preflight": {
                    "status": "succeeded",
                    "duration_ms": 0,
                    "work_item_count": 0,
                }
            }
        ),
        lambda manifest: manifest["retries_and_recovery"].update(
            {"transport_retry": 1}
        ),
        lambda manifest: manifest.update(
            {
                "delivery": {
                    "build_state": "not_started",
                    "verification_state": "not_started",
                    "publication_state": "not_started",
                }
            }
        ),
        lambda manifest: manifest["errors"]["associated_errors"].append(
            _safe_error()
        ),
    ],
)
def test_reader_rejects_manifest_facts_that_contradict_the_event_log(
    mutate,
):
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=_manifest(snapshot.events, mutate=mutate),
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID


@pytest.mark.parametrize(
    ("manifest", "reason"),
    [
        (
            (
                '{"schema_version":"run_manifest.v1",'
                '"schema_version":"run_manifest.v1"}\n'
            ).encode(),
            DiagnosticPackageReadReason.MANIFEST_DUPLICATE_FIELD,
        ),
        (
            b'{"schema_version":"run_manifest.v1","duration_ms":NaN}\n',
            DiagnosticPackageReadReason.MANIFEST_NON_FINITE_NUMBER,
        ),
    ],
)
def test_reader_rejects_non_strict_manifest_json(manifest, reason):
    snapshot = _complete_snapshot()

    result = DiagnosticPackageReader.read(
        DiagnosticPackageSnapshot(
            events=snapshot.events,
            manifest=manifest,
        )
    )

    assert result.state is DiagnosticPackageReadState.CORRUPT
    assert result.reason is reason
