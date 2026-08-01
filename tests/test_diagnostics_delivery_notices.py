import json
from datetime import datetime, timezone

from video_auto_editor.cache import CacheNamespace, CacheOutcome
from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.configuration import ConfigurationFailure
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    ArtifactRole,
    DeliveryBuildState,
    DeliveryVerificationState,
    DiagnosticCompletion,
    Facts,
    OperationKind,
    OperationOutcome,
    PublicationState,
    RecoveredNoticeKind,
    RetryKind,
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
from video_auto_editor.runtime.identity import RunId, ShortVideoId
from video_auto_editor.workspace import Workspace


def _wall_clock():
    return datetime(2026, 7, 26, tzinfo=timezone.utc)


def _monotonic():
    value = 0.0

    def read():
        nonlocal value
        value += 0.1
        return value

    return read


def _published(run_workspace):
    return PublishedDelivery._from_publication(
        VerifiedDelivery._from_verification(
            UnverifiedDelivery._from_build(
                run_workspace.run_id,
                run_workspace.delivery_staging,
            ),
            verification_snapshot="snapshot-001",
        ),
        published_directory=run_workspace.published_delivery,
    )


def test_delivery_lifecycle_and_artifacts_are_owned_by_their_modules(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    run_id = RunId.new()
    workspace = Workspace.open(source, tmp_path / "workspace")
    diagnostics = initialize_collecting_diagnostics(
        run_id,
        application_version="4.7.0",
        wall_clock=_wall_clock,
        monotonic_clock=_monotonic(),
    )
    short_video_path = f"clips/{ShortVideoId.new()}.mp4"

    build_stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
    build = build_stage.scope(ErrorModule.DELIVERY_BUILD)
    build.record(Facts.delivery_build(DeliveryBuildState.IN_PROGRESS))
    build.record(
        Facts.artifact_created(
            ArtifactRole.SHORT_VIDEO,
            relative_path=short_video_path,
        )
    )
    build.record(
        Facts.artifact_created(
            ArtifactRole.MANIFEST,
            relative_path="manifest.json",
        )
    )
    build.record(Facts.delivery_build(DeliveryBuildState.COMPLETED))
    build_stage.complete(StageOutcome.SUCCEEDED, work_item_count=2)

    verification_stage = diagnostics.start_stage(
        RunStage.DELIVERY_VERIFICATION
    )
    verification = verification_stage.scope(
        ErrorModule.DELIVERY_VERIFICATION
    )
    verification.record(
        Facts.delivery_verification(
            DeliveryVerificationState.IN_PROGRESS
        )
    )
    verification.record(
        Facts.artifact_verified(
            ArtifactRole.SHORT_VIDEO,
            relative_path=short_video_path,
        )
    )
    verification.record(
        Facts.artifact_verified(
            ArtifactRole.MANIFEST,
            relative_path="manifest.json",
        )
    )
    verification.record(
        Facts.delivery_verification(DeliveryVerificationState.PASSED)
    )
    verification_stage.complete(
        StageOutcome.SUCCEEDED,
        work_item_count=2,
    )

    with workspace.acquire_run(run_id) as run_workspace:
        published = _published(run_workspace)
        publishing_stage = diagnostics.start_stage(RunStage.PUBLISHING)
        publication = publishing_stage.scope(ErrorModule.PUBLICATION)
        publication.record(
            Facts.publication(PublicationState.IN_PROGRESS)
        )
        publication.record(
            Facts.publication(
                PublicationState.COMMITTED,
                published_delivery=published,
            )
        )
        publishing_stage.complete(
            StageOutcome.SUCCEEDED,
            work_item_count=1,
        )
        diagnostics.finish(
            DiagnosticCompletion.succeeded(
                published,
                result_kind=ResultKind.CLIPS,
            )
        )

    snapshot = diagnostics.snapshot()
    manifest = json.loads(snapshot.manifest)
    events = [
        json.loads(line)
        for line in snapshot.events.decode("utf-8").splitlines()
    ]

    assert manifest["delivery"] == {
        "build_state": "completed",
        "verification_state": "passed",
        "publication_state": "committed",
        "artifacts": {
            "status": "observed",
            "created_by_role": {
                "manifest": 1,
                "short_video": 1,
            },
            "verified_by_role": {
                "manifest": 1,
                "short_video": 1,
            },
        },
    }
    assert [event["event_code"] for event in events].count(
        "delivery.state_changed"
    ) == 6
    assert [event["event_code"] for event in events].count(
        "artifact.created"
    ) == 2
    assert [event["event_code"] for event in events].count(
        "artifact.verified"
    ) == 2


def test_recovered_work_is_a_notice_and_never_a_terminal_error():
    diagnostics = initialize_collecting_diagnostics(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_wall_clock,
        monotonic_clock=_monotonic(),
    )
    stage = diagnostics.start_stage(RunStage.TRANSCRIPTION)
    scope = stage.scope(ErrorModule.TRANSCRIPTION)

    request = scope.start_operation(
        OperationKind.TRANSCRIPTION_SHARD,
        item_index=1,
        item_count=1,
    )
    request.schedule_retry(
        RetryKind.TRANSPORT_RETRY,
        next_attempt=2,
        reason_code="transport.rate_limited",
        backoff_ms=10,
    )
    request.complete(OperationOutcome.SUCCEEDED, attempt_count=2)
    scope.record(
        Facts.recovered(
            RecoveredNoticeKind.TRANSPORT_RETRY_SUCCEEDED
        )
    )

    quarantine = scope.start_operation(
        OperationKind.CACHE_QUARANTINE,
        item_index=1,
        item_count=1,
    )
    quarantine.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPTION_SHARD,
            CacheOutcome.CORRUPT_QUARANTINED,
            reason_code="cache.digest_mismatch",
            quarantine_digest_prefix="sha256:01234567",
        )
    )
    quarantine.complete(OperationOutcome.SUCCEEDED, attempt_count=1)
    scope.record(
        Facts.recovered(
            RecoveredNoticeKind.CACHE_CORRUPTION_RECOVERED
        )
    )
    primary = scope.record_failure(
        ConfigurationFailure(
            ErrorCode.CONFIG_VALUE_INVALID,
            {
                "field": "clip_policy.max_clips",
                "reason_code": "value.out_of_range",
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=1)
    diagnostics.finish(DiagnosticCompletion.failed(primary))

    manifest = json.loads(diagnostics.snapshot().manifest)

    assert manifest["notices"] == [
        {"kind": "transport_retry_succeeded", "count": 1},
        {"kind": "cache_corruption_recovered", "count": 1},
    ]
    assert manifest["errors"]["associated_errors"] == []
