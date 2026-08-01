"""只消费不可变脚本的确定性文本模型 Adapter。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from video_auto_editor.runtime.errors import PreservedApplicationFailure
from video_auto_editor.runtime.identity import OperationId

from .interface import (
    ReadinessReport,
    TextGenerationRequest,
    TextGenerationResponse,
    TextModelEvent,
    TextModelEventKind,
    TextModelEventSink,
    TextModelExecutionFacts,
    TextModelFailure,
    TextModelFailureKind,
)

_DETERMINISTIC_FINGERPRINT = hashlib.sha256(b"deterministic-text-model.v1").hexdigest()
_Outcome = TextGenerationResponse | TextModelFailure


@dataclass(frozen=True, slots=True)
class DeterministicTextModelScript:
    """按操作标识重放终态，不依赖调用顺序或外部状态。"""

    default_outcome: _Outcome
    readiness: ReadinessReport
    outcomes_by_operation: Mapping[OperationId, _Outcome] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        _validate_outcome(self.default_outcome)
        if not isinstance(self.readiness, ReadinessReport):
            raise TypeError("确定性脚本准备结果必须使用 ReadinessReport")
        if not isinstance(self.outcomes_by_operation, Mapping):
            raise TypeError("确定性操作脚本必须是映射")
        frozen: dict[OperationId, _Outcome] = {}
        for operation_id, outcome in self.outcomes_by_operation.items():
            if not isinstance(operation_id, OperationId):
                raise TypeError("确定性操作脚本键必须使用 OperationId")
            _validate_outcome(outcome)
            frozen[operation_id] = outcome
        object.__setattr__(
            self,
            "outcomes_by_operation",
            MappingProxyType(frozen),
        )

    @classmethod
    def succeed(
        cls,
        response: TextGenerationResponse,
        *,
        readiness: ReadinessReport | None = None,
        outcomes_by_operation: Mapping[OperationId, _Outcome] | None = None,
    ) -> DeterministicTextModelScript:
        if not isinstance(response, TextGenerationResponse):
            raise TypeError("确定性成功脚本必须包含 TextGenerationResponse")
        return cls(
            default_outcome=response,
            readiness=_ready_report() if readiness is None else readiness,
            outcomes_by_operation=(
                {} if outcomes_by_operation is None else outcomes_by_operation
            ),
        )

    @classmethod
    def fail(
        cls,
        failure: TextModelFailure,
        *,
        readiness: ReadinessReport | None = None,
        outcomes_by_operation: Mapping[OperationId, _Outcome] | None = None,
    ) -> DeterministicTextModelScript:
        if not isinstance(failure, TextModelFailure):
            raise TypeError("确定性失败脚本必须包含 TextModelFailure")
        return cls(
            default_outcome=failure,
            readiness=_ready_report() if readiness is None else readiness,
            outcomes_by_operation=(
                {} if outcomes_by_operation is None else outcomes_by_operation
            ),
        )


class DeterministicTextModel:
    """在共享接口上重放预编排结果且不访问任何外部能力。"""

    __slots__ = ("_event_sink", "_script")

    def __init__(
        self,
        script: DeterministicTextModelScript,
        *,
        event_sink: TextModelEventSink | None = None,
    ) -> None:
        if not isinstance(script, DeterministicTextModelScript):
            raise TypeError("确定性文本模型只接受预编排脚本")
        if event_sink is not None and not callable(getattr(event_sink, "record", None)):
            raise TypeError("确定性文本模型必须使用事件接收端")
        self._script = script
        self._event_sink = event_sink

    def check_readiness(self) -> ReadinessReport:
        """直接返回脚本中的本地准备快照。"""
        return self._script.readiness

    def generate(
        self,
        request: TextGenerationRequest,
    ) -> TextGenerationResponse:
        """按操作标识重放完整终态，取消永远优先。"""
        if not isinstance(request, TextGenerationRequest):
            raise TypeError("确定性文本模型只接受 TextGenerationRequest")
        self._record_event(
            TextModelEvent(
                kind=TextModelEventKind.CALL_STARTED,
                observation=request.observation,
                transport_attempt_count=0,
                elapsed_ms=0,
            )
        )
        if request.cancellation.cancelled:
            failure = _cancelled_failure(request.cancellation.signal_number)
            self._record_terminal(request, failure)
            raise failure from None
        outcome = self._script.outcomes_by_operation.get(
            request.observation.operation_id,
            self._script.default_outcome,
        )
        if isinstance(outcome, TextModelFailure):
            failure = outcome._fresh()
            self._record_terminal(request, failure)
            raise failure from None
        if request.cancellation.cancelled:
            failure = _cancelled_failure(request.cancellation.signal_number)
            self._record_terminal(request, failure)
            raise failure from None
        self._record_event(
            TextModelEvent(
                kind=TextModelEventKind.SUCCEEDED,
                observation=request.observation,
                transport_attempt_count=(
                    outcome.execution_facts.transport_attempt_count
                ),
                elapsed_ms=outcome.execution_facts.elapsed_ms,
            )
        )
        return outcome

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
            raise TextModelFailure(
                TextModelFailureKind.INTERNAL,
                execution_facts=TextModelExecutionFacts(
                    transport_attempt_count=event.transport_attempt_count,
                    elapsed_ms=event.elapsed_ms,
                ),
                diagnostics={"reason_code": "observation.sink_failed"},
            ) from None


def _ready_report() -> ReadinessReport:
    return ReadinessReport(
        ready=True,
        configuration_fingerprint=_DETERMINISTIC_FINGERPRINT,
    )


def _validate_outcome(value: object) -> None:
    if not isinstance(value, (TextGenerationResponse, TextModelFailure)):
        raise TypeError("确定性脚本终态必须是响应或类型化失败")


def _cancelled_failure(signal_number: int | None) -> TextModelFailure:
    diagnostics = {} if signal_number is None else {"signal_number": signal_number}
    return TextModelFailure(
        TextModelFailureKind.CANCELLED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
        diagnostics=diagnostics,
    )


__all__ = [
    "DeterministicTextModel",
    "DeterministicTextModelScript",
]
