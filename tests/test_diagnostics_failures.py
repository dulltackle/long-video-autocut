import json
from datetime import datetime, timezone

import pytest

from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.configuration import ConfigurationFailure
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    DeliveryBuildState,
    DeliveryVerificationState,
    DiagnosticCompletion,
    DiagnosticsFailure,
    Facts,
    InterruptionSignal,
    PublicationState,
    StageOutcome,
)
from video_auto_editor.diagnostics.collecting import (
    _CollectingDiagnosticStore,
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
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import Workspace
from video_auto_editor.workspace import _workspace as workspace_module


def _wall_clock():
    return datetime(2026, 7, 26, tzinfo=timezone.utc)


def _monotonic_clock():
    value = 0

    def read():
        nonlocal value
        value += 1
        return float(value)

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


def _raise_disk_error(*_args, **_kwargs):
    raise OSError("包含 /secret/workspace 和 token 的底层错误")


def test_initial_event_failure_becomes_safe_environment_failure(monkeypatch):
    monkeypatch.setattr(
        _CollectingDiagnosticStore,
        "append",
        _raise_disk_error,
    )

    with pytest.raises(DiagnosticsFailure) as captured:
        initialize_collecting_diagnostics(
            RunId.new(),
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )

    assert captured.value.error_code is (
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE
    )
    assert dict(captured.value.diagnostics) == {
        "component": "run_diagnostics",
        "operation": "diagnostics.append",
        "reason_code": "diagnostics.append_failed",
    }
    assert "/secret/workspace" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_existing_event_log_is_not_overwritten_and_fails_initialization(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        first = initialize_persistent_diagnostics(
            run_workspace.diagnostics,
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )
        first_bytes = first.snapshot().events

        with pytest.raises(DiagnosticsFailure) as captured:
            initialize_persistent_diagnostics(
                run_workspace.diagnostics,
                application_version="4.7.0",
                wall_clock=_wall_clock,
                monotonic_clock=_monotonic_clock(),
            )

        assert first.snapshot().events == first_bytes

    assert captured.value.error_code is (
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE
    )


def test_missing_persistent_event_log_is_never_recreated_mid_run(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        diagnostics = initialize_persistent_diagnostics(
            run_workspace.diagnostics,
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )
        event_path = (
            workspace.root
            / "work"
            / "runs"
            / str(run_id)
            / "events.jsonl"
        )
        event_path.unlink()

        with pytest.raises(DiagnosticsFailure) as captured:
            diagnostics.start_stage(RunStage.PREFLIGHT)

        assert not event_path.exists()

    assert captured.value.error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED


def test_runtime_append_failure_poison_session_without_recursive_event(
    monkeypatch,
):
    diagnostics = initialize_collecting_diagnostics(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_wall_clock,
        monotonic_clock=_monotonic_clock(),
    )
    before = diagnostics.snapshot().events
    monkeypatch.setattr(
        _CollectingDiagnosticStore,
        "append",
        _raise_disk_error,
    )

    with pytest.raises(DiagnosticsFailure) as captured:
        diagnostics.start_stage(RunStage.PREFLIGHT)

    assert captured.value.error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED
    assert dict(captured.value.diagnostics) == {
        "operation": "diagnostics.append",
        "reason_code": "diagnostics.append_failed",
    }
    assert diagnostics.snapshot().events == before
    with pytest.raises(RuntimeError, match="诊断已经完成"):
        diagnostics.start_stage(RunStage.PREFLIGHT)


def test_precommit_manifest_failure_seals_terminal_without_duplicate_event(
    monkeypatch,
):
    diagnostics = initialize_collecting_diagnostics(
        RunId.new(),
        application_version="4.7.0",
        wall_clock=_wall_clock,
        monotonic_clock=_monotonic_clock(),
    )
    stage = diagnostics.start_stage(RunStage.PREFLIGHT)
    error = stage.scope(ErrorModule.CONFIGURATION).record_failure(
        ConfigurationFailure(
            ErrorCode.CONFIG_VALUE_INVALID,
            {
                "field": "clip_policy.max_clips",
                "reason_code": "value.out_of_range",
            },
        )
    )
    stage.complete(StageOutcome.FAILED, work_item_count=0)
    monkeypatch.setattr(
        _CollectingDiagnosticStore,
        "publish_manifest",
        _raise_disk_error,
    )

    with pytest.raises(DiagnosticsFailure) as captured:
        diagnostics.finish(DiagnosticCompletion.failed(error))

    assert captured.value.error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED
    assert dict(captured.value.diagnostics) == {
        "operation": "diagnostics.publish_manifest",
        "reason_code": "diagnostics.atomic_replace_failed",
    }
    first_snapshot = diagnostics.snapshot()
    assert first_snapshot.manifest is None
    assert [
        json.loads(line)["event_code"]
        for line in first_snapshot.events.decode("utf-8").splitlines()
    ].count("run.completed") == 1

    with pytest.raises(RuntimeError, match="诊断已经完成"):
        diagnostics.finish(DiagnosticCompletion.failed(error))
    assert diagnostics.snapshot().events == first_snapshot.events


def test_persistent_finish_never_reports_complete_after_manifest_parent_replacement(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    original_rename_no_replace = (
        workspace_module._rename_no_replace
    )

    with workspace.acquire_run(run_id) as run_workspace:
        diagnostics = initialize_persistent_diagnostics(
            run_workspace.diagnostics,
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )
        stage = diagnostics.start_stage(RunStage.PREFLIGHT)
        scope = stage.scope(ErrorModule.APPLICATION)
        scope.record(Facts.interruption(InterruptionSignal.SIGINT))
        stage.complete(StageOutcome.INTERRUPTED, work_item_count=0)
        diagnostic_directory = (
            workspace.root / "work" / "runs" / str(run_id)
        )
        displaced = diagnostic_directory.with_name(
            "displaced-run-diagnostics"
        )
        swapped = False

        def replace_parent_at_manifest_commit(
            source_parent,
            source_name,
            target_parent,
            target_name,
        ):
            nonlocal swapped
            if target_name == "run.json" and not swapped:
                swapped = True
                diagnostic_directory.rename(displaced)
                diagnostic_directory.mkdir(mode=0o700)
            return original_rename_no_replace(
                source_parent,
                source_name,
                target_parent,
                target_name,
            )

        monkeypatch.setattr(
            workspace_module,
            "_rename_no_replace",
            replace_parent_at_manifest_commit,
        )

        with pytest.raises(DiagnosticsFailure) as captured:
            diagnostics.finish(
                DiagnosticCompletion.interrupted(
                    InterruptionSignal.SIGINT,
                    cleanup_duration_ms=0,
                )
            )

        assert swapped
        assert captured.value.error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED
        assert captured.value.diagnostics["operation"] == (
            "diagnostics.publish_manifest"
        )
        assert not (diagnostic_directory / "events.jsonl").exists()
        assert not (diagnostic_directory / "run.json").exists()
        assert (displaced / "events.jsonl").is_file()
        assert (displaced / "run.json").is_file()


def test_postcommit_manifest_failure_reports_incomplete_without_undoing_success(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        published = _published(run_workspace)
        diagnostics = initialize_collecting_diagnostics(
            run_id,
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )
        stage = diagnostics.enter_stage(
            RunStage.PUBLISHING,
            ErrorModule.PUBLICATION,
        )
        stage.complete(StageOutcome.SUCCEEDED, work_item_count=1)
        monkeypatch.setattr(
            _CollectingDiagnosticStore,
            "publish_manifest",
            _raise_disk_error,
        )

        finalization = diagnostics.finish(
            DiagnosticCompletion.succeeded(
                published,
                result_kind=ResultKind.CLIPS,
            )
        )

        assert published.run_id is run_id

    assert finalization.manifest_persisted is False
    assert finalization.diagnostics_incomplete is True
    assert finalization.terminal_sequence is not None
    assert diagnostics.snapshot().manifest is None


def test_postcommit_event_failure_is_best_effort_through_stage_completion(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        published = _published(run_workspace)
        diagnostics = initialize_collecting_diagnostics(
            run_id,
            application_version="4.7.0",
            wall_clock=_wall_clock,
            monotonic_clock=_monotonic_clock(),
        )
        build_stage = diagnostics.start_stage(RunStage.DELIVERY_BUILD)
        build = build_stage.scope(ErrorModule.DELIVERY_BUILD)
        build.record(
            Facts.delivery_build(DeliveryBuildState.IN_PROGRESS)
        )
        build.record(Facts.delivery_build(DeliveryBuildState.COMPLETED))
        build_stage.complete(
            StageOutcome.SUCCEEDED,
            work_item_count=0,
        )
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
            Facts.delivery_verification(
                DeliveryVerificationState.PASSED
            )
        )
        verification_stage.complete(
            StageOutcome.SUCCEEDED,
            work_item_count=0,
        )
        stage = diagnostics.start_stage(RunStage.PUBLISHING)
        publication = stage.scope(ErrorModule.PUBLICATION)
        publication.record(
            Facts.publication(PublicationState.IN_PROGRESS)
        )
        monkeypatch.setattr(
            _CollectingDiagnosticStore,
            "append",
            _raise_disk_error,
        )

        committed_sequence = publication.record(
            Facts.publication(
                PublicationState.COMMITTED,
                published_delivery=published,
            )
        )
        stage_sequence = stage.complete(
            StageOutcome.SUCCEEDED,
            work_item_count=1,
        )
        finalization = diagnostics.finish(
            DiagnosticCompletion.succeeded(
                published,
                result_kind=ResultKind.CLIPS,
            )
        )

        assert published.run_id is run_id

    assert committed_sequence is None
    assert stage_sequence is None
    assert finalization.manifest_persisted is False
    assert finalization.diagnostics_incomplete is True
    assert finalization.terminal_sequence is None
    assert diagnostics.snapshot().manifest is None
