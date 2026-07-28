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
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
    ExecutionFacts,
    ReadinessReport,
    SpeechPresence,
    SpeechRecognition,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
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
    monkeypatch.setattr(
        CacheRepository,
        "initialize",
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
