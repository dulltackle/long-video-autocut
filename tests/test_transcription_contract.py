import json
import signal
from dataclasses import replace

import pytest

from video_auto_editor.cache import CacheRepository
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    ExecutionFacts,
    ReadinessIssue,
    SpeechPresence,
    SpeechRecognition,
    TranscriptionChunk,
    TranscriptionRequest,
    TranscriptionResult,
)
from video_auto_editor.transcription.deterministic import (
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
)
from video_auto_editor.transcription.stepaudio import (
    NormalizedPcmAudio,
    StepAudioSettings,
    StepAudioSpeechRecognition,
    StepAudioTransportRequest,
    StepAudioTransportResponse,
)
from video_auto_editor.workspace import Workspace


class _FakeAudioPreparer:
    def __init__(self, audio: NormalizedPcmAudio) -> None:
        self._audio = audio

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def prepare(
        self,
        request: TranscriptionRequest,
    ) -> NormalizedPcmAudio:
        del request
        return self._audio


class _FakeStepAudioTransport:
    def __init__(self, response: StepAudioTransportResponse) -> None:
        self._response = response

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(
        self,
        request: StepAudioTransportRequest,
        cancellation: object,
    ) -> StepAudioTransportResponse:
        del request, cancellation
        return self._response


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


def _strict_stepaudio_sse() -> bytes:
    deltas = (
        (100, 200, "忠"),
        (200, 300, "实"),
        (300, 400, "转"),
        (400, 500, "写"),
        (500, 600, "文"),
        (600, 700, "本"),
    )
    events = [
        {
            "type": "transcript.text.delta",
            "delta": text,
            "start_time": start_ms,
            "end_time": end_ms,
        }
        for start_ms, end_ms, text in deltas
    ]
    events.append(
        {
            "type": "transcript.text.done",
            "text": "忠实转写文本",
        }
    )
    lines = [
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    ]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _recognition(
    adapter_kind: str,
    *,
    scripted_result: TranscriptionResult,
    audio: NormalizedPcmAudio,
) -> SpeechRecognition:
    if adapter_kind == "deterministic":
        return DeterministicSpeechRecognition(
            DeterministicTranscriptionScript.succeed(scripted_result)
        )
    assert adapter_kind == "stepaudio"
    return StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint="https://stepaudio.example.test/v1/audio/transcriptions",
            model="stepaudio-2-mini",
            timeout_seconds=30,
            max_concurrency=1,
        ),
        credential="test-credential",
        cache_repository=CacheRepository.in_memory(
            application_version="contract-test"
        ),
        audio_preparer=_FakeAudioPreparer(audio),
        transport=_FakeStepAudioTransport(
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream; charset=utf-8",
                body=_strict_stepaudio_sse(),
            )
        ),
    )


@pytest.mark.parametrize("adapter_kind", ["deterministic", "stepaudio"])
def test_speech_recognition_contract_returns_complete_transcription(
    adapter_kind,
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    expected = TranscriptionResult(
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
    recognition = _recognition(
        adapter_kind,
        scripted_result=expected,
        audio=NormalizedPcmAudio(
            pcm=b"\xe8\x03" * 16_000,
            duration_ms=1_000,
        ),
    )

    try:
        result = recognition.transcribe(request)

        assert result.speech_presence is SpeechPresence.PRESENT
        assert tuple(
            (chunk.start_ms, chunk.end_ms, chunk.text)
            for chunk in result.chunks
        ) == ((100, 700, "忠实转写文本"),)
        assert result.execution_facts.cache_use is CacheUse.MISS
        assert not hasattr(result, "provider")
        assert not hasattr(result, "raw_response")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize("adapter_kind", ["deterministic", "stepaudio"])
def test_speech_recognition_contract_returns_confirmed_silence(
    adapter_kind,
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    expected = TranscriptionResult(
        chunks=(),
        speech_presence=SpeechPresence.ABSENT,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
    )
    recognition = _recognition(
        adapter_kind,
        scripted_result=expected,
        audio=NormalizedPcmAudio(
            pcm=b"\x00\x00" * 16_000,
            duration_ms=1_000,
        ),
    )

    try:
        result = recognition.transcribe(request)

        assert result.speech_presence is SpeechPresence.ABSENT
        assert result.chunks == ()
        assert result.execution_facts.cache_use is CacheUse.MISS
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize("adapter_kind", ["deterministic", "stepaudio"])
def test_speech_recognition_contract_propagates_cancellation_without_result(
    adapter_kind,
    tmp_path,
):
    request, run_workspace = _transcription_request(tmp_path)
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    request = replace(request, cancellation=cancellation.token)
    expected = TranscriptionResult(
        chunks=(
            TranscriptionChunk(
                start_ms=100,
                end_ms=700,
                text="不得返回的部分转写",
            ),
        ),
        speech_presence=SpeechPresence.PRESENT,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
    )
    recognition = _recognition(
        adapter_kind,
        scripted_result=expected,
        audio=NormalizedPcmAudio(
            pcm=b"\xe8\x03" * 16_000,
            duration_ms=1_000,
        ),
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
