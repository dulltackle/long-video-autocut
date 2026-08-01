import json
from datetime import datetime, timezone

import pytest

from video_auto_editor.cache import CacheNamespace, CacheOutcome
from video_auto_editor.configuration import (
    Configuration,
    ConfigurationDiagnosticProjection,
    ConfigurationFailure,
)
from video_auto_editor.diagnostics import (
    DiagnosticCompletion,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    RetryKind,
    StageDiagnostics,
    StageOutcome,
)
from video_auto_editor.diagnostics.collecting import (
    initialize as initialize_collecting_diagnostics,
)
from video_auto_editor.diagnostics.persistent import (
    initialize as initialize_persistent_diagnostics,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    RunStage,
)
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.workspace import Workspace, WorkspaceFailure


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


def test_initialize_persists_the_run_bound_first_event(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        diagnostics = initialize_persistent_diagnostics(
            run_workspace.diagnostics,
            application_version="4.7.0",
            wall_clock=_fixed_wall_clock,
            monotonic_clock=lambda: 42.0,
        )

        event_lines = (
            workspace.root / "work" / "runs" / str(run_id) / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        manifest_exists = (
            workspace.root / "work" / "runs" / str(run_id) / "run.json"
        ).exists()

        assert diagnostics.run_id is run_id

    assert len(event_lines) == 1
    assert json.loads(event_lines[0]) == {
        "schema_version": "run_event.v1",
        "timestamp": "2026-07-26T12:34:56.123Z",
        "sequence": 1,
        "run_id": str(run_id),
        "level": "info",
        "event_code": "run.initialized",
        "stage": "initialized",
        "module": "application",
        "message": "直播拆条运行已初始化。",
        "attributes": {
            "application_version": "4.7.0",
            "release": {"status": "unknown"},
        },
    }
    assert manifest_exists is False


def test_stage_scope_persists_one_monotonic_start_and_completion_pair(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    monotonic_values = iter([10.0, 10.125, 10.875])

    with workspace.acquire_run(run_id) as run_workspace:
        diagnostics = initialize_persistent_diagnostics(
            run_workspace.diagnostics,
            application_version="4.7.0",
            wall_clock=_fixed_wall_clock,
            monotonic_clock=lambda: next(monotonic_values),
        )
        scope = diagnostics.enter_stage(
            RunStage.PREFLIGHT,
            ErrorModule.READINESS,
        )
        completed_sequence = scope.complete(
            StageOutcome.SUCCEEDED,
            work_item_count=0,
        )

        with pytest.raises(RuntimeError, match="阶段已经完成"):
            scope.complete(StageOutcome.SUCCEEDED, work_item_count=0)

        events = [
            json.loads(line)
            for line in (
                workspace.root
                / "work"
                / "runs"
                / str(run_id)
                / "events.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    assert completed_sequence == 3
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["event_code"] for event in events] == [
        "run.initialized",
        "stage.started",
        "stage.completed",
    ]
    assert events[1] == {
        "schema_version": "run_event.v1",
        "timestamp": "2026-07-26T12:34:56.123Z",
        "sequence": 2,
        "run_id": str(run_id),
        "level": "info",
        "event_code": "stage.started",
        "stage": "preflight",
        "module": "readiness",
        "message": "直播拆条运行阶段已开始。",
        "attributes": {},
    }
    assert events[2] == {
        "schema_version": "run_event.v1",
        "timestamp": "2026-07-26T12:34:56.123Z",
        "sequence": 3,
        "run_id": str(run_id),
        "level": "info",
        "event_code": "stage.completed",
        "stage": "preflight",
        "module": "readiness",
        "message": "直播拆条运行阶段已完成。",
        "attributes": {
            "duration_ms": 750,
            "outcome": "succeeded",
            "work_item_count": 0,
        },
    }


def test_in_memory_adapter_observes_the_same_event_contract():
    run_id = RunId.new()
    monotonic_values = iter([1.0, 1.25, 1.5])
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )

    diagnostics.enter_stage(
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ).complete(StageOutcome.SUCCEEDED, work_item_count=0)
    snapshot = diagnostics.snapshot()
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]

    assert snapshot.manifest is None
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["event_code"] for event in events] == [
        "run.initialized",
        "stage.started",
        "stage.completed",
    ]
    assert all(event["run_id"] == str(run_id) for event in events)


def test_persistent_and_memory_adapters_produce_identical_package_bytes(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    def exercise(diagnostics):
        stage = diagnostics.start_stage(RunStage.PREFLIGHT)
        scope = stage.scope(ErrorModule.APPLICATION)
        scope.record(Facts.interruption(InterruptionSignal.SIGINT))
        stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
        diagnostics.finish(
            DiagnosticCompletion.interrupted(
                InterruptionSignal.SIGINT,
                cleanup_duration_ms=25,
            )
        )
        return diagnostics.snapshot()

    with workspace.acquire_run(run_id) as run_workspace:
        persistent = exercise(
            initialize_persistent_diagnostics(
                run_workspace.diagnostics,
                application_version="4.7.0",
                wall_clock=_fixed_wall_clock,
                monotonic_clock=iter(
                    [1.0, 2.0, 3.0, 4.0]
                ).__next__,
            )
        )
        memory = exercise(
            initialize_collecting_diagnostics(
                run_id,
                application_version="4.7.0",
                wall_clock=_fixed_wall_clock,
                monotonic_clock=iter(
                    [1.0, 2.0, 3.0, 4.0]
                ).__next__,
            )
        )

    assert persistent == memory


def test_operation_scope_owns_timing_retry_relationship_and_completion():
    run_id = RunId.new()
    monotonic_values = iter([1.0, 2.0, 2.25, 3.0, 4.0])
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )
    stage = diagnostics.enter_stage(
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    )

    operation = stage.start_operation(
        OperationKind.TRANSCRIPTION_SHARD,
        item_index=1,
        item_count=2,
    )
    retry_sequence = operation.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="transport.rate_limited",
        backoff_ms=500,
    )
    completed_sequence = operation.complete(
        OperationOutcome.SUCCEEDED,
        attempt_count=2,
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=2)
    events = [
        json.loads(line)
        for line in diagnostics.snapshot().events.decode("utf-8").splitlines()
    ]

    assert isinstance(operation.operation_id, OperationId)
    assert retry_sequence == 4
    assert completed_sequence == 5
    assert [event["event_code"] for event in events] == [
        "run.initialized",
        "stage.started",
        "operation.started",
        "retry.scheduled",
        "operation.completed",
        "stage.completed",
    ]
    assert events[2]["operation_id"] == str(operation.operation_id)
    assert events[2]["attributes"] == {
        "item_count": 2,
        "item_index": 1,
        "operation_kind": "transcription_shard",
    }
    assert events[3]["level"] == "warning"
    assert events[3]["operation_id"] == str(operation.operation_id)
    assert events[3]["attributes"] == {
        "backoff_ms": 500,
        "next_attempt": 2,
        "reason_code": "transport.rate_limited",
        "retry_kind": "transport_retry",
    }
    assert events[4]["operation_id"] == str(operation.operation_id)
    assert events[4]["attributes"] == {
        "attempt_count": 2,
        "duration_ms": 750,
        "operation_kind": "transcription_shard",
        "outcome": "succeeded",
    }


def test_one_stage_can_issue_minimum_scopes_to_multiple_business_modules():
    run_id = RunId.new()
    monotonic_values = iter([1.0, 2.0, 2.25, 2.5, 3.0, 3.25, 4.0])
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )

    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    assert isinstance(stage, StageDiagnostics)
    subtitle_scope = stage.scope(ErrorModule.SUBTITLE_OPTIMIZATION)
    build_scope = stage.scope(ErrorModule.DELIVERY_BUILD)
    subtitle_operation = subtitle_scope.start_operation(
        OperationKind.SUBTITLE_WINDOW,
        item_index=1,
        item_count=1,
    )
    build_operation = build_scope.start_operation(
        OperationKind.SHORT_VIDEO_EXPORT,
        item_index=1,
        item_count=1,
    )
    subtitle_operation.complete(
        OperationOutcome.SUCCEEDED,
        attempt_count=1,
    )
    build_operation.complete(
        OperationOutcome.SUCCEEDED,
        attempt_count=1,
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
    events = [
        json.loads(line)
        for line in diagnostics.snapshot().events.decode("utf-8").splitlines()
    ]

    assert subtitle_scope.run_id is run_id
    assert subtitle_scope.stage is RunStage.DELIVERY_BUILD
    assert subtitle_scope.module is ErrorModule.SUBTITLE_OPTIMIZATION
    assert build_scope.module is ErrorModule.DELIVERY_BUILD
    assert events[1]["module"] == "application"
    assert events[2]["module"] == "subtitle_optimization"
    assert events[3]["module"] == "delivery_build"
    assert events[-1]["module"] == "application"


def test_failed_run_forms_a_complete_manifest_from_recorded_facts():
    run_id = RunId.new()
    monotonic_values = iter([1.0, 2.0, 3.0, 4.0])
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    configuration_scope = stage.scope(ErrorModule.CONFIGURATION)
    primary_error = configuration_scope.record_failure(
        ConfigurationFailure(
            ErrorCode.CONFIG_VALUE_INVALID,
            {
                "field": "clip_policy.max_duration_seconds",
                "reason_code": "value.out_of_range",
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)

    finalization = diagnostics.finish(DiagnosticCompletion.failed(primary_error))
    snapshot = diagnostics.snapshot()
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]
    manifest = json.loads(snapshot.manifest)

    assert finalization.manifest_persisted is True
    assert finalization.terminal_sequence == 5
    assert events[2]["event_code"] == "error.recorded"
    assert events[2]["sequence"] == primary_error.event_sequence
    assert events[2]["message"] == primary_error.safe_message
    assert events[-1]["event_code"] == "run.completed"
    assert events[-1]["attributes"] == {
        "duration_ms": 3000,
        "exit_code": 2,
        "outcome": "failed",
        "result_kind": {"status": "not_applicable"},
    }
    assert manifest["schema_version"] == "run_manifest.v1"
    assert set(manifest) == {
        "schema_version",
        "identity",
        "lifecycle",
        "source",
        "environment",
        "configuration",
        "stages",
        "operations",
        "retries_and_recovery",
        "cache",
        "external_services",
        "delivery",
        "notices",
        "errors",
        "event_log",
    }
    assert manifest["lifecycle"]["outcome"] == "failed"
    assert manifest["stages"]["preflight"] == {
        "status": "failed",
        "duration_ms": 1000,
        "work_item_count": 0,
    }
    assert manifest["stages"]["source_analysis"] == {
        "status": "not_started"
    }
    assert manifest["errors"]["primary_error"]["error_id"] == str(
        primary_error.error_id
    )
    assert manifest["errors"]["associated_errors"] == []
    assert manifest["errors"]["recovery_incomplete"] is False
    assert manifest["event_log"] == {
        "path": "events.jsonl",
        "event_count": 5,
        "byte_length": len(snapshot.events),
        "sha256": "sha256:"
        + __import__("hashlib").sha256(snapshot.events).hexdigest(),
    }

    def assert_no_null(value):
        if isinstance(value, dict):
            for nested in value.values():
                assert_no_null(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_no_null(nested)
        else:
            assert value is not None

    assert_no_null(manifest)


def test_configuration_fact_persists_only_the_existing_safe_projection(tmp_path):
    secret_topic = "绝不能进入诊断的课程正文"
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    (tmp_path / "course.context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": secret_topic,
                "priority_topics": ["私密重点"],
            }
        ),
        encoding="utf-8",
    )
    loaded = Configuration.load(source)
    monotonic_values = iter([1.0, 2.0, 3.0, 4.0])
    diagnostics = initialize_collecting_diagnostics(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    configuration_scope = stage.scope(ErrorModule.CONFIGURATION)

    observed_sequence = configuration_scope.record(
        Facts.configuration(loaded.diagnostic_projection)
    )
    primary_error = stage.scope(ErrorModule.WORKSPACE).record_failure(
        WorkspaceFailure(
            ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
            {
                "component": "workspace",
                "operation": "workspace.verify",
                "reason_code": "filesystem.permission_denied",
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)
    diagnostics.finish(DiagnosticCompletion.failed(primary_error))
    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)

    assert observed_sequence == 3
    assert manifest["configuration"]["status"] == "available"
    assert manifest["configuration"]["configuration_fingerprint"].startswith(
        "sha256:"
    )
    assert manifest["configuration"]["course_context"] == {
        "provided": True,
        "attribution_provided": False,
        "priority_topic_count": 1,
        "excluded_content_count": 0,
    }
    assert manifest["configuration"]["result_configuration"][
        "clip_policy"
    ]["max_clips"] == {"status": "unlimited"}
    persisted = snapshot.events + snapshot.manifest
    assert secret_topic.encode("utf-8") not in persisted
    assert "私密重点".encode("utf-8") not in persisted
    assert loaded.effective.transcription_provider_config.endpoint.encode(
        "utf-8"
    ) not in persisted
    assert (
        loaded.effective.transcription_provider_config.key_environment_variable.encode(
            "utf-8"
        )
        not in persisted
    )


def test_configuration_fact_rejects_unissued_and_recursively_mutated_projections(
    tmp_path,
):
    credential_canary = "sk-live-credential-canary"
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    legitimate = Configuration.load(source).diagnostic_projection

    assert Facts.configuration(legitimate) is not None

    forged = object.__new__(ConfigurationDiagnosticProjection)
    object.__setattr__(
        forged,
        "configuration_fingerprint",
        credential_canary,
    )
    object.__setattr__(
        forged,
        "result_configuration",
        legitimate.result_configuration,
    )
    object.__setattr__(
        forged,
        "runtime_policy",
        legitimate.runtime_policy,
    )
    object.__setattr__(
        forged,
        "course_context",
        legitimate.course_context,
    )

    with pytest.raises(TypeError, match="Configuration 创建"):
        Facts.configuration(forged)

    object.__setattr__(
        legitimate.result_configuration,
        "transcription_model",
        credential_canary,
    )

    with pytest.raises(TypeError, match="Configuration 创建"):
        Facts.configuration(legitimate)

    recursive = Configuration.load(source).diagnostic_projection
    object.__setattr__(
        recursive.result_configuration,
        "topic_review",
        recursive.result_configuration,
    )

    with pytest.raises(TypeError, match="Configuration 创建"):
        Facts.configuration(recursive)


def test_cache_fact_records_only_closed_outcomes_and_aggregates_by_namespace():
    run_id = RunId.new()
    monotonic_values = iter([1.0, 2.0, 2.25, 2.5, 3.0, 4.0, 5.0])
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=lambda: next(monotonic_values),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    operation = scope.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )

    cache_sequence = operation.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPTION_SHARD,
            CacheOutcome.HIT,
            singleflight_wait_ms=25,
        )
    )
    operation.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    primary_error = scope.record_failure(
        WorkspaceFailure(
            ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
            {
                "component": "workspace",
                "operation": "workspace.access",
                "reason_code": "workspace.io_failed",
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=1)
    diagnostics.finish(DiagnosticCompletion.failed(primary_error))
    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]

    assert cache_sequence == 4
    assert events[3]["event_code"] == "cache.observed"
    assert events[3]["operation_id"] == str(operation.operation_id)
    assert events[3]["attributes"] == {
        "duration_ms": 250,
        "namespace": "transcription_shard",
        "outcome": "hit",
        "singleflight_wait_ms": 25,
    }
    assert manifest["cache"] == {
        "status": "observed",
        "namespaces": {
            "transcription_shard": {
                "queries": 1,
                "hits": 1,
                "misses": 0,
                "corrupt_quarantined": 0,
                "writes_published": 0,
                "writes_already_present": 0,
                "infrastructure_failures": 0,
                "singleflight_wait_count": 1,
                "singleflight_wait_ms_total": 25,
            }
        },
    }
    assert manifest["operations"]["summaries"] == [
        {
            "operation_kind": "cache_read",
            "count": 1,
            "outcomes": {
                "succeeded": 1,
                "failed": 0,
                "interrupted": 0,
                "cancelled_due_to_primary": 0,
            },
            "duration_ms_total": 750,
            "duration_ms_max": 750,
        }
    ]

    with pytest.raises(ValueError, match="完整缓存身份摘要"):
        Facts.cache(
            CacheNamespace.TRANSCRIPTION_SHARD,
            CacheOutcome.CORRUPT_QUARANTINED,
            reason_code="cache.digest_mismatch",
            quarantine_digest_prefix="sha256:" + "a" * 64,
        )
