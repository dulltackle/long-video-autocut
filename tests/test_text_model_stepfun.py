import json
import signal
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from video_auto_editor.runtime.cancellation import CancellationSource
from video_auto_editor.runtime.errors import RemoteRequestId, RunStage
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.text_model import (
    GenerationSettings,
    ObservationContext,
    PromptMessage,
    PromptRole,
    ReadinessIssue,
    ReasoningEffort,
    TextGenerationRequest,
    TextModelEventKind,
    TextModelFailure,
    TextModelFailureKind,
    TextModelReadinessCode,
)
from video_auto_editor.text_model.stepfun import (
    StepFunSettings,
    StepFunTextModel,
    StepFunTransportFailure,
    StepFunTransportFailureKind,
    StepFunTransportResponse,
)

_RUN_ID = RunId("run_00000000-0000-4000-8000-000000000001")
_OPERATION_ID = OperationId("00000000-0000-4000-8000-000000000002")
_ENDPOINT = "https://stepfun.example.test/v1/path-canary"
_CREDENTIAL = "credential-canary-must-not-leak"
_SYSTEM_CONTENT = "  system content must remain byte-for-byte  \n"
_USER_CONTENT = "user prompt canary must not leak into failures"


_TransportResponse = StepFunTransportResponse


class _ScriptedTransport:
    def __init__(
        self,
        outcomes,
        *,
        readiness: tuple[ReadinessIssue, ...] = (),
    ) -> None:
        self._outcomes = list(outcomes)
        self.readiness = readiness
        self.readiness_calls = 0
        self.send_calls: list[tuple[object, object]] = []

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        self.readiness_calls += 1
        return self.readiness

    def send(self, request, cancellation):
        self.send_calls.append((request, cancellation))
        if not self._outcomes:
            raise AssertionError("发生了未编排的额外 StepFun 请求")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _settings(*, max_concurrency: int = 1) -> StepFunSettings:
    return StepFunSettings(
        endpoint=_ENDPOINT,
        timeout_seconds=17,
        max_concurrency=max_concurrency,
    )


def _request(
    *,
    cancellation=None,
    operation_id: OperationId = _OPERATION_ID,
) -> TextGenerationRequest:
    source = cancellation or CancellationSource(clock=lambda: 0.0)
    return TextGenerationRequest(
        messages=(
            PromptMessage(PromptRole.SYSTEM, _SYSTEM_CONTENT),
            PromptMessage(PromptRole.USER, _USER_CONTENT),
        ),
        settings=GenerationSettings(
            model="step-2-mini",
            temperature=0.25,
            reasoning_effort=ReasoningEffort.HIGH,
            max_output_tokens=321,
        ),
        observation=ObservationContext(
            run_id=_RUN_ID,
            stage=RunStage.TOPIC_REVIEW,
            operation_id=operation_id,
        ),
        cancellation=source.token,
    )


def _chat_response(
    *,
    text: str = "  raw model content must remain unchanged  \n",
    finish_reason: str = "stop",
    usage: object = None,
    remote_request_id: str | None = "remote-request-canary",
) -> _TransportResponse:
    if usage is None:
        usage = {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
        }
    return _TransportResponse(
        status_code=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {
                            "role": "assistant",
                            "content": text,
                        },
                    }
                ],
                "usage": usage,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        remote_request_id=remote_request_id,
    )


def _model(transport, *, max_concurrency: int = 1) -> StepFunTextModel:
    return StepFunTextModel(
        _settings(max_concurrency=max_concurrency),
        credential=_CREDENTIAL,
        transport=transport,
    )


class _RecordingEventSink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class _FailOnEventSink(_RecordingEventSink):
    def __init__(self, target: TextModelEventKind) -> None:
        super().__init__()
        self._target = target

    def record(self, event) -> None:
        super().record(event)
        if event.kind is self._target:
            raise RuntimeError(
                f"event-sink-canary:{event.kind.value}:{_CREDENTIAL}:{_USER_CONTENT}"
            )


def test_stepfun_generate_sends_only_chat_protocol_fields_and_preserves_raw_text():
    transport = _ScriptedTransport([_chat_response()])
    model = _model(transport)
    request = _request()

    result = model.generate(request)

    assert result.text == "  raw model content must remain unchanged  \n"
    assert result.execution_facts.transport_attempt_count == 1
    assert result.execution_facts.transport_retry_count == 0
    assert result.execution_facts.finish_reason == "stop"
    assert result.execution_facts.input_tokens == 17
    assert result.execution_facts.output_tokens == 9
    assert len(transport.send_calls) == 1
    transport_request, cancellation = transport.send_calls[0]
    assert cancellation is request.cancellation
    assert transport_request.endpoint == f"{_ENDPOINT}/chat/completions"
    assert transport_request.credential == _CREDENTIAL
    assert transport_request.timeout_seconds == 17
    assert json.loads(transport_request.body.decode("utf-8")) == {
        "model": "step-2-mini",
        "messages": [
            {"role": "system", "content": _SYSTEM_CONTENT},
            {"role": "user", "content": _USER_CONTENT},
        ],
        "temperature": 0.25,
        "reasoning_effort": "high",
        "max_tokens": 321,
        "stream": False,
    }
    body_text = transport_request.body.decode("utf-8")
    for forbidden in (
        str(_RUN_ID),
        RunStage.TOPIC_REVIEW.value,
        str(_OPERATION_ID),
        "path-canary",
        _ENDPOINT,
        _CREDENTIAL,
    ):
        assert forbidden not in body_text
    assert _USER_CONTENT in body_text
    assert result.execution_facts.remote_request_id == RemoteRequestId.from_adapter(
        "remote-request-canary"
    )
    assert "remote-request-canary" not in repr(result.execution_facts)


@pytest.mark.parametrize(
    ("status_code", "expected_kind", "expected_attempts"),
    [
        (401, TextModelFailureKind.AUTHENTICATION_FAILED, 1),
        (403, TextModelFailureKind.AUTHENTICATION_FAILED, 1),
        (422, TextModelFailureKind.REQUEST_REJECTED, 1),
        (429, TextModelFailureKind.RATE_LIMITED, 3),
        (408, TextModelFailureKind.REQUEST_TIMEOUT, 3),
        (504, TextModelFailureKind.REQUEST_TIMEOUT, 3),
        (500, TextModelFailureKind.SERVICE_UNAVAILABLE, 3),
        (503, TextModelFailureKind.SERVICE_UNAVAILABLE, 3),
    ],
)
def test_stepfun_maps_http_statuses_with_bounded_retry_and_safe_attempt_facts(
    status_code,
    expected_kind,
    expected_attempts,
):
    error_body = (f"provider-error-body-canary:{_CREDENTIAL}:{_USER_CONTENT}").encode()
    response = _TransportResponse(
        status_code=status_code,
        content_type="application/json",
        body=error_body,
        remote_request_id="raw-remote-request-id-canary",
    )
    transport = _ScriptedTransport([response] * expected_attempts)
    model = _model(transport)

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert failure.kind is expected_kind
    assert failure.execution_facts.transport_attempt_count == expected_attempts
    assert failure.execution_facts.transport_retry_count == expected_attempts - 1
    assert failure.diagnostics["attempt"] == expected_attempts
    assert failure.diagnostics["http_status"] == status_code
    assert len(transport.send_calls) == expected_attempts
    rendered = repr(failure)
    for secret in (
        "provider-error-body-canary",
        _CREDENTIAL,
        _SYSTEM_CONTENT.strip(),
        _USER_CONTENT,
        "raw-remote-request-id-canary",
    ):
        assert secret not in rendered
    assert failure.__cause__ is None
    assert failure.__context__ is None


def test_stepfun_emits_correlated_safe_events_for_transport_retry_and_success():
    sink = _RecordingEventSink()
    transport = _ScriptedTransport(
        [
            StepFunTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"",
            ),
            _chat_response(),
        ]
    )
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=sink,
        clock=lambda: 0.0,
    )
    request = _request()

    result = model.generate(request)

    assert result.text == "  raw model content must remain unchanged  \n"
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.TRANSPORT_FAILED,
        TextModelEventKind.RETRY_PLANNED,
        TextModelEventKind.SUCCEEDED,
    ]
    assert all(event.observation is request.observation for event in sink.events)
    assert [event.transport_attempt_count for event in sink.events] == [
        0,
        1,
        1,
        2,
    ]
    assert sink.events[1].failure_kind is TextModelFailureKind.SERVICE_UNAVAILABLE
    assert sink.events[1].reason_code == "service.server_error"
    assert sink.events[2].retry_delay_ms == 50
    rendered = repr(sink.events)
    for secret in (
        _CREDENTIAL,
        _SYSTEM_CONTENT.strip(),
        _USER_CONTENT,
        _ENDPOINT,
        "raw model content",
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("transport_kind", "expected_kind", "expected_attempts"),
    [
        (
            StepFunTransportFailureKind.CONNECT_TIMEOUT,
            TextModelFailureKind.REQUEST_TIMEOUT,
            3,
        ),
        (
            StepFunTransportFailureKind.WRITE_TIMEOUT,
            TextModelFailureKind.REQUEST_TIMEOUT,
            3,
        ),
        (
            StepFunTransportFailureKind.READ_TIMEOUT,
            TextModelFailureKind.REQUEST_TIMEOUT,
            3,
        ),
        (
            StepFunTransportFailureKind.DNS_FAILED,
            TextModelFailureKind.SERVICE_UNAVAILABLE,
            3,
        ),
        (
            StepFunTransportFailureKind.CONNECTION_FAILED,
            TextModelFailureKind.SERVICE_UNAVAILABLE,
            3,
        ),
        (
            StepFunTransportFailureKind.TLS_FAILED,
            TextModelFailureKind.SERVICE_UNAVAILABLE,
            1,
        ),
        (
            StepFunTransportFailureKind.RESPONSE_PROTOCOL_INVALID,
            TextModelFailureKind.RESPONSE_PROTOCOL_INVALID,
            1,
        ),
        (
            StepFunTransportFailureKind.RESPONSE_TRUNCATED,
            TextModelFailureKind.OUTPUT_TRUNCATED,
            3,
        ),
        (
            StepFunTransportFailureKind.RESPONSE_TOO_LARGE,
            TextModelFailureKind.OUTPUT_TRUNCATED,
            1,
        ),
    ],
)
def test_stepfun_maps_transport_failures_with_bounded_retry(
    transport_kind,
    expected_kind,
    expected_attempts,
):
    transport = _ScriptedTransport(
        [StepFunTransportFailure(transport_kind) for _ in range(expected_attempts)]
    )

    with pytest.raises(TextModelFailure) as captured:
        _model(transport).generate(_request())

    failure = captured.value
    assert failure.kind is expected_kind
    assert failure.execution_facts.transport_attempt_count == expected_attempts
    assert failure.execution_facts.transport_retry_count == expected_attempts - 1
    assert len(transport.send_calls) == expected_attempts
    if transport_kind in {
        StepFunTransportFailureKind.RESPONSE_TRUNCATED,
        StepFunTransportFailureKind.RESPONSE_TOO_LARGE,
    }:
        assert failure.execution_facts.finish_reason is None
        assert "finish_reason" not in failure.diagnostics
    assert _CREDENTIAL not in repr(failure)
    assert _USER_CONTENT not in repr(failure)
    assert failure.__cause__ is None
    assert failure.__context__ is None


@pytest.mark.parametrize(
    ("endpoint", "credential", "expected_code"),
    [
        (
            "http://stepfun.example.test/v1",
            _CREDENTIAL,
            "configuration.https_required",
        ),
        (
            _ENDPOINT,
            "",
            "configuration.credential_missing",
        ),
        (
            "https://stepfun.example.test/v1\npath-canary",
            _CREDENTIAL,
            "configuration.https_required",
        ),
    ],
)
def test_stepfun_generate_maps_invalid_configuration_without_sending(
    endpoint,
    credential,
    expected_code,
):
    transport = _ScriptedTransport([])
    model = StepFunTextModel(
        StepFunSettings(
            endpoint=endpoint,
            timeout_seconds=17,
            max_concurrency=1,
        ),
        credential=credential,
        transport=transport,
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.INVALID_CONFIGURATION
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics["reason_code"] == expected_code
    assert transport.send_calls == []
    assert _CREDENTIAL not in repr(failure)
    assert endpoint not in repr(failure)


def test_stepfun_settings_reject_invalid_unicode_without_echoing_endpoint():
    endpoint = "https://endpoint-canary.example.test/\ud800secret-canary"

    with pytest.raises(ValueError) as captured:
        StepFunSettings(
            endpoint=endpoint,
            timeout_seconds=17,
            max_concurrency=1,
        )

    rendered = repr(captured.value) + str(captured.value)
    assert "endpoint-canary" not in rendered
    assert "secret-canary" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("credential", "headers", "expected_field"),
    [
        (
            "credential-canary\nheader-injection-canary",
            None,
            "text_model_provider_config.credential",
        ),
        (
            _CREDENTIAL,
            {"X-Route": "route-canary-💣"},
            "text_model_provider_config.headers",
        ),
    ],
)
def test_stepfun_rejects_unsafe_credential_and_headers_as_local_configuration(
    credential,
    headers,
    expected_field,
):
    transport = _ScriptedTransport([])
    model = StepFunTextModel(
        _settings(),
        credential=credential,
        headers=headers,
        transport=transport,
        clock=lambda: 0.0,
    )

    report = model.check_readiness()
    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    assert report.ready is False
    assert report.issues == (
        ReadinessIssue(
            TextModelReadinessCode.HEADERS_INVALID,
            {"field": expected_field},
        ),
    )
    failure = captured.value
    assert failure.kind is TextModelFailureKind.INVALID_CONFIGURATION
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics["field"] == expected_field
    assert transport.send_calls == []
    rendered = repr((report, failure))
    assert "header-injection-canary" not in rendered
    assert "route-canary" not in rendered


def test_stepfun_maps_unexpected_transport_exception_to_safe_internal_failure():
    exception_canary = f"unexpected-transport-canary:{_CREDENTIAL}:{_USER_CONTENT}"
    transport = _ScriptedTransport([RuntimeError(exception_canary)])
    sink = _RecordingEventSink()
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=sink,
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.execution_facts.transport_retry_count == 0
    assert failure.diagnostics["reason_code"] == "adapter.unexpected_failure"
    assert len(transport.send_calls) == 1
    assert exception_canary not in repr(failure)
    assert _CREDENTIAL not in repr(failure)
    assert _USER_CONTENT not in repr(failure)
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.FAILED,
    ]
    assert sink.events[-1].failure_kind is TextModelFailureKind.INTERNAL
    assert sink.events[-1].reason_code == "adapter.unexpected_failure"
    assert failure.__cause__ is None
    assert failure.__context__ is None


class _ExplodingResponse:
    @property
    def status_code(self):
        raise RuntimeError(f"response-property-canary:{_CREDENTIAL}:{_USER_CONTENT}")


def test_stepfun_rejects_non_dto_transport_response_as_safe_internal_failure():
    transport = _ScriptedTransport([_ExplodingResponse()])

    with pytest.raises(TextModelFailure) as captured:
        _model(transport).generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.diagnostics["reason_code"] == "adapter.transport_result_invalid"
    assert "response-property-canary" not in repr(failure)
    assert _CREDENTIAL not in repr(failure)
    assert _USER_CONTENT not in repr(failure)
    assert failure.__cause__ is None
    assert failure.__context__ is None


@pytest.mark.parametrize(
    ("finish_reason", "expected_kind"),
    [
        ("length", TextModelFailureKind.OUTPUT_TRUNCATED),
        ("refusal", TextModelFailureKind.GENERATION_REFUSED),
    ],
)
def test_stepfun_translates_non_success_finish_reason_without_returning_partial_text(
    finish_reason,
    expected_kind,
):
    partial_canary = "partial-model-output-canary-must-not-leak"
    transport = _ScriptedTransport(
        [
            _chat_response(
                text=partial_canary,
                finish_reason=finish_reason,
            )
        ]
    )

    with pytest.raises(TextModelFailure) as captured:
        _model(transport).generate(_request())

    failure = captured.value
    assert failure.kind is expected_kind
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.execution_facts.finish_reason == finish_reason
    assert failure.diagnostics["finish_reason"] == finish_reason
    assert partial_canary not in repr(failure)
    assert len(transport.send_calls) == 1


@pytest.mark.parametrize(
    ("case", "response"),
    [
        (
            "content_type",
            _TransportResponse(
                status_code=200,
                content_type="text/plain",
                body=b'{"choices":[]}',
            ),
        ),
        (
            "json",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=b"{malformed-json-canary",
            ),
        ),
        (
            "choices",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=b'{"choices":[],"usage":{}}',
            ),
        ),
        (
            "message",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=(
                    b'{"choices":[{"finish_reason":"stop",'
                    b'"message":{"role":"assistant"}}],'
                    b'"usage":{"prompt_tokens":1,"completion_tokens":1,'
                    b'"total_tokens":2}}'
                ),
            ),
        ),
        (
            "usage",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=(
                    b'{"choices":[{"finish_reason":"stop",'
                    b'"message":{"role":"assistant","content":"ok"}}],'
                    b'"usage":"malformed-usage-canary"}'
                ),
            ),
        ),
        (
            "content_unicode",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=(
                    b'{"choices":[{"finish_reason":"stop",'
                    b'"message":{"role":"assistant",'
                    b'"content":"provider-canary\\ud800secret-canary"}}]}'
                ),
            ),
        ),
        (
            "message_role",
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=(
                    b'{"choices":[{"finish_reason":"stop",'
                    b'"message":{"role":"user","content":"ok"}}]}'
                ),
            ),
        ),
    ],
)
def test_stepfun_rejects_malformed_chat_protocol_without_retry_or_raw_body(
    case,
    response,
):
    transport = _ScriptedTransport([response])

    with pytest.raises(TextModelFailure) as captured:
        _model(transport).generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.RESPONSE_PROTOCOL_INVALID
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.execution_facts.transport_retry_count == 0
    assert failure.diagnostics["attempt"] == 1
    assert failure.diagnostics["reason_code"].startswith("protocol.")
    assert len(transport.send_calls) == 1
    assert failure.__cause__ is None
    assert failure.__context__ is None
    rendered = repr(failure)
    for canary in (
        "malformed-json-canary",
        "malformed-usage-canary",
        _USER_CONTENT,
        _CREDENTIAL,
    ):
        assert canary not in rendered


def test_stepfun_maps_excessively_nested_json_to_protocol_failure():
    nested_body = b"[" * 10_000 + b"0" + b"]" * 10_000
    transport = _ScriptedTransport(
        [
            _TransportResponse(
                status_code=200,
                content_type="application/json",
                body=nested_body,
            )
        ]
    )

    with pytest.raises(TextModelFailure) as captured:
        _model(transport).generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.RESPONSE_PROTOCOL_INVALID
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.diagnostics["reason_code"] == "protocol.json_invalid"
    assert len(transport.send_calls) == 1
    assert failure.__cause__ is None
    assert failure.__context__ is None


class _ConcurrencyTransport:
    def __init__(self, *, releases: Event) -> None:
        self._releases = releases
        self._lock = Lock()
        self.two_entered = Event()
        self.active = 0
        self.max_active = 0
        self.send_count = 0

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(self, request, cancellation):
        del request, cancellation
        with self._lock:
            self.send_count += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.send_count == 2:
                self.two_entered.set()
        try:
            if not self._releases.wait(2):
                raise AssertionError("测试未释放 StepFun 伪传输")
            return _chat_response()
        finally:
            with self._lock:
                self.active -= 1


def test_stepfun_generate_honors_adapter_wide_concurrency_limit():
    releases = Event()
    transport = _ConcurrencyTransport(releases=releases)
    model = _model(transport, max_concurrency=2)
    operation_ids = (
        OperationId("00000000-0000-4000-8000-000000000011"),
        OperationId("00000000-0000-4000-8000-000000000012"),
        OperationId("00000000-0000-4000-8000-000000000013"),
    )

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(
                model.generate,
                _request(operation_id=operation_id),
            )
            for operation_id in operation_ids
        ]
        assert transport.two_entered.wait(1)
        assert transport.send_count == 2
        assert transport.max_active == 2
        releases.set()
        results = [future.result(timeout=2) for future in futures]

    assert transport.send_count == 3
    assert transport.max_active == 2
    assert all(
        result.text.strip() == "raw model content must remain unchanged"
        for result in results
    )


class _BlockingTransport:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.send_count = 0
        self._lock = Lock()

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(self, request, cancellation):
        del request, cancellation
        with self._lock:
            self.send_count += 1
        self.entered.set()
        if not self.release.wait(2):
            raise AssertionError("测试未释放占用并发许可的伪传输")
        return _chat_response()


def test_stepfun_cancels_while_queued_for_semaphore_without_sending():
    transport = _BlockingTransport()
    model = _model(transport, max_concurrency=1)
    queued_cancellation = CancellationSource(clock=lambda: 1.0)
    queued_started = Event()

    def generate_queued():
        queued_started.set()
        return model.generate(_request(cancellation=queued_cancellation))

    with ThreadPoolExecutor(max_workers=2) as executor:
        occupying = executor.submit(model.generate, _request())
        assert transport.entered.wait(1)
        queued = executor.submit(generate_queued)
        assert queued_started.wait(1)
        queued_cancellation.request(signal.SIGTERM)
        try:
            with pytest.raises(TextModelFailure) as captured:
                queued.result(timeout=1)
        finally:
            transport.release.set()
        occupying.result(timeout=2)

    failure = captured.value
    assert failure.kind is TextModelFailureKind.CANCELLED
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.execution_facts.transport_retry_count == 0
    assert transport.send_count == 1


class _CancellingTransport:
    def __init__(self, source: CancellationSource) -> None:
        self._source = source
        self.send_count = 0

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(self, request, cancellation):
        del request
        self.send_count += 1
        self._source.request(signal.SIGTERM)
        cancellation.raise_if_cancelled()
        raise AssertionError("取消检查后不得继续 StepFun 传输")


def test_stepfun_maps_inflight_transport_cancellation_with_attempt_facts():
    cancellation = CancellationSource(clock=lambda: 1.0)
    transport = _CancellingTransport(cancellation)
    sink = _RecordingEventSink()
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=sink,
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request(cancellation=cancellation))

    failure = captured.value
    assert failure.kind is TextModelFailureKind.CANCELLED
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.execution_facts.transport_retry_count == 0
    assert failure.diagnostics == {
        "attempt": 1,
        "signal_number": signal.SIGTERM,
    }
    assert transport.send_count == 1
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.CANCELLED,
    ]
    assert sink.events[-1].failure_kind is TextModelFailureKind.CANCELLED
    assert sink.events[-1].transport_attempt_count == 1
    assert failure.__cause__ is None
    assert failure.__context__ is None


class _CancelBeforeBackoffTransport:
    def __init__(self, source: CancellationSource) -> None:
        self._source = source
        self.send_count = 0

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        return ()

    def send(self, request, cancellation):
        del request, cancellation
        self.send_count += 1
        self._source.request(signal.SIGTERM)
        raise StepFunTransportFailure(StepFunTransportFailureKind.CONNECTION_FAILED)


def test_stepfun_cancellation_interrupts_retry_backoff_without_new_transport():
    cancellation = CancellationSource(clock=lambda: 1.0)
    transport = _CancelBeforeBackoffTransport(cancellation)
    sink = _RecordingEventSink()
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=sink,
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request(cancellation=cancellation))

    failure = captured.value
    assert failure.kind is TextModelFailureKind.CANCELLED
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.diagnostics == {
        "attempt": 1,
        "signal_number": signal.SIGTERM,
    }
    assert transport.send_count == 1
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.TRANSPORT_FAILED,
        TextModelEventKind.RETRY_PLANNED,
        TextModelEventKind.CANCELLED,
    ]
    assert failure.__cause__ is None
    assert failure.__context__ is None


def test_stepfun_readiness_aggregates_all_local_issues_repeatably_without_send():
    transport_issue = ReadinessIssue(
        TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE,
        {"reason_code": "tls.ca_store_unavailable"},
    )
    transport = _ScriptedTransport([], readiness=(transport_issue,))
    model = StepFunTextModel(
        StepFunSettings(
            endpoint="http://stepfun.example.test/v1/path-canary",
            timeout_seconds=17,
            max_concurrency=1,
        ),
        credential="",
        transport=transport,
    )

    first = model.check_readiness()
    second = model.check_readiness()

    assert first.ready is False
    assert first.issues == (
        ReadinessIssue(
            TextModelReadinessCode.CREDENTIAL_MISSING,
        ),
        ReadinessIssue(
            TextModelReadinessCode.HTTPS_REQUIRED,
            {"field": "text_model_provider_config.endpoint"},
        ),
        transport_issue,
    )
    assert second == first
    assert transport.readiness_calls == 2
    assert transport.send_calls == []


def test_stepfun_configuration_fingerprint_tracks_routes_but_excludes_credential():
    def fingerprint(*, route: str, credential: str) -> str:
        return (
            StepFunTextModel(
                _settings(),
                credential=credential,
                headers={"X-Deployment": route},
                transport=_ScriptedTransport([]),
            )
            .check_readiness()
            .configuration_fingerprint
        )

    first = fingerprint(
        route="deployment-a",
        credential="first-credential-canary",
    )
    different_route = fingerprint(
        route="deployment-b",
        credential="first-credential-canary",
    )
    different_credential = fingerprint(
        route="deployment-a",
        credential="second-credential-canary",
    )

    assert different_route != first
    assert different_credential == first
    assert "deployment-a" not in first
    assert "credential-canary" not in first


class _ExplodingEventSink:
    def record(self, event) -> None:
        raise RuntimeError(
            f"event-sink-canary:{event.kind.value}:{_CREDENTIAL}:{_USER_CONTENT}"
        )


def test_stepfun_maps_event_sink_failure_without_sending_or_leaking():
    transport = _ScriptedTransport([_chat_response()])
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=_ExplodingEventSink(),
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics["reason_code"] == "observation.sink_failed"
    assert transport.send_calls == []
    rendered = repr(failure)
    assert "event-sink-canary" not in rendered
    assert _CREDENTIAL not in rendered
    assert _USER_CONTENT not in rendered
    assert failure.__cause__ is None
    assert failure.__context__ is None


def test_stepfun_stops_retry_when_required_transport_event_cannot_be_recorded():
    sink = _FailOnEventSink(TextModelEventKind.TRANSPORT_FAILED)
    transport = _ScriptedTransport(
        [
            StepFunTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"",
            ),
            _chat_response(),
        ]
    )
    model = StepFunTextModel(
        _settings(),
        credential=_CREDENTIAL,
        transport=transport,
        event_sink=sink,
        clock=lambda: 0.0,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.TRANSPORT_FAILED,
    ]
    assert len(transport.send_calls) == 1
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.diagnostics["reason_code"] == "observation.sink_failed"
    rendered = repr(failure)
    assert "event-sink-canary" not in rendered
    assert _CREDENTIAL not in rendered
    assert _USER_CONTENT not in rendered
