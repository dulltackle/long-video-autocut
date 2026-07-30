import json
import os
import signal
import socket
import time
from dataclasses import FrozenInstanceError

import pytest

from video_auto_editor.diagnostics import DiagnosticsFailure
from video_auto_editor.runtime.cancellation import CancellationSource
from video_auto_editor.runtime.errors import ErrorCode, RunStage
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.text_model import (
    DeterministicTextModel,
    DeterministicTextModelScript,
    GenerationSettings,
    ObservationContext,
    PromptMessage,
    PromptRole,
    ReadinessReport,
    ReasoningEffort,
    StepFunSettings,
    StepFunTextModel,
    TextGenerationRequest,
    TextGenerationResponse,
    TextModelEventKind,
    TextModelExecutionFacts,
    TextModelFailure,
    TextModelFailureKind,
    TextModelPort,
)
from video_auto_editor.text_model.stepfun import StepFunTransportResponse


class _ContractTransport:
    def __init__(self) -> None:
        self.send_count = 0

    def check_readiness(self):
        return ()

    def send(self, request, cancellation):
        self.send_count += 1
        return StepFunTransportResponse(
            status_code=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "  原始文本\n",
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                },
                ensure_ascii=False,
            ).encode(),
        )


class _RecordingEventSink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


def _contract_adapter(
    adapter_name: str,
    *,
    event_sink=None,
) -> tuple[TextModelPort, _ContractTransport | None]:
    if adapter_name == "deterministic":
        return (
            DeterministicTextModel(
                DeterministicTextModelScript.succeed(_response()),
                event_sink=event_sink,
            ),
            None,
        )
    transport = _ContractTransport()
    return (
        StepFunTextModel(
            StepFunSettings(
                endpoint="https://stepfun.example.test/v1",
                timeout_seconds=17,
                max_concurrency=1,
            ),
            credential="contract-credential-canary",
            transport=transport,
            event_sink=event_sink,
            clock=lambda: 0.0,
        ),
        transport,
    )


def _request(
    *,
    operation_id: OperationId | None = None,
    cancellation=None,
) -> TextGenerationRequest:
    return TextGenerationRequest(
        messages=(
            PromptMessage(
                role=PromptRole.SYSTEM,
                content="system-prompt-canary",
            ),
            PromptMessage(
                role=PromptRole.USER,
                content="user-prompt-canary",
            ),
        ),
        settings=GenerationSettings(
            model="step-contract-model",
            temperature=0.2,
            reasoning_effort=ReasoningEffort.MEDIUM,
            max_output_tokens=4096,
        ),
        observation=ObservationContext(
            run_id=RunId.new(),
            stage=RunStage.TOPIC_REVIEW,
            operation_id=operation_id or OperationId.new(),
        ),
        cancellation=(
            CancellationSource(clock=lambda: 0.0).token
            if cancellation is None
            else cancellation
        ),
    )


def _response(text: str = "  原始文本\n") -> TextGenerationResponse:
    return TextGenerationResponse(
        text=text,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )


@pytest.mark.parametrize("adapter_name", ["deterministic", "stepfun"])
def test_adapters_pass_the_same_sync_text_generation_contract(adapter_name):
    model, _ = _contract_adapter(adapter_name)
    request = _request()

    first_readiness = model.check_readiness()
    second_readiness = model.check_readiness()
    result = model.generate(request)

    assert first_readiness.ready is True
    assert second_readiness == first_readiness
    assert result.text == "  原始文本\n"
    assert result.execution_facts.transport_retry_count == 0
    if adapter_name == "deterministic":
        assert result.execution_facts.transport_attempt_count == 0
        assert result.execution_facts.finish_reason is None
        assert result.execution_facts.input_tokens is None
        assert result.execution_facts.output_tokens is None
    else:
        assert result.execution_facts.transport_attempt_count == 1
        assert result.execution_facts.finish_reason == "stop"
        assert result.execution_facts.input_tokens == 11
        assert result.execution_facts.output_tokens == 7
    assert not hasattr(result, "provider")
    assert not hasattr(result, "raw_response")
    rendered = repr((request, result))
    assert "system-prompt-canary" not in rendered
    assert "user-prompt-canary" not in rendered
    assert "原始文本" not in rendered


@pytest.mark.parametrize("adapter_name", ["deterministic", "stepfun"])
def test_adapters_pass_the_same_pre_cancellation_contract(adapter_name):
    model, transport = _contract_adapter(adapter_name)
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request(cancellation=cancellation.token))

    failure = captured.value
    assert failure.kind is TextModelFailureKind.CANCELLED
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics == {"signal_number": signal.SIGTERM}
    if transport is not None:
        assert transport.send_count == 0


def test_deterministic_adapter_satisfies_shared_port_and_preserves_raw_text():
    expected = _response()
    model: TextModelPort = DeterministicTextModel(
        DeterministicTextModelScript.succeed(expected)
    )
    request = _request()

    result = model.generate(request)

    assert result is expected
    assert result.text == "  原始文本\n"
    assert result.execution_facts.transport_attempt_count == 0
    assert result.execution_facts.transport_retry_count == 0
    assert not hasattr(result, "provider")
    assert not hasattr(result, "raw_response")
    rendered = repr((request, result))
    assert "system-prompt-canary" not in rendered
    assert "user-prompt-canary" not in rendered
    assert "原始文本" not in rendered


def test_deterministic_readiness_is_local_repeatable_and_has_opaque_fingerprint():
    report = ReadinessReport(
        ready=True,
        configuration_fingerprint="a" * 64,
    )
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(
            _response(),
            readiness=report,
        )
    )

    first = model.check_readiness()
    second = model.check_readiness()

    assert first is report
    assert second is report
    assert first.configuration_fingerprint == "a" * 64


def test_deterministic_failure_is_fresh_stable_and_does_not_expose_content():
    failure = TextModelFailure(
        TextModelFailureKind.AUTHENTICATION_FAILED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=1,
            elapsed_ms=9,
        ),
        diagnostics={
            "attempt": 1,
            "http_status": 401,
            "reason_code": "authentication.credential_rejected",
        },
    )
    model = DeterministicTextModel(DeterministicTextModelScript.fail(failure))

    captured = []
    for _ in range(2):
        with pytest.raises(TextModelFailure) as raised:
            model.generate(_request())
        captured.append(raised.value)

    assert captured[0] is not captured[1]
    assert all(
        item.kind is TextModelFailureKind.AUTHENTICATION_FAILED for item in captured
    )
    assert all(item.safe_message == str(item) for item in captured)
    rendered = repr(captured)
    assert "system-prompt-canary" not in rendered
    assert "user-prompt-canary" not in rendered
    assert "credential" not in rendered.casefold()


def test_deterministic_adapter_turns_pre_cancellation_into_typed_failure():
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response("取消后不得返回"))
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request(cancellation=cancellation.token))

    failure = captured.value
    assert failure.kind is TextModelFailureKind.CANCELLED
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics == {"signal_number": signal.SIGTERM}
    assert "取消后不得返回" not in repr(failure)


def test_deterministic_script_routes_without_mutable_call_order():
    first_operation = OperationId.new()
    second_operation = OperationId.new()
    first = _response("第一项")
    second = _response("第二项")
    script = DeterministicTextModelScript.succeed(
        _response("默认项"),
        outcomes_by_operation={
            first_operation: first,
            second_operation: second,
        },
    )
    model = DeterministicTextModel(script)

    assert model.generate(_request(operation_id=second_operation)) is second
    assert model.generate(_request(operation_id=first_operation)) is first
    assert model.generate(_request(operation_id=first_operation)) is first


def test_deterministic_adapter_does_not_use_external_or_clock_boundaries(
    monkeypatch,
):
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response("本地结果"))
    )
    request = _request()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("确定性 Adapter 不得访问外部能力")

    monkeypatch.setattr(socket, "create_connection", unexpected)
    monkeypatch.setattr(os, "getenv", unexpected)
    monkeypatch.setattr(time, "monotonic", unexpected)

    assert model.generate(request).text == "本地结果"


def test_text_model_contract_values_are_immutable_and_validate_public_shape():
    message = PromptMessage(PromptRole.USER, "正文")
    with pytest.raises(FrozenInstanceError):
        message.content = "被修改"

    with pytest.raises(TypeError):
        TextGenerationRequest(
            messages=[message],
            settings=_request().settings,
            observation=_request().observation,
            cancellation=_request().cancellation,
        )
    with pytest.raises(ValueError):
        GenerationSettings(
            model="step-model",
            temperature=2.1,
            reasoning_effort=ReasoningEffort.NONE,
            max_output_tokens=None,
        )
    with pytest.raises(ValueError):
        ReadinessReport(
            ready=True,
            configuration_fingerprint="not-a-fingerprint",
        )


def test_prompt_message_rejects_invalid_unicode_without_echoing_content():
    invalid_content = "prompt-canary\ud800secret-canary"

    with pytest.raises(ValueError) as captured:
        PromptMessage(PromptRole.USER, invalid_content)

    rendered = repr(captured.value) + str(captured.value)
    assert "prompt-canary" not in rendered
    assert "secret-canary" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_text_response_rejects_invalid_unicode_without_exception_chain():
    invalid_content = "response-canary\ud800secret-canary"

    with pytest.raises(ValueError) as captured:
        TextGenerationResponse(
            text=invalid_content,
            execution_facts=TextModelExecutionFacts(
                transport_attempt_count=0,
                elapsed_ms=0,
            ),
        )

    rendered = repr(captured.value) + str(captured.value)
    assert "response-canary" not in rendered
    assert "secret-canary" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_deterministic_success_can_truthfully_report_zero_external_transports():
    response = TextGenerationResponse(
        text="本地确定性结果",
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )

    result = DeterministicTextModel(
        DeterministicTextModelScript.succeed(response)
    ).generate(_request())

    assert result is response
    assert result.execution_facts.transport_attempt_count == 0
    assert result.execution_facts.transport_retry_count == 0


def test_deterministic_adapter_emits_safe_correlated_call_events():
    sink = _RecordingEventSink()
    request = _request()
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response("event-output-canary")),
        event_sink=sink,
    )

    model.generate(request)

    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.SUCCEEDED,
    ]
    assert all(event.observation is request.observation for event in sink.events)
    assert [event.transport_attempt_count for event in sink.events] == [0, 0]
    rendered = repr(sink.events)
    assert "system-prompt-canary" not in rendered
    assert "user-prompt-canary" not in rendered
    assert "event-output-canary" not in rendered


class _ExplodingEventSink:
    def record(self, event) -> None:
        raise RuntimeError(
            f"event-sink-secret-canary:{event.kind.value}:"
            "system-prompt-canary:user-prompt-canary"
        )


class _FailOnEventSink:
    def __init__(self, target: TextModelEventKind) -> None:
        self.events = []
        self._target = target

    def record(self, event) -> None:
        self.events.append(event)
        if event.kind is self._target:
            raise RuntimeError("event-terminal-secret-canary")


class _DiagnosticsFailingEventSink:
    def __init__(self, failure: DiagnosticsFailure) -> None:
        self._failure = failure

    def record(self, event) -> None:
        del event
        raise self._failure


def test_deterministic_adapter_maps_event_sink_failure_without_exception_chain():
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response("event-output-canary")),
        event_sink=_ExplodingEventSink(),
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics["reason_code"] == "observation.sink_failed"
    rendered = repr(failure)
    assert "event-sink-secret-canary" not in rendered
    assert "system-prompt-canary" not in rendered
    assert "user-prompt-canary" not in rendered
    assert "event-output-canary" not in rendered
    assert failure.__cause__ is None
    assert failure.__context__ is None


def test_deterministic_adapter_fails_closed_when_success_event_cannot_be_recorded():
    sink = _FailOnEventSink(TextModelEventKind.SUCCEEDED)
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response("event-output-canary")),
        event_sink=sink,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.SUCCEEDED,
    ]
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.diagnostics["reason_code"] == "observation.sink_failed"
    assert "event-terminal-secret-canary" not in repr(failure)
    assert "event-output-canary" not in repr(failure)


def test_deterministic_adapter_fails_closed_when_cancel_event_cannot_be_recorded():
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    sink = _FailOnEventSink(TextModelEventKind.CANCELLED)
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(_response()),
        event_sink=sink,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request(cancellation=cancellation.token))

    failure = captured.value
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.CANCELLED,
    ]
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 0
    assert failure.diagnostics == {"reason_code": "observation.sink_failed"}


def test_deterministic_adapter_fails_closed_when_failure_event_cannot_be_recorded():
    scripted_failure = TextModelFailure(
        TextModelFailureKind.AUTHENTICATION_FAILED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=1,
            elapsed_ms=9,
        ),
        diagnostics={
            "attempt": 1,
            "http_status": 401,
            "reason_code": "authentication.credential_rejected",
        },
    )
    sink = _FailOnEventSink(TextModelEventKind.FAILED)
    model = DeterministicTextModel(
        DeterministicTextModelScript.fail(scripted_failure),
        event_sink=sink,
    )

    with pytest.raises(TextModelFailure) as captured:
        model.generate(_request())

    failure = captured.value
    assert [event.kind for event in sink.events] == [
        TextModelEventKind.CALL_STARTED,
        TextModelEventKind.FAILED,
    ]
    assert failure.kind is TextModelFailureKind.INTERNAL
    assert failure.execution_facts.transport_attempt_count == 1
    assert failure.diagnostics == {"reason_code": "observation.sink_failed"}


@pytest.mark.parametrize("adapter_name", ["deterministic", "stepfun"])
def test_adapters_preserve_typed_run_diagnostics_failure(adapter_name):
    diagnostics_failure = DiagnosticsFailure(
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        {
            "operation": "diagnostics.append",
            "reason_code": "diagnostics.append_failed",
        },
    )
    model, transport = _contract_adapter(
        adapter_name,
        event_sink=_DiagnosticsFailingEventSink(diagnostics_failure),
    )

    with pytest.raises(DiagnosticsFailure) as captured:
        model.generate(_request())

    assert captured.value is diagnostics_failure
    assert captured.value.error_code is ErrorCode.DIAGNOSTICS_WRITE_FAILED
    if transport is not None:
        assert transport.send_count == 0
