"""可由生产组合根装配的 StepAudio 语音识别 Adapter。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import signal
from collections.abc import Callable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from dataclasses import dataclass, field
from enum import Enum
from threading import Event, Lock
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit

from video_auto_editor.cache import (
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheFailure,
    CacheIdentity,
    CacheNamespace,
    CacheRepository,
    CacheResolution,
)
from video_auto_editor.runtime._classified_failure import (
    PreservedApplicationFailure,
)
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    InternalLocation,
    RemoteRequestId,
)
from video_auto_editor.runtime.identity import OperationId

from ._normalized_audio import (
    AUDIO_NORMALIZATION_VERSION,
    PCM_BITS_PER_SAMPLE,
    PCM_BYTES_PER_MILLISECOND,
    PCM_BYTES_PER_SAMPLE,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    SPEECH_EVIDENCE_VERSION,
    NormalizedPcmAudio,
    NormalizedPcmReadFailure,
    confirmed_speech_intervals,
)
from ._reconciliation import (
    RecognitionBatch,
    RecognitionObservation,
    RecognitionWork,
    TimeInterval,
    complete_transcription,
)
from .interface import (
    CacheUse,
    CharacterSpan,
    ExecutionFacts,
    ReadinessIssue,
    ReadinessReport,
    SpeechPresence,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRemoteRequestEvent,
    TranscriptionRemoteRequestEventKind,
    TranscriptionRemoteRequestEventSink,
    TranscriptionRequest,
    TranscriptionResult,
    validate_result_for_source,
)

_ADAPTER_ID = "stepaudio"
_WHOLE_IDENTITY_SCHEMA = "transcript.identity.v1"
_WHOLE_ALGORITHM_VERSION = "stepaudio-transcript.v1"
_WHOLE_PAYLOAD_SCHEMA = "transcript.payload.v1"
_SHARD_IDENTITY_SCHEMA = "transcription-shard.identity.v1"
_SHARD_ALGORITHM_VERSION = "stepaudio-shard.v1"
_SHARD_PAYLOAD_SCHEMA = "transcription-shard.payload.v1"
_REQUEST_VERSION = "stepaudio-asr-sse-request.v1"
_RESPONSE_PARSER_VERSION = "stepaudio-asr-sse-parser.v2"
_SHARD_VALIDATION_VERSION = "stepaudio-shard-validation.v1"
_SHARD_PLAN_VERSION = "coverage-work-plan.v1"
_CORE_REGION_VERSION = "coverage-core-region.v1"
_MERGE_VERSION = "coverage-reconciliation.v1"
_COVERAGE_VERSION = "coverage-ledger.v1"
_DEDUPLICATION_VERSION = "utterance-deduplication.v1"
_RESULT_VALIDATION_VERSION = "transcription-result.v1"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_MAX_CREDENTIAL_HEADER_VALUE_LENGTH = 8 * 1024
_MAX_TRANSPORT_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (0.05, 0.1)
_ERROR_ORDER = {code: index for index, code in enumerate(ErrorCode)}


@dataclass(frozen=True, slots=True)
class StepAudioSettings:
    """组合根交给 StepAudio Adapter 的认证设置。"""

    endpoint: str = field(repr=False)
    model: str
    timeout_seconds: int
    max_concurrency: int
    language: str = "zh"

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TypeError("StepAudio endpoint 必须是非空字符串")
        if not isinstance(self.model, str) or _MODEL.fullmatch(self.model) is None:
            raise ValueError("StepAudio 模型标识格式不合法")
        if (
            not isinstance(self.language, str)
            or re.fullmatch(r"[a-z]{2,8}(?:-[A-Za-z0-9]{2,8})?", self.language)
            is None
        ):
            raise ValueError("StepAudio 语言标识格式不合法")
        for value, field_name, maximum in (
            (self.timeout_seconds, "StepAudio 超时秒数", 600),
            (self.max_concurrency, "StepAudio 最大并发数", 32),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if not 1 <= value <= maximum:
                raise ValueError(f"{field_name}超出认证范围")


@dataclass(frozen=True, slots=True)
class StepAudioTransportRequest:
    """只携带当前识别分片与必要协议参数的传输请求。"""

    endpoint: str = field(repr=False)
    credential: str = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TypeError("StepAudio 传输 endpoint 必须是非空字符串")
        if not isinstance(self.credential, str):
            raise TypeError("StepAudio 传输凭据必须是字符串")
        if not isinstance(self.body, bytes):
            raise TypeError("StepAudio 传输正文必须是 bytes")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("StepAudio 传输超时必须是正整数秒")


@dataclass(frozen=True, slots=True)
class StepAudioTransportResponse:
    """传输边界交给 Adapter 的有限响应事实。"""

    status_code: int
    content_type: str
    body: bytes = field(repr=False)
    remote_request_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status_code, int)
            or isinstance(self.status_code, bool)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("StepAudio 响应状态码必须位于 100 到 599")
        if not isinstance(self.content_type, str):
            raise TypeError("StepAudio 响应 Content-Type 必须是字符串")
        if not isinstance(self.body, bytes):
            raise TypeError("StepAudio 响应正文必须是 bytes")
        if self.remote_request_id is not None and not isinstance(
            self.remote_request_id,
            str,
        ):
            raise TypeError("StepAudio 远端请求标识必须是字符串或 None")


class StepAudioTransportFailureKind(str, Enum):
    """传输实现交给 Adapter 的稳定、无正文失败事实。"""

    CONNECT_TIMEOUT = "connect_timeout"
    WRITE_TIMEOUT = "write_timeout"
    READ_TIMEOUT = "read_timeout"
    DNS_FAILED = "dns_failed"
    CONNECTION_FAILED = "connection_failed"
    TLS_FAILED = "tls_failed"
    RESPONSE_PROTOCOL_INVALID = "response_protocol_invalid"
    RESPONSE_TRUNCATED = "response_truncated"
    RESPONSE_TOO_LARGE = "response_too_large"


_RETRYABLE_TRANSPORT_FAILURES = frozenset(
    {
        StepAudioTransportFailureKind.CONNECT_TIMEOUT,
        StepAudioTransportFailureKind.WRITE_TIMEOUT,
        StepAudioTransportFailureKind.READ_TIMEOUT,
        StepAudioTransportFailureKind.DNS_FAILED,
        StepAudioTransportFailureKind.CONNECTION_FAILED,
        StepAudioTransportFailureKind.RESPONSE_TRUNCATED,
    }
)


class StepAudioTransportFailure(RuntimeError):
    """隐藏套接字异常与自由文本的 StepAudio 传输失败。"""

    __slots__ = ("kind",)

    def __init__(self, kind: StepAudioTransportFailureKind) -> None:
        if not isinstance(kind, StepAudioTransportFailureKind):
            raise TypeError("StepAudio 传输失败必须使用稳定失败类型")
        self.kind = kind
        super().__init__("StepAudio 传输失败")


class StepAudioAudioPreparer(Protocol):
    """真实媒体能力与测试替身共同满足的窄端口。"""

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        ...

    def prepare(
        self,
        request: TranscriptionRequest,
    ) -> NormalizedPcmAudio:
        ...


class StepAudioTransport(Protocol):
    """HTTPS 传输的窄端口。"""

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        ...

    def send(
        self,
        request: StepAudioTransportRequest,
        cancellation: _CancellationView,
    ) -> StepAudioTransportResponse:
        ...


class _CancellationView(Protocol):
    @property
    def cancelled(self) -> bool:
        ...

    @property
    def signal_number(self) -> int | None:
        ...

    def wait(self, timeout: float | None = None) -> bool:
        ...

    def raise_if_cancelled(self) -> None:
        ...


class _SiblingCancelled(Exception):
    """并发兄弟失败后只用于停止 Adapter 内部在途工作。"""


class _CombinedCancellation:
    __slots__ = ("_local", "_root")

    def __init__(
        self,
        root: CancellationToken,
        local: Event,
    ) -> None:
        self._root = root
        self._local = local

    @property
    def cancelled(self) -> bool:
        return self._root.cancelled or self._local.is_set()

    @property
    def signal_number(self) -> int | None:
        return self._root.signal_number

    def wait(self, timeout: float | None = None) -> bool:
        if self.cancelled:
            return True
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            if self.cancelled:
                return True
            remaining = (
                None if deadline is None else deadline - monotonic()
            )
            if remaining is not None and remaining <= 0:
                return False
            self._local.wait(
                0.01 if remaining is None else min(0.01, remaining)
            )

    def raise_if_cancelled(self) -> None:
        self._root.raise_if_cancelled()
        if self._local.is_set():
            raise _SiblingCancelled()


@dataclass(frozen=True, slots=True)
class _CachedTranscript:
    chunks: tuple[TranscriptionChunk, ...]
    speech_presence: SpeechPresence


@dataclass(frozen=True, slots=True)
class _CachedShard:
    chunks: tuple[TranscriptionChunk, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedObservation:
    observation: RecognitionObservation
    retry_count: int


class _AttemptLedger:
    """记录每个工作真正进入 transport.send 的尝试次数。"""

    __slots__ = ("_attempts", "_lock")

    def __init__(self) -> None:
        self._attempts: dict[int, int] = {}
        self._lock = Lock()

    def record(self, sequence: int) -> None:
        with self._lock:
            self._attempts[sequence] = self._attempts.get(sequence, 0) + 1

    @property
    def retry_count(self) -> int:
        with self._lock:
            return sum(
                max(attempts - 1, 0)
                for attempts in self._attempts.values()
            )


class StepAudioSpeechRecognition:
    """在供应商无感知公开 seam 内完成全有或全无语音识别。"""

    __slots__ = (
        "_audio_preparer",
        "_cache",
        "_credential",
        "_event_sink",
        "_settings",
        "_transport",
    )

    def __init__(
        self,
        settings: StepAudioSettings,
        *,
        credential: str,
        cache_repository: CacheRepository,
        audio_preparer: StepAudioAudioPreparer | None = None,
        transport: StepAudioTransport | None = None,
        event_sink: TranscriptionRemoteRequestEventSink | None = None,
    ) -> None:
        if not isinstance(settings, StepAudioSettings):
            raise TypeError("StepAudio 语音识别必须使用 StepAudioSettings")
        if not isinstance(credential, str):
            raise TypeError("StepAudio 凭据必须是字符串")
        if not isinstance(cache_repository, CacheRepository):
            raise TypeError("StepAudio 语音识别必须使用处理缓存仓库")
        if audio_preparer is None:
            from ._stepaudio_audio import FFmpegNormalizedPcmPreparer

            audio_preparer = FFmpegNormalizedPcmPreparer()
        if transport is None:
            from ._stepaudio_https import StdlibStepAudioTransport

            transport = StdlibStepAudioTransport(settings.endpoint)
        if not callable(getattr(audio_preparer, "check_readiness", None)) or not callable(
            getattr(audio_preparer, "prepare", None)
        ):
            raise TypeError("StepAudio 语音识别必须使用音频准备端口")
        if not callable(getattr(transport, "check_readiness", None)) or not callable(
            getattr(transport, "send", None)
        ):
            raise TypeError("StepAudio 语音识别必须使用传输端口")
        if event_sink is not None and not callable(
            getattr(event_sink, "record", None)
        ):
            raise TypeError("StepAudio 语音识别必须使用远程请求事件接收端")
        self._settings = settings
        self._credential = credential
        self._cache = cache_repository
        self._audio_preparer = audio_preparer
        self._transport = transport
        self._event_sink = event_sink

    def check_readiness(self) -> ReadinessReport:
        """聚合本地只读检查，不准备音频、不查询缓存、不发送请求。"""
        issues: list[ReadinessIssue] = []
        if not self._credential:
            issues.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_CREDENTIAL_MISSING,
                    {"capability": "transcription"},
                )
            )
        elif not _safe_credential_header_value(self._credential):
            issues.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_VALUE_INVALID,
                    {
                        "field": "transcription_provider_config.credential",
                        "reason_code": "value.invalid_format",
                    },
                )
            )
        if not _valid_https_endpoint(self._settings.endpoint):
            issues.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_HTTPS_REQUIRED,
                    {"field": "transcription_provider_config.endpoint"},
                )
            )
        issues.extend(_readiness_issues(self._audio_preparer.check_readiness()))
        issues.extend(_readiness_issues(self._transport.check_readiness()))
        unique_items: list[ReadinessIssue] = []
        for issue in issues:
            if issue not in unique_items:
                unique_items.append(issue)
        unique_items.sort(
            key=lambda issue: _ERROR_ORDER[issue.error_code]
        )
        unique = tuple(unique_items)
        return ReadinessReport(ready=not unique, issues=unique)

    def transcribe(
        self,
        request: TranscriptionRequest,
    ) -> TranscriptionResult:
        """只在两级缓存或 StepAudio 处理形成完整结果后返回。"""
        if not isinstance(request, TranscriptionRequest):
            raise TypeError("StepAudio 语音识别只接受 TranscriptionRequest")
        request.cancellation.raise_if_cancelled()
        self._raise_configuration_failure()
        audio = self._audio_preparer.prepare(request)
        request.cancellation.raise_if_cancelled()
        if not isinstance(audio, NormalizedPcmAudio):
            raise TypeError("音频准备端口必须返回 NormalizedPcmAudio")
        if audio.duration_ms != request.source.duration_ms:
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={
                    "operation": "ffmpeg.transcode",
                    "reason_code": "media.output_invalid",
                },
            )

        def collect_speech() -> tuple[TimeInterval, ...]:
            safe_failure: TranscriptionFailure
            try:
                return confirmed_speech_intervals(
                    audio,
                    request.cancellation,
                )
            except NormalizedPcmReadFailure:
                safe_failure = _normalized_audio_read_failure()
            raise safe_failure from None

        speech = collect_speech()
        request.cancellation.raise_if_cancelled()

        computed_facts: list[ExecutionFacts] = []
        whole_spec = _whole_cache_spec(audio, self._settings)

        def compute() -> _CachedTranscript:
            request.cancellation.raise_if_cancelled()
            if not speech:
                computed_facts.append(
                    ExecutionFacts(cache_use=CacheUse.MISS)
                )
                return _CachedTranscript((), SpeechPresence.ABSENT)
            observation_source = _StepAudioObservationSource(
                audio=audio,
                settings=self._settings,
                credential=self._credential,
                cache_repository=self._cache,
                transport=self._transport,
                event_sink=self._event_sink,
            )
            result = complete_transcription(
                source_duration_ms=audio.duration_ms,
                confirmed_speech=speech,
                observation_source=observation_source,
                cancellation=request.cancellation,
            )
            computed_facts.append(result.execution_facts)
            return _CachedTranscript(result.chunks, result.speech_presence)

        def resolve_whole() -> CacheResolution[_CachedTranscript]:
            safe_failure: TranscriptionFailure
            try:
                return self._cache.resolve(
                    whole_spec,
                    cancellation=request.cancellation,
                    compute=compute,
                )
            except CacheFailure as failure:
                safe_failure = TranscriptionFailure(
                    ErrorCode.CACHE_INFRASTRUCTURE_FAILED,
                    execution_facts=ExecutionFacts(
                        cache_use=CacheUse.MISS
                    ),
                    diagnostics=failure.diagnostics,
                )
            except NormalizedPcmReadFailure:
                safe_failure = _normalized_audio_read_failure()
            raise safe_failure from None

        resolution = resolve_whole()
        request.cancellation.raise_if_cancelled()
        facts = (
            ExecutionFacts(cache_use=CacheUse.HIT)
            if resolution.from_cache
            else computed_facts[0]
        )
        result = TranscriptionResult(
            chunks=resolution.value.chunks,
            speech_presence=resolution.value.speech_presence,
            execution_facts=facts,
        )
        validate_result_for_source(result, request.source)
        request.cancellation.raise_if_cancelled()
        return result

    def _raise_configuration_failure(self) -> None:
        if not self._credential:
            raise TranscriptionFailure(
                ErrorCode.CONFIG_CREDENTIAL_MISSING,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={"capability": "transcription"},
            )
        if not _safe_credential_header_value(self._credential):
            raise TranscriptionFailure(
                ErrorCode.CONFIG_VALUE_INVALID,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={
                    "field": "transcription_provider_config.credential",
                    "reason_code": "value.invalid_format",
                },
            )
        if not _valid_https_endpoint(self._settings.endpoint):
            raise TranscriptionFailure(
                ErrorCode.CONFIG_HTTPS_REQUIRED,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={
                    "field": "transcription_provider_config.endpoint"
                },
            )


def _safe_credential_header_value(value: str) -> bool:
    if (
        not 1 <= len(value) <= _MAX_CREDENTIAL_HEADER_VALUE_LENGTH
        or not value.isprintable()
        or "\r" in value
        or "\n" in value
    ):
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


class _StepAudioObservationSource:
    __slots__ = (
        "_audio",
        "_cache",
        "_credential",
        "_event_sink",
        "_settings",
        "_transport",
    )

    def __init__(
        self,
        *,
        audio: NormalizedPcmAudio,
        settings: StepAudioSettings,
        credential: str,
        cache_repository: CacheRepository,
        transport: StepAudioTransport,
        event_sink: TranscriptionRemoteRequestEventSink | None = None,
    ) -> None:
        self._audio = audio
        self._settings = settings
        self._credential = credential
        self._cache = cache_repository
        self._transport = transport
        self._event_sink = event_sink

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation: CancellationToken,
    ) -> RecognitionBatch:
        cancellation.raise_if_cancelled()
        if not works:
            return RecognitionBatch(())
        attempt_ledger = _AttemptLedger()
        worker_count = min(self._settings.max_concurrency, len(works))
        if worker_count == 1:
            safe_failure: TranscriptionFailure
            try:
                resolved = tuple(
                    self._resolve_work(
                        work,
                        cancellation=cancellation,
                        cache_cancellation=cancellation,
                        transport_cancellation=cancellation,
                        attempt_ledger=attempt_ledger,
                    )
                    for work in works
                )
            except TranscriptionFailure as failure:
                safe_failure = _with_retry_count(
                    failure,
                    attempt_ledger.retry_count,
                )
            else:
                return _recognition_batch(resolved)
            raise safe_failure from None

        local_stop = Event()
        cache_stop = CancellationSource()
        transport_cancellation = _CombinedCancellation(
            cancellation,
            local_stop,
        )
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="stepaudio",
        )
        pending: dict[Future[_ResolvedObservation], RecognitionWork] = {}
        resolved_items: list[_ResolvedObservation] = []
        failures: list[tuple[int, Exception]] = []
        next_index = 0

        def stop_siblings() -> None:
            local_stop.set()
            cache_stop.request(signal.SIGTERM)

        def dispatch() -> None:
            nonlocal next_index
            while (
                not failures
                and next_index < len(works)
                and len(pending) < worker_count
            ):
                work = works[next_index]
                next_index += 1
                future = executor.submit(
                    self._resolve_work,
                    work,
                    cancellation=cancellation,
                    cache_cancellation=cache_stop.token,
                    transport_cancellation=transport_cancellation,
                    attempt_ledger=attempt_ledger,
                )
                pending[future] = work

        try:
            dispatch()
            while pending:
                cancellation.raise_if_cancelled()
                completed, _unfinished = wait(
                    pending,
                    timeout=0.05,
                    return_when=FIRST_COMPLETED,
                )
                if not completed:
                    continue
                ordered = sorted(
                    completed,
                    key=lambda future: pending[future].sequence,
                )
                for future in ordered:
                    work = pending.pop(future)
                    try:
                        resolved_items.append(future.result())
                    except CancelledError:
                        continue
                    except _SiblingCancelled:
                        continue
                    except CancellationRequested:
                        cancellation.raise_if_cancelled()
                        if not failures:
                            failures.append(
                                (
                                    work.sequence,
                                    TranscriptionFailure(
                                        ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                                        execution_facts=ExecutionFacts(
                                            cache_use=CacheUse.MISS
                                        ),
                                        diagnostics={
                                            "reason_code": "protocol.body_invalid"
                                        },
                                    ),
                                )
                            )
                    except Exception as failure:  # noqa: BLE001
                        failures.append((work.sequence, failure))
                if failures:
                    stop_siblings()
                    for future in pending:
                        future.cancel()
                else:
                    dispatch()
            cancellation.raise_if_cancelled()
            if failures:
                _sequence, primary_failure = min(
                    failures,
                    key=lambda item: item[0],
                )
                if isinstance(primary_failure, TranscriptionFailure):
                    raise _with_retry_count(
                        primary_failure,
                        attempt_ledger.retry_count,
                    ) from primary_failure
                raise primary_failure
            return _recognition_batch(tuple(resolved_items))
        finally:
            stop_siblings()
            for future in pending:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

    def _resolve_work(
        self,
        work: RecognitionWork,
        *,
        cancellation: CancellationToken,
        cache_cancellation: CancellationToken,
        transport_cancellation: _CancellationView,
        attempt_ledger: _AttemptLedger,
    ) -> _ResolvedObservation:
        cancellation.raise_if_cancelled()
        pcm = self._audio.slice(work.request)
        shard_spec = _shard_cache_spec(pcm, self._settings)
        computed_retries: list[int] = []

        def compute() -> _CachedShard:
            payload, retries = _request_shard(
                pcm=pcm,
                duration_ms=work.request.duration_ms,
                settings=self._settings,
                credential=self._credential,
                transport=self._transport,
                cancellation=transport_cancellation,
                attempt_started=lambda: attempt_ledger.record(
                    work.sequence
                ),
                event_sink=self._event_sink,
            )
            computed_retries.append(retries)
            return payload

        resolution = self._cache.resolve(
            shard_spec,
            cancellation=cache_cancellation,
            compute=compute,
        )
        transport_cancellation.raise_if_cancelled()
        retries = 0 if resolution.from_cache else computed_retries[0]
        shifted = tuple(
            _shift_chunk(chunk, work.request.start_ms)
            for chunk in resolution.value.chunks
        )
        return _ResolvedObservation(
            RecognitionObservation(work=work, chunks=shifted),
            retries,
        )


def _recognition_batch(
    resolved: tuple[_ResolvedObservation, ...],
) -> RecognitionBatch:
    ordered = tuple(
        sorted(
            resolved,
            key=lambda item: item.observation.work.sequence,
        )
    )
    return RecognitionBatch(
        observations=tuple(item.observation for item in ordered),
        retry_count=sum(item.retry_count for item in ordered),
    )


def _with_retry_count(
    failure: TranscriptionFailure,
    retry_count: int,
) -> TranscriptionFailure:
    facts = failure.execution_facts
    return type(failure)(
        failure.error_code,
        execution_facts=ExecutionFacts(
            cache_use=facts.cache_use,
            retry_count=retry_count,
            recovery_count=facts.recovery_count,
        ),
        diagnostics=failure.diagnostics,
    )


def _request_shard(
    *,
    pcm: bytes,
    duration_ms: int,
    settings: StepAudioSettings,
    credential: str,
    transport: StepAudioTransport,
    cancellation: _CancellationView,
    attempt_started: Callable[[], None],
    event_sink: TranscriptionRemoteRequestEventSink | None,
) -> tuple[_CachedShard, int]:
    cancellation.raise_if_cancelled()
    request = StepAudioTransportRequest(
        endpoint=settings.endpoint,
        credential=credential,
        body=_request_body(pcm, settings),
        timeout_seconds=settings.timeout_seconds,
    )
    cancellation.raise_if_cancelled()
    correlation_id = OperationId.new()
    _record_remote_request_event(
        event_sink,
        TranscriptionRemoteRequestEvent(
            kind=TranscriptionRemoteRequestEventKind.STARTED,
            correlation_id=correlation_id,
            transport_attempt_count=0,
        ),
    )
    for attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
        attempt_started()
        response: StepAudioTransportResponse | None = None
        chunks: tuple[TranscriptionChunk, ...] | None = None
        root_cancellation: CancellationRequested | None = None
        sibling_cancelled = False
        request_failure: TranscriptionFailure | None = None
        retryable = False
        unexpected_failure: Exception | None = None
        try:
            returned = transport.send(request, cancellation)
            if isinstance(returned, StepAudioTransportResponse):
                response = returned
            cancellation.raise_if_cancelled()
            if response is None:
                raise _provider_failure(
                    ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                    reason_code="protocol.body_invalid",
                    attempt=attempt,
                )
            chunks = _parse_response(
                response,
                duration_ms=duration_ms,
                attempt=attempt,
            )
            if not chunks and confirmed_speech_intervals(
                NormalizedPcmAudio(pcm=pcm, duration_ms=duration_ms),
                cancellation,
            ):
                raise _provider_failure(
                    ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                    reason_code="output.empty_with_speech",
                    attempt=attempt,
                    http_status=response.status_code,
                    remote_request_id=response.remote_request_id,
                    finish_reason="stop",
                )
        except CancellationRequested as failure:
            root_cancellation = failure
        except _SiblingCancelled:
            sibling_cancelled = True
        except StepAudioTransportFailure as failure:
            request_failure = _transport_failure(
                failure.kind,
                attempt=attempt,
            )
            retryable = (
                failure.kind in _RETRYABLE_TRANSPORT_FAILURES
                and attempt < _MAX_TRANSPORT_ATTEMPTS
            )
        except TranscriptionFailure as failure:
            request_failure = failure
            retryable = (
                failure.error_code in _TRANSIENT_PROVIDER_ERRORS
                and attempt < _MAX_TRANSPORT_ATTEMPTS
            )
        except Exception as failure:  # noqa: BLE001
            unexpected_failure = failure

        remote_request_id = _safe_remote_request_id(
            response.remote_request_id if response is not None else None
        )
        if root_cancellation is not None:
            _record_request_terminal(
                event_sink,
                correlation_id=correlation_id,
                kind=TranscriptionRemoteRequestEventKind.INTERRUPTED,
                attempt=attempt,
                reason_code="cancellation.root_requested",
                remote_request_id=remote_request_id,
            )
            raise root_cancellation
        if sibling_cancelled:
            _record_request_terminal(
                event_sink,
                correlation_id=correlation_id,
                kind=(
                    TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY
                ),
                attempt=attempt,
                reason_code="concurrency.primary_failed",
                remote_request_id=remote_request_id,
            )
            raise _SiblingCancelled() from None
        if unexpected_failure is not None:
            _record_request_terminal(
                event_sink,
                correlation_id=correlation_id,
                kind=TranscriptionRemoteRequestEventKind.FAILED,
                attempt=attempt,
                reason_code="internal.unexpected",
                remote_request_id=remote_request_id,
            )
            raise unexpected_failure
        if request_failure is not None:
            reason_code = _request_failure_reason(request_failure)
            _record_remote_request_event(
                event_sink,
                TranscriptionRemoteRequestEvent(
                    kind=TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
                    correlation_id=correlation_id,
                    transport_attempt_count=attempt,
                    remote_request_id=remote_request_id,
                    reason_code=reason_code,
                ),
            )
            if retryable:
                delay = _RETRY_DELAYS_SECONDS[attempt - 1]
                _record_remote_request_event(
                    event_sink,
                    TranscriptionRemoteRequestEvent(
                        kind=TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
                        correlation_id=correlation_id,
                        transport_attempt_count=attempt,
                        reason_code=reason_code,
                        retry_delay_ms=int(delay * 1000),
                    ),
                )
                _wait_before_retry(
                    cancellation,
                    attempt,
                    correlation_id=correlation_id,
                    event_sink=event_sink,
                    remote_request_id=remote_request_id,
                )
                continue
            _record_request_terminal(
                event_sink,
                correlation_id=correlation_id,
                kind=TranscriptionRemoteRequestEventKind.FAILED,
                attempt=attempt,
                reason_code=reason_code,
                remote_request_id=remote_request_id,
            )
            raise request_failure from None

        if chunks is None:
            raise AssertionError("StepAudio 成功传输缺少已验证响应")
        _record_remote_request_event(
            event_sink,
            TranscriptionRemoteRequestEvent(
                kind=TranscriptionRemoteRequestEventKind.SUCCEEDED,
                correlation_id=correlation_id,
                transport_attempt_count=attempt,
                remote_request_id=remote_request_id,
            ),
        )
        return _CachedShard(chunks), attempt - 1
    raise AssertionError("StepAudio 传输尝试循环不应自然结束")


def _record_request_terminal(
    event_sink: TranscriptionRemoteRequestEventSink | None,
    *,
    correlation_id: OperationId,
    kind: TranscriptionRemoteRequestEventKind,
    attempt: int,
    reason_code: str,
    remote_request_id: RemoteRequestId | None,
) -> None:
    _record_remote_request_event(
        event_sink,
        TranscriptionRemoteRequestEvent(
            kind=kind,
            correlation_id=correlation_id,
            transport_attempt_count=attempt,
            remote_request_id=remote_request_id,
            reason_code=reason_code,
        ),
    )


def _request_failure_reason(failure: TranscriptionFailure) -> str:
    reason_code = failure.diagnostics.get("reason_code")
    return reason_code if isinstance(reason_code, str) else "internal.unexpected"


def _record_remote_request_event(
    sink: TranscriptionRemoteRequestEventSink | None,
    event: TranscriptionRemoteRequestEvent,
) -> None:
    if sink is None:
        return
    internal_location: InternalLocation | None = None
    try:
        sink.record(event)
    except PreservedApplicationFailure:
        raise
    except Exception as failure:  # noqa: BLE001
        internal_location = _event_sink_internal_location(failure)
    if internal_location is not None:
        raise TranscriptionFailure(
            ErrorCode.INTERNAL_UNEXPECTED,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.MISS,
                retry_count=max(event.transport_attempt_count - 1, 0),
            ),
            diagnostics={
                "source_module": internal_location.source_module,
                "function": internal_location.function,
                "line": internal_location.line,
            },
        ) from None


def _event_sink_internal_location(failure: Exception) -> InternalLocation:
    source_module = "video_auto_editor.transcription.stepaudio"
    function = "_record_remote_request_event"
    line = 1
    traceback = failure.__traceback__
    while traceback is not None:
        candidate_module = traceback.tb_frame.f_globals.get("__name__")
        if (
            isinstance(candidate_module, str)
            and candidate_module.startswith("video_auto_editor.")
        ):
            source_module = candidate_module
            function = traceback.tb_frame.f_code.co_name
            line = traceback.tb_lineno
        traceback = traceback.tb_next
    try:
        return InternalLocation.from_runtime(
            source_module=source_module,
            function=function,
            line=line,
        )
    except (TypeError, ValueError):
        return InternalLocation.from_runtime(
            source_module="video_auto_editor.transcription.stepaudio",
            function="_record_remote_request_event",
            line=1,
        )


def _safe_remote_request_id(value: str | None) -> RemoteRequestId | None:
    if not value:
        return None
    try:
        return RemoteRequestId.from_adapter(value)
    except (TypeError, ValueError):
        return None


_TRANSIENT_PROVIDER_ERRORS = frozenset(
    {
        ErrorCode.TRANSCRIPTION_RATE_LIMITED,
        ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
        ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
        ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
    }
)


def _wait_before_retry(
    cancellation: _CancellationView,
    attempt: int,
    *,
    correlation_id: OperationId,
    event_sink: TranscriptionRemoteRequestEventSink | None,
    remote_request_id: RemoteRequestId | None,
) -> None:
    delay = _RETRY_DELAYS_SECONDS[attempt - 1]
    root_cancellation: CancellationRequested | None = None
    sibling_cancelled = False
    try:
        if cancellation.wait(delay):
            cancellation.raise_if_cancelled()
    except CancellationRequested as failure:
        root_cancellation = failure
    except _SiblingCancelled:
        sibling_cancelled = True
    if root_cancellation is not None:
        _record_request_terminal(
            event_sink,
            correlation_id=correlation_id,
            kind=TranscriptionRemoteRequestEventKind.INTERRUPTED,
            attempt=attempt,
            reason_code="cancellation.root_requested",
            remote_request_id=remote_request_id,
        )
        raise root_cancellation
    if sibling_cancelled:
        _record_request_terminal(
            event_sink,
            correlation_id=correlation_id,
            kind=(
                TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY
            ),
            attempt=attempt,
            reason_code="concurrency.primary_failed",
            remote_request_id=remote_request_id,
        )
        raise _SiblingCancelled() from None


def _transport_failure(
    kind: StepAudioTransportFailureKind,
    *,
    attempt: int,
) -> TranscriptionFailure:
    if kind is StepAudioTransportFailureKind.CONNECT_TIMEOUT:
        code = ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT
        reason = "timeout.connect"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.WRITE_TIMEOUT:
        code = ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT
        reason = "timeout.write"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.READ_TIMEOUT:
        code = ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT
        reason = "timeout.read"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.DNS_FAILED:
        code = ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
        reason = "transport.dns_failed"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.TLS_FAILED:
        code = ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
        reason = "transport.tls_failed"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.RESPONSE_PROTOCOL_INVALID:
        code = ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID
        reason = "protocol.body_invalid"
        finish_reason = None
    elif kind is StepAudioTransportFailureKind.RESPONSE_TRUNCATED:
        code = ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED
        reason = "output.body_truncated"
        finish_reason = "length"
    elif kind is StepAudioTransportFailureKind.RESPONSE_TOO_LARGE:
        code = ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED
        reason = "output.length_limit"
        finish_reason = "length"
    else:
        code = ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
        reason = "transport.connection_failed"
        finish_reason = None
    return _provider_failure(
        code,
        reason_code=reason,
        attempt=attempt,
        finish_reason=finish_reason,
    )


def _parse_response(
    response: StepAudioTransportResponse,
    *,
    duration_ms: int,
    attempt: int,
) -> tuple[TranscriptionChunk, ...]:
    if response.status_code < 200 or response.status_code >= 300:
        raise _http_failure(response, attempt=attempt)
    media_type = response.content_type.split(";", 1)[0].strip().casefold()
    if media_type != "text/event-stream":
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            reason_code="protocol.content_type_invalid",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
        )
    try:
        text = response.body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            reason_code="protocol.body_invalid",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
        ) from None

    deltas: list[tuple[int, int, str]] = []
    done_text: str | None = None
    terminal_seen = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                reason_code="protocol.body_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
            )
        data = line[5:].strip()
        if data == "[DONE]":
            if terminal_seen or done_text is None:
                raise _provider_failure(
                    ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                    reason_code="protocol.body_invalid",
                    attempt=attempt,
                    http_status=response.status_code,
                    remote_request_id=response.remote_request_id,
                )
            terminal_seen = True
            continue
        if terminal_seen or done_text is not None:
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                reason_code="protocol.body_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
            )
        if not data:
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                reason_code="protocol.body_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
            )
        try:
            event = json.loads(data)
        except ValueError:
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                reason_code="protocol.json_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
            ) from None
        if not isinstance(event, Mapping):
            raise _protocol_field_failure(response, attempt, "type")
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise _protocol_field_failure(response, attempt, "type")
        if event_type == "transcript.text.delta":
            deltas.append(
                _parse_delta(
                    event,
                    duration_ms=duration_ms,
                    response=response,
                    attempt=attempt,
                )
            )
        elif event_type == "transcript.text.done":
            candidate = event.get("text")
            if not isinstance(candidate, str):
                raise _protocol_field_failure(response, attempt, "text")
            if done_text is not None:
                raise _provider_failure(
                    ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                    reason_code="protocol.body_invalid",
                    attempt=attempt,
                    http_status=response.status_code,
                    remote_request_id=response.remote_request_id,
                )
            done_text = candidate
        elif event_type == "error":
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_GENERATION_REFUSED,
                reason_code="generation.provider_refused",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
                finish_reason="refusal",
            )
        else:
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
                reason_code="protocol.body_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
            )

    if done_text is None:
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            reason_code="output.incomplete",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
            finish_reason="length",
        )
    joined = "".join(delta for _start, _end, delta in deltas)
    if done_text != joined:
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
            reason_code="protocol.body_invalid",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
        )
    if not deltas:
        return ()
    previous_end = -1
    for start_ms, end_ms, _delta in deltas:
        if start_ms < previous_end or end_ms > duration_ms:
            raise _provider_failure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                reason_code="output.time_invalid",
                attempt=attempt,
                http_status=response.status_code,
                remote_request_id=response.remote_request_id,
                finish_reason="stop",
            )
        previous_end = end_ms
    groups: list[list[tuple[int, int, str]]] = []
    for delta in deltas:
        if groups and groups[-1][-1][1] == delta[0]:
            groups[-1].append(delta)
        else:
            groups.append([delta])
    return tuple(
        TranscriptionChunk(
            start_ms=group[0][0],
            end_ms=group[-1][1],
            text="".join(delta for _start, _end, delta in group),
            character_spans=(
                tuple(
                    CharacterSpan(start_ms, end_ms)
                    for start_ms, end_ms, _delta in group
                )
                if all(
                    len(delta) == 1
                    for _start, _end, delta in group
                )
                else None
            ),
        )
        for group in groups
    )


def _parse_delta(
    event: Mapping[str, Any],
    *,
    duration_ms: int,
    response: StepAudioTransportResponse,
    attempt: int,
) -> tuple[int, int, str]:
    missing = {"delta", "start_time", "end_time"} - event.keys()
    if missing:
        raise _protocol_field_failure(response, attempt, min(missing))
    delta = event["delta"]
    start_ms = event["start_time"]
    end_ms = event["end_time"]
    if not isinstance(delta, str) or not delta.strip():
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
            reason_code="output.text_invalid",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
            finish_reason="stop",
        )
    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
    ):
        raise _protocol_field_failure(response, attempt, "timestamp")
    if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
        raise _provider_failure(
            ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
            reason_code="output.time_invalid",
            attempt=attempt,
            http_status=response.status_code,
            remote_request_id=response.remote_request_id,
            finish_reason="stop",
        )
    return start_ms, end_ms, delta


def _protocol_field_failure(
    response: StepAudioTransportResponse,
    attempt: int,
    _field: str,
) -> TranscriptionFailure:
    return _provider_failure(
        ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
        reason_code="protocol.field_missing",
        attempt=attempt,
        http_status=response.status_code,
        remote_request_id=response.remote_request_id,
    )


def _http_failure(
    response: StepAudioTransportResponse,
    *,
    attempt: int,
) -> TranscriptionFailure:
    status = response.status_code
    if status in {401, 403}:
        code = ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED
        reason = "authentication.credential_rejected"
    elif status == 429:
        code = ErrorCode.TRANSCRIPTION_RATE_LIMITED
        reason = "rate_limit.requests"
    elif status in {408, 504}:
        code = ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT
        reason = "timeout.overall"
    elif 500 <= status <= 599:
        code = ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
        reason = "service.server_error"
    elif 400 <= status <= 499:
        code = ErrorCode.TRANSCRIPTION_REQUEST_REJECTED
        reason = "request.invalid"
    else:
        code = ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID
        reason = "protocol.status_invalid"
    return _provider_failure(
        code,
        reason_code=reason,
        attempt=attempt,
        http_status=status,
        remote_request_id=response.remote_request_id,
    )


def _provider_failure(
    code: ErrorCode,
    *,
    reason_code: str,
    attempt: int,
    http_status: int | None = None,
    remote_request_id: str | None = None,
    finish_reason: str | None = None,
) -> TranscriptionFailure:
    diagnostics: dict[str, object] = {
        "attempt": attempt,
        "reason_code": reason_code,
    }
    if http_status is not None:
        diagnostics["http_status"] = http_status
    if remote_request_id:
        try:
            diagnostics["remote_request_id"] = RemoteRequestId.from_adapter(
                remote_request_id
            )
        except (TypeError, ValueError):
            pass
    if finish_reason is not None:
        diagnostics["finish_reason"] = finish_reason
    return TranscriptionFailure(
        code,
        execution_facts=ExecutionFacts(
            cache_use=CacheUse.MISS,
            retry_count=attempt - 1,
        ),
        diagnostics=diagnostics,
    )


def _normalized_audio_read_failure() -> TranscriptionFailure:
    return TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={
            "operation": "ffmpeg.transcode",
            "reason_code": "media.output_invalid",
        },
    )


def _request_body(pcm: bytes, settings: StepAudioSettings) -> bytes:
    return json.dumps(
        {
            "audio": {
                "data": base64.b64encode(pcm).decode("ascii"),
                "input": {
                    "transcription": {
                        "language": settings.language,
                        "model": settings.model,
                        "enable_itn": True,
                        "enable_timestamp": True,
                    },
                    "format": {
                        "type": "pcm",
                        "codec": "pcm_s16le",
                        "rate": PCM_SAMPLE_RATE,
                        "bits": PCM_BITS_PER_SAMPLE,
                        "channel": PCM_CHANNELS,
                    },
                },
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _shift_chunk(
    chunk: TranscriptionChunk,
    offset_ms: int,
) -> TranscriptionChunk:
    spans = (
        tuple(
            CharacterSpan(
                span.start_ms + offset_ms,
                span.end_ms + offset_ms,
            )
            for span in chunk.character_spans
        )
        if chunk.character_spans is not None
        else None
    )
    return TranscriptionChunk(
        start_ms=chunk.start_ms + offset_ms,
        end_ms=chunk.end_ms + offset_ms,
        text=chunk.text,
        character_spans=spans,
    )


def _whole_cache_spec(
    audio: NormalizedPcmAudio,
    settings: StepAudioSettings,
) -> CacheEntrySpec[_CachedTranscript]:
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version=_WHOLE_IDENTITY_SCHEMA,
        algorithm_version=_WHOLE_ALGORITHM_VERSION,
        payload_schema_version=_WHOLE_PAYLOAD_SCHEMA,
        adapter_id=_ADAPTER_ID,
        model_id=settings.model,
        configuration_fingerprint=_result_configuration_fingerprint(settings),
        result_inputs={
            "pcm": {
                "sha256": audio.content_sha256,
                "byte_length": audio.byte_length,
                "sample_length": audio.sample_length,
            },
            "language": settings.language,
            "audio_rule_version": AUDIO_NORMALIZATION_VERSION,
            "shard_plan_version": _SHARD_PLAN_VERSION,
            "core_region_version": _CORE_REGION_VERSION,
            "merge_version": _MERGE_VERSION,
            "speech_evidence_version": SPEECH_EVIDENCE_VERSION,
            "coverage_version": _COVERAGE_VERSION,
            "deduplication_version": _DEDUPLICATION_VERSION,
            "validation_version": _RESULT_VALIDATION_VERSION,
        },
    )
    return CacheEntrySpec(
        identity=identity,
        encode=_encode_transcript,
        decode=lambda payload: _decode_transcript(
            payload,
            duration_ms=audio.duration_ms,
        ),
    )


def _shard_cache_spec(
    pcm: bytes,
    settings: StepAudioSettings,
) -> CacheEntrySpec[_CachedShard]:
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPTION_SHARD,
        identity_schema_version=_SHARD_IDENTITY_SCHEMA,
        algorithm_version=_SHARD_ALGORITHM_VERSION,
        payload_schema_version=_SHARD_PAYLOAD_SCHEMA,
        adapter_id=_ADAPTER_ID,
        model_id=settings.model,
        configuration_fingerprint=_result_configuration_fingerprint(settings),
        result_inputs={
            "pcm": {
                "sha256": hashlib.sha256(pcm).hexdigest(),
                "sample_length": len(pcm) // PCM_BYTES_PER_SAMPLE,
            },
            "language": settings.language,
            "request_version": _REQUEST_VERSION,
            "response_parser_version": _RESPONSE_PARSER_VERSION,
            "validation_version": _SHARD_VALIDATION_VERSION,
        },
    )
    return CacheEntrySpec(
        identity=identity,
        encode=_encode_shard,
        decode=lambda payload: _decode_shard(
            payload,
            duration_ms=len(pcm) // PCM_BYTES_PER_MILLISECOND,
        ),
    )


def _result_configuration_fingerprint(
    settings: StepAudioSettings,
) -> str:
    canonical = json.dumps(
        {
            "endpoint": settings.endpoint,
            "enable_itn": True,
            "enable_timestamp": True,
            "language": settings.language,
            "model": settings.model,
            "request_version": _REQUEST_VERSION,
            "response_parser_version": _RESPONSE_PARSER_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _encode_transcript(value: _CachedTranscript) -> object:
    return {
        "chunks": [_encode_chunk(chunk) for chunk in value.chunks],
        "speech_presence": value.speech_presence.value,
    }


def _decode_transcript(
    payload: object,
    *,
    duration_ms: int,
) -> _CachedTranscript:
    try:
        if not isinstance(payload, Mapping) or set(payload) != {
            "chunks",
            "speech_presence",
        }:
            raise ValueError
        chunks_payload = payload["chunks"]
        if not isinstance(chunks_payload, list):
            raise TypeError
        chunks = tuple(_decode_chunk(item) for item in chunks_payload)
        _validate_cached_chunks(chunks, duration_ms=duration_ms)
        presence = SpeechPresence(payload["speech_presence"])
        if presence is SpeechPresence.PRESENT and not chunks:
            raise ValueError
        if presence is SpeechPresence.ABSENT and chunks:
            raise ValueError
        return _CachedTranscript(chunks, presence)
    except (KeyError, TypeError, ValueError):
        raise CachedPayloadInvalid() from None


def _encode_shard(value: _CachedShard) -> object:
    return {"chunks": [_encode_chunk(chunk) for chunk in value.chunks]}


def _decode_shard(
    payload: object,
    *,
    duration_ms: int,
) -> _CachedShard:
    try:
        if not isinstance(payload, Mapping) or set(payload) != {"chunks"}:
            raise ValueError
        chunks_payload = payload["chunks"]
        if not isinstance(chunks_payload, list):
            raise TypeError
        chunks = tuple(_decode_chunk(item) for item in chunks_payload)
        _validate_cached_chunks(chunks, duration_ms=duration_ms)
        return _CachedShard(chunks)
    except (KeyError, TypeError, ValueError):
        raise CachedPayloadInvalid() from None


def _encode_chunk(chunk: TranscriptionChunk) -> object:
    return {
        "character_spans": (
            [
                {"end_ms": span.end_ms, "start_ms": span.start_ms}
                for span in chunk.character_spans
            ]
            if chunk.character_spans is not None
            else None
        ),
        "end_ms": chunk.end_ms,
        "start_ms": chunk.start_ms,
        "text": chunk.text,
    }


def _decode_chunk(payload: object) -> TranscriptionChunk:
    if not isinstance(payload, Mapping) or set(payload) != {
        "character_spans",
        "end_ms",
        "start_ms",
        "text",
    }:
        raise ValueError
    raw_spans = payload["character_spans"]
    if raw_spans is None:
        spans = None
    else:
        if not isinstance(raw_spans, list):
            raise ValueError
        spans = tuple(
            CharacterSpan(
                start_ms=_mapping_integer(span, "start_ms"),
                end_ms=_mapping_integer(span, "end_ms"),
            )
            for span in raw_spans
        )
    return TranscriptionChunk(
        start_ms=_mapping_integer(payload, "start_ms"),
        end_ms=_mapping_integer(payload, "end_ms"),
        text=payload["text"],
        character_spans=spans,
    )


def _mapping_integer(payload: object, field_name: str) -> int:
    if not isinstance(payload, Mapping):
        raise TypeError
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError
    return value


def _validate_cached_chunks(
    chunks: tuple[TranscriptionChunk, ...],
    *,
    duration_ms: int,
) -> None:
    previous_key: tuple[int, int] | None = None
    for chunk in chunks:
        key = (chunk.start_ms, chunk.end_ms)
        if chunk.end_ms > duration_ms:
            raise ValueError
        if previous_key is not None and key < previous_key:
            raise ValueError
        previous_key = key


def _readiness_issues(value: object) -> tuple[ReadinessIssue, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(issue, ReadinessIssue) for issue in value
    ):
        raise TypeError("StepAudio 准备端口必须返回 ReadinessIssue 元组")
    return value


def _valid_https_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65_535)
    )


__all__ = [
    "NormalizedPcmAudio",
    "StepAudioSettings",
    "StepAudioSpeechRecognition",
    "StepAudioTransportFailure",
    "StepAudioTransportFailureKind",
    "StepAudioTransportRequest",
    "StepAudioTransportResponse",
]
