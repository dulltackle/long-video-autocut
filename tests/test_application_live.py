import inspect
import json
import os
import signal
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import video_auto_editor.application as application_api
import video_auto_editor.workspace._workspace as workspace_effects
from video_auto_editor.application import (
    LiveApplication,
    LiveRunOutcome,
    LiveRunRequest,
    LiveRunState,
)
from video_auto_editor.application._deterministic import (
    compose_deterministic_live_application,
)
from video_auto_editor.application.live import _CommitState
from video_auto_editor.diagnostics import InterruptionSignal, ResultKind
from video_auto_editor.runtime.errors import ErrorCode, ExitCode, RunStage
from video_auto_editor.runtime.identity import (
    RunId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.workspace import (
    ManagedPathCapability,
    RunWorkspace,
    Workspace,
    WorkspaceFailure,
)

EXPECTED_STAGES = [
    "preflight",
    "source_analysis",
    "transcription",
    "candidate_planning",
    "topic_review",
    "delivery_build",
    "delivery_verification",
    "publishing",
]


def test_application_package_exposes_one_business_entry_and_terminal_contract():
    assert application_api.__all__ == [
        "LiveApplication",
        "LiveRunOutcome",
        "LiveRunRequest",
        "LiveRunState",
    ]
    assert list(inspect.signature(LiveApplication.execute).parameters) == [
        "self",
        "request",
    ]
    assert set(LiveRunOutcome.__dataclass_fields__) == {
        "associated_errors",
        "diagnostics_incomplete",
        "exit_code",
        "interruption_signal",
        "primary_error",
        "primary_error_code",
        "recovery_incomplete",
        "result_kind",
        "run_id",
        "state",
    }
    forbidden_details = {
        "batch",
        "clip",
        "export",
        "shard",
        "subtitle_window",
        "transcription_shard",
    }
    assert forbidden_details.isdisjoint(
        inspect.signature(LiveRunRequest).parameters
    )
    assert forbidden_details.isdisjoint(
        LiveRunOutcome.__dataclass_fields__
    )


def test_live_run_request_snapshots_immutable_public_inputs(tmp_path):
    source_text = str(tmp_path / "course.mp4")
    workspace_text = str(tmp_path / "workspace")

    request = LiveRunRequest(
        source_text,
        workspace_dir=workspace_text,
        overwrite=True,
    )

    assert request.source == Path(source_text)
    assert request.workspace_dir == Path(workspace_text)
    assert request.overwrite is True
    assert not hasattr(request, "__dict__")
    with pytest.raises(FrozenInstanceError):
        request.overwrite = False


def test_clips_run_advances_once_through_the_fixed_lifecycle(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application()

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert isinstance(application, LiveApplication)
    assert isinstance(outcome, LiveRunOutcome)
    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.primary_error is None
    assert outcome.associated_errors == ()
    assert outcome.recovery_incomplete is False
    assert outcome.diagnostics_incomplete is False
    with pytest.raises(FrozenInstanceError):
        outcome.result_kind = ResultKind.EMPTY

    run_directory = workspace / "work" / "runs" / str(outcome.run_id)
    events = [
        json.loads(line)
        for line in (run_directory / "events.jsonl").read_text().splitlines()
    ]
    started = [
        event["stage"]
        for event in events
        if event["event_code"] == "stage.started"
    ]
    completed = [
        (event["stage"], event["attributes"]["outcome"])
        for event in events
        if event["event_code"] == "stage.completed"
    ]
    assert started == EXPECTED_STAGES
    assert completed == [
        (stage, "succeeded") for stage in EXPECTED_STAGES
    ]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    delivery_manifest = json.loads(
        (workspace / "delivery" / "manifest.json").read_text()
    )
    assert {
        key: delivery_manifest[key]
        for key in ("result_kind", "run_id", "schema_version")
    } == {
        "result_kind": "clips",
        "run_id": str(outcome.run_id),
        "schema_version": "deterministic.v1",
    }
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["lifecycle"]["outcome"] == "succeeded"
    assert manifest["lifecycle"]["exit_code"] == 0
    assert manifest["lifecycle"]["result_kind"] == {
        "status": "available",
        "value": "clips",
    }


def test_effective_empty_result_uses_the_same_success_path(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        result_kind=ResultKind.EMPTY
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.EMPTY
    events = [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "events.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    completed_stages = [
        event["stage"]
        for event in events
        if event["event_code"] == "stage.completed"
    ]

    assert completed_stages == EXPECTED_STAGES
    assert not any(
        event["event_code"] == "operation.started"
        and event["attributes"]["operation_kind"] == "subtitle_window"
        for event in events
    )
    delivery_manifest = json.loads(
        (workspace / "delivery" / "manifest.json").read_text()
    )
    assert {
        key: delivery_manifest[key]
        for key in ("result_kind", "run_id", "schema_version")
    } == {
        "result_kind": "empty",
        "run_id": str(outcome.run_id),
        "schema_version": "deterministic.v1",
    }
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["result_kind"] == {
        "status": "available",
        "value": "empty",
    }
    assert manifest["delivery"] == {
        "artifacts": {
            "created_by_role": {"manifest": 1},
            "status": "observed",
            "verified_by_role": {"manifest": 1},
        },
        "build_state": "completed",
        "publication_state": "committed",
        "verification_state": "passed",
    }


def test_topic_review_failure_stops_later_stages_and_cleans_temporary_content(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    sentinel = workspace / "delivery" / "existing.txt"
    sentinel.write_bytes(b"existing delivery")
    application = compose_deterministic_live_application(
        failure_stage=RunStage.TOPIC_REVIEW
    )

    outcome = application.execute(
        LiveRunRequest(
            source,
            workspace_dir=workspace,
            overwrite=True,
        )
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.EXTERNAL_SERVICE_FAILED
    assert outcome.result_kind is None
    assert outcome.primary_error_code is ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID
    assert outcome.primary_error is not None
    assert outcome.primary_error.stage is RunStage.TOPIC_REVIEW
    assert outcome.associated_errors == ()
    assert outcome.recovery_incomplete is False
    assert outcome.diagnostics_incomplete is False
    assert sentinel.read_bytes() == b"existing delivery"
    assert list((workspace / "delivery").iterdir()) == [sentinel]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()

    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "failed"
    assert manifest["lifecycle"]["exit_code"] == 30
    assert manifest["stages"]["topic_review"]["status"] == "failed"
    assert manifest["stages"]["delivery_build"] == {
        "status": "not_started"
    }
    assert manifest["stages"]["publishing"] == {
        "status": "not_started"
    }


def test_nonempty_delivery_without_overwrite_is_rejected_after_diagnostics(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    current = workspace / "delivery" / "current.txt"
    previous = workspace / "delivery.previous" / "previous.txt"
    current.write_bytes(b"current delivery")
    previous.write_bytes(b"previous delivery")
    application = compose_deterministic_live_application(
        unexpected_stage=RunStage.SOURCE_ANALYSIS,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PUBLICATION_FAILED
    assert outcome.primary_error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
    assert outcome.primary_error is not None
    assert outcome.primary_error.stage is RunStage.PREFLIGHT
    assert outcome.primary_error.diagnostics == {
        "operation": "publication.verify_binding",
        "reason_code": "publication.destination_not_empty",
    }
    assert current.read_bytes() == b"current delivery"
    assert previous.read_bytes() == b"previous delivery"
    run_manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert run_manifest["stages"]["preflight"]["status"] == "failed"
    assert run_manifest["stages"]["source_analysis"] == {
        "status": "not_started"
    }


def test_explicit_overwrite_promotes_current_to_the_only_previous_version(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    current = workspace / "delivery" / "current.txt"
    older = workspace / "delivery.previous" / "older.txt"
    current.write_bytes(b"current delivery")
    older.write_bytes(b"older delivery")

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(
            source,
            workspace_dir=workspace,
            overwrite=True,
        )
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert json.loads(
        (workspace / "delivery" / "manifest.json").read_text()
    )["run_id"] == str(outcome.run_id)
    assert sorted(
        path.name for path in (workspace / "delivery").iterdir()
    ) == ["manifest.json"]
    assert (
        workspace / "delivery.previous" / "current.txt"
    ).read_bytes() == b"current delivery"
    assert sorted(
        path.name for path in (workspace / "delivery.previous").iterdir()
    ) == ["current.txt"]
    assert sorted(
        path.name for path in workspace.iterdir() if path.is_dir()
    ) == ["delivery", "delivery.previous", "work"]


@pytest.mark.parametrize(
    ("stage", "error_code", "exit_code", "exit_code_value"),
    [
        (
            RunStage.PREFLIGHT,
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            ExitCode.PREFLIGHT_FAILED,
            10,
        ),
        (
            RunStage.SOURCE_ANALYSIS,
            ErrorCode.INPUT_MEDIA_INVALID,
            ExitCode.INPUT_FAILED,
            20,
        ),
        (
            RunStage.TRANSCRIPTION,
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            ExitCode.EXTERNAL_SERVICE_FAILED,
            30,
        ),
        (
            RunStage.CANDIDATE_PLANNING,
            ErrorCode.MEDIA_PROCESSING_FAILED,
            ExitCode.LOCAL_PROCESSING_FAILED,
            40,
        ),
        (
            RunStage.TOPIC_REVIEW,
            ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID,
            ExitCode.EXTERNAL_SERVICE_FAILED,
            30,
        ),
        (
            RunStage.DELIVERY_BUILD,
            ErrorCode.DELIVERY_BUILD_FAILED,
            ExitCode.DELIVERY_FAILED,
            50,
        ),
        (
            RunStage.DELIVERY_VERIFICATION,
            ErrorCode.DELIVERY_VERIFICATION_FAILED,
            ExitCode.DELIVERY_FAILED,
            50,
        ),
        (
            RunStage.PUBLISHING,
            ErrorCode.PUBLICATION_COMMIT_FAILED,
            ExitCode.PUBLICATION_FAILED,
            60,
        ),
    ],
)
def test_each_stage_failure_has_a_stable_terminal_error(
    tmp_path,
    stage,
    error_code,
    exit_code,
    exit_code_value,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        failure_stage=stage
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.primary_error_code is error_code
    assert outcome.primary_error is not None
    assert outcome.primary_error.stage is stage
    assert outcome.exit_code is exit_code
    assert int(outcome.exit_code) == exit_code_value
    assert outcome.diagnostics_incomplete is False
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    delivery = manifest["delivery"]
    assert "in_progress" not in delivery.values()
    if stage is RunStage.DELIVERY_BUILD:
        assert delivery["build_state"] == "failed"
    elif stage is RunStage.DELIVERY_VERIFICATION:
        assert delivery["build_state"] == "completed"
        assert delivery["verification_state"] == "failed"
    elif stage is RunStage.PUBLISHING:
        assert delivery["build_state"] == "completed"
        assert delivery["verification_state"] == "passed"
        assert delivery["publication_state"] == "failed"


def test_configuration_failure_is_a_formal_run_with_usage_exit_code(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v1","unknown":true}'
    )
    workspace = tmp_path / "workspace"

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INVALID_USAGE
    assert outcome.primary_error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert outcome.primary_error is not None
    assert outcome.primary_error.module.value == "configuration"
    assert outcome.primary_error.stage is RunStage.PREFLIGHT
    assert (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).is_file()


@pytest.mark.parametrize(
    ("interruption_signal", "exit_code", "exit_code_value"),
    [
        (InterruptionSignal.SIGINT, ExitCode.SIGINT, 130),
        (InterruptionSignal.SIGTERM, ExitCode.SIGTERM, 143),
    ],
)
def test_interruption_before_publication_commit_preserves_existing_delivery(
    tmp_path,
    interruption_signal,
    exit_code,
    exit_code_value,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    sentinel = workspace / "delivery" / "existing.txt"
    sentinel.write_bytes(b"existing delivery")
    application = compose_deterministic_live_application(
        interruption_stage=RunStage.PUBLISHING,
        interruption_signal=interruption_signal,
        deliver_signal_through_os=True,
    )

    outcome = application.execute(
        LiveRunRequest(
            source,
            workspace_dir=workspace,
            overwrite=True,
        )
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is exit_code
    assert int(outcome.exit_code) == exit_code_value
    assert outcome.result_kind is None
    assert outcome.primary_error is None
    assert outcome.primary_error_code is None
    assert outcome.interruption_signal is interruption_signal
    assert outcome.associated_errors == ()
    assert outcome.recovery_incomplete is False
    assert outcome.diagnostics_incomplete is False
    assert sentinel.read_bytes() == b"existing delivery"
    assert list((workspace / "delivery").iterdir()) == [sentinel]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()

    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["lifecycle"]["exit_code"] == int(exit_code)
    assert manifest["stages"]["publishing"]["status"] == "interrupted"
    assert manifest["delivery"]["publication_state"] == "rolled_back"


@pytest.mark.parametrize(
    ("interruption_signal", "exit_code_value"),
    [
        (InterruptionSignal.SIGINT, 130),
        (InterruptionSignal.SIGTERM, 143),
    ],
)
def test_signal_during_run_id_generation_enters_the_typed_lifecycle(
    tmp_path,
    interruption_signal,
    exit_code_value,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interruption_signal=interruption_signal,
        interrupt_during_run_id_factory=True,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert int(outcome.exit_code) == exit_code_value
    assert outcome.interruption_signal is interruption_signal
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()

    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["stages"]["preflight"]["status"] == "interrupted"


@pytest.mark.parametrize(
    ("interruption_signal", "exit_code_value"),
    [
        (InterruptionSignal.SIGINT, 130),
        (InterruptionSignal.SIGTERM, 143),
    ],
)
def test_signal_during_workspace_open_enters_the_typed_lifecycle(
    tmp_path,
    interruption_signal,
    exit_code_value,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interruption_signal=interruption_signal,
        interrupt_during_workspace_open=True,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert int(outcome.exit_code) == exit_code_value
    assert outcome.interruption_signal is interruption_signal
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


@pytest.mark.parametrize(
    ("interruption_signal", "exit_code_value"),
    [
        (InterruptionSignal.SIGINT, 130),
        (InterruptionSignal.SIGTERM, 143),
    ],
)
def test_signal_during_missing_source_open_remains_an_auditable_interruption(
    tmp_path,
    interruption_signal,
    exit_code_value,
):
    source = tmp_path / "missing.mp4"
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interruption_signal=interruption_signal,
        interrupt_during_workspace_open=True,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert int(outcome.exit_code) == exit_code_value
    assert outcome.primary_error is None
    assert outcome.primary_error_code is None
    assert outcome.interruption_signal is interruption_signal
    assert outcome.diagnostics_incomplete is False
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["lifecycle"]["exit_code"] == exit_code_value
    assert manifest["stages"]["initialized"]["status"] == "interrupted"
    assert manifest["errors"]["primary_error"] == {
        "status": "not_applicable"
    }


def test_signal_arriving_during_startup_failure_cleanup_wins_the_terminal_race(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "missing.mp4"
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application()
    signal_sent = False

    def interrupt_then_cleanup(run_workspace):
        nonlocal signal_sent
        if not signal_sent:
            signal_sent = True
            os.kill(os.getpid(), signal.SIGINT)
        run_workspace.cleanup()

    monkeypatch.setattr(
        application,
        "_cleanup_run",
        interrupt_then_cleanup,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGINT
    assert outcome.primary_error is None
    assert outcome.interruption_signal is InterruptionSignal.SIGINT
    assert outcome.diagnostics_incomplete is False
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["stages"]["initialized"]["status"] == "interrupted"


def test_startup_interruption_records_a_cleanup_failure_as_associated(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "missing.mp4"
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application()
    signal_sent = False

    def interrupt_then_fail_cleanup(run_workspace):
        nonlocal signal_sent
        del run_workspace
        if not signal_sent:
            signal_sent = True
            os.kill(os.getpid(), signal.SIGINT)
        raise WorkspaceFailure(
            ErrorCode.WORKSPACE_CLEANUP_FAILED,
            {
                "operation": "workspace.cleanup",
                "reason_code": "workspace.directory_sync_failed",
            },
        )

    monkeypatch.setattr(
        application,
        "_cleanup_run",
        interrupt_then_fail_cleanup,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGINT
    assert outcome.primary_error is None
    assert len(outcome.associated_errors) == 1
    assert (
        outcome.associated_errors[0].error_code
        is ErrorCode.WORKSPACE_CLEANUP_FAILED
    )
    assert outcome.recovery_incomplete is True
    assert outcome.diagnostics_incomplete is False


def test_signal_inside_publication_critical_section_observes_commit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_exchange = workspace_effects._exchange_publication_directories
    exchange_count = 0

    def exchange_after_signal(*args):
        nonlocal exchange_count
        original_exchange(*args)
        exchange_count += 1
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        exchange_after_signal,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert exchange_count == 1
    assert (workspace / "delivery" / "manifest.json").is_file()
    events = [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "events.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    committed_index = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "delivery.state_changed"
        and event["attributes"] == {
            "phase": "publication",
            "state": "committed",
        }
    )
    interruption_index = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "interruption.requested"
    )
    assert committed_index < interruption_index


@pytest.mark.parametrize(
    ("signal_number", "expected_signal", "expected_exit_code"),
    [
        (
            signal.SIGINT,
            InterruptionSignal.SIGINT,
            ExitCode.SIGINT,
        ),
        (
            signal.SIGTERM,
            InterruptionSignal.SIGTERM,
            ExitCode.SIGTERM,
        ),
    ],
)
def test_signal_after_overwrite_backup_rolls_back_before_commit(
    tmp_path,
    monkeypatch,
    signal_number,
    expected_signal,
    expected_exit_code,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    current = workspace / "delivery" / "current.txt"
    previous = workspace / "delivery.previous" / "previous.txt"
    current.write_bytes(b"current delivery")
    previous.write_bytes(b"previous delivery")
    original_exchange = workspace_effects._exchange_publication_directories
    exchange_count = 0

    def signal_after_backup(*args):
        nonlocal exchange_count
        original_exchange(*args)
        exchange_count += 1
        if exchange_count == 1:
            os.kill(os.getpid(), signal_number)

    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        signal_after_backup,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(
            source,
            workspace_dir=workspace,
            overwrite=True,
        )
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is expected_exit_code
    assert outcome.interruption_signal is expected_signal
    assert exchange_count == 2
    assert current.read_bytes() == b"current delivery"
    assert previous.read_bytes() == b"previous delivery"
    assert sorted(path.name for path in (workspace / "delivery").iterdir()) == [
        "current.txt"
    ]
    assert sorted(
        path.name for path in (workspace / "delivery.previous").iterdir()
    ) == ["previous.txt"]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


def test_parent_sync_failure_rolls_back_before_publication_commit(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_sync = workspace_effects._sync_publication_directory
    sync_count = 0

    def fail_first_sync(descriptor):
        nonlocal sync_count
        sync_count += 1
        if sync_count == 1:
            raise OSError("deterministic directory sync failure")
        original_sync(descriptor)

    monkeypatch.setattr(
        workspace_effects,
        "_sync_publication_directory",
        fail_first_sync,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PUBLICATION_FAILED
    assert outcome.primary_error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
    assert sync_count >= 3
    assert not any((workspace / "delivery").iterdir())
    assert not any((workspace / "delivery.previous").iterdir())


def test_signal_during_terminal_manifest_marks_diagnostics_incomplete(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_publish = ManagedPathCapability.publish_bytes_atomically
    signal_sent = False

    def publish_after_signal(path, contents):
        nonlocal signal_sent
        if b'"schema_version":"run_manifest.v1"' in bytes(contents):
            signal_sent = True
            os.kill(os.getpid(), signal.SIGTERM)
        return original_publish(path, contents)

    monkeypatch.setattr(
        ManagedPathCapability,
        "publish_bytes_atomically",
        publish_after_signal,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert signal_sent is True
    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.diagnostics_incomplete is True
    events = (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "events.jsonl"
    ).read_text()
    assert '"event_code":"interruption.requested"' not in events


def test_interruption_after_publication_commit_keeps_success(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interrupt_after_commit=True,
        interruption_signal=InterruptionSignal.SIGTERM,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.interruption_signal is None
    assert outcome.primary_error is None
    assert outcome.diagnostics_incomplete is False
    events = [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "events.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    committed_index = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "delivery.state_changed"
        and event["attributes"] == {
            "phase": "publication",
            "state": "committed",
        }
    )
    interruption_index = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "interruption.requested"
        and event["attributes"]["signal"] == "sigterm"
    )
    assert committed_index < interruption_index
    assert (workspace / "delivery" / "manifest.json").is_file()


def test_retry_cache_recovery_and_subtitle_work_remain_events(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        emit_non_stage_events=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    events = [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "events.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    assert [
        event["stage"]
        for event in events
        if event["event_code"] == "stage.started"
    ] == EXPECTED_STAGES
    assert any(
        event["event_code"] == "cache.observed"
        and event["attributes"]["outcome"] == "miss"
        and event["stage"] == "transcription"
        for event in events
    )
    execution_events = [
        event
        for event in events
        if event["event_code"] == "transcription.execution_observed"
    ]
    assert [event["attributes"] for event in execution_events] == [
        {"recovery_count": 1, "retry_count": 1}
    ]
    assert not any(
        event["event_code"] in {
            "retry.scheduled",
            "notice.recorded",
        }
        and event["stage"] == "transcription"
        for event in events
    )
    subtitle_started = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "operation.started"
        and event["attributes"]["operation_kind"] == "subtitle_window"
    )
    delivery_started = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "stage.started"
        and event["stage"] == "delivery_build"
    )
    delivery_completed = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "stage.completed"
        and event["stage"] == "delivery_build"
    )
    assert delivery_started < subtitle_started < delivery_completed
    assert events[subtitle_started]["stage"] == "delivery_build"
    assert events[subtitle_started]["module"] == "subtitle_optimization"
    subtitle_operation_id = events[subtitle_started]["operation_id"]
    subtitle_completed = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "operation.completed"
        and event.get("operation_id") == subtitle_operation_id
    )
    assert subtitle_started < subtitle_completed < delivery_completed
    assert events[subtitle_completed]["attributes"] == {
        "attempt_count": 1,
        "duration_ms": events[subtitle_completed]["attributes"][
            "duration_ms"
        ],
        "operation_kind": "subtitle_window",
        "outcome": "succeeded",
    }


def test_cleanup_failure_is_associated_after_primary_stage_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        failure_stage=RunStage.TOPIC_REVIEW,
        cleanup_failure=True,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.EXTERNAL_SERVICE_FAILED
    assert outcome.primary_error_code is ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID
    assert len(outcome.associated_errors) == 1
    assert (
        outcome.associated_errors[0].error_code
        is ErrorCode.WORKSPACE_CLEANUP_FAILED
    )
    assert (
        outcome.associated_errors[0].event_sequence
        > outcome.primary_error.event_sequence
    )
    assert outcome.recovery_incomplete is True
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


def test_cleanup_failure_does_not_replace_interruption_exit_code(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interruption_stage=RunStage.PUBLISHING,
        interruption_signal=InterruptionSignal.SIGINT,
        cleanup_failure=True,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGINT
    assert outcome.primary_error is None
    assert len(outcome.associated_errors) == 1
    assert (
        outcome.associated_errors[0].error_code
        is ErrorCode.WORKSPACE_CLEANUP_FAILED
    )
    assert outcome.recovery_incomplete is True


def test_precommit_diagnostics_failure_returns_typed_local_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        diagnostics_failure="precommit"
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.LOCAL_PROCESSING_FAILED
    assert outcome.primary_error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED
    assert outcome.primary_error is None
    assert outcome.diagnostics_incomplete is True
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    assert not any((workspace / "delivery").iterdir())


def test_postcommit_diagnostics_failure_does_not_revoke_success(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        diagnostics_failure="postcommit"
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.primary_error is None
    assert outcome.diagnostics_incomplete is True
    assert (workspace / "delivery" / "manifest.json").is_file()


def test_terminal_manifest_failure_preserves_primary_stage_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        failure_stage=RunStage.SOURCE_ANALYSIS,
        diagnostics_failure="finalization",
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_MEDIA_INVALID
    assert outcome.diagnostics_incomplete is True
    assert not (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).exists()


def test_terminal_manifest_failure_preserves_interruption(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        interruption_stage=RunStage.CANDIDATE_PLANNING,
        interruption_signal=InterruptionSignal.SIGINT,
        diagnostics_failure="finalization",
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGINT
    assert outcome.interruption_signal is InterruptionSignal.SIGINT
    assert outcome.diagnostics_incomplete is True
    assert not (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).exists()


def test_postcommit_cleanup_failure_does_not_revoke_success(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        cleanup_failure=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.recovery_incomplete is True
    assert outcome.diagnostics_incomplete is True
    assert (workspace / "delivery" / "manifest.json").is_file()
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    assert not (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).exists()


def test_postcommit_cleanup_residue_is_reported_without_revoking_success(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        cleanup_failure_before_delete=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.recovery_incomplete is True
    assert outcome.diagnostics_incomplete is True
    assert (workspace / "delivery" / "manifest.json").is_file()
    assert (workspace / "work" / "tmp" / str(outcome.run_id)).is_dir()
    assert not (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).exists()


def test_lease_close_failure_is_reported_without_overriding_success(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_close = RunWorkspace.close
    close_calls = 0

    def fail_first_close(run_workspace):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise OSError("secret close detail")
        original_close(run_workspace)

    monkeypatch.setattr(RunWorkspace, "close", fail_first_close)

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.recovery_incomplete is True
    assert outcome.diagnostics_incomplete is True
    assert close_calls == 2


@pytest.mark.parametrize(
    "stage",
    [
        RunStage.DELIVERY_BUILD,
        RunStage.DELIVERY_VERIFICATION,
        RunStage.PUBLISHING,
    ],
)
def test_invalid_delivery_stage_result_is_a_typed_internal_failure(
    tmp_path,
    stage,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        invalid_result_stage=stage
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INTERNAL_ERROR
    assert outcome.primary_error_code is ErrorCode.INTERNAL_UNEXPECTED
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


@pytest.mark.parametrize(
    "stage",
    [RunStage.CANDIDATE_PLANNING, RunStage.PUBLISHING],
)
def test_invalid_work_item_count_is_a_typed_internal_failure(
    tmp_path,
    stage,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        invalid_work_count_stage=stage
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INTERNAL_ERROR
    assert outcome.primary_error_code is ErrorCode.INTERNAL_UNEXPECTED
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


def test_transcription_work_item_count_injection_is_rejected():
    with pytest.raises(ValueError, match="转写工作项数量由应用"):
        compose_deterministic_live_application(
            invalid_work_count_stage=RunStage.TRANSCRIPTION
        )


def test_verification_rejects_manifest_result_kind_derived_from_untrusted_content(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_read = ManagedPathCapability.read_bytes

    def substitute_result_kind(path):
        contents = original_read(path)
        if b'"schema_version":"deterministic.v1"' not in contents:
            return contents
        manifest = json.loads(contents)
        manifest["result_kind"] = "empty"
        manifest["speech_presence"] = "absent"
        manifest["transcript_chunk_ids"] = []
        return (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        substitute_result_kind,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.DELIVERY_VERIFICATION_FAILED
    )


def test_verification_rejects_transcript_ids_not_created_by_this_run(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_read = ManagedPathCapability.read_bytes
    forged_transcript_id = TranscriptId.new()
    forged_chunk_id = TranscriptChunkId.new()

    def substitute_transcript_ids(path):
        contents = original_read(path)
        if b'"schema_version":"deterministic.v1"' not in contents:
            return contents
        manifest = json.loads(contents)
        manifest["transcript_id"] = str(forged_transcript_id)
        manifest["transcript_chunk_ids"] = [str(forged_chunk_id)]
        return (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        substitute_transcript_ids,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.DELIVERY_VERIFICATION_FAILED
    )


def test_publication_rejects_manifest_snapshot_changed_after_verification(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_read = ManagedPathCapability.read_bytes
    forged_transcript_id = TranscriptId.new()
    forged_chunk_id = TranscriptChunkId.new()
    manifest_read_count = 0

    def substitute_after_verification(path):
        nonlocal manifest_read_count
        contents = original_read(path)
        if b'"schema_version":"deterministic.v1"' not in contents:
            return contents
        manifest_read_count += 1
        if manifest_read_count == 1:
            return contents
        manifest = json.loads(contents)
        manifest["transcript_id"] = str(forged_transcript_id)
        manifest["transcript_chunk_ids"] = [str(forged_chunk_id)]
        return (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        substitute_after_verification,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PUBLICATION_FAILED
    assert outcome.primary_error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
    assert outcome.primary_error is not None
    assert outcome.primary_error.diagnostics == {
        "operation": "publication.verify_snapshot",
        "reason_code": "publication.snapshot_changed",
    }
    assert manifest_read_count == 2
    assert not any((workspace / "delivery").iterdir())


def test_verification_reports_manifest_read_failure_as_stable_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_read = ManagedPathCapability.read_bytes

    def fail_manifest_read(path):
        contents = original_read(path)
        if b'"schema_version":"deterministic.v1"' in contents:
            raise OSError("secret manifest read failure")
        return contents

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        fail_manifest_read,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.DELIVERY_VERIFICATION_FAILED
    )


@pytest.mark.parametrize(
    "corruption",
    ("duplicate_field", "truncated"),
)
def test_verification_reports_malformed_manifest_as_stable_failure(
    tmp_path,
    monkeypatch,
    corruption,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_read = ManagedPathCapability.read_bytes

    def corrupt_manifest(path):
        contents = original_read(path)
        if b'"schema_version":"deterministic.v1"' not in contents:
            return contents
        if corruption == "truncated":
            return b'{"schema_version":"deterministic.v1"'
        manifest = json.loads(contents)
        duplicate = (
            '"run_id":'
            + json.dumps(manifest["run_id"])
            + ","
        ).encode("utf-8")
        return b"{" + duplicate + contents[1:]

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        corrupt_manifest,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.DELIVERY_VERIFICATION_FAILED
    )


def test_postcommit_control_failure_does_not_revoke_success(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        postcommit_control_failure=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert (workspace / "delivery" / "manifest.json").is_file()


def test_commit_proof_capture_failure_prevents_physical_publication(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"

    def fail_capture(commit_state, delivery):
        del commit_state, delivery
        raise RuntimeError("deterministic capture failure")

    monkeypatch.setattr(_CommitState, "capture", fail_capture)

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INTERNAL_ERROR
    assert outcome.primary_error_code is ErrorCode.INTERNAL_UNEXPECTED
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


def test_physical_publication_failure_discards_reserved_commit_proof(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    exchange_attempts = 0

    def fail_final_publication(*_args):
        nonlocal exchange_attempts
        exchange_attempts += 1
        raise OSError("deterministic publication failure")

    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        fail_final_publication,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PUBLICATION_FAILED
    assert outcome.primary_error_code is ErrorCode.PUBLICATION_COMMIT_FAILED
    assert exchange_attempts == 1
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()


def test_publication_rollback_failure_marks_recovery_incomplete(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_inspection = workspace_effects._inspect_layout_descriptor
    original_sync = workspace_effects._sync_publication_directory
    original_exchange = workspace_effects._exchange_publication_directories
    exchanged = False
    sync_count = 0

    def fail_commit_inspection(*args, **kwargs):
        if exchanged and kwargs.get("expected") is None:
            raise OSError("deterministic commit inspection failure")
        return original_inspection(*args, **kwargs)

    def observe_exchange(*args):
        nonlocal exchanged
        original_exchange(*args)
        exchanged = True

    def fail_rollback_sync(descriptor):
        nonlocal sync_count
        sync_count += 1
        if sync_count == 3:
            raise OSError("deterministic rollback sync failure")
        original_sync(descriptor)

    monkeypatch.setattr(
        workspace_effects,
        "_inspect_layout_descriptor",
        fail_commit_inspection,
    )
    monkeypatch.setattr(
        workspace_effects,
        "_sync_publication_directory",
        fail_rollback_sync,
    )
    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        observe_exchange,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.primary_error_code is ErrorCode.PUBLICATION_ROLLBACK_FAILED
    assert outcome.recovery_incomplete is True
    assert not any((workspace / "delivery").iterdir())
    assert not any((workspace / "delivery.previous").iterdir())
    assert len(outcome.associated_errors) == 1
    assert (
        outcome.associated_errors[0].error_code
        is ErrorCode.WORKSPACE_CLEANUP_FAILED
    )
    interrupted_temporary = (
        workspace / "work" / "tmp" / str(outcome.run_id)
    )
    assert (
        interrupted_temporary / ".publication-transaction.json"
    ).is_file()

    monkeypatch.undo()
    recovered = Workspace.open(source, workspace)
    with recovered.acquire_run(RunId.new()):
        pass
    assert not interrupted_temporary.exists()


def test_signal_precedes_failure_inside_publication_critical_section(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    exchange_attempts = 0

    def interrupt_then_fail_final_publication(*_args):
        nonlocal exchange_attempts
        exchange_attempts += 1
        os.kill(os.getpid(), signal.SIGTERM)
        raise OSError("failure after pending interruption")

    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        interrupt_then_fail_final_publication,
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGTERM
    assert outcome.interruption_signal is InterruptionSignal.SIGTERM
    assert outcome.primary_error is None
    assert exchange_attempts == 1
    assert not any((workspace / "delivery").iterdir())
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()

    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["lifecycle"]["outcome"] == "interrupted"
    assert manifest["stages"]["publishing"]["status"] == "interrupted"
    assert manifest["delivery"]["publication_state"] == "rolled_back"


def test_postcommit_cancellation_exception_does_not_revoke_success(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        postcommit_cancellation=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.diagnostics_incomplete is True
    assert (workspace / "delivery" / "manifest.json").is_file()


def test_postcommit_module_failure_does_not_revoke_success(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        postcommit_effect_failure=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.result_kind is ResultKind.CLIPS
    assert outcome.diagnostics_incomplete is True
    assert (workspace / "delivery" / "manifest.json").is_file()


def test_unexpected_exception_is_redacted_to_stable_internal_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        unexpected_stage=RunStage.CANDIDATE_PLANNING
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INTERNAL_ERROR
    assert outcome.primary_error_code is ErrorCode.INTERNAL_UNEXPECTED
    assert outcome.primary_error is not None
    assert set(outcome.primary_error.diagnostics) == {
        "function",
        "line",
        "source_module",
    }
    run_directory = (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
    )
    diagnostic_bytes = b"".join(
        path.read_bytes()
        for path in sorted(run_directory.iterdir())
        if path.is_file()
    )
    assert b"secret exception detail" not in diagnostic_bytes


def test_workspace_startup_input_failure_forms_an_auditable_run(tmp_path):
    source = tmp_path / "missing.mp4"
    workspace = tmp_path / "workspace"

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_MISSING
    assert outcome.primary_error is not None
    assert outcome.primary_error.diagnostics == {
        "reason_code": "input.not_found"
    }
    assert outcome.associated_errors == ()
    assert outcome.diagnostics_incomplete is False
    run_directory = (
        workspace / "work" / "runs" / str(outcome.run_id)
    )
    manifest = json.loads((run_directory / "run.json").read_text())
    assert manifest["lifecycle"]["outcome"] == "failed"
    assert manifest["lifecycle"]["exit_code"] == 20
    assert manifest["errors"]["primary_error"]["error_code"] == (
        "input.missing"
    )
    assert manifest["stages"]["initialized"]["status"] == "failed"
    for stage in EXPECTED_STAGES:
        assert manifest["stages"][stage] == {"status": "not_started"}
    assert b"external_request.completed" not in (
        run_directory / "events.jsonl"
    ).read_bytes()


@pytest.mark.parametrize(
    "cleanup_options",
    [
        {"cleanup_failure": True},
        {"cleanup_failure_before_delete": True},
    ],
)
def test_startup_input_error_precedes_its_cleanup_error(
    tmp_path,
    cleanup_options,
):
    source = tmp_path / "missing.mp4"
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        **cleanup_options,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_MISSING
    assert outcome.primary_error is not None
    assert len(outcome.associated_errors) == 1
    cleanup_error = outcome.associated_errors[0]
    assert cleanup_error.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
    assert outcome.primary_error.event_sequence < cleanup_error.event_sequence
    assert outcome.recovery_incomplete is True
    assert outcome.diagnostics_incomplete is False
    assert (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).is_file()


def test_missing_source_uses_the_default_workspace_for_its_audit(tmp_path):
    source = tmp_path / "missing.mp4"

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source)
    )

    workspace = tmp_path / "missing.autocut"
    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_MISSING
    assert outcome.diagnostics_incomplete is False
    assert (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).is_file()


def test_non_regular_source_is_an_auditable_input_failure(tmp_path):
    source = tmp_path / "course.mp4"
    source.mkdir()
    workspace = tmp_path / "workspace"

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_UNREADABLE
    assert outcome.primary_error is not None
    assert outcome.primary_error.diagnostics == {
        "reason_code": "input.not_regular_file"
    }
    assert outcome.diagnostics_incomplete is False
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["errors"]["primary_error"]["error_code"] == (
        "input.unreadable"
    )
    assert manifest["lifecycle"]["exit_code"] == 20


def test_self_referential_source_link_uses_a_separate_default_audit_workspace(
    tmp_path,
):
    source = tmp_path / "loop.mp4"
    source.symlink_to(source.name)

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source)
    )

    workspace = tmp_path / "loop.autocut"
    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_UNREADABLE
    assert outcome.diagnostics_incomplete is False
    assert source.is_symlink()
    assert (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).is_file()


def test_invalid_autocut_directory_is_not_initialized_as_its_own_audit(
    tmp_path,
):
    source = tmp_path / "bad.autocut"
    source.mkdir()

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source)
    )

    workspace = tmp_path / "bad.autocut.autocut"
    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_UNREADABLE
    assert outcome.diagnostics_incomplete is False
    assert list(source.iterdir()) == []
    assert (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    ).is_file()


def test_explicit_audit_workspace_cannot_replace_the_missing_source(tmp_path):
    source = tmp_path / "missing.mp4"

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=source)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PREFLIGHT_FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert outcome.diagnostics_incomplete is True
    assert not source.exists()


def test_subtitle_optimization_failure_terminates_delivery_build_stage(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        subtitle_failure=True
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.EXTERNAL_SERVICE_FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID
    )
    assert outcome.primary_error is not None
    assert outcome.primary_error.stage is RunStage.DELIVERY_BUILD
    assert outcome.primary_error.module.value == "subtitle_optimization"
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text()
    )
    assert manifest["stages"]["delivery_build"]["status"] == "failed"
    assert manifest["stages"]["delivery_verification"] == {
        "status": "not_started"
    }
    events = [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "events.jsonl"
        )
        .read_text()
        .splitlines()
    ]
    subtitle_events = [
        event
        for event in events
        if event["event_code"] in {
            "operation.started",
            "operation.completed",
        }
        and event["attributes"]["operation_kind"] == "subtitle_window"
    ]
    assert [
        (
            event["event_code"],
            event["stage"],
            event["module"],
            event["attributes"].get("outcome"),
        )
        for event in subtitle_events
    ] == [
        (
            "operation.started",
            "delivery_build",
            "subtitle_optimization",
            None,
        ),
        (
            "operation.completed",
            "delivery_build",
            "subtitle_optimization",
            "failed",
        ),
    ]
    assert manifest["delivery"]["build_state"] == "failed"
    assert not any((workspace / "delivery").iterdir())
