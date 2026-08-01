import base64
import json
import signal
import socket
from pathlib import Path
from threading import Barrier, Event, Lock, Thread, Timer
from time import sleep

import pytest

from video_auto_editor.cache import CacheFailure, CacheRepository
from video_auto_editor.diagnostics import DiagnosticsFailure
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode, RemoteRequestId
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    ReadinessIssue,
    SpeechPresence,
    TranscriptionFailure,
    TranscriptionRemoteRequestEvent,
    TranscriptionRemoteRequestEventKind,
    TranscriptionRequest,
)
from video_auto_editor.transcription._stepaudio_audio import (
    FFmpegNormalizedPcmPreparer,
)
from video_auto_editor.transcription._stepaudio_https import (
    StdlibStepAudioTransport,
)
from video_auto_editor.transcription.reconciliation import (
    RecognitionKind,
    RecognitionWork,
    TimeInterval,
)
from video_auto_editor.transcription.stepaudio import (
    NormalizedPcmAudio,
    StepAudioSettings,
    StepAudioSpeechRecognition,
    StepAudioTransportFailure,
    StepAudioTransportFailureKind,
    StepAudioTransportRequest,
    StepAudioTransportResponse,
    _shard_cache_spec,
    _StepAudioObservationSource,
)
from video_auto_editor.workspace import Workspace


class _FakeAudioPreparer:
    def __init__(
        self,
        *,
        readiness: tuple[ReadinessIssue, ...] = (),
        audio: NormalizedPcmAudio | None = None,
    ) -> None:
        self.readiness = readiness
        self.audio = audio
        self.readiness_calls = 0
        self.prepare_calls: list[TranscriptionRequest] = []

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        self.readiness_calls += 1
        return self.readiness

    def prepare(
        self,
        request: TranscriptionRequest,
    ) -> NormalizedPcmAudio:
        self.prepare_calls.append(request)
        if self.audio is None:
            raise AssertionError("准备检查不得准备业务音频")
        return self.audio


class _FakeTransport:
    def __init__(
        self,
        *,
        readiness: tuple[ReadinessIssue, ...] = (),
        response: StepAudioTransportResponse | None = None,
    ) -> None:
        self.readiness = readiness
        self.response = response
        self.readiness_calls = 0
        self.send_calls: list[
            tuple[StepAudioTransportRequest, object]
        ] = []

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        self.readiness_calls += 1
        return self.readiness

    def send(self, request, cancellation):
        self.send_calls.append((request, cancellation))
        if self.response is None:
            raise AssertionError("准备检查不得发送业务请求")
        return self.response


class _ScriptedTransport:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.send_calls: list[StepAudioTransportRequest] = []

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(self, request, cancellation):
        del cancellation
        self.send_calls.append(request)
        if not self._outcomes:
            raise AssertionError("发生了未编排的额外远程请求")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _RecordingRemoteRequestEventSink:
    def __init__(self) -> None:
        self.events: list[TranscriptionRemoteRequestEvent] = []

    def record(self, event: TranscriptionRemoteRequestEvent) -> None:
        self.events.append(event)


class _FailingRemoteRequestEventSink(_RecordingRemoteRequestEventSink):
    def __init__(
        self,
        target: TranscriptionRemoteRequestEventKind,
        failure: RuntimeError,
    ) -> None:
        super().__init__()
        self._target = target
        self._failure = failure

    def record(self, event: TranscriptionRemoteRequestEvent) -> None:
        super().record(event)
        if event.kind is self._target:
            raise self._failure


def _settings() -> StepAudioSettings:
    return StepAudioSettings(
        endpoint="https://stepaudio.example.test/v1/audio/asr/sse",
        model="stepaudio-2.5-asr",
        timeout_seconds=17,
        max_concurrency=1,
    )


def _successful_response(
    *,
    start_ms: int = 100,
    end_ms: int = 800,
    text: str = "faithful text",
) -> StepAudioTransportResponse:
    body = (
        "data: "
        + json.dumps(
            {
                "type": "transcript.text.delta",
                "delta": text,
                "start_time": start_ms,
                "end_time": end_ms,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n"
        + "data: "
        + json.dumps(
            {
                "type": "transcript.text.done",
                "text": text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\ndata: [DONE]\n\n"
    )
    return StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream; charset=utf-8",
        body=body.encode("utf-8"),
        remote_request_id="remote-request-canary",
    )


def _speech_audio(duration_ms: int = 1_000) -> NormalizedPcmAudio:
    return NormalizedPcmAudio(
        pcm=b"\xe8\x03" * (duration_ms * 16),
        duration_ms=duration_ms,
    )


def _cache_repository() -> CacheRepository:
    return CacheRepository.in_memory(application_version="test.v1")


def _transcription_request(
    tmp_path: Path,
    *,
    duration_ms: int = 1_000,
    label: str = "",
):
    source_path = (
        tmp_path / f"source-path-canary-do-not-send{label}.mp4"
    )
    source_bytes = b"source-file-canary-do-not-send"
    source_path.write_bytes(source_bytes)
    workspace = Workspace.open(
        source_path,
        tmp_path / f"workspace-path-canary-do-not-send{label}",
    )
    run_workspace = workspace.acquire_run(RunId.new())
    source_file = workspace.source
    assert source_file is not None
    source = SourceDescription._from_analysis(
        source_file=source_file,
        sha256="sha256:" + ("0" * 64),
        byte_length=len(source_bytes),
        duration_ms=duration_ms,
    )
    return (
        TranscriptionRequest(
            source=source,
            temporary_workspace=run_workspace.temporary,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        ),
        run_workspace,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        stat = path.stat()
        entries.append(
            (
                path.relative_to(root).as_posix(),
                path.is_dir(),
                stat.st_mode,
                stat.st_size,
                stat.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(entries)


def test_stepaudio_requires_explicit_production_effects():
    with pytest.raises(TypeError):
        StepAudioSpeechRecognition(
            _settings(),
            credential="credential-canary-must-not-leak",
            cache_repository=_cache_repository(),
        )


def test_stepaudio_readiness_aggregates_all_local_issues_repeatably_without_business_io(
    tmp_path,
):
    workspace = tmp_path / "untouched-workspace"
    workspace.mkdir()
    (workspace / "sentinel").write_bytes(b"workspace-canary")
    audio_issue = ReadinessIssue(
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        {"reason_code": "tool.missing"},
    )
    transport_issue = ReadinessIssue(
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
        {"reason_code": "tls.ca_store_unavailable"},
    )
    audio_preparer = _FakeAudioPreparer(readiness=(audio_issue,))
    transport = _FakeTransport(readiness=(transport_issue,))
    sink = _RecordingRemoteRequestEventSink()
    recognition = StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint="http://stepaudio.example.test/v1/audio/asr/sse",
            model=_settings().model,
            timeout_seconds=17,
            max_concurrency=1,
        ),
        credential="",
        cache_repository=_cache_repository(),
        audio_preparer=audio_preparer,
        transport=transport,
        event_sink=sink,
    )
    before = _tree_snapshot(workspace)

    first = recognition.check_readiness()
    second = recognition.check_readiness()

    credential_issue = ReadinessIssue(
        ErrorCode.CONFIG_CREDENTIAL_MISSING,
        {"capability": "transcription"},
    )
    https_issue = ReadinessIssue(
        ErrorCode.CONFIG_HTTPS_REQUIRED,
        {"field": "transcription_provider_config.endpoint"},
    )
    assert first.ready is False
    assert first.issues == (
        credential_issue,
        https_issue,
        audio_issue,
        transport_issue,
    )
    assert second == first
    assert audio_preparer.readiness_calls == 2
    assert transport.readiness_calls == 2
    assert audio_preparer.prepare_calls == []
    assert transport.send_calls == []
    assert sink.events == []
    assert _tree_snapshot(workspace) == before


@pytest.mark.parametrize(
    ("credential", "leak_fragment"),
    [
        ("credential\r\nX-Leaked: secret", "X-Leaked: secret"),
        ("credential\x00secret", "secret"),
        ("x" * 8_193, "x" * 128),
    ],
)
def test_stepaudio_rejects_unsafe_credential_header_before_business_io(
    tmp_path,
    credential,
    leak_fragment,
):
    audio_preparer = _FakeAudioPreparer(audio=_speech_audio())
    transport = _FakeTransport(response=_successful_response())
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential=credential,
        cache_repository=_cache_repository(),
        audio_preparer=audio_preparer,
        transport=transport,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        report = recognition.check_readiness()
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        expected_diagnostics = {
            "field": "transcription_provider_config.credential",
            "reason_code": "value.invalid_format",
        }
        assert report.ready is False
        assert report.issues == (
            ReadinessIssue(
                ErrorCode.CONFIG_VALUE_INVALID,
                expected_diagnostics,
            ),
        )
        assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
        assert captured.value.diagnostics == expected_diagnostics
        assert audio_preparer.prepare_calls == []
        assert transport.send_calls == []
        assert leak_fragment not in repr(report)
        assert leak_fragment not in repr(captured.value)
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_explicit_production_effects_readiness_is_local_and_repeatable(
    tmp_path,
    monkeypatch,
):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    untouched = tmp_path / "default-readiness-untouched"
    untouched.mkdir()
    (untouched / "sentinel").write_bytes(b"unchanged")
    tls_key_log = tmp_path / "tls-key-log-must-not-exist"
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("SSLKEYLOGFILE", str(tls_key_log))
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("准备检查不得建立网络连接")
        ),
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="",
        cache_repository=_cache_repository(),
        audio_preparer=FFmpegNormalizedPcmPreparer(),
        transport=StdlibStepAudioTransport(_settings().endpoint),
    )
    before = _tree_snapshot(untouched)

    first = recognition.check_readiness()
    second = recognition.check_readiness()

    codes = tuple(issue.error_code for issue in first.issues)
    assert first == second
    assert first.ready is False
    assert ErrorCode.CONFIG_CREDENTIAL_MISSING in codes
    assert ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE in codes
    assert _tree_snapshot(untouched) == before
    assert not tls_key_log.exists()


def test_stepaudio_success_sends_only_current_normalized_pcm_and_protocol_whitelist(
    tmp_path,
):
    pcm_canary = b"pcm-audio-canary-current-standard-pcm"
    pcm = (pcm_canary * (32_000 // len(pcm_canary) + 1))[:32_000]
    normalized_audio = NormalizedPcmAudio(pcm=pcm, duration_ms=1_000)
    audio_preparer = _FakeAudioPreparer(audio=normalized_audio)
    response_body = (
        b'data: {"type":"transcript.text.delta","delta":"faithful text",'
        b'"start_time":100,"end_time":800}\n\n'
        b'data: {"type":"transcript.text.done",'
        b'"text":"faithful text"}\n\n'
        b"data: [DONE]\n\n"
    )
    transport = _FakeTransport(
        response=StepAudioTransportResponse(
            status_code=200,
            content_type="text/event-stream; charset=utf-8",
            body=response_body,
            remote_request_id="remote-request-canary",
        )
    )
    credential = "credential-canary-must-not-leak"
    settings = _settings()
    recognition = StepAudioSpeechRecognition(
        settings,
        credential=credential,
        cache_repository=_cache_repository(),
        audio_preparer=audio_preparer,
        transport=transport,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        result = recognition.transcribe(request)

        assert result.speech_presence is SpeechPresence.PRESENT
        assert tuple(
            (chunk.start_ms, chunk.end_ms, chunk.text)
            for chunk in result.chunks
        ) == ((100, 800, "faithful text"),)
        assert audio_preparer.prepare_calls == [request]
        assert len(transport.send_calls) == 1
        transport_request, cancellation = transport.send_calls[0]
        assert isinstance(
            transport_request,
            StepAudioTransportRequest,
        )
        assert cancellation is request.cancellation
        assert transport_request.endpoint == settings.endpoint
        assert transport_request.credential == credential
        assert transport_request.timeout_seconds == settings.timeout_seconds

        encoded_pcm = base64.b64encode(pcm).decode("ascii")
        payload = json.loads(transport_request.body.decode("utf-8"))
        assert payload == {
            "audio": {
                "data": encoded_pcm,
                "input": {
                    "transcription": {
                        "language": "zh",
                        "model": "stepaudio-2.5-asr",
                        "enable_itn": True,
                        "enable_timestamp": True,
                    },
                    "format": {
                        "type": "pcm",
                        "codec": "pcm_s16le",
                        "rate": 16_000,
                        "bits": 16,
                        "channel": 1,
                    },
                },
            }
        }
        assert base64.b64decode(
            payload["audio"]["data"],
            validate=True,
        ) == normalized_audio.pcm

        request_repr = repr(transport_request)
        assert credential not in request_repr
        assert pcm_canary.decode("ascii") not in request_repr
        assert encoded_pcm not in request_repr
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_observes_one_safe_correlated_remote_request_on_success(
    tmp_path,
):
    sink = _RecordingRemoteRequestEventSink()
    remote_request_id = "remote-request-event-canary"
    response = _successful_response()
    response = StepAudioTransportResponse(
        status_code=response.status_code,
        content_type=response.content_type,
        body=response.body,
        remote_request_id=remote_request_id,
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential-canary-must-not-leak",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=_ScriptedTransport([response]),
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        recognition.transcribe(request)

        assert [event.kind for event in sink.events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.SUCCEEDED,
        ]
        assert isinstance(sink.events[0].correlation_id, OperationId)
        assert sink.events[1].correlation_id == sink.events[0].correlation_id
        assert [event.transport_attempt_count for event in sink.events] == [
            0,
            1,
        ]
        assert sink.events[0].remote_request_id is None
        assert sink.events[1].remote_request_id == RemoteRequestId.from_adapter(
            remote_request_id
        )
        rendered = repr(sink.events)
        assert remote_request_id not in rendered
        assert "credential-canary-must-not-leak" not in rendered
        assert str(request.source.source_file.path) not in rendered
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_observes_one_logical_request_across_transport_retry(
    tmp_path,
):
    sink = _RecordingRemoteRequestEventSink()
    failed_remote_id = "failed-remote-request-canary"
    successful_remote_id = "successful-remote-request-canary"
    success = _successful_response()

    class _RetryOrderTransport(_ScriptedTransport):
        def send(self, request, cancellation):
            if self.send_calls:
                assert (
                    sink.events[-1].kind
                    is TranscriptionRemoteRequestEventKind.RETRY_PLANNED
                )
            return super().send(request, cancellation)

    transport = _RetryOrderTransport(
        [
            StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"",
                remote_request_id=failed_remote_id,
            ),
            StepAudioTransportResponse(
                status_code=success.status_code,
                content_type=success.content_type,
                body=success.body,
                remote_request_id=successful_remote_id,
            ),
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        result = recognition.transcribe(request)

        assert result.execution_facts.retry_count == 1
        assert [event.kind for event in sink.events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            TranscriptionRemoteRequestEventKind.SUCCEEDED,
        ]
        assert len({event.correlation_id for event in sink.events}) == 1
        assert [event.transport_attempt_count for event in sink.events] == [
            0,
            1,
            1,
            2,
        ]
        assert sink.events[1].remote_request_id == (
            RemoteRequestId.from_adapter(failed_remote_id)
        )
        assert sink.events[1].reason_code == "service.server_error"
        assert sink.events[2].reason_code == "service.server_error"
        assert sink.events[2].retry_delay_ms == 50
        assert sink.events[3].remote_request_id == (
            RemoteRequestId.from_adapter(successful_remote_id)
        )
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_observes_transport_exception_as_failed_remote_request(
    tmp_path,
):
    sink = _RecordingRemoteRequestEventSink()
    transport = _ScriptedTransport(
        [StepAudioTransportFailure(StepAudioTransportFailureKind.TLS_FAILED)]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        with pytest.raises(TranscriptionFailure):
            recognition.transcribe(request)

        assert [event.kind for event in sink.events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            TranscriptionRemoteRequestEventKind.FAILED,
        ]
        assert len({event.correlation_id for event in sink.events}) == 1
        assert [event.transport_attempt_count for event in sink.events] == [
            0,
            1,
            1,
        ]
        assert sink.events[1].reason_code == "transport.tls_failed"
        assert sink.events[2].reason_code == "transport.tls_failed"
        assert sink.events[1].remote_request_id is None
        assert sink.events[2].remote_request_id is None
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize(
    ("target", "expected_send_count", "response"),
    [
        (
            TranscriptionRemoteRequestEventKind.STARTED,
            0,
            _successful_response(),
        ),
        (
            TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            1,
            StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"",
            ),
        ),
        (
            TranscriptionRemoteRequestEventKind.SUCCEEDED,
            1,
            _successful_response(),
        ),
    ],
)
def test_stepaudio_maps_event_sink_failure_at_each_lifecycle_phase(
    tmp_path,
    target,
    expected_send_count,
    response,
):
    sink_failure = RuntimeError("event-sink-failure-canary")
    sink = _FailingRemoteRequestEventSink(target, sink_failure)
    transport = _ScriptedTransport([response, _successful_response()])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is ErrorCode.INTERNAL_UNEXPECTED
        assert set(failure.diagnostics) == {
            "function",
            "line",
            "source_module",
        }
        assert len(transport.send_calls) == expected_send_count
        assert sink.events[-1].kind is target
        assert "event-sink-failure-canary" not in repr(failure)
        assert failure.__cause__ is None
        assert failure.__context__ is None
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_preserves_classified_event_sink_failure(tmp_path):
    sink_failure = DiagnosticsFailure(
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        {
            "operation": "diagnostics.append",
            "reason_code": "diagnostics.append_failed",
        },
    )
    sink = _FailingRemoteRequestEventSink(
        TranscriptionRemoteRequestEventKind.STARTED,
        sink_failure,
    )
    transport = _ScriptedTransport([_successful_response()])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        with pytest.raises(DiagnosticsFailure) as captured:
            recognition.transcribe(request)

        assert captured.value is sink_failure
        assert transport.send_calls == []
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_emits_no_remote_request_event_when_already_cancelled(
    tmp_path,
):
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    sink = _RecordingRemoteRequestEventSink()
    transport = _ScriptedTransport([_successful_response()])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)
    cancelled_request = TranscriptionRequest(
        source=request.source,
        temporary_workspace=request.temporary_workspace,
        cancellation=cancellation.token,
    )

    try:
        with pytest.raises(CancellationRequested):
            recognition.transcribe(cancelled_request)

        assert transport.send_calls == []
        assert sink.events == []
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize("mutation", ["delete", "truncate", "replace"])
def test_stepaudio_maps_managed_pcm_integrity_failure_without_remote_request(
    tmp_path,
    mutation,
):
    request, run_workspace = _transcription_request(
        tmp_path,
        label=f"-managed-pcm-{mutation}",
    )
    original = b"\xe8\x03" * 16_000
    location = request.temporary_workspace.location("normalized.s16le")
    location.write_bytes(original)
    audio = NormalizedPcmAudio._from_managed(
        location=location,
        duration_ms=1_000,
        byte_length=len(original),
        content_sha256=NormalizedPcmAudio(
            pcm=original,
            duration_ms=1_000,
        ).content_sha256,
    )
    transport = _ScriptedTransport(
        [
            _successful_response(
                start_ms=0,
                end_ms=1_000,
                text="primed",
            )
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=audio),
        transport=transport,
    )

    try:
        primed = recognition.transcribe(request)
        assert primed.execution_facts.cache_use is CacheUse.MISS
        if mutation == "delete":
            normalized_path = next(
                (
                    tmp_path
                    / f"workspace-path-canary-do-not-send-managed-pcm-{mutation}"
                ).glob("work/tmp/*/scratch/normalized.s16le")
            )
            normalized_path.unlink()
        else:
            location.write_bytes(
                original[:-2]
                if mutation == "truncate"
                else b"\xd0\x07" * 16_000
            )

        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED
        )
        assert failure.diagnostics == {
            "operation": "ffmpeg.transcode",
            "reason_code": "media.output_invalid",
        }
        assert failure.execution_facts == failure.execution_facts.__class__(
            cache_use=CacheUse.MISS
        )
        assert failure.__cause__ is None
        assert failure.__context__ is None
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_never_reports_absent_for_low_level_nonzero_signal(tmp_path):
    pcm = bytearray(b"\x00\x00" * 16_000)
    pcm[500 * 32 : 500 * 32 + 2] = b"\x01\x00"
    transport = _ScriptedTransport(
        [
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"transcript.text.done","text":""}\n\n'
                ),
            )
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(
            audio=NormalizedPcmAudio(
                pcm=bytes(pcm),
                duration_ms=1_000,
            )
        ),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-low-level-signal",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is ErrorCode.TRANSCRIPTION_OUTPUT_INVALID
        assert failure.diagnostics["reason_code"] == "output.empty_with_speech"
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_retries_transient_transport_then_reports_successful_retry_facts(
    tmp_path,
):
    transport = _ScriptedTransport(
        [
            StepAudioTransportFailure(
                StepAudioTransportFailureKind.CONNECTION_FAILED
            ),
            StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b'{"provider-error-canary":"must-not-leak"}',
            ),
            _successful_response(),
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        result = recognition.transcribe(request)

        assert result.execution_facts == result.execution_facts.__class__(
            cache_use=CacheUse.MISS,
            retry_count=2,
        )
        assert len(transport.send_calls) == 3
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_retries_incomplete_sse_then_reports_successful_retry(
    tmp_path,
):
    incomplete = StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream",
        body=(
            b'data: {"type":"transcript.text.delta","delta":"partial",'
            b'"start_time":0,"end_time":1000}\n\n'
        ),
    )
    transport = _ScriptedTransport(
        [
            incomplete,
            _successful_response(
                start_ms=0,
                end_ms=1_000,
                text="complete",
            ),
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-incomplete-then-success",
    )

    try:
        result = recognition.transcribe(request)

        assert result.execution_facts == result.execution_facts.__class__(
            cache_use=CacheUse.MISS,
            retry_count=1,
        )
        assert tuple(chunk.text for chunk in result.chunks) == ("complete",)
        assert len(transport.send_calls) == 2
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_exhausts_retries_for_incomplete_sse_with_stable_failure(
    tmp_path,
):
    incomplete = StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream",
        body=(
            b'data: {"type":"transcript.text.delta","delta":"partial-canary",'
            b'"start_time":0,"end_time":1000}\n\n'
        ),
    )
    transport = _ScriptedTransport([incomplete, incomplete, incomplete])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-incomplete-exhausted",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED
        assert failure.diagnostics["reason_code"] == "output.incomplete"
        assert failure.diagnostics["attempt"] == 3
        assert failure.execution_facts.retry_count == 2
        assert "partial-canary" not in repr(failure)
        assert len(transport.send_calls) == 3
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_serial_failure_keeps_retries_from_earlier_successful_work(
    tmp_path,
):
    duration_ms = 200_000
    transport = _ScriptedTransport(
        [
            StepAudioTransportFailure(
                StepAudioTransportFailureKind.RESPONSE_TRUNCATED
            ),
            _successful_response(
                start_ms=0,
                end_ms=185_000,
                text="first work",
            ),
            StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"rejected",
            ),
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(
            audio=_speech_audio(duration_ms)
        ),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=duration_ms,
        label="-serial-retry-facts",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
        )
        assert failure.execution_facts.retry_count == 1
        assert len(transport.send_calls) == 3
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_attempts"),
    [
        (
            StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"credential-canary",
            ),
            ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED,
            1,
        ),
        (
            StepAudioTransportResponse(
                status_code=422,
                content_type="application/json",
                body=b"request-canary",
            ),
            ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
            1,
        ),
        (
            StepAudioTransportResponse(
                status_code=429,
                content_type="application/json",
                body=b"rate-limit-canary",
            ),
            ErrorCode.TRANSCRIPTION_RATE_LIMITED,
            3,
        ),
        (
            StepAudioTransportResponse(
                status_code=408,
                content_type="application/json",
                body=b"timeout-canary",
            ),
            ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
            3,
        ),
        (
            StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"service-canary",
            ),
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            3,
        ),
        (
            StepAudioTransportResponse(
                status_code=302,
                content_type="text/plain",
                body=b"redirect-canary",
            ),
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            1,
        ),
    ],
)
def test_stepaudio_maps_provider_statuses_to_stable_failures_without_raw_body(
    tmp_path,
    outcome,
    expected_code,
    expected_attempts,
):
    transport = _ScriptedTransport([outcome] * expected_attempts)
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is expected_code
        assert failure.execution_facts.retry_count == expected_attempts - 1
        assert failure.diagnostics["attempt"] == expected_attempts
        assert len(transport.send_calls) == expected_attempts
        rendered = repr(failure.diagnostics)
        for canary in (
            "credential-canary",
            "request-canary",
            "rate-limit-canary",
            "timeout-canary",
            "service-canary",
            "redirect-canary",
        ):
            assert canary not in rendered
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize(
    (
        "kind",
        "expected_code",
        "expected_reason",
        "expected_attempts",
    ),
    [
        (
            StepAudioTransportFailureKind.CONNECT_TIMEOUT,
            ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
            "timeout.connect",
            3,
        ),
        (
            StepAudioTransportFailureKind.WRITE_TIMEOUT,
            ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
            "timeout.write",
            3,
        ),
        (
            StepAudioTransportFailureKind.READ_TIMEOUT,
            ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
            "timeout.read",
            3,
        ),
        (
            StepAudioTransportFailureKind.DNS_FAILED,
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            "transport.dns_failed",
            3,
        ),
        (
            StepAudioTransportFailureKind.CONNECTION_FAILED,
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            "transport.connection_failed",
            3,
        ),
        (
            StepAudioTransportFailureKind.TLS_FAILED,
            ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
            "transport.tls_failed",
            1,
        ),
        (
            StepAudioTransportFailureKind.RESPONSE_PROTOCOL_INVALID,
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            "protocol.body_invalid",
            1,
        ),
        (
            StepAudioTransportFailureKind.RESPONSE_TRUNCATED,
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            "output.body_truncated",
            3,
        ),
        (
            StepAudioTransportFailureKind.RESPONSE_TOO_LARGE,
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            "output.length_limit",
            1,
        ),
    ],
)
def test_stepaudio_maps_transport_failures_and_retries_only_transient_kinds(
    tmp_path,
    kind,
    expected_code,
    expected_reason,
    expected_attempts,
):
    transport = _ScriptedTransport(
        [
            StepAudioTransportFailure(kind)
            for _index in range(expected_attempts)
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label=f"-transport-{kind.value}",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is expected_code
        assert failure.diagnostics["reason_code"] == expected_reason
        assert failure.execution_facts.retry_count == expected_attempts - 1
        assert len(transport.send_calls) == expected_attempts
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_cancellation_interrupts_retry_backoff_without_another_request(
    tmp_path,
):
    cancellation = CancellationSource(clock=lambda: 1.0)
    sink = _RecordingRemoteRequestEventSink()

    class _CancelDuringBackoffTransport(_ScriptedTransport):
        def send(self, request, token):
            outcome = super().send(request, token)
            Timer(
                0.01,
                lambda: cancellation.request(signal.SIGTERM),
            ).start()
            return outcome

    transport = _CancelDuringBackoffTransport(
        [
            StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"service unavailable",
            )
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(tmp_path)
    request = TranscriptionRequest(
        source=request.source,
        temporary_workspace=request.temporary_workspace,
        cancellation=cancellation.token,
    )

    try:
        with pytest.raises(CancellationRequested) as captured:
            recognition.transcribe(request)

        assert captured.value.signal_number == signal.SIGTERM
        assert len(transport.send_calls) == 1
        assert [event.kind for event in sink.events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            TranscriptionRemoteRequestEventKind.INTERRUPTED,
        ]
        assert len({event.correlation_id for event in sink.events}) == 1
        assert [event.transport_attempt_count for event in sink.events] == [
            0,
            1,
            1,
            1,
        ]
    finally:
        run_workspace.cleanup()
        run_workspace.close()


@pytest.mark.parametrize(
    ("response", "expected_code", "expected_reason"),
    [
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="application/json",
                body=b'{"type":"not-sse"}',
            ),
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            "protocol.content_type_invalid",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=b"data: {not-json}\n\ndata: [DONE]\n\n",
            ),
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            "protocol.json_invalid",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"transcript.text.delta","delta":"text",'
                    b'"start_time":'
                    + (b"9" * 4_301)
                    + b',"end_time":800}\n\n'
                    b'data: {"type":"transcript.text.done","text":"text"}\n\n'
                ),
            ),
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            "protocol.json_invalid",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"transcript.text.delta","delta":"   ",'
                    b'"start_time":100,"end_time":800}\n\n'
                    b'data: {"type":"transcript.text.done","text":"   "}\n\n'
                ),
            ),
            ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
            "output.text_invalid",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"provider.future.event"}\n\n'
                    b'data: {"type":"transcript.text.done","text":""}\n\n'
                    b"data: [DONE]\n\n"
                ),
            ),
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            "protocol.body_invalid",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"error","error":'
                    b'{"message":"provider-body-canary"}}\n\n'
                    b"data: [DONE]\n\n"
                ),
            ),
            ErrorCode.TRANSCRIPTION_GENERATION_REFUSED,
            "generation.provider_refused",
        ),
        (
            StepAudioTransportResponse(
                status_code=200,
                content_type="text/event-stream",
                body=(
                    b'data: {"type":"transcript.text.delta","delta":"text",'
                    b'"start_time":100,"end_time":1001}\n\n'
                    b'data: {"type":"transcript.text.done","text":"text"}\n\n'
                    b"data: [DONE]\n\n"
                ),
            ),
            ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
            "output.time_invalid",
        ),
    ],
)
def test_stepaudio_strictly_translates_malformed_or_refused_sse(
    tmp_path,
    response,
    expected_code,
    expected_reason,
):
    response = StepAudioTransportResponse(
        status_code=response.status_code,
        content_type=response.content_type,
        body=response.body,
        remote_request_id="raw-remote-request-id-canary",
    )
    transport = _ScriptedTransport([response])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(tmp_path)

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert failure.error_code is expected_code
        assert failure.diagnostics["reason_code"] == expected_reason
        assert failure.diagnostics["attempt"] == 1
        assert str(failure.diagnostics["remote_request_id"]).startswith(
            "sha256:"
        )
        assert "raw-remote-request-id-canary" not in repr(failure.diagnostics)
        assert "provider-body-canary" not in repr(failure.diagnostics)
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_accepts_documented_done_event_without_extra_terminal_marker(
    tmp_path,
):
    response = StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream",
        body=(
            b'data: {"type":"transcript.text.delta","delta":"text",'
            b'"start_time":100,"end_time":800}\n\n'
            b'data: {"type":"transcript.text.done","text":"text"}\n\n'
        ),
    )
    transport = _ScriptedTransport([response])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-documented-done",
    )

    try:
        result = recognition.transcribe(request)

        assert tuple(chunk.text for chunk in result.chunks) == ("text",)
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_preserves_each_documented_multichar_delta_interval(tmp_path):
    audio = NormalizedPcmAudio(
        pcm=(
            b"\xe8\x03" * (100 * 16)
            + b"\x00\x00" * (800 * 16)
            + b"\xe8\x03" * (100 * 16)
        ),
        duration_ms=1_000,
    )
    response = StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream",
        body=(
            b'data: {"type":"transcript.text.delta","delta":"\xe8\xaf\x86\xe5\x88\xab\xe7\x9a\x84",'
            b'"start_time":0,"end_time":100}\n\n'
            b'data: {"type":"transcript.text.delta","delta":"\xe6\x96\x87\xe6\x9c\xac\xe6\xae\xb5",'
            b'"start_time":900,"end_time":1000}\n\n'
            b'data: {"type":"transcript.text.done","text":'
            b'"\xe8\xaf\x86\xe5\x88\xab\xe7\x9a\x84\xe6\x96\x87\xe6\x9c\xac\xe6\xae\xb5"}\n\n'
        ),
    )
    transport = _ScriptedTransport([response])
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=audio),
        transport=transport,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-multichar-deltas",
    )

    try:
        result = recognition.transcribe(request)

        assert tuple(
            (chunk.start_ms, chunk.end_ms, chunk.text)
            for chunk in result.chunks
        ) == (
            (0, 100, "识别的"),
            (900, 1_000, "文本段"),
        )
        assert len(transport.send_calls) == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_accepts_documented_sse_metadata_without_disclosing_it(
    tmp_path,
):
    response = StepAudioTransportResponse(
        status_code=200,
        content_type="text/event-stream",
        body=(
            b'data: {"type":"transcript.text.delta","meta":'
            b'{"session_id":"provider-session-canary","timestamp":1},'
            b'"delta":"text","item_id":"provider-item-canary",'
            b'"content_index":0,"start_time":100,"end_time":800}\n\n'
            b'data: {"type":"transcript.text.done","meta":'
            b'{"session_id":"provider-session-canary","timestamp":2},'
            b'"text":"text","usage":{"type":"realtime_asr",'
            b'"input_tokens":1,"output_tokens":1,"total_tokens":2}}\n\n'
        ),
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=_ScriptedTransport([response]),
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-documented-metadata",
    )

    try:
        result = recognition.transcribe(request)

        assert tuple(chunk.text for chunk in result.chunks) == ("text",)
        assert "provider-session-canary" not in repr(result)
        assert "provider-item-canary" not in repr(result)
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_whole_cache_uses_only_result_affecting_content_identity(
    tmp_path,
):
    cache = _cache_repository()
    audio = _speech_audio()
    first_transport = _ScriptedTransport([_successful_response()])
    first = StepAudioSpeechRecognition(
        _settings(),
        credential="first-credential",
        cache_repository=cache,
        audio_preparer=_FakeAudioPreparer(audio=audio),
        transport=first_transport,
    )
    second_settings = StepAudioSettings(
        endpoint=_settings().endpoint,
        model=_settings().model,
        timeout_seconds=99,
        max_concurrency=7,
    )
    second_transport = _ScriptedTransport([])
    second_sink = _RecordingRemoteRequestEventSink()
    second = StepAudioSpeechRecognition(
        second_settings,
        credential="different-credential",
        cache_repository=cache,
        audio_preparer=_FakeAudioPreparer(audio=audio),
        transport=second_transport,
        event_sink=second_sink,
    )
    first_request, first_workspace = _transcription_request(
        tmp_path,
        label="-first",
    )
    second_request, second_workspace = _transcription_request(
        tmp_path,
        label="-second",
    )

    try:
        cold = first.transcribe(first_request)
        cached = second.transcribe(second_request)

        assert cold.execution_facts.cache_use is CacheUse.MISS
        assert cached.execution_facts == cached.execution_facts.__class__(
            cache_use=CacheUse.HIT
        )
        assert cached.chunks == cold.chunks
        assert len(first_transport.send_calls) == 1
        assert second_transport.send_calls == []
        assert second_sink.events == []
    finally:
        first_workspace.cleanup()
        first_workspace.close()
        second_workspace.cleanup()
        second_workspace.close()


def test_stepaudio_model_or_pcm_change_invalidates_whole_cache(tmp_path):
    cache = _cache_repository()
    baseline_audio = _speech_audio()
    changed_audio = NormalizedPcmAudio(
        pcm=b"\xd0\x07" * 16_000,
        duration_ms=1_000,
    )
    transports = [
        _ScriptedTransport([_successful_response(text="baseline")]),
        _ScriptedTransport([_successful_response(text="new model")]),
        _ScriptedTransport([_successful_response(text="new pcm")]),
    ]
    settings = (
        _settings(),
        StepAudioSettings(
            endpoint=_settings().endpoint,
            model="stepaudio-2.5-asr-revision-2",
            timeout_seconds=17,
            max_concurrency=1,
        ),
        _settings(),
    )
    audios = (baseline_audio, baseline_audio, changed_audio)
    run_workspaces = []

    try:
        results = []
        for index, (current_settings, audio, transport) in enumerate(
            zip(settings, audios, transports, strict=True)
        ):
            recognition = StepAudioSpeechRecognition(
                current_settings,
                credential="credential",
                cache_repository=cache,
                audio_preparer=_FakeAudioPreparer(audio=audio),
                transport=transport,
            )
            request, run_workspace = _transcription_request(
                tmp_path,
                label=f"-identity-{index}",
            )
            run_workspaces.append(run_workspace)
            results.append(recognition.transcribe(request))

        assert all(
            result.execution_facts.cache_use is CacheUse.MISS
            for result in results
        )
        assert [len(transport.send_calls) for transport in transports] == [
            1,
            1,
            1,
        ]
    finally:
        for run_workspace in run_workspaces:
            run_workspace.cleanup()
            run_workspace.close()


def test_stepaudio_shard_cache_is_relative_to_pcm_not_global_offset(tmp_path):
    cache = _cache_repository()
    tail_duration_ms = 100_000
    tail_pcm = b"\xe8\x03" * (tail_duration_ms * 16)
    tail_audio = NormalizedPcmAudio(
        pcm=tail_pcm,
        duration_ms=tail_duration_ms,
    )
    first_transport = _ScriptedTransport(
        [
            _successful_response(
                start_ms=0,
                end_ms=tail_duration_ms,
                text="shared tail",
            )
        ]
    )
    first = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=cache,
        audio_preparer=_FakeAudioPreparer(audio=tail_audio),
        transport=first_transport,
    )

    prefix_duration_ms = 175_000
    long_duration_ms = prefix_duration_ms + tail_duration_ms
    long_audio = NormalizedPcmAudio(
        pcm=(
            b"\xd0\x07" * (prefix_duration_ms * 16)
            + tail_pcm
        ),
        duration_ms=long_duration_ms,
    )
    second_transport = _ScriptedTransport(
        [
            _successful_response(
                start_ms=0,
                end_ms=prefix_duration_ms,
                text="new prefix",
            )
        ]
    )
    second = StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint=_settings().endpoint,
            model=_settings().model,
            timeout_seconds=33,
            max_concurrency=2,
        ),
        credential="new credential",
        cache_repository=cache,
        audio_preparer=_FakeAudioPreparer(audio=long_audio),
        transport=second_transport,
    )
    first_request, first_workspace = _transcription_request(
        tmp_path,
        duration_ms=tail_duration_ms,
        label="-tail",
    )
    second_request, second_workspace = _transcription_request(
        tmp_path,
        duration_ms=long_duration_ms,
        label="-long",
    )

    try:
        first_result = first.transcribe(first_request)
        second_result = second.transcribe(second_request)

        assert first_result.execution_facts.cache_use is CacheUse.MISS
        assert second_result.execution_facts.cache_use is CacheUse.MISS
        assert tuple(
            (chunk.start_ms, chunk.end_ms, chunk.text)
            for chunk in second_result.chunks
        ) == (
            (0, prefix_duration_ms, "new prefix"),
            (prefix_duration_ms, long_duration_ms, "shared tail"),
        )
        assert len(first_transport.send_calls) == 1
        assert len(second_transport.send_calls) == 1
    finally:
        first_workspace.cleanup()
        first_workspace.close()
        second_workspace.cleanup()
        second_workspace.close()


def test_stepaudio_honors_bounded_concurrency_and_returns_time_ordered_result(
    tmp_path,
):
    class _ConcurrentTransport:
        def __init__(self) -> None:
            self.barrier = Barrier(2)
            self.lock = Lock()
            self.active = 0
            self.max_active = 0
            self.send_calls = 0

        def check_readiness(self) -> tuple[ReadinessIssue, ...]:
            return ()

        def send(self, request, cancellation):
            del cancellation
            payload = json.loads(request.body)
            pcm = base64.b64decode(payload["audio"]["data"], validate=True)
            first_samples = pcm.count(b"\xe8\x03")
            second_samples = pcm.count(b"\xd0\x07")
            with self.lock:
                self.send_calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.barrier.wait(timeout=2)
                if first_samples > second_samples:
                    sleep(0.02)
                    return _successful_response(
                        start_ms=0,
                        end_ms=180_000,
                        text="first",
                    )
                return _successful_response(
                    start_ms=5_000,
                    end_ms=185_000,
                    text="second",
                )
            finally:
                with self.lock:
                    self.active -= 1

    duration_ms = 360_000
    audio = NormalizedPcmAudio(
        pcm=(
            b"\xe8\x03" * (180_000 * 16)
            + b"\xd0\x07" * (180_000 * 16)
        ),
        duration_ms=duration_ms,
    )
    transport = _ConcurrentTransport()
    sink = _RecordingRemoteRequestEventSink()
    recognition = StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint=_settings().endpoint,
            model=_settings().model,
            timeout_seconds=17,
            max_concurrency=2,
        ),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=audio),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=duration_ms,
        label="-concurrent",
    )

    try:
        result = recognition.transcribe(request)

        assert tuple(
            (chunk.start_ms, chunk.end_ms, chunk.text)
            for chunk in result.chunks
        ) == (
            (0, 180_000, "first"),
            (180_000, 360_000, "second"),
        )
        assert transport.send_calls == 2
        assert transport.max_active == 2
        events_by_request: dict[
            OperationId,
            list[TranscriptionRemoteRequestEventKind],
        ] = {}
        for event in sink.events:
            events_by_request.setdefault(event.correlation_id, []).append(
                event.kind
            )
        assert len(events_by_request) == 2
        assert list(events_by_request.values()) == [
            [
                TranscriptionRemoteRequestEventKind.STARTED,
                TranscriptionRemoteRequestEventKind.SUCCEEDED,
            ],
            [
                TranscriptionRemoteRequestEventKind.STARTED,
                TranscriptionRemoteRequestEventKind.SUCCEEDED,
            ],
        ]
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_inflight_request_observes_root_cancellation(tmp_path):
    cancellation = CancellationSource(clock=lambda: 1.0)
    started = Event()
    sink = _RecordingRemoteRequestEventSink()

    class _BlockingTransport:
        def check_readiness(self) -> tuple[ReadinessIssue, ...]:
            return ()

        def send(self, request, token):
            del request
            started.set()
            assert token.wait(2)
            token.raise_if_cancelled()
            raise AssertionError("取消后不得形成供应商响应")

    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=_BlockingTransport(),
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        label="-inflight-cancel",
    )
    request = TranscriptionRequest(
        source=request.source,
        temporary_workspace=request.temporary_workspace,
        cancellation=cancellation.token,
    )
    captured: list[CancellationRequested] = []

    def transcribe() -> None:
        try:
            recognition.transcribe(request)
        except CancellationRequested as failure:
            captured.append(failure)

    worker = Thread(target=transcribe)
    worker.start()
    try:
        assert started.wait(2)
        cancellation.request(signal.SIGINT)
        worker.join(2)

        assert not worker.is_alive()
        assert len(captured) == 1
        assert isinstance(captured[0], CancellationRequested)
        assert captured[0].signal_number == signal.SIGINT
        assert [event.kind for event in sink.events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.INTERRUPTED,
        ]
        assert sink.events[1].correlation_id == sink.events[0].correlation_id
        assert sink.events[1].transport_attempt_count == 1
        assert sink.events[1].reason_code == "cancellation.root_requested"
    finally:
        if worker.is_alive():
            cancellation.request(signal.SIGINT)
            worker.join(2)
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_fail_fast_cancels_sibling_and_stops_queued_dispatch(
    tmp_path,
):
    sink = _RecordingRemoteRequestEventSink()

    class _FailFastTransport:
        def __init__(self) -> None:
            self.barrier = Barrier(2)
            self.lock = Lock()
            self.calls = 0
            self.sibling_cancelled = Event()

        def check_readiness(self) -> tuple[ReadinessIssue, ...]:
            return ()

        def send(self, request, cancellation):
            del request
            with self.lock:
                call_index = self.calls
                self.calls += 1
            self.barrier.wait(timeout=2)
            if call_index == 0:
                return StepAudioTransportResponse(
                    status_code=401,
                    content_type="application/json",
                    body=b"credential rejected",
                )
            if cancellation.wait(2):
                self.sibling_cancelled.set()
                cancellation.raise_if_cancelled()
            raise AssertionError("并发兄弟失败后在途请求必须收到取消")

    duration_ms = 540_000
    transport = _FailFastTransport()
    recognition = StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint=_settings().endpoint,
            model=_settings().model,
            timeout_seconds=17,
            max_concurrency=2,
        ),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(
            audio=NormalizedPcmAudio(
                pcm=b"\xe8\x03" * (duration_ms * 16),
                duration_ms=duration_ms,
            )
        ),
        transport=transport,
        event_sink=sink,
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=duration_ms,
        label="-fail-fast",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        assert (
            captured.value.error_code
            is ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
        )
        assert transport.sibling_cancelled.wait(2)
        assert transport.calls == 2
        events_by_request: dict[
            OperationId,
            list[TranscriptionRemoteRequestEvent],
        ] = {}
        for event in sink.events:
            events_by_request.setdefault(event.correlation_id, []).append(
                event
            )
        assert len(events_by_request) == 2
        terminal_events = {events[-1].kind for events in events_by_request.values()}
        assert terminal_events == {
            TranscriptionRemoteRequestEventKind.FAILED,
            TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY,
        }
        cancelled_events = next(
            events
            for events in events_by_request.values()
            if events[-1].kind
            is TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY
        )
        assert [event.kind for event in cancelled_events] == [
            TranscriptionRemoteRequestEventKind.STARTED,
            TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY,
        ]
        assert cancelled_events[-1].transport_attempt_count == 1
        assert (
            cancelled_events[-1].reason_code
            == "concurrency.primary_failed"
        )
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_fail_fast_keeps_retry_started_by_cancelled_sibling(
    tmp_path,
):
    retry_started = Event()
    first_attempts = 0
    attempts_lock = Lock()

    class _RetryingSiblingTransport:
        def check_readiness(self) -> tuple[ReadinessIssue, ...]:
            return ()

        def send(self, request, cancellation):
            nonlocal first_attempts
            payload = json.loads(request.body)
            pcm = base64.b64decode(payload["audio"]["data"], validate=True)
            if pcm.count(b"\xe8\x03") > pcm.count(b"\xd0\x07"):
                with attempts_lock:
                    first_attempts += 1
                    attempt = first_attempts
                if attempt == 1:
                    raise StepAudioTransportFailure(
                        StepAudioTransportFailureKind.RESPONSE_TRUNCATED
                    )
                retry_started.set()
                assert cancellation.wait(2)
                cancellation.raise_if_cancelled()
                raise AssertionError("兄弟失败后重试请求必须观察取消")
            assert retry_started.wait(2)
            return StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"rejected",
            )

    duration_ms = 360_000
    recognition = StepAudioSpeechRecognition(
        StepAudioSettings(
            endpoint=_settings().endpoint,
            model=_settings().model,
            timeout_seconds=17,
            max_concurrency=2,
        ),
        credential="credential",
        cache_repository=_cache_repository(),
        audio_preparer=_FakeAudioPreparer(
            audio=NormalizedPcmAudio(
                pcm=(
                    b"\xe8\x03" * (180_000 * 16)
                    + b"\xd0\x07" * (180_000 * 16)
                ),
                duration_ms=duration_ms,
            )
        ),
        transport=_RetryingSiblingTransport(),
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=duration_ms,
        label="-cancelled-sibling-retry-facts",
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
        )
        assert failure.execution_facts.retry_count == 1
        assert first_attempts == 2
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_stepaudio_fail_fast_cancels_sibling_waiting_for_shard_cache_claim():
    cache = _cache_repository()
    settings = StepAudioSettings(
        endpoint=_settings().endpoint,
        model=_settings().model,
        timeout_seconds=17,
        max_concurrency=2,
    )
    audio = NormalizedPcmAudio(
        pcm=(
            b"\xe8\x03" * (1_000 * 16)
            + b"\xd0\x07" * (1_000 * 16)
        ),
        duration_ms=2_000,
    )
    works = (
        RecognitionWork(
            sequence=0,
            kind=RecognitionKind.PRIMARY,
            core=TimeInterval(0, 1_000),
            request=TimeInterval(0, 1_000),
        ),
        RecognitionWork(
            sequence=1,
            kind=RecognitionKind.PRIMARY,
            core=TimeInterval(1_000, 2_000),
            request=TimeInterval(1_000, 2_000),
        ),
    )
    held = Event()
    release = Event()
    holder_failures: list[BaseException] = []

    def hold_second_shard_claim() -> None:
        try:
            cache.claim(
                _shard_cache_spec(
                    audio.slice(works[1].request),
                    settings,
                ).identity,
                cancellation=CancellationSource().token,
                effect=lambda _claim: (
                    held.set(),
                    release.wait(timeout=5),
                )[1],
            )
        except (CacheFailure, CancellationRequested) as failure:
            holder_failures.append(failure)

    holder = Thread(target=hold_second_shard_claim)
    holder.start()
    assert held.wait(timeout=2)

    class _ImmediateAuthenticationFailure:
        def check_readiness(self) -> tuple[ReadinessIssue, ...]:
            return ()

        def send(self, request, cancellation):
            del request, cancellation
            return StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"rejected",
            )

    source = _StepAudioObservationSource(
        audio=audio,
        settings=settings,
        credential="credential",
        cache_repository=cache,
        transport=_ImmediateAuthenticationFailure(),
    )
    root_cancellation = CancellationSource()
    observed_failures: list[BaseException] = []

    def observe() -> None:
        try:
            source.observe(works, root_cancellation.token)
        except (CancellationRequested, TranscriptionFailure) as failure:
            observed_failures.append(failure)

    worker = Thread(target=observe)
    worker.start()
    try:
        worker.join(timeout=1)
        completed_promptly = not worker.is_alive()
    finally:
        release.set()
        holder.join(timeout=2)
        worker.join(timeout=2)

    assert completed_promptly
    assert not holder.is_alive()
    assert not worker.is_alive()
    assert holder_failures == []
    assert len(observed_failures) == 1
    assert isinstance(observed_failures[0], TranscriptionFailure)
    assert (
        observed_failures[0].error_code
        is ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
    )


def test_stepaudio_failure_never_publishes_a_whole_transcript_cache(tmp_path):
    cache = _cache_repository()
    transport = _ScriptedTransport(
        [
            StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"rejected",
            ),
            StepAudioTransportResponse(
                status_code=401,
                content_type="application/json",
                body=b"rejected again",
            ),
        ]
    )
    recognition = StepAudioSpeechRecognition(
        _settings(),
        credential="credential",
        cache_repository=cache,
        audio_preparer=_FakeAudioPreparer(audio=_speech_audio()),
        transport=transport,
    )
    workspaces = []

    try:
        for index in range(2):
            request, run_workspace = _transcription_request(
                tmp_path,
                label=f"-failed-cache-{index}",
            )
            workspaces.append(run_workspace)
            with pytest.raises(TranscriptionFailure) as captured:
                recognition.transcribe(request)
            assert (
                captured.value.error_code
                is ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
            )

        assert len(transport.send_calls) == 2
    finally:
        for run_workspace in workspaces:
            run_workspace.cleanup()
            run_workspace.close()


def test_stepaudio_confirmed_silence_is_whole_cached_with_zero_remote_requests(
    tmp_path,
):
    cache = _cache_repository()
    audio = NormalizedPcmAudio(
        pcm=b"\x00\x00" * 16_000,
        duration_ms=1_000,
    )
    transports = (_ScriptedTransport([]), _ScriptedTransport([]))
    workspaces = []
    results = []

    try:
        for index, transport in enumerate(transports):
            recognition = StepAudioSpeechRecognition(
                _settings(),
                credential="credential",
                cache_repository=cache,
                audio_preparer=_FakeAudioPreparer(audio=audio),
                transport=transport,
            )
            request, run_workspace = _transcription_request(
                tmp_path,
                label=f"-silence-cache-{index}",
            )
            workspaces.append(run_workspace)
            results.append(recognition.transcribe(request))

        assert results[0].speech_presence is SpeechPresence.ABSENT
        assert results[0].execution_facts.cache_use is CacheUse.MISS
        assert results[1].speech_presence is SpeechPresence.ABSENT
        assert results[1].execution_facts.cache_use is CacheUse.HIT
        assert all(transport.send_calls == [] for transport in transports)
    finally:
        for run_workspace in workspaces:
            run_workspace.cleanup()
            run_workspace.close()
