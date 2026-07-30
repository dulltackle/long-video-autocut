"""可由生产组合根装配的 StepFun 纯文本生成 Adapter。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from threading import BoundedSemaphore
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol, TypeGuard
from urllib.parse import urlsplit

from video_auto_editor.runtime._classified_failure import (
    PreservedApplicationFailure,
)
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import RemoteRequestId

from .interface import (
    ReadinessIssue,
    ReadinessReport,
    ReasoningEffort,
    TextGenerationRequest,
    TextGenerationResponse,
    TextModelEvent,
    TextModelEventKind,
    TextModelEventSink,
    TextModelExecutionFacts,
    TextModelFailure,
    TextModelFailureKind,
    TextModelReadinessCode,
)

_REQUEST_VERSION = "stepfun-chat-completions-request.v1"
_RESPONSE_VERSION = "stepfun-chat-completions-response.v1"
_MAX_CONCURRENCY = 32
_MAX_TRANSPORT_ATTEMPTS = 32
_SEMAPHORE_POLL_SECONDS = 0.01
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_PROTECTED_HEADERS = frozenset(
    {
        "accept",
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "host",
        "proxy-authorization",
    }
)
_TRANSIENT_FAILURES = frozenset(
    {
        TextModelFailureKind.RATE_LIMITED,
        TextModelFailureKind.REQUEST_TIMEOUT,
        TextModelFailureKind.SERVICE_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class StepFunSettings:
    """组合根交给 StepFun Adapter 的认证传输策略。"""

    endpoint: str = field(repr=False)
    timeout_seconds: int
    max_concurrency: int
    max_transport_attempts: int = 3
    retry_delays_seconds: tuple[float, ...] = (0.05, 0.1)

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TypeError("StepFun endpoint 必须是非空字符串")
        invalid_unicode = False
        try:
            self.endpoint.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            invalid_unicode = True
        if invalid_unicode:
            raise ValueError("StepFun endpoint 必须是有效 Unicode 文本") from None
        _bounded_integer(
            self.timeout_seconds,
            field_name="StepFun 超时秒数",
            minimum=1,
            maximum=600,
        )
        _bounded_integer(
            self.max_concurrency,
            field_name="StepFun 最大并发数",
            minimum=1,
            maximum=_MAX_CONCURRENCY,
        )
        _bounded_integer(
            self.max_transport_attempts,
            field_name="StepFun 最大传输尝试次数",
            minimum=1,
            maximum=_MAX_TRANSPORT_ATTEMPTS,
        )
        if (
            not isinstance(self.retry_delays_seconds, tuple)
            or len(self.retry_delays_seconds) != self.max_transport_attempts - 1
        ):
            raise ValueError("StepFun 重试退避必须逐次覆盖额外传输尝试")
        normalized_delays = []
        for delay in self.retry_delays_seconds:
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(delay)
                or not 0.0 <= delay <= 60.0
            ):
                raise ValueError("StepFun 重试退避必须位于 0 到 60 秒")
            normalized_delays.append(float(delay))
        object.__setattr__(
            self,
            "retry_delays_seconds",
            tuple(normalized_delays),
        )


@dataclass(frozen=True, slots=True)
class StepFunTransportRequest:
    """Adapter 交给单次 HTTPS 传输的最小请求。"""

    endpoint: str = field(repr=False)
    credential: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TypeError("StepFun 传输 endpoint 必须是非空字符串")
        if not isinstance(self.credential, str):
            raise TypeError("StepFun 传输凭据必须是字符串")
        if not isinstance(self.headers, Mapping):
            raise TypeError("StepFun 自定义请求头必须是映射")
        frozen_headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("StepFun 自定义请求头必须是字符串映射")
            frozen_headers[name] = value
        object.__setattr__(
            self,
            "headers",
            MappingProxyType(frozen_headers),
        )
        if not isinstance(self.body, bytes):
            raise TypeError("StepFun 传输正文必须是 bytes")
        _bounded_integer(
            self.timeout_seconds,
            field_name="StepFun 传输超时秒数",
            minimum=1,
            maximum=600,
        )


@dataclass(frozen=True, slots=True)
class StepFunTransportResponse:
    """HTTPS 传输交给 Adapter 的有限响应事实。"""

    status_code: int
    content_type: str
    body: bytes = field(repr=False)
    remote_request_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _bounded_integer(
            self.status_code,
            field_name="StepFun HTTP 状态码",
            minimum=100,
            maximum=599,
        )
        if not isinstance(self.content_type, str):
            raise TypeError("StepFun Content-Type 必须是字符串")
        if not isinstance(self.body, bytes):
            raise TypeError("StepFun 响应正文必须是 bytes")
        if self.remote_request_id is not None and not isinstance(
            self.remote_request_id, str
        ):
            raise TypeError("StepFun 远端请求标识必须是字符串或 None")


class StepFunTransportFailureKind(str, Enum):
    """单次 HTTPS 传输可以披露给 Adapter 的稳定失败。"""

    CONNECT_TIMEOUT = "connect_timeout"
    WRITE_TIMEOUT = "write_timeout"
    READ_TIMEOUT = "read_timeout"
    DNS_FAILED = "dns_failed"
    CONNECTION_FAILED = "connection_failed"
    TLS_FAILED = "tls_failed"
    RESPONSE_PROTOCOL_INVALID = "response_protocol_invalid"
    RESPONSE_TRUNCATED = "response_truncated"
    RESPONSE_TOO_LARGE = "response_too_large"


class StepFunTransportFailure(RuntimeError):
    """不携带套接字异常、正文或请求配置的单次传输失败。"""

    __slots__ = ("kind",)

    def __init__(self, kind: StepFunTransportFailureKind) -> None:
        if not isinstance(kind, StepFunTransportFailureKind):
            raise TypeError("StepFun 传输失败必须使用稳定 kind")
        self.kind = kind
        super().__init__("StepFun HTTPS 传输失败")


class StepFunTransport(Protocol):
    """生产 HTTPS 与测试伪传输共同满足的内部窄接口。"""

    def check_readiness(self) -> tuple[ReadinessIssue, ...]: ...

    def send(
        self,
        request: StepFunTransportRequest,
        cancellation: CancellationToken,
    ) -> StepFunTransportResponse: ...


class StepFunTextModel:
    """隐藏 StepFun 协议、凭据、重试与供应商并发的共享 Adapter。"""

    __slots__ = (
        "_clock",
        "_credential",
        "_endpoint",
        "_event_sink",
        "_headers",
        "_semaphore",
        "_settings",
        "_transport",
    )

    def __init__(
        self,
        settings: StepFunSettings,
        *,
        credential: str,
        headers: Mapping[str, str] | None = None,
        transport: StepFunTransport | None = None,
        event_sink: TextModelEventSink | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not isinstance(settings, StepFunSettings):
            raise TypeError("StepFun 文本模型必须使用 StepFunSettings")
        if not isinstance(credential, str):
            raise TypeError("StepFun 凭据必须是字符串")
        if not callable(clock):
            raise TypeError("StepFun 单调时钟必须可调用")
        if event_sink is not None and not callable(getattr(event_sink, "record", None)):
            raise TypeError("StepFun 文本模型必须使用事件接收端")
        frozen_headers = _freeze_headers(headers)
        endpoint = _chat_completions_endpoint(settings.endpoint)
        if transport is None:
            from ._stepfun_https import StdlibStepFunTransport

            transport = StdlibStepFunTransport(endpoint)
        if not callable(getattr(transport, "check_readiness", None)) or not callable(
            getattr(transport, "send", None)
        ):
            raise TypeError("StepFun 文本模型必须使用传输端口")
        self._settings = settings
        self._credential = credential
        self._headers = frozen_headers
        self._endpoint = endpoint
        self._transport = transport
        self._event_sink = event_sink
        self._clock = clock
        self._semaphore = BoundedSemaphore(settings.max_concurrency)

    def check_readiness(self) -> ReadinessReport:
        """聚合全部本地阻塞项，不发送业务请求。"""
        issues = list(self._configuration_issues())
        issues.extend(_readiness_issues(self._transport.check_readiness()))
        unique: list[ReadinessIssue] = []
        for issue in issues:
            if issue not in unique:
                unique.append(issue)
        return ReadinessReport(
            ready=not unique,
            configuration_fingerprint=self._configuration_fingerprint(),
            issues=tuple(unique),
        )

    def generate(
        self,
        request: TextGenerationRequest,
    ) -> TextGenerationResponse:
        """完成一次逻辑生成，并在内部收敛传输重试与并发许可。"""
        if not isinstance(request, TextGenerationRequest):
            raise TypeError("StepFun 文本模型只接受 TextGenerationRequest")
        started = _read_clock(self._clock)
        self._record_event(
            TextModelEvent(
                kind=TextModelEventKind.CALL_STARTED,
                observation=request.observation,
                transport_attempt_count=0,
                elapsed_ms=_elapsed_ms(self._clock, started),
            )
        )
        if request.cancellation.cancelled:
            failure = _cancelled_failure(
                request.cancellation,
                attempts=0,
                elapsed_ms=_elapsed_ms(self._clock, started),
            )
            self._record_terminal(request, failure)
            raise failure from None
        configuration_issues = self._configuration_issues()
        if configuration_issues:
            issue = configuration_issues[0]
            diagnostics: dict[str, object] = {
                "reason_code": f"configuration.{issue.code.value}"
            }
            field_name = issue.diagnostics.get("field")
            if isinstance(field_name, str):
                diagnostics["field"] = field_name
            failure = TextModelFailure(
                TextModelFailureKind.INVALID_CONFIGURATION,
                execution_facts=TextModelExecutionFacts(
                    transport_attempt_count=0,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                ),
                diagnostics=diagnostics,
            )
            self._record_terminal(request, failure)
            raise failure from None

        acquired = False
        try:
            while not acquired:
                if request.cancellation.cancelled:
                    failure = _cancelled_failure(
                        request.cancellation,
                        attempts=0,
                        elapsed_ms=_elapsed_ms(self._clock, started),
                    )
                    self._record_terminal(request, failure)
                    raise failure from None
                acquired = self._semaphore.acquire(timeout=_SEMAPHORE_POLL_SECONDS)
            return self._generate_with_retries(request, started=started)
        finally:
            if acquired:
                self._semaphore.release()

    def _generate_with_retries(
        self,
        request: TextGenerationRequest,
        *,
        started: float,
    ) -> TextGenerationResponse:
        serialization_failed = False
        try:
            body = _request_body(request)
        except Exception:  # noqa: BLE001
            serialization_failed = True
        if serialization_failed:
            failure = _failure(
                TextModelFailureKind.INTERNAL,
                attempt=0,
                elapsed_ms=_elapsed_ms(self._clock, started),
                reason_code="adapter.request_serialization_failed",
            )
            self._record_terminal(request, failure)
            raise failure from None
        transport_request = StepFunTransportRequest(
            endpoint=self._endpoint,
            credential=self._credential,
            headers=self._headers,
            body=body,
            timeout_seconds=self._settings.timeout_seconds,
        )
        for attempt in range(
            1,
            self._settings.max_transport_attempts + 1,
        ):
            if request.cancellation.cancelled:
                failure = _cancelled_failure(
                    request.cancellation,
                    attempts=attempt - 1,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
                self._record_terminal(request, failure)
                raise failure from None

            transport_failure: StepFunTransportFailure | None = None
            cancellation_failure: CancellationRequested | None = None
            unexpected_failure: Exception | None = None
            response: object | None = None
            try:
                response = self._transport.send(
                    transport_request,
                    request.cancellation,
                )
            except CancellationRequested as caught_cancellation:
                cancellation_failure = caught_cancellation
            except StepFunTransportFailure as caught_transport_failure:
                transport_failure = caught_transport_failure
            except Exception as caught_unexpected:  # noqa: BLE001
                unexpected_failure = caught_unexpected

            if cancellation_failure is not None:
                failure = _cancelled_failure(
                    request.cancellation,
                    attempts=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                    signal_number=cancellation_failure.signal_number,
                )
                self._record_terminal(request, failure)
                raise failure from None
            if unexpected_failure is not None:
                failure = _failure(
                    TextModelFailureKind.INTERNAL,
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                    reason_code="adapter.unexpected_failure",
                )
                self._record_terminal(request, failure)
                raise failure from None
            if transport_failure is not None:
                failure = _transport_failure(
                    transport_failure.kind,
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
                self._record_transport_failure(request, failure)
                if self._should_retry(failure, attempt=attempt):
                    self._wait_before_retry(
                        request,
                        failure,
                        attempt,
                        started,
                    )
                    continue
                self._record_terminal(request, failure)
                raise failure from None

            normalized_failure: TextModelFailure | None
            try:
                normalized = _transport_response(
                    response,
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
            except TextModelFailure as caught_failure:
                normalized_failure = caught_failure
            else:
                normalized_failure = None
            if normalized_failure is not None:
                self._record_terminal(request, normalized_failure)
                raise normalized_failure from None
            if not 200 <= normalized.status_code <= 299:
                failure = _http_failure(
                    normalized,
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
                self._record_transport_failure(request, failure)
                if self._should_retry(failure, attempt=attempt):
                    self._wait_before_retry(
                        request,
                        failure,
                        attempt,
                        started,
                    )
                    continue
                self._record_terminal(request, failure)
                raise failure from None

            parse_failure: TextModelFailure | None
            try:
                result = _parse_success_response(
                    normalized,
                    attempt=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
            except TextModelFailure as caught_failure:
                parse_failure = caught_failure
            else:
                parse_failure = None
            if parse_failure is not None:
                self._record_terminal(request, parse_failure)
                raise parse_failure from None
            if request.cancellation.cancelled:
                failure = _cancelled_failure(
                    request.cancellation,
                    attempts=attempt,
                    elapsed_ms=_elapsed_ms(self._clock, started),
                )
                self._record_terminal(request, failure)
                raise failure from None
            self._record_event(
                TextModelEvent(
                    kind=TextModelEventKind.SUCCEEDED,
                    observation=request.observation,
                    transport_attempt_count=(
                        result.execution_facts.transport_attempt_count
                    ),
                    elapsed_ms=result.execution_facts.elapsed_ms,
                )
            )
            return result
        raise AssertionError("StepFun 传输尝试循环不应自然结束")

    def _should_retry(
        self,
        failure: TextModelFailure,
        *,
        attempt: int,
    ) -> bool:
        if attempt >= self._settings.max_transport_attempts:
            return False
        if failure.kind in _TRANSIENT_FAILURES:
            return failure.diagnostics.get("reason_code") != "transport.tls_failed"
        return (
            failure.kind is TextModelFailureKind.OUTPUT_TRUNCATED
            and failure.diagnostics.get("reason_code") == "output.body_truncated"
        )

    def _wait_before_retry(
        self,
        request: TextGenerationRequest,
        failure: TextModelFailure,
        attempt: int,
        started: float,
    ) -> None:
        delay = self._settings.retry_delays_seconds[attempt - 1]
        reason = failure.diagnostics.get("reason_code")
        self._record_event(
            TextModelEvent(
                kind=TextModelEventKind.RETRY_PLANNED,
                observation=request.observation,
                transport_attempt_count=attempt,
                elapsed_ms=_elapsed_ms(self._clock, started),
                failure_kind=failure.kind,
                reason_code=reason if isinstance(reason, str) else None,
                retry_delay_ms=int(delay * 1000),
            )
        )
        if request.cancellation.wait(delay):
            cancellation_failure = _cancelled_failure(
                request.cancellation,
                attempts=attempt,
                elapsed_ms=_elapsed_ms(self._clock, started),
            )
            self._record_terminal(request, cancellation_failure)
            raise cancellation_failure from None

    def _record_transport_failure(
        self,
        request: TextGenerationRequest,
        failure: TextModelFailure,
    ) -> None:
        reason = failure.diagnostics.get("reason_code")
        self._record_event(
            TextModelEvent(
                kind=TextModelEventKind.TRANSPORT_FAILED,
                observation=request.observation,
                transport_attempt_count=(
                    failure.execution_facts.transport_attempt_count
                ),
                elapsed_ms=failure.execution_facts.elapsed_ms,
                failure_kind=failure.kind,
                reason_code=reason if isinstance(reason, str) else None,
            )
        )

    def _record_terminal(
        self,
        request: TextGenerationRequest,
        failure: TextModelFailure,
    ) -> None:
        reason = failure.diagnostics.get("reason_code")
        self._record_event(
            TextModelEvent(
                kind=(
                    TextModelEventKind.CANCELLED
                    if failure.kind is TextModelFailureKind.CANCELLED
                    else TextModelEventKind.FAILED
                ),
                observation=request.observation,
                transport_attempt_count=(
                    failure.execution_facts.transport_attempt_count
                ),
                elapsed_ms=failure.execution_facts.elapsed_ms,
                failure_kind=failure.kind,
                reason_code=reason if isinstance(reason, str) else None,
            )
        )

    def _record_event(self, event: TextModelEvent) -> None:
        if self._event_sink is None:
            return
        sink_failed = False
        try:
            self._event_sink.record(event)
        except PreservedApplicationFailure:
            raise
        except Exception:  # noqa: BLE001
            sink_failed = True
        if sink_failed:
            raise _failure(
                TextModelFailureKind.INTERNAL,
                attempt=event.transport_attempt_count,
                elapsed_ms=event.elapsed_ms,
                reason_code="observation.sink_failed",
            ) from None

    def _configuration_issues(self) -> tuple[ReadinessIssue, ...]:
        issues = []
        if not self._credential:
            issues.append(
                ReadinessIssue(
                    TextModelReadinessCode.CREDENTIAL_MISSING,
                )
            )
        elif not _safe_header_value(self._credential):
            issues.append(
                ReadinessIssue(
                    TextModelReadinessCode.HEADERS_INVALID,
                    {"field": "text_model_provider_config.credential"},
                )
            )
        if not _valid_custom_headers(self._headers):
            issues.append(
                ReadinessIssue(
                    TextModelReadinessCode.HEADERS_INVALID,
                    {"field": "text_model_provider_config.headers"},
                )
            )
        if not _valid_https_base_endpoint(self._settings.endpoint):
            issues.append(
                ReadinessIssue(
                    TextModelReadinessCode.HTTPS_REQUIRED,
                    {"field": "text_model_provider_config.endpoint"},
                )
            )
        return tuple(issues)

    def _configuration_fingerprint(self) -> str:
        payload = json.dumps(
            {
                "endpoint": self._settings.endpoint.rstrip("/"),
                "headers": sorted(
                    (name.casefold(), value) for name, value in self._headers.items()
                ),
                "request_version": _REQUEST_VERSION,
                "response_version": _RESPONSE_VERSION,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _request_body(request: TextGenerationRequest) -> bytes:
    payload: dict[str, object] = {
        "model": request.settings.model,
        "messages": [
            {
                "role": message.role.value,
                "content": message.content,
            }
            for message in request.messages
        ],
        "temperature": request.settings.temperature,
        "stream": False,
    }
    if request.settings.reasoning_effort is not ReasoningEffort.NONE:
        payload["reasoning_effort"] = request.settings.reasoning_effort.value
    if request.settings.max_output_tokens is not None:
        payload["max_tokens"] = request.settings.max_output_tokens
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parse_success_response(
    response: StepFunTransportResponse,
    *,
    attempt: int,
    elapsed_ms: int,
) -> TextGenerationResponse:
    media_type = response.content_type.split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.content_type_invalid",
        ) from None
    json_invalid = False
    try:
        decoded = response.body.decode("utf-8", errors="strict")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        json_invalid = True
    if json_invalid:
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.json_invalid",
        ) from None
    if not isinstance(payload, Mapping):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.body_invalid",
        ) from None
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_missing",
        ) from None
    normalized_finish = (
        "refusal" if finish_reason in {"content_filter", "refusal"} else finish_reason
    )
    input_tokens, output_tokens = _usage(payload, response, attempt, elapsed_ms)
    request_id = _safe_remote_request_id(response.remote_request_id)
    facts = TextModelExecutionFacts(
        transport_attempt_count=attempt,
        elapsed_ms=elapsed_ms,
        finish_reason=(
            normalized_finish
            if normalized_finish in {"stop", "length", "refusal"}
            else None
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        remote_request_id=request_id,
    )
    if normalized_finish == "length":
        raise TextModelFailure(
            TextModelFailureKind.OUTPUT_TRUNCATED,
            execution_facts=facts,
            diagnostics=_provider_diagnostics(
                response,
                attempt=attempt,
                reason_code="output.length_limit",
                finish_reason="length",
            ),
        ) from None
    if normalized_finish == "refusal":
        raise TextModelFailure(
            TextModelFailureKind.GENERATION_REFUSED,
            execution_facts=facts,
            diagnostics=_provider_diagnostics(
                response,
                attempt=attempt,
                reason_code="generation.provider_refused",
                finish_reason="refusal",
            ),
        ) from None
    if normalized_finish != "stop":
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    message = choice.get("message")
    if not isinstance(message, Mapping) or "content" not in message:
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_missing",
        ) from None
    if message.get("role") != "assistant":
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    content = message["content"]
    if not isinstance(content, str):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    invalid_content_unicode = False
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        invalid_content_unicode = True
    if invalid_content_unicode:
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    return TextGenerationResponse(
        text=content,
        execution_facts=facts,
    )


def _usage(
    payload: Mapping[str, Any],
    response: StepFunTransportResponse,
    attempt: int,
    elapsed_ms: int,
) -> tuple[int | None, int | None]:
    usage = payload.get("usage")
    if usage is None:
        return None, None
    if not isinstance(usage, Mapping):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if not _non_negative_integer(prompt_tokens) or not _non_negative_integer(
        completion_tokens
    ):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    total_tokens = usage.get("total_tokens")
    if total_tokens is not None and (
        not _non_negative_integer(total_tokens)
        or total_tokens != prompt_tokens + completion_tokens
    ):
        raise _protocol_failure(
            response,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="protocol.field_type_invalid",
        ) from None
    return prompt_tokens, completion_tokens


def _transport_response(
    value: object,
    *,
    attempt: int,
    elapsed_ms: int,
) -> StepFunTransportResponse:
    if not isinstance(value, StepFunTransportResponse):
        raise _failure(
            TextModelFailureKind.INTERNAL,
            attempt=attempt,
            elapsed_ms=elapsed_ms,
            reason_code="adapter.transport_result_invalid",
        ) from None
    return value


def _http_failure(
    response: StepFunTransportResponse,
    *,
    attempt: int,
    elapsed_ms: int,
) -> TextModelFailure:
    status = response.status_code
    if status in {401, 403}:
        kind = TextModelFailureKind.AUTHENTICATION_FAILED
        reason = "authentication.credential_rejected"
    elif status == 429:
        kind = TextModelFailureKind.RATE_LIMITED
        reason = "rate_limit.requests"
    elif status in {408, 504}:
        kind = TextModelFailureKind.REQUEST_TIMEOUT
        reason = "timeout.overall"
    elif 500 <= status <= 599:
        kind = TextModelFailureKind.SERVICE_UNAVAILABLE
        reason = "service.server_error"
    elif 400 <= status <= 499:
        kind = TextModelFailureKind.REQUEST_REJECTED
        reason = "request.invalid"
    else:
        kind = TextModelFailureKind.RESPONSE_PROTOCOL_INVALID
        reason = "protocol.status_invalid"
    return TextModelFailure(
        kind,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=attempt,
            elapsed_ms=elapsed_ms,
        ),
        diagnostics=_provider_diagnostics(
            response,
            attempt=attempt,
            reason_code=reason,
        ),
    )


def _transport_failure(
    kind: StepFunTransportFailureKind,
    *,
    attempt: int,
    elapsed_ms: int,
) -> TextModelFailure:
    if kind is StepFunTransportFailureKind.CONNECT_TIMEOUT:
        failure_kind = TextModelFailureKind.REQUEST_TIMEOUT
        reason = "timeout.connect"
    elif kind is StepFunTransportFailureKind.WRITE_TIMEOUT:
        failure_kind = TextModelFailureKind.REQUEST_TIMEOUT
        reason = "timeout.write"
    elif kind is StepFunTransportFailureKind.READ_TIMEOUT:
        failure_kind = TextModelFailureKind.REQUEST_TIMEOUT
        reason = "timeout.read"
    elif kind is StepFunTransportFailureKind.DNS_FAILED:
        failure_kind = TextModelFailureKind.SERVICE_UNAVAILABLE
        reason = "transport.dns_failed"
    elif kind is StepFunTransportFailureKind.CONNECTION_FAILED:
        failure_kind = TextModelFailureKind.SERVICE_UNAVAILABLE
        reason = "transport.connection_failed"
    elif kind is StepFunTransportFailureKind.TLS_FAILED:
        failure_kind = TextModelFailureKind.SERVICE_UNAVAILABLE
        reason = "transport.tls_failed"
    elif kind is StepFunTransportFailureKind.RESPONSE_TRUNCATED:
        failure_kind = TextModelFailureKind.OUTPUT_TRUNCATED
        reason = "output.body_truncated"
    elif kind is StepFunTransportFailureKind.RESPONSE_TOO_LARGE:
        failure_kind = TextModelFailureKind.OUTPUT_TRUNCATED
        reason = "output.length_limit"
    else:
        failure_kind = TextModelFailureKind.RESPONSE_PROTOCOL_INVALID
        reason = "protocol.body_invalid"
    facts = TextModelExecutionFacts(
        transport_attempt_count=attempt,
        elapsed_ms=elapsed_ms,
    )
    diagnostics: dict[str, object] = {
        "attempt": attempt,
        "reason_code": reason,
    }
    return TextModelFailure(
        failure_kind,
        execution_facts=facts,
        diagnostics=diagnostics,
    )


def _protocol_failure(
    response: StepFunTransportResponse,
    *,
    attempt: int,
    elapsed_ms: int,
    reason_code: str,
) -> TextModelFailure:
    return TextModelFailure(
        TextModelFailureKind.RESPONSE_PROTOCOL_INVALID,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=attempt,
            elapsed_ms=elapsed_ms,
        ),
        diagnostics=_provider_diagnostics(
            response,
            attempt=attempt,
            reason_code=reason_code,
        ),
    )


def _failure(
    kind: TextModelFailureKind,
    *,
    attempt: int,
    elapsed_ms: int,
    reason_code: str,
) -> TextModelFailure:
    diagnostics: dict[str, object] = {"reason_code": reason_code}
    if attempt:
        diagnostics["attempt"] = attempt
    return TextModelFailure(
        kind,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=attempt,
            elapsed_ms=elapsed_ms,
        ),
        diagnostics=diagnostics,
    )


def _cancelled_failure(
    cancellation: CancellationToken,
    *,
    attempts: int,
    elapsed_ms: int,
    signal_number: int | None = None,
) -> TextModelFailure:
    signal_value = (
        cancellation.signal_number if signal_number is None else signal_number
    )
    diagnostics: dict[str, object] = {}
    if attempts:
        diagnostics["attempt"] = attempts
    if signal_value is not None:
        diagnostics["signal_number"] = signal_value
    return TextModelFailure(
        TextModelFailureKind.CANCELLED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=attempts,
            elapsed_ms=elapsed_ms,
        ),
        diagnostics=diagnostics,
    )


def _provider_diagnostics(
    response: StepFunTransportResponse,
    *,
    attempt: int,
    reason_code: str,
    finish_reason: str | None = None,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "attempt": attempt,
        "http_status": response.status_code,
        "reason_code": reason_code,
    }
    request_id = _safe_remote_request_id(response.remote_request_id)
    if request_id is not None:
        diagnostics["remote_request_id"] = request_id
    if finish_reason is not None:
        diagnostics["finish_reason"] = finish_reason
    return diagnostics


def _safe_remote_request_id(value: str | None) -> RemoteRequestId | None:
    if value is None:
        return None
    try:
        return RemoteRequestId.from_adapter(value)
    except (TypeError, ValueError):
        return None


def _readiness_issues(value: object) -> tuple[ReadinessIssue, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(issue, ReadinessIssue) for issue in value
    ):
        raise TypeError("StepFun 准备端口必须返回 ReadinessIssue 元组")
    return value


def _freeze_headers(
    headers: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if headers is None:
        return MappingProxyType({})
    if not isinstance(headers, Mapping):
        raise TypeError("StepFun 自定义请求头必须是映射")
    frozen: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("StepFun 自定义请求头必须是字符串映射")
        frozen[name] = value
    return MappingProxyType(frozen)


def _safe_header_value(value: str) -> bool:
    if not value or not value.isprintable() or "\r" in value or "\n" in value:
        return False
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _valid_custom_headers(headers: Mapping[str, str]) -> bool:
    seen: set[str] = set()
    for name, value in headers.items():
        normalized = name.casefold()
        if (
            _HEADER_NAME.fullmatch(name) is None
            or not _safe_header_value(value)
            or normalized in _PROTECTED_HEADERS
            or normalized in seen
        ):
            return False
        seen.add(normalized)
    return True


def _chat_completions_endpoint(base_endpoint: str) -> str:
    return f"{base_endpoint.rstrip('/')}/chat/completions"


def _valid_https_base_endpoint(value: str) -> bool:
    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
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


def _read_clock(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError("StepFun 单调时钟必须返回有限数值")
    return float(value)


def _elapsed_ms(clock: Callable[[], float], started: float) -> int:
    elapsed = max(0.0, _read_clock(clock) - started)
    return min(int(elapsed * 1000), 2**53 - 1)


def _non_negative_integer(value: object) -> TypeGuard[int]:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 2**53 - 1
    )


def _bounded_integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name}必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name}超出允许范围")
    return value


__all__ = [
    "StepFunSettings",
    "StepFunTextModel",
    "StepFunTransport",
    "StepFunTransportFailure",
    "StepFunTransportFailureKind",
    "StepFunTransportRequest",
    "StepFunTransportResponse",
]
