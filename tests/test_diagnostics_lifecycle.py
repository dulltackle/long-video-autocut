import json
from datetime import datetime, timezone
from threading import Barrier, Lock, Thread

import pytest

from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    CertifiedPlatform,
    ExternalDataCategory,
    ExternalRequestOutcome,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    PreflightOutcome,
    ProviderCapability,
    ProviderTransport,
    ResultKind,
    RunDiagnostics,
    RunOutcome,
    StageOutcome,
    ZeroRequestReason,
)
from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ErrorCode,
    ErrorModule,
    RemoteRequestId,
    RunStage,
)
from video_auto_editor.runtime.identity import RunId
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


def _monotonic_counter():
    lock = Lock()
    value = 0.0

    def read():
        nonlocal value
        with lock:
            value += 0.125
            return value

    return read


def _published_delivery(run_workspace):
    unverified = UnverifiedDelivery._from_build(
        run_workspace.run_id,
        run_workspace.delivery_staging,
    )
    verified = VerifiedDelivery._from_verification(
        unverified,
        verification_snapshot="snapshot-001",
    )
    return PublishedDelivery._from_publication(
        verified,
        published_directory=run_workspace.published_delivery,
    )


@pytest.mark.parametrize("result_kind", [ResultKind.CLIPS, ResultKind.EMPTY])
def test_success_requires_same_run_published_delivery_and_projects_commit(
    tmp_path,
    result_kind,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        diagnostics = RunDiagnostics.in_memory(
            run_id,
            application_version="4.7.0",
            wall_clock=_fixed_wall_clock,
            monotonic_clock=_monotonic_counter(),
        )
        stage = diagnostics.enter_stage(
            RunStage.PUBLISHING,
            ErrorModule.PUBLICATION,
        )
        stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
        published = _published_delivery(run_workspace)

        finalization = diagnostics.finish(
            RunOutcome.succeeded(
                published,
                result_kind=result_kind,
            )
        )

    manifest = json.loads(diagnostics.snapshot().manifest)
    terminal_event = json.loads(
        diagnostics.snapshot().events.decode("utf-8").splitlines()[-1]
    )

    assert finalization.manifest_persisted is True
    assert finalization.diagnostics_incomplete is False
    assert terminal_event["attributes"]["outcome"] == "succeeded"
    assert terminal_event["attributes"]["exit_code"] == 0
    assert terminal_event["attributes"]["result_kind"] == {
        "status": "available",
        "value": result_kind.value,
    }
    assert manifest["lifecycle"]["outcome"] == "succeeded"
    assert manifest["lifecycle"]["result_kind"] == {
        "status": "available",
        "value": result_kind.value,
    }
    assert manifest["lifecycle"]["interruption"] == {
        "status": "not_applicable"
    }
    assert manifest["delivery"] == {
        "build_state": "completed",
        "verification_state": "passed",
        "publication_state": "committed",
    }
    assert manifest["errors"] == {
        "primary_error": {"status": "not_applicable"},
        "associated_errors": [],
        "recovery_incomplete": False,
    }


def test_success_rejects_a_published_delivery_from_another_run(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        published = _published_delivery(run_workspace)

        with pytest.raises(ValueError, match="同一次直播拆条运行"):
            diagnostics.finish(
                RunOutcome.succeeded(
                    published,
                    result_kind=ResultKind.EMPTY,
                )
            )


def test_interrupted_run_has_no_primary_error_and_records_signal_cleanup():
    run_id = RunId.new()
    diagnostics = RunDiagnostics.in_memory(
        run_id,
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.APPLICATION)

    scope.record(Facts.interruption(InterruptionSignal.SIGTERM))
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=2)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGTERM,
            cleanup_duration_ms=375,
        )
    )

    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]

    assert [event["event_code"] for event in events][-3:] == [
        "interruption.requested",
        "stage.completed",
        "run.completed",
    ]
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["lifecycle"]["exit_code"] == 143
    assert manifest["lifecycle"]["result_kind"] == {
        "status": "not_applicable"
    }
    assert manifest["lifecycle"]["interruption"] == {
        "status": "available",
        "signal": "sigterm",
        "received_at": "2026-07-26T12:34:56.123Z",
        "cleanup_duration_ms": 375,
    }
    assert manifest["errors"]["primary_error"] == {
        "status": "not_applicable"
    }


def test_interruption_cleanup_failure_is_associated_without_becoming_primary():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    stage.scope(ErrorModule.APPLICATION).record(
        Facts.interruption(InterruptionSignal.SIGINT)
    )
    cleanup_error = stage.scope(ErrorModule.WORKSPACE).record_failure(
        WorkspaceFailure(
            ErrorCode.WORKSPACE_CLEANUP_FAILED,
            {
                "operation": "workspace.cleanup",
                "reason_code": "workspace.remove_failed",
            },
        )
    )
    stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGINT,
            cleanup_duration_ms=20,
            associated_errors=(cleanup_error,),
            recovery_incomplete=True,
        )
    )

    errors = json.loads(diagnostics.snapshot().manifest)["errors"]

    assert errors["primary_error"] == {"status": "not_applicable"}
    assert errors["associated_errors"][0]["error_id"] == str(
        cleanup_error.error_id
    )
    assert errors["recovery_incomplete"] is True


def test_source_and_environment_facts_use_safe_closed_projections():
    source_sha256 = "sha256:" + "a" * 64
    context_sha256 = "sha256:" + "b" * 64
    installation_sha256 = "sha256:" + "c" * 64
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    stage.scope(ErrorModule.SOURCE_ANALYSIS).record(
        Facts.source(
            sha256=source_sha256,
            byte_length=123456,
            duration_ms=98765,
            course_context_provided=True,
            course_context_sha256=context_sha256,
        )
    )
    stage.scope(ErrorModule.READINESS).record(
        Facts.environment(
            certified_platform=CertifiedPlatform.UBUNTU_24_04_AMD64,
            python_version=DetectedVersion.from_readiness("3.12.3"),
            ffmpeg_version=DetectedVersion.from_readiness("6.1.1"),
            ffprobe_version=DetectedVersion.from_readiness("6.1.1"),
            font_family="Noto Sans CJK SC",
            font_available=True,
            installation_fingerprint=installation_sha256,
            preflight_outcome=PreflightOutcome.SUCCEEDED,
        )
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=2)

    events = diagnostics.snapshot().events
    assert source_sha256.encode("ascii") not in events
    assert context_sha256.encode("ascii") not in events
    event_objects = [
        json.loads(line) for line in events.decode("utf-8").splitlines()
    ]
    assert event_objects[2]["attributes"] == {
        "byte_length": 123456,
        "course_context_provided": True,
        "duration_ms": 98765,
    }
    assert event_objects[3]["attributes"]["certified_platform"] == (
        "ubuntu_24_04_amd64"
    )


def test_external_service_discloses_zero_requests_and_aggregates_requests():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.TOPIC_REVIEW)
    scope = stage.scope(ErrorModule.TOPIC_REVIEW)
    scope.record(
        Facts.external_service(
            capability=ProviderCapability.TOPIC_REVIEW,
            adapter_id="stepfun_chat",
            provider_id="stepfun",
            model_id="step-2-mini",
            configuration_fingerprint="sha256:" + "d" * 64,
            endpoint_origin="https://api.stepfun.com",
            transport=ProviderTransport.REMOTE,
            allowed_data_categories=(
                ExternalDataCategory.CANDIDATE_TRANSCRIPT,
                ExternalDataCategory.COURSE_CONTEXT,
                ExternalDataCategory.BUSINESS_CONSTRAINTS,
            ),
        )
    )
    scope.record(
        Facts.external_service_zero_requests(
            ProviderCapability.TOPIC_REVIEW,
            ZeroRequestReason.CACHE_HIT,
        )
    )
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=0)

    # 第二个 capability 实际发起一次请求。
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    scope.record(
        Facts.external_service(
            capability=ProviderCapability.TRANSCRIPTION,
            adapter_id="stepaudio_http",
            provider_id="stepaudio",
            model_id="stepaudio-2.5-asr",
            configuration_fingerprint="sha256:" + "e" * 64,
            endpoint_origin="https://api.stepfun.com",
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
    request.record(
        Facts.external_request(
            ProviderCapability.TRANSCRIPTION,
            ExternalRequestOutcome.SUCCEEDED,
            attempt_count=1,
            remote_request_id=RemoteRequestId.from_adapter(
                "raw-provider-request-id"
            ),
            input_tokens=120,
            output_tokens=30,
        )
    )
    request.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)

    snapshot = diagnostics.snapshot()
    event_bytes = snapshot.events
    assert b"raw-provider-request-id" not in event_bytes

    # 用中断终态完成清单，避免为供应商聚合构造无关失败。
    terminal_stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    terminal_scope = terminal_stage.scope(ErrorModule.APPLICATION)
    terminal_scope.record(Facts.interruption(InterruptionSignal.SIGINT))
    terminal_stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
    diagnostics.finish(
        RunOutcome.interrupted(
            InterruptionSignal.SIGINT,
            cleanup_duration_ms=10,
        )
    )
    services = json.loads(diagnostics.snapshot().manifest)[
        "external_services"
    ]["services"]

    assert services[0]["capability"] == "transcription"
    assert services[0]["contact"] == {"status": "contacted"}
    assert services[0]["requests"] == {
        "count": 1,
        "succeeded": 1,
        "failed": 0,
        "attempt_count_total": 1,
        "duration_ms_total": 125,
        "duration_ms_max": 125,
        "token_usage": {
            "status": "reported",
            "input_tokens": 120,
            "output_tokens": 30,
        },
    }
    assert services[1]["capability"] == "topic_review"
    assert services[1]["contact"] == {
        "status": "not_contacted",
        "reason": "cache_hit",
    }
    assert services[1]["requests"]["count"] == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.example.com",
        "https://user:secret@api.example.com",
        "https://api.example.com/v1",
        "https://api.example.com?api_key=secret",
    ],
)
def test_external_service_rejects_more_than_an_https_origin(endpoint):
    with pytest.raises(ValueError, match="HTTPS origin"):
        Facts.external_service(
            capability=ProviderCapability.TOPIC_REVIEW,
            adapter_id="stepfun_chat",
            provider_id="stepfun",
            model_id="step-2-mini",
            configuration_fingerprint="sha256:" + "d" * 64,
            endpoint_origin=endpoint,
            transport=ProviderTransport.REMOTE,
            allowed_data_categories=(
                ExternalDataCategory.CANDIDATE_TRANSCRIPT,
            ),
        )


def test_external_service_preserves_a_canonical_ipv6_origin():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    scope = diagnostics.start_stage(RunStage.TOPIC_REVIEW).scope(
        ErrorModule.TOPIC_REVIEW
    )

    scope.record(
        Facts.external_service(
            capability=ProviderCapability.TOPIC_REVIEW,
            adapter_id="stepfun_chat",
            provider_id="stepfun",
            model_id="step-2-mini",
            configuration_fingerprint="sha256:" + "d" * 64,
            endpoint_origin="https://[2001:0db8::1]:443",
            transport=ProviderTransport.REMOTE,
            allowed_data_categories=(
                ExternalDataCategory.CANDIDATE_TRANSCRIPT,
            ),
        )
    )
    event = json.loads(
        diagnostics.snapshot().events.decode("utf-8").splitlines()[-1]
    )

    assert event["attributes"]["endpoint"] == {
        "status": "available",
        "origin": "https://[2001:db8::1]",
    }


def test_concurrent_operations_receive_a_single_strict_sequence():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)
    barrier = Barrier(9)
    failures = []

    def work(item_index):
        try:
            barrier.wait()
            operation = scope.start_operation(
                OperationKind.TRANSCRIPTION_SHARD,
                item_index=item_index,
                item_count=8,
            )
            operation.complete(
                OperationOutcome.SUCCEEDED,
                attempt_count=1,
            )
        except BaseException as exc:  # pragma: no cover - 仅用于线程回传
            failures.append(exc)

    workers = [
        Thread(target=work, args=(item_index,))
        for item_index in range(1, 9)
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    stage.complete(StageOutcome.SUCCEEDED, work_item_count=8)

    events = [
        json.loads(line)
        for line in diagnostics.snapshot().events.decode("utf-8").splitlines()
    ]
    sequences = [event["sequence"] for event in events]

    assert failures == []
    assert sequences == list(range(1, len(events) + 1))
    assert len(
        {
            event["operation_id"]
            for event in events
            if event["event_code"] == "operation.started"
        }
    ) == 8


def test_a_forged_scope_cannot_emit_into_an_active_stage():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    authentic = stage.scope(ErrorModule.READINESS)
    forged = object.__new__(type(authentic))
    for slot in ("_owner", "_stage_diagnostics", "_stage", "_module"):
        object.__setattr__(forged, slot, getattr(authentic, slot))

    with pytest.raises(TypeError, match="当前活动阶段"):
        forged.start_operation(
            OperationKind.PREFLIGHT_CHECK,
            item_index=1,
            item_count=1,
        )


def test_a_cloned_or_mutated_fact_cannot_bypass_the_factory_authority():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    scope = diagnostics.start_stage(RunStage.PREFLIGHT).scope(
        ErrorModule.SOURCE_ANALYSIS
    )
    authentic = Facts.source(
        sha256="sha256:" + "a" * 64,
        byte_length=1,
        duration_ms=1,
        course_context_provided=False,
    )
    forged = object.__new__(type(authentic))
    for slot in ("_kind", "_payload", "_seal"):
        object.__setattr__(forged, slot, getattr(authentic, slot))

    with pytest.raises(TypeError, match="必须由 Facts 创建"):
        scope.record(forged)

    object.__setattr__(authentic._payload, "byte_length", 999)
    with pytest.raises(TypeError, match="必须由 Facts 创建"):
        scope.record(authentic)


def test_completed_stage_cannot_be_entered_twice():
    diagnostics = RunDiagnostics.in_memory(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_fixed_wall_clock,
        monotonic_clock=_monotonic_counter(),
    )
    diagnostics.enter_stage(
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ).complete(StageOutcome.SUCCEEDED, work_item_count=0)

    with pytest.raises(RuntimeError, match="不能重复进入"):
        diagnostics.start_stage(RunStage.PREFLIGHT)
