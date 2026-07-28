import json
import signal

import pytest

from video_auto_editor.application import (
    LiveRunRequest,
    LiveRunState,
)
from video_auto_editor.application._deterministic import (
    compose_deterministic_live_application,
)
from video_auto_editor.diagnostics import (
    InterruptionSignal,
    ResultKind,
)
from video_auto_editor.runtime.errors import ErrorCode, ExitCode, RunStage
from video_auto_editor.runtime.identity import (
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.transcription import (
    CacheUse,
    DeterministicTranscriptionScript,
    ExecutionFacts,
    SpeechPresence,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionResult,
)
from video_auto_editor.workspace import Workspace


def _events(workspace, run_id):
    return [
        json.loads(line)
        for line in (
            workspace
            / "work"
            / "runs"
            / str(run_id)
            / "events.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_successful_scripted_transcription_completes_before_planning_and_refreshes_run_ids(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    script = DeterministicTranscriptionScript.succeed(
        TranscriptionResult(
            chunks=(
                TranscriptionChunk(
                    start_ms=50,
                    end_ms=400,
                    text="第一段忠实转写",
                ),
                TranscriptionChunk(
                    start_ms=500,
                    end_ms=950,
                    text="第二段忠实转写",
                ),
            ),
            speech_presence=SpeechPresence.PRESENT,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.HIT,
            ),
        )
    )
    observed_transcript_ids = []
    observed_chunk_ids = []

    for workspace_name in ("workspace-first", "workspace-replay"):
        workspace = tmp_path / workspace_name
        application = compose_deterministic_live_application(
            transcription_script=script,
        )

        outcome = application.execute(
            LiveRunRequest(source, workspace_dir=workspace)
        )

        assert outcome.state is LiveRunState.SUCCEEDED
        events = _events(workspace, outcome.run_id)
        transcription_started = [
            index
            for index, event in enumerate(events)
            if event["event_code"] == "stage.started"
            and event["stage"] == "transcription"
        ]
        transcription_completed = [
            index
            for index, event in enumerate(events)
            if event["event_code"] == "stage.completed"
            and event["stage"] == "transcription"
        ]
        candidate_planning_started = next(
            index
            for index, event in enumerate(events)
            if event["event_code"] == "stage.started"
            and event["stage"] == "candidate_planning"
        )
        assert len(transcription_started) == 1
        assert len(transcription_completed) == 1
        assert (
            transcription_started[0]
            < transcription_completed[0]
            < candidate_planning_started
        )
        assert events[transcription_completed[0]]["attributes"][
            "outcome"
        ] == "succeeded"
        assert events[transcription_completed[0]]["attributes"][
            "work_item_count"
        ] == 2

        delivery = json.loads(
            (workspace / "delivery" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        observed_transcript_ids.append(
            TranscriptId(delivery["transcript_id"])
        )
        observed_chunk_ids.append(
            tuple(
                TranscriptChunkId(value)
                for value in delivery["transcript_chunk_ids"]
            )
        )

    assert observed_transcript_ids[0] != observed_transcript_ids[1]
    assert set(observed_chunk_ids[0]).isdisjoint(observed_chunk_ids[1])
    assert [len(values) for values in observed_chunk_ids] == [2, 2]


def test_confirmed_absence_completes_transcription_as_zero_work_before_planning(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic silent source")
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        transcription_script=DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(),
                speech_presence=SpeechPresence.ABSENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            )
        ),
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.result_kind is ResultKind.EMPTY
    events = _events(workspace, outcome.run_id)
    transcription_completed = next(
        (index, event)
        for index, event in enumerate(events)
        if event["event_code"] == "stage.completed"
        and event["stage"] == "transcription"
    )
    candidate_planning_started = next(
        index
        for index, event in enumerate(events)
        if event["event_code"] == "stage.started"
        and event["stage"] == "candidate_planning"
    )
    assert transcription_completed[0] < candidate_planning_started
    assert transcription_completed[1]["attributes"][
        "work_item_count"
    ] == 0
    for stage in ("candidate_planning", "topic_review"):
        completed = next(
            event
            for event in events
            if event["event_code"] == "stage.completed"
            and event["stage"] == stage
        )
        assert completed["attributes"]["work_item_count"] == 0
    assert not any(
        event["event_code"] == "operation.started"
        and event["attributes"]["operation_kind"] == "subtitle_window"
        for event in events
    )
    delivery = json.loads(
        (workspace / "delivery" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert TranscriptId(delivery["transcript_id"])
    assert delivery["transcript_chunk_ids"] == []
    assert delivery["speech_presence"] == "absent"


def test_confirmed_absence_rejects_unreachable_subtitle_failure_injection():
    script = DeterministicTranscriptionScript.succeed(
        TranscriptionResult(
            chunks=(),
            speech_presence=SpeechPresence.ABSENT,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.MISS
            ),
        )
    )

    with pytest.raises(ValueError, match="有效空结果没有字幕优化工作项"):
        compose_deterministic_live_application(
            transcription_script=script,
            subtitle_failure=True,
        )


def test_typed_transcription_failure_stops_before_planning_and_new_delivery(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    sentinel = workspace / "delivery" / "existing.txt"
    sentinel.write_text("keep", encoding="utf-8")
    application = compose_deterministic_live_application(
        transcription_script=DeterministicTranscriptionScript.fail(
            TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS,
                    retry_count=2,
                    recovery_count=1,
                ),
                diagnostics={"attempt": 3, "http_status": 503},
            )
        )
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.EXTERNAL_SERVICE_FAILED
    assert (
        outcome.primary_error_code
        is ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
    )
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in (workspace / "delivery").iterdir()) == [
        "existing.txt"
    ]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    events = _events(workspace, outcome.run_id)
    assert not any(
        event["event_code"] == "stage.started"
        and event["stage"] == "candidate_planning"
        for event in events
    )
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["stages"]["transcription"]["status"] == "failed"
    assert manifest["stages"]["candidate_planning"] == {
        "status": "not_started"
    }
    assert manifest["retries_and_recovery"] == {
        "coverage_recovery": 1,
        "semantic_retry": 0,
        "transport_retry": 2,
    }
    assert not any(
        event["event_code"] == "notice.recorded"
        and event["attributes"]["kind"]
        in {
            "coverage_recovery_succeeded",
            "transport_retry_succeeded",
        }
        for event in events
    )
    execution_events = [
        event
        for event in events
        if event["event_code"] == "transcription.execution_observed"
    ]
    assert [event["attributes"] for event in execution_events] == [
        {"recovery_count": 1, "retry_count": 2}
    ]
    assert not any(
        event["event_code"] == "operation.started"
        and event["stage"] == "transcription"
        and event["attributes"]["operation_kind"]
        in {"coverage_recovery", "external_request"}
        for event in events
    )
    assert b"transcript_chunk_" not in (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "events.jsonl"
    ).read_bytes()


def test_transcription_cancellation_stops_before_planning_and_new_delivery(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    Workspace.open(source, workspace)
    sentinel = workspace / "delivery" / "existing.txt"
    sentinel.write_text("keep", encoding="utf-8")
    application = compose_deterministic_live_application(
        transcription_script=DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(
                    TranscriptionChunk(
                        start_ms=0,
                        end_ms=900,
                        text="不得形成标识的部分转写",
                    ),
                ),
                speech_presence=SpeechPresence.PRESENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            )
        ),
        interruption_stage=RunStage.TRANSCRIPTION,
        interruption_signal=InterruptionSignal.SIGTERM,
    )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGTERM
    assert outcome.primary_error is None
    assert outcome.interruption_signal is InterruptionSignal.SIGTERM
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in (workspace / "delivery").iterdir()) == [
        "existing.txt"
    ]
    assert not (workspace / "work" / "tmp" / str(outcome.run_id)).exists()
    events = _events(workspace, outcome.run_id)
    assert not any(
        event["event_code"] == "stage.started"
        and event["stage"] == "candidate_planning"
        for event in events
    )
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["stages"]["transcription"]["status"] == "interrupted"
    assert manifest["stages"]["candidate_planning"] == {
        "status": "not_started"
    }


def test_cancellation_during_transcript_id_projection_stops_before_planning(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    original_new = TranscriptId.new

    def interrupt_during_projection(cls):
        del cls
        transcript_id = original_new()
        signal.raise_signal(signal.SIGTERM)
        return transcript_id

    monkeypatch.setattr(
        TranscriptId,
        "new",
        classmethod(interrupt_during_projection),
    )

    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.INTERRUPTED
    assert outcome.exit_code is ExitCode.SIGTERM
    events = _events(workspace, outcome.run_id)
    transcription_completed = next(
        event
        for event in events
        if event["event_code"] == "stage.completed"
        and event["stage"] == "transcription"
    )
    assert transcription_completed["attributes"]["outcome"] == "interrupted"
    assert not any(
        event["event_code"] == "stage.started"
        and event["stage"] == "candidate_planning"
        for event in events
    )
    assert not any((workspace / "delivery").iterdir())
