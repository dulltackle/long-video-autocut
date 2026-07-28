"""只消费预编排脚本的确定性语音识别 Adapter。"""

from dataclasses import dataclass

from .interface import (
    ReadinessReport,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
    validate_result_for_source,
)


@dataclass(frozen=True, slots=True)
class DeterministicTranscriptionScript:
    """确定性 Adapter 可以重放的不可变终态脚本。"""

    result: TranscriptionResult | None
    failure: TranscriptionFailure | None
    readiness: ReadinessReport

    def __post_init__(self) -> None:
        if (self.result is None) == (self.failure is None):
            raise ValueError("确定性脚本必须且只能包含成功或失败终态")
        if self.result is not None and not isinstance(
            self.result,
            TranscriptionResult,
        ):
            raise TypeError("确定性成功脚本必须包含 TranscriptionResult")
        if self.failure is not None and not isinstance(
            self.failure,
            TranscriptionFailure,
        ):
            raise TypeError("确定性失败脚本必须包含 TranscriptionFailure")
        if not isinstance(self.readiness, ReadinessReport):
            raise TypeError("确定性脚本准备结果必须使用 ReadinessReport")

    @classmethod
    def succeed(
        cls,
        result: TranscriptionResult,
        *,
        readiness: ReadinessReport | None = None,
    ) -> "DeterministicTranscriptionScript":
        if not isinstance(result, TranscriptionResult):
            raise TypeError("确定性成功脚本必须包含 TranscriptionResult")
        report = (
            ReadinessReport(ready=True)
            if readiness is None
            else readiness
        )
        if not isinstance(report, ReadinessReport):
            raise TypeError("确定性脚本准备结果必须使用 ReadinessReport")
        return cls(result=result, failure=None, readiness=report)

    @classmethod
    def fail(
        cls,
        failure: TranscriptionFailure,
        *,
        readiness: ReadinessReport | None = None,
    ) -> "DeterministicTranscriptionScript":
        if not isinstance(failure, TranscriptionFailure):
            raise TypeError("确定性失败脚本必须包含 TranscriptionFailure")
        report = (
            ReadinessReport(ready=True)
            if readiness is None
            else readiness
        )
        if not isinstance(report, ReadinessReport):
            raise TypeError("确定性脚本准备结果必须使用 ReadinessReport")
        return cls(result=None, failure=failure, readiness=report)


class DeterministicSpeechRecognition:
    """在正式接口上重放预编排结果且不访问外部能力。"""

    __slots__ = ("_script",)

    def __init__(self, script: DeterministicTranscriptionScript) -> None:
        if not isinstance(script, DeterministicTranscriptionScript):
            raise TypeError("确定性语音识别只接受预编排脚本")
        self._script = script

    def check_readiness(self) -> ReadinessReport:
        """直接返回脚本中的本地准备快照。"""
        return self._script.readiness

    def transcribe(
        self,
        request: TranscriptionRequest,
    ) -> TranscriptionResult:
        """在取消检查之间重放并验证完整成功结果。"""
        if not isinstance(request, TranscriptionRequest):
            raise TypeError("确定性语音识别只接受 TranscriptionRequest")
        request.cancellation.raise_if_cancelled()
        result = self._script.result
        failure = self._script.failure
        if failure is not None:
            raise failure._fresh()
        assert result is not None, "确定性脚本缺少终态"
        validate_result_for_source(result, request.source)
        request.cancellation.raise_if_cancelled()
        return result
