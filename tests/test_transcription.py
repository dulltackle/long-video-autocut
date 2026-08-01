import inspect
import os
import signal
import socket
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from video_auto_editor.cache import CacheRepository
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    RemoteRequestId,
)
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    CharacterSpan,
    ExecutionFacts,
    ReadinessIssue,
    ReadinessReport,
    SpeechPresence,
    SpeechRecognition,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRemoteRequestEvent,
    TranscriptionRemoteRequestEventKind,
    TranscriptionRequest,
    TranscriptionResult,
)
from video_auto_editor.transcription.deterministic import (
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
)
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    Workspace,
)


def _transcription_request(tmp_path):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"verified source")
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=len(b"verified source"),
        duration_ms=1_000,
    )
    return (
        TranscriptionRequest(
            source=source,
            temporary_workspace=run_workspace.temporary,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        ),
        run_workspace,
    )


def test_speech_recognition_interface_exposes_only_stage_inputs_and_neutral_facts():
    assert set(TranscriptionRequest.__dataclass_fields__) == {
        "cancellation",
        "source",
        "temporary_workspace",
    }
    assert set(TranscriptionResult.__dataclass_fields__) == {
        "chunks",
        "execution_facts",
        "speech_presence",
    }
    assert set(TranscriptionRemoteRequestEvent.__dataclass_fields__) == {
        "correlation_id",
        "kind",
        "reason_code",
        "remote_request_id",
        "retry_delay_ms",
        "transport_attempt_count",
    }
    assert list(
        inspect.signature(SpeechRecognition.check_readiness).parameters
    ) == ["self"]
    assert list(
        inspect.signature(SpeechRecognition.transcribe).parameters
    ) == ["self", "request"]
    forbidden_details = {
        "adapter",
        "attempt",
        "cache_path",
        "endpoint",
        "model",
        "provider",
        "raw_response",
        "shard",
    }
    assert forbidden_details.isdisjoint(
        TranscriptionRequest.__dataclass_fields__
    )
    assert forbidden_details.isdisjoint(
        TranscriptionResult.__dataclass_fields__
    )
    assert forbidden_details.isdisjoint(
        TranscriptionRemoteRequestEvent.__dataclass_fields__
    )


@pytest.mark.parametrize(
    "event",
    [
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.STARTED,
            correlation_id=OperationId.new(),
            transport_attempt_count=0,
        ),
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            correlation_id=OperationId.new(),
            transport_attempt_count=1,
            remote_request_id=RemoteRequestId.from_adapter("request-1"),
            reason_code="service.server_error",
        ),
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            correlation_id=OperationId.new(),
            transport_attempt_count=2,
            reason_code="timeout.read",
            retry_delay_ms=100,
        ),
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.SUCCEEDED,
            correlation_id=OperationId.new(),
            transport_attempt_count=3,
            remote_request_id=RemoteRequestId.from_adapter("request-1"),
        ),
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.FAILED,
            correlation_id=OperationId.new(),
            transport_attempt_count=3,
            remote_request_id=RemoteRequestId.from_adapter("request-1"),
            reason_code="output.incomplete",
        ),
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.INTERRUPTED,
            correlation_id=OperationId.new(),
            transport_attempt_count=1,
            remote_request_id=RemoteRequestId.from_adapter("request-1"),
            reason_code="cancellation.root_requested",
        ),
        TranscriptionRemoteRequestEvent(
            kind=(
                TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY
            ),
            correlation_id=OperationId.new(),
            transport_attempt_count=1,
            remote_request_id=RemoteRequestId.from_adapter("request-1"),
            reason_code="concurrency.primary_failed",
        ),
    ],
)
def test_remote_request_event_accepts_only_safe_closed_lifecycle(event):
    representation = repr(event)
    assert "request-1" not in representation
    assert "sha256:" not in representation
    if event.remote_request_id is not None:
        assert str(event.remote_request_id).startswith("sha256:")


@pytest.mark.parametrize(
    "attributes",
    [
        {
            "kind": TranscriptionRemoteRequestEventKind.STARTED,
            "transport_attempt_count": 1,
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.SUCCEEDED,
            "transport_attempt_count": 0,
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.FAILED,
            "transport_attempt_count": 1,
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.SUCCEEDED,
            "transport_attempt_count": 1,
            "reason_code": "secret failure text",
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            "transport_attempt_count": 1,
            "reason_code": "timeout.read",
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            "transport_attempt_count": 4,
            "reason_code": "timeout.read",
        },
        {
            "kind": TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            "transport_attempt_count": 1,
            "remote_request_id": RemoteRequestId.from_adapter("request-2"),
            "reason_code": "timeout.read",
            "retry_delay_ms": 50,
        },
    ],
)
def test_remote_request_event_rejects_incoherent_or_unbounded_fields(
    attributes,
):
    with pytest.raises((TypeError, ValueError)):
        TranscriptionRemoteRequestEvent(
            correlation_id=OperationId.new(),
            **attributes,
        )


def test_readiness_report_contains_only_frozen_stable_issues():
    source_diagnostics = {"capability": "transcription"}
    issue = ReadinessIssue(
        ErrorCode.CONFIG_CREDENTIAL_MISSING,
        source_diagnostics,
    )
    source_diagnostics["capability"] = "topic_review"

    report = ReadinessReport(ready=False, issues=(issue,))

    assert report.issues == (issue,)
    assert issue.error_code is ErrorCode.CONFIG_CREDENTIAL_MISSING
    assert issue.diagnostics == {"capability": "transcription"}
    with pytest.raises(TypeError):
        issue.diagnostics["capability"] = "topic_review"
    with pytest.raises(TypeError, match="ReadinessIssue"):
        ReadinessReport(ready=False, issues=(object(),))


@pytest.mark.parametrize(
    ("retry_count", "recovery_count"),
    [(1, 0), (0, 1)],
)
def test_whole_transcript_cache_hit_rejects_internal_work(
    retry_count,
    recovery_count,
):
    with pytest.raises(ValueError, match="整场转写缓存命中"):
        ExecutionFacts(
            cache_use=CacheUse.HIT,
            retry_count=retry_count,
            recovery_count=recovery_count,
        )


def test_transcription_failure_rejects_diagnostics_outside_error_registry():
    with pytest.raises(ValueError, match="缺少必需诊断字段"):
        TranscriptionFailure(
            ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.MISS
            ),
            diagnostics={"reason_code": "coverage.gap_remaining"},
        )


def test_transcription_failure_rejects_another_module_error_code():
    with pytest.raises(ValueError, match="不属于语音识别模块"):
        TranscriptionFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.MISS
            ),
        )


def test_transcription_failure_rejects_whole_transcript_cache_hit():
    with pytest.raises(ValueError, match="失败不得声明整场转写缓存命中"):
        TranscriptionFailure(
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.HIT
            ),
        )


def test_transcription_result_rejects_empty_text_blocks_when_speech_is_present():
    with pytest.raises(ValueError, match="确认存在语音"):
        TranscriptionResult(
            chunks=(),
            speech_presence=SpeechPresence.PRESENT,
            execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        )


@pytest.mark.parametrize(
    ("text", "expected_error", "message"),
    [
        (" \n ", ValueError, "正文不能为空"),
        (object(), TypeError, "正文必须是字符串"),
    ],
)
def test_transcription_chunk_rejects_invalid_supplier_text(
    text,
    expected_error,
    message,
):
    with pytest.raises(expected_error, match=message):
        TranscriptionChunk(start_ms=100, end_ms=200, text=text)


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "expected_error", "message"),
    [
        (-1, 200, ValueError, "开始时间不能为负数"),
        (200, 200, ValueError, "结束时间必须晚于开始时间"),
        (100.0, 200, TypeError, "开始时间必须是整数毫秒"),
    ],
)
def test_transcription_chunk_rejects_invalid_supplier_time(
    start_ms,
    end_ms,
    expected_error,
    message,
):
    with pytest.raises(expected_error, match=message):
        TranscriptionChunk(
            start_ms=start_ms,
            end_ms=end_ms,
            text="无效时间",
        )


@pytest.mark.parametrize(
    "character_spans",
    [
        (CharacterSpan(start_ms=100, end_ms=200),),
        (
            CharacterSpan(start_ms=100, end_ms=200),
            CharacterSpan(start_ms=200, end_ms=301),
        ),
    ],
)
def test_transcription_chunk_rejects_invalid_character_timing(
    character_spans,
):
    with pytest.raises(ValueError, match="逐字时间"):
        TranscriptionChunk(
            start_ms=100,
            end_ms=300,
            text="你🙂",
            character_spans=character_spans,
        )


@pytest.mark.parametrize(
    "reason_code",
    [
        "output.empty_with_speech",
        "output.text_invalid",
        "output.time_invalid",
        "output.overlap_text_mismatch",
        "output.char_timing_invalid",
        "output.out_of_bounds",
    ],
)
def test_invalid_supplier_output_has_stable_external_failure(reason_code):
    failure = TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={"reason_code": reason_code},
    )

    assert failure.category is ErrorCategory.EXTERNAL_SERVICE
    assert failure.diagnostics == {"reason_code": reason_code}
    assert failure.retryable_in_new_run is True
    assert not hasattr(failure, "chunks")
    assert not hasattr(failure, "partial_result")


@pytest.mark.parametrize(
    ("result", "failure", "readiness"),
    [
        (object(), None, ReadinessReport(ready=True)),
        (None, object(), ReadinessReport(ready=True)),
        (
            TranscriptionResult(
                chunks=(),
                speech_presence=SpeechPresence.ABSENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            ),
            None,
            object(),
        ),
    ],
)
def test_deterministic_script_rejects_untyped_fields(
    result,
    failure,
    readiness,
):
    with pytest.raises(TypeError):
        DeterministicTranscriptionScript(
            result=result,
            failure=failure,
            readiness=readiness,
        )


def test_deterministic_speech_recognition_replays_success_without_external_io(
    tmp_path,
    monkeypatch,
):
    request, run_workspace = _transcription_request(tmp_path)
    scripted_result = TranscriptionResult(
        chunks=(
            TranscriptionChunk(
                start_ms=100,
                end_ms=700,
                text="忠实转写文本",
            ),
        ),
        speech_presence=SpeechPresence.PRESENT,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
    )
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(scripted_result)
    )

    def reject_external_access(*args, **kwargs):
        del args, kwargs
        raise AssertionError("确定性语音识别不得访问外部能力")

    monkeypatch.setattr(socket.socket, "connect", reject_external_access)
    monkeypatch.setattr(subprocess, "Popen", reject_external_access)
    monkeypatch.setattr(subprocess, "run", reject_external_access)
    monkeypatch.setattr(os, "getenv", reject_external_access)
    monkeypatch.setattr(time, "time", reject_external_access)
    monkeypatch.setattr(time, "monotonic", reject_external_access)
    monkeypatch.setattr(Path, "read_bytes", reject_external_access)
    monkeypatch.setattr(
        ManagedDirectoryCapability,
        "location",
        reject_external_access,
    )
    monkeypatch.setattr(
        CacheRepository,
        "in_memory",
        classmethod(reject_external_access),
    )

    try:
        assert recognition.check_readiness().ready is True
        assert recognition.transcribe(request) is scripted_result
        assert not hasattr(scripted_result, "transcript_id")
        assert not hasattr(scripted_result.chunks[0], "transcript_chunk_id")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_preserves_faithful_unicode_transcript(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    faithful_text = " 嗯，e\u0301🙂 "
    character_spans = tuple(
        CharacterSpan(
            start_ms=100 + index * 100,
            end_ms=200 + index * 100,
        )
        for index in range(7)
    )
    scripted_result = TranscriptionResult(
        chunks=(
            TranscriptionChunk(
                start_ms=100,
                end_ms=800,
                text=faithful_text,
                character_spans=character_spans,
            ),
            TranscriptionChunk(
                start_ms=800,
                end_ms=1_000,
                text="啊，保持原样",
            ),
        ),
        speech_presence=SpeechPresence.PRESENT,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
    )
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(scripted_result)
    )

    try:
        result = recognition.transcribe(request)

        assert tuple(chunk.text for chunk in result.chunks) == (
            " 嗯，e\u0301🙂 ",
            "啊，保持原样",
        )
        assert len(faithful_text) == 7
        assert len(result.chunks[0].character_spans or ()) == 7
        assert result.chunks[1].character_spans is None
        assert tuple(
            (chunk.start_ms, chunk.end_ms) for chunk in result.chunks
        ) == ((100, 800), (800, 1_000))
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_raises_fresh_typed_failure_without_partial_result(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    facts = ExecutionFacts(
        cache_use=CacheUse.MISS,
        retry_count=2,
        recovery_count=1,
    )
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.fail(
            TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
                execution_facts=facts,
                diagnostics={"http_status": 503, "attempt": 3},
            )
        )
    )

    try:
        with pytest.raises(TranscriptionFailure) as first:
            recognition.transcribe(request)
        with pytest.raises(TranscriptionFailure) as second:
            recognition.transcribe(request)

        assert first.value is not second.value
        assert (
            first.value.error_code
            is ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
        )
        assert first.value.execution_facts is facts
        assert first.value.diagnostics == {
            "attempt": 3,
            "http_status": 503,
        }
        assert first.value.retryable_in_new_run is True
        assert not hasattr(first.value, "chunks")
        assert not hasattr(first.value, "partial_result")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_returns_confirmed_absence(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    scripted_result = TranscriptionResult(
        chunks=(),
        speech_presence=SpeechPresence.ABSENT,
        execution_facts=ExecutionFacts(cache_use=CacheUse.HIT),
    )
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(scripted_result)
    )

    try:
        result = recognition.transcribe(request)

        assert result is scripted_result
        assert result.speech_presence is SpeechPresence.ABSENT
        assert result.chunks == ()
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_rejects_out_of_bounds_supplier_output(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(
                    TranscriptionChunk(
                        start_ms=900,
                        end_ms=1_001,
                        text="越过素材末尾的供应商输出",
                    ),
                ),
                speech_presence=SpeechPresence.PRESENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            )
        )
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        assert (
            captured.value.error_code
            is ErrorCode.TRANSCRIPTION_OUTPUT_INVALID
        )
        assert captured.value.diagnostics == {
            "reason_code": "output.out_of_bounds"
        }
        assert not hasattr(captured.value, "chunks")
        assert not hasattr(captured.value, "partial_result")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_rejects_unordered_supplier_output(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(
                    TranscriptionChunk(
                        start_ms=500,
                        end_ms=700,
                        text="较晚返回的文本块",
                    ),
                    TranscriptionChunk(
                        start_ms=100,
                        end_ms=300,
                        text="较早返回的文本块",
                    ),
                ),
                speech_presence=SpeechPresence.PRESENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            )
        )
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        assert (
            captured.value.error_code
            is ErrorCode.TRANSCRIPTION_OUTPUT_INVALID
        )
        assert captured.value.diagnostics == {
            "reason_code": "output.time_invalid"
        }
        assert not hasattr(captured.value, "chunks")
        assert not hasattr(captured.value, "partial_result")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_deterministic_speech_recognition_propagates_cancellation_without_result(
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    request = replace(request, cancellation=cancellation.token)
    recognition = DeterministicSpeechRecognition(
        DeterministicTranscriptionScript.succeed(
            TranscriptionResult(
                chunks=(
                    TranscriptionChunk(
                        start_ms=0,
                        end_ms=500,
                        text="不得返回的部分转写",
                    ),
                ),
                speech_presence=SpeechPresence.PRESENT,
                execution_facts=ExecutionFacts(
                    cache_use=CacheUse.MISS
                ),
            )
        )
    )

    try:
        with pytest.raises(CancellationRequested) as captured:
            recognition.transcribe(request)

        assert captured.value.signal_number == signal.SIGTERM
        assert not hasattr(captured.value, "chunks")
        assert not hasattr(captured.value, "partial_result")
    finally:
        run_workspace.cleanup()
        run_workspace.close()
