"""只装配认证生产实现的直播拆条组合根。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from types import MappingProxyType

from video_auto_editor.application import LiveApplication, LiveRunRequest
from video_auto_editor.application.live import (
    _DeliveryBuildWork,
    _RunAssembly,
    _StageWork,
)
from video_auto_editor.cache import CacheNamespace, CacheOutcome, CacheRepository
from video_auto_editor.clip_planning import CandidatePlan, ClipPlanning, DeliveryPlan
from video_auto_editor.configuration import Configuration, LoadedConfiguration
from video_auto_editor.configuration._model import TextGenerationSettings
from video_auto_editor.delivery import (
    DeliveryBuild,
    DeliveryBuildRequest,
    DeliveryVerification,
    Publication,
    PublicationFailure,
)
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    ArtifactRole,
    ExternalRequestOutcome,
    Facts,
    OperationKind,
    OperationOutcome,
    ProviderCapability,
    ResultKind,
    RetryKind,
    RunDiagnostics,
    StageOutcome,
    ZeroRequestReason,
)
from video_auto_editor.diagnostics._facts import DiagnosticFact
from video_auto_editor.diagnostics._session import (
    DiagnosticOperation,
    DiagnosticScope,
    StageDiagnostics,
)
from video_auto_editor.readiness import (
    ProviderBinding,
    ProviderDisclosure,
    Readiness,
    ReadinessIssue,
    ReadinessReport,
    ReadinessRequest,
)
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import (
    ERROR_REGISTRY,
    ErrorCode,
    ErrorModule,
)
from video_auto_editor.runtime.identity import OperationId
from video_auto_editor.source_analysis import SourceAnalysis, SourceDescription
from video_auto_editor.subtitle_optimization import (
    SubtitleOptimization,
    SubtitleOptimizationExecutionFacts,
    SubtitleOptimizationFailure,
    SubtitleOptimizationRequest,
    SubtitleOptimizationResult,
    SubtitleOptimizationSettings,
)
from video_auto_editor.text_model import (
    GenerationSettings,
    ReasoningEffort,
    StepFunSettings,
    StepFunTextModel,
    TextModelEvent,
    TextModelEventKind,
)
from video_auto_editor.topic_review import (
    TopicReview,
    TopicReviewExecutionFacts,
    TopicReviewFailure,
    TopicReviewRequest,
    TopicReviewResult,
    TopicReviewSettings,
)
from video_auto_editor.transcription import (
    CacheUse,
    CompleteTranscript,
    ExecutionFacts,
    SpeechPresence,
    StepAudioSettings,
    StepAudioSpeechRecognition,
    TranscriptionFailure,
    TranscriptionRemoteRequestEvent,
    TranscriptionRemoteRequestEventKind,
    TranscriptionRequest,
    TranscriptionResult,
)
from video_auto_editor.workspace import (
    DiagnosticRunWorkspace,
    ManagedTreeEntryKind,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
)

_APPLICATION_VERSION = "4.7.0"
_ERROR_ORDER = {error_code: index for index, error_code in enumerate(ERROR_REGISTRY)}
_PROVIDER_MODULE = {
    ProviderCapability.TRANSCRIPTION: ErrorModule.TRANSCRIPTION,
    ProviderCapability.TOPIC_REVIEW: ErrorModule.TOPIC_REVIEW,
    ProviderCapability.SUBTITLE_OPTIMIZATION: (ErrorModule.SUBTITLE_OPTIMIZATION),
}
_ARTIFACT_ROLES = {
    "manifest.json": ArtifactRole.MANIFEST,
    "transcript.json": ArtifactRole.TRANSCRIPT_JSON,
    "transcript.srt": ArtifactRole.TRANSCRIPT_SRT,
    "plan.json": ArtifactRole.PLAN,
    "metadata.json": ArtifactRole.METADATA,
    "report.md": ArtifactRole.REPORT,
}

DisclosureSink = Callable[[tuple[ProviderDisclosure, ...]], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _ProviderLedger:
    """串行阶段与并发 Adapter 事件之间共享的供应商终态账本。"""

    __slots__ = ("_contacted", "_declared", "_lock", "_resolved")

    def __init__(self) -> None:
        self._lock = RLock()
        self._declared: set[ProviderCapability] = set()
        self._contacted: set[ProviderCapability] = set()
        self._resolved: set[ProviderCapability] = set()

    def declare(
        self,
        stage: StageDiagnostics,
        disclosures: tuple[ProviderDisclosure, ...],
    ) -> None:
        for disclosure in disclosures:
            fact = disclosure.to_diagnostic_fact()
            if fact is None:
                continue
            if not isinstance(fact, DiagnosticFact):
                raise TypeError("供应商披露必须形成类型化诊断事实")
            stage.scope(_PROVIDER_MODULE[disclosure.capability]).record(fact)
            with self._lock:
                self._declared.add(disclosure.capability)

    def mark_contacted(self, capability: ProviderCapability) -> None:
        with self._lock:
            if capability not in self._declared:
                raise RuntimeError("远程请求发生前必须声明供应商能力")
            if capability in self._resolved:
                raise RuntimeError("已经声明零请求的供应商不能再发起请求")
            self._contacted.add(capability)

    def contacted(self, capability: ProviderCapability) -> bool:
        with self._lock:
            return capability in self._contacted

    def resolve_zero(
        self,
        stage: StageDiagnostics,
        capability: ProviderCapability,
        reason: ZeroRequestReason,
    ) -> None:
        with self._lock:
            if capability not in self._declared:
                return
            if capability in self._contacted or capability in self._resolved:
                return
            stage.scope(_PROVIDER_MODULE[capability]).record(
                Facts.external_service_zero_requests(capability, reason)
            )
            self._resolved.add(capability)

    def close_unresolved(self, stage: StageDiagnostics) -> None:
        for capability in ProviderCapability:
            self.resolve_zero(
                stage,
                capability,
                ZeroRequestReason.PRECONDITION_FAILED,
            )


@dataclass(slots=True)
class _StepAudioCallState:
    operation: DiagnosticOperation
    observed_attempt_count: int = 0
    scheduled_attempt: int = 1
    request_fact_recorded: bool = False
    ledger_contacted: bool = False


class _StepAudioDiagnosticBridge:
    """把可并发的 StepAudio 传输事件关联到严格诊断操作。"""

    __slots__ = ("_active_scope", "_ledger", "_lock", "_operations")

    def __init__(self, ledger: _ProviderLedger) -> None:
        self._ledger = ledger
        self._lock = RLock()
        self._active_scope: DiagnosticScope | None = None
        self._operations: dict[OperationId, _StepAudioCallState] = {}

    def activate(self, scope: DiagnosticScope) -> None:
        with self._lock:
            if self._active_scope is not None:
                raise RuntimeError("StepAudio 诊断作用域不能重入")
            self._active_scope = scope

    def deactivate(self) -> None:
        with self._lock:
            self._active_scope = None

    def record(self, event: TranscriptionRemoteRequestEvent) -> None:
        if not isinstance(event, TranscriptionRemoteRequestEvent):
            raise TypeError("StepAudio 诊断桥只接受结构化远程请求事件")
        with self._lock:
            scope = self._active_scope
            if scope is None:
                raise RuntimeError("StepAudio 远程请求缺少活动诊断作用域")
            if event.kind is TranscriptionRemoteRequestEventKind.STARTED:
                if event.correlation_id in self._operations:
                    raise RuntimeError("StepAudio 远程请求关联标识重复")
                self._operations[event.correlation_id] = _StepAudioCallState(
                    scope.start_operation(
                        OperationKind.EXTERNAL_REQUEST,
                        item_index=1,
                        item_count=1,
                    )
                )
                return
            state = self._operations.get(event.correlation_id)
            if state is None:
                raise RuntimeError("StepAudio 远程请求终态缺少开始事件")
            state.observed_attempt_count = max(
                state.observed_attempt_count,
                event.transport_attempt_count,
            )
            if event.kind is TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED:
                return
            if event.kind is TranscriptionRemoteRequestEventKind.RETRY_PLANNED:
                next_attempt = event.transport_attempt_count + 1
                state.operation.schedule_retry(
                    RetryKind.TRANSPORT_RETRY,
                    next_attempt=next_attempt,
                    reason_code=(event.reason_code or "transport.retry_planned"),
                    backoff_ms=event.retry_delay_ms or 0,
                )
                state.scheduled_attempt = next_attempt
                return
            operation_outcome = {
                TranscriptionRemoteRequestEventKind.SUCCEEDED: (
                    OperationOutcome.SUCCEEDED
                ),
                TranscriptionRemoteRequestEventKind.FAILED: (OperationOutcome.FAILED),
                TranscriptionRemoteRequestEventKind.INTERRUPTED: (
                    OperationOutcome.INTERRUPTED
                ),
                TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY: (
                    OperationOutcome.CANCELLED_DUE_TO_PRIMARY
                ),
            }.get(event.kind)
            if operation_outcome is None:
                raise RuntimeError("StepAudio 远程请求事件种类不受支持")
            operation = state.operation
            succeeded = event.kind is TranscriptionRemoteRequestEventKind.SUCCEEDED
            if not state.request_fact_recorded:
                operation.record(
                    Facts.external_request(
                        ProviderCapability.TRANSCRIPTION,
                        (
                            ExternalRequestOutcome.SUCCEEDED
                            if succeeded
                            else ExternalRequestOutcome.FAILED
                        ),
                        attempt_count=event.transport_attempt_count,
                        remote_request_id=event.remote_request_id,
                    )
                )
                state.request_fact_recorded = True
            if not state.ledger_contacted:
                self._ledger.mark_contacted(ProviderCapability.TRANSCRIPTION)
                state.ledger_contacted = True
            # Diagnostics 的操作尝试计数描述“已调度到第几次尝试”；若在退避中取消，
            # 它会大于上面的外部请求事实（后者只统计真实发生的网络尝试）。
            operation.complete(
                operation_outcome,
                attempt_count=state.scheduled_attempt,
            )
            self._operations.pop(event.correlation_id)

    def fail_unresolved(self, stage_outcome: StageOutcome) -> None:
        with self._lock:
            states = tuple(self._operations.items())
            operation_outcome = (
                OperationOutcome.INTERRUPTED
                if stage_outcome is StageOutcome.INTERRUPTED
                else OperationOutcome.CANCELLED_DUE_TO_PRIMARY
            )
            first_failure: BaseException | None = None
            for correlation_id, state in states:
                try:
                    if (
                        state.observed_attempt_count > 0
                        and not state.request_fact_recorded
                    ):
                        state.operation.record(
                            Facts.external_request(
                                ProviderCapability.TRANSCRIPTION,
                                ExternalRequestOutcome.FAILED,
                                attempt_count=state.observed_attempt_count,
                            )
                        )
                        state.request_fact_recorded = True
                    if (
                        state.observed_attempt_count > 0
                        and not state.ledger_contacted
                    ):
                        self._ledger.mark_contacted(
                            ProviderCapability.TRANSCRIPTION
                        )
                        state.ledger_contacted = True
                    state.operation.complete(
                        operation_outcome,
                        attempt_count=state.scheduled_attempt,
                    )
                except BaseException as failure:  # noqa: BLE001
                    if first_failure is None:
                        first_failure = failure
                    continue
                self._operations.pop(correlation_id, None)
            if first_failure is not None:
                raise first_failure


@dataclass(slots=True)
class _TextCallState:
    capability: ProviderCapability
    scope: DiagnosticScope
    operation: DiagnosticOperation | None = None
    observed_attempt_count: int = 0
    scheduled_attempt: int = 1
    request_fact_recorded: bool = False
    ledger_contacted: bool = False


class _TextModelDiagnosticBridge:
    """把共享 StepFun 的逻辑调用终态投影为真实外部请求事实。"""

    __slots__ = (
        "_active",
        "_calls",
        "_ledger",
        "_lock",
        "_terminal_calls",
    )

    def __init__(self, ledger: _ProviderLedger) -> None:
        self._ledger = ledger
        self._lock = RLock()
        self._active: tuple[ProviderCapability, DiagnosticScope] | None = None
        self._calls: dict[OperationId, _TextCallState] = {}
        self._terminal_calls: set[OperationId] = set()

    def activate(
        self,
        capability: ProviderCapability,
        scope: DiagnosticScope,
    ) -> None:
        if capability not in {
            ProviderCapability.TOPIC_REVIEW,
            ProviderCapability.SUBTITLE_OPTIMIZATION,
        }:
            raise ValueError("StepFun 诊断桥只服务文本模型能力")
        with self._lock:
            if self._active is not None:
                raise RuntimeError("StepFun 诊断作用域不能重入")
            self._active = (capability, scope)

    def deactivate(self) -> None:
        with self._lock:
            self._active = None

    def record(self, event: TextModelEvent) -> None:
        if not isinstance(event, TextModelEvent):
            raise TypeError("StepFun 诊断桥只接受结构化模型事件")
        with self._lock:
            active = self._active
            if active is None:
                raise RuntimeError("StepFun 远程请求缺少活动诊断作用域")
            correlation_id = event.observation.operation_id
            if event.kind is TextModelEventKind.CALL_STARTED:
                if correlation_id in self._calls or correlation_id in (
                    self._terminal_calls
                ):
                    raise RuntimeError("StepFun 逻辑调用关联标识重复")
                capability, scope = active
                self._calls[correlation_id] = _TextCallState(
                    capability,
                    scope,
                )
                return
            state = self._calls.get(correlation_id)
            if state is None:
                raise RuntimeError("StepFun 模型事件缺少调用开始事件")
            if active != (state.capability, state.scope):
                raise RuntimeError("StepFun 模型事件越过了活动业务能力")
            if event.transport_attempt_count > 0:
                state.observed_attempt_count = max(
                    state.observed_attempt_count,
                    event.transport_attempt_count,
                )
                self._ensure_text_operation(state)
            if event.kind is TextModelEventKind.RETRY_PLANNED:
                operation = state.operation
                if operation is None:
                    raise RuntimeError("StepFun 重试计划缺少真实传输尝试")
                next_attempt = event.transport_attempt_count + 1
                operation.schedule_retry(
                    RetryKind.TRANSPORT_RETRY,
                    next_attempt=next_attempt,
                    reason_code=(event.reason_code or "transport.retry_planned"),
                    backoff_ms=event.retry_delay_ms or 0,
                )
                state.scheduled_attempt = next_attempt
                return
            if event.kind is TextModelEventKind.TRANSPORT_FAILED:
                return
            if event.kind not in {
                TextModelEventKind.SUCCEEDED,
                TextModelEventKind.CANCELLED,
                TextModelEventKind.FAILED,
            }:
                return
            if correlation_id in self._terminal_calls:
                raise RuntimeError("StepFun 逻辑调用终态重复")
            if event.transport_attempt_count == 0:
                self._calls.pop(correlation_id)
                self._terminal_calls.add(correlation_id)
                return
            operation = state.operation
            if operation is None:
                raise RuntimeError("StepFun 终态缺少真实传输诊断操作")
            if event.transport_attempt_count > state.scheduled_attempt:
                raise RuntimeError("StepFun 传输尝试次数与重试计划不一致")
            if (
                event.transport_attempt_count < state.scheduled_attempt
                and event.kind is not TextModelEventKind.CANCELLED
            ):
                raise RuntimeError("StepFun 终态发生在已计划重试之前")
            succeeded = event.kind is TextModelEventKind.SUCCEEDED
            if not state.request_fact_recorded:
                operation.record(
                    Facts.external_request(
                        state.capability,
                        (
                            ExternalRequestOutcome.SUCCEEDED
                            if succeeded
                            else ExternalRequestOutcome.FAILED
                        ),
                        attempt_count=event.transport_attempt_count,
                    )
                )
                state.request_fact_recorded = True
            if not state.ledger_contacted:
                self._ledger.mark_contacted(state.capability)
                state.ledger_contacted = True
            operation_outcome = (
                OperationOutcome.SUCCEEDED
                if succeeded
                else (
                    OperationOutcome.INTERRUPTED
                    if event.kind is TextModelEventKind.CANCELLED
                    else OperationOutcome.FAILED
                )
            )
            # 已计划重试后可能在退避中取消；操作终态仍须闭合到已调度尝试，
            # 外部请求事实则继续使用真实观察到的传输尝试数。
            operation.complete(
                operation_outcome,
                attempt_count=state.scheduled_attempt,
            )
            self._calls.pop(correlation_id)
            self._terminal_calls.add(correlation_id)

    @staticmethod
    def _ensure_text_operation(state: _TextCallState) -> None:
        if state.operation is None:
            state.operation = state.scope.start_operation(
                OperationKind.EXTERNAL_REQUEST,
                item_index=1,
                item_count=1,
            )

    def fail_unresolved(self, stage_outcome: StageOutcome) -> None:
        with self._lock:
            states = tuple(self._calls.items())
            operation_outcome = (
                OperationOutcome.INTERRUPTED
                if stage_outcome is StageOutcome.INTERRUPTED
                else OperationOutcome.CANCELLED_DUE_TO_PRIMARY
            )
            first_failure: BaseException | None = None
            for correlation_id, state in states:
                operation = state.operation
                if operation is None:
                    self._calls.pop(correlation_id, None)
                    continue
                try:
                    if not state.request_fact_recorded:
                        operation.record(
                            Facts.external_request(
                                state.capability,
                                ExternalRequestOutcome.FAILED,
                                attempt_count=state.observed_attempt_count,
                            )
                        )
                        state.request_fact_recorded = True
                    if not state.ledger_contacted:
                        self._ledger.mark_contacted(state.capability)
                        state.ledger_contacted = True
                    operation.complete(
                        operation_outcome,
                        attempt_count=state.scheduled_attempt,
                    )
                except BaseException as failure:  # noqa: BLE001
                    if first_failure is None:
                        first_failure = failure
                    continue
                self._calls.pop(correlation_id, None)
            if first_failure is not None:
                raise first_failure


class _ReadinessFailure(RuntimeError):
    __slots__ = ("diagnostics", "error_code", "module")

    def __init__(self, issue: ReadinessIssue) -> None:
        self.error_code = issue.error_code
        self.diagnostics = issue.diagnostics
        self.module = ErrorModule.READINESS
        super().__init__(issue.safe_message)


class _AggregatedPreflightFailure(RuntimeError):
    __slots__ = (
        "associated_failures",
        "diagnostics",
        "error_code",
        "module",
    )

    def __init__(
        self,
        primary: Exception,
        associated: tuple[Exception, ...],
    ) -> None:
        error_code = getattr(primary, "error_code", None)
        diagnostics = getattr(primary, "diagnostics", None)
        if not isinstance(error_code, ErrorCode) or not isinstance(
            diagnostics,
            Mapping,
        ):
            raise TypeError("聚合预检主错误必须是稳定类型化失败")
        self.error_code = error_code
        self.diagnostics = MappingProxyType(dict(diagnostics))
        self.module = getattr(primary, "module", _module_for(error_code))
        self.associated_failures = associated
        super().__init__("聚合预检失败")


def _module_for(error_code: ErrorCode) -> ErrorModule:
    if error_code.value.startswith("environment."):
        return ErrorModule.READINESS
    if error_code.value.startswith("publication."):
        return ErrorModule.PUBLICATION
    if error_code.value.startswith("config."):
        return ErrorModule.CONFIGURATION
    return ErrorModule.APPLICATION


def _raise_preflight_failures(failures: list[Exception]) -> None:
    if not failures:
        return
    ordered = sorted(failures, key=_preflight_failure_order)
    if len(ordered) == 1:
        raise ordered[0]
    raise _AggregatedPreflightFailure(ordered[0], tuple(ordered[1:]))


def _preflight_failure_order(failure: Exception) -> int:
    error_code = getattr(failure, "error_code", None)
    if not isinstance(error_code, ErrorCode):
        return len(_ERROR_ORDER)
    return _ERROR_ORDER[error_code]


def _generation_settings(settings: TextGenerationSettings) -> GenerationSettings:
    return GenerationSettings(
        model=settings.model,
        temperature=settings.temperature,
        reasoning_effort=ReasoningEffort(settings.reasoning_effort),
        max_output_tokens=settings.max_output_tokens,
    )


def _record_cache_facts(
    scope: DiagnosticScope,
    namespace: CacheNamespace,
    *,
    hit_count: int,
    miss_count: int,
) -> None:
    outcomes = (CacheOutcome.HIT,) * hit_count + (CacheOutcome.MISS,) * miss_count
    for index, outcome in enumerate(outcomes, start=1):
        operation = scope.start_operation(
            OperationKind.CACHE_READ,
            item_index=index,
            item_count=len(outcomes),
        )
        operation.record(Facts.cache(namespace, outcome))
        operation.complete(OperationOutcome.SUCCEEDED, attempt_count=1)


def _record_transcription_execution(
    scope: DiagnosticScope,
    execution: ExecutionFacts,
) -> None:
    _record_cache_facts(
        scope,
        CacheNamespace.TRANSCRIPT,
        hit_count=int(execution.cache_use is CacheUse.HIT),
        miss_count=int(execution.cache_use is CacheUse.MISS),
    )
    scope.record(
        Facts.transcription_execution(
            retry_count=execution.retry_count,
            recovery_count=execution.recovery_count,
        )
    )


def _zero_reason(*, hit_count: int) -> ZeroRequestReason:
    return ZeroRequestReason.CACHE_HIT if hit_count > 0 else ZeroRequestReason.NO_WORK


def _record_artifacts(
    scope: DiagnosticScope,
    run_workspace: RunWorkspace,
    *,
    verified: bool,
) -> None:
    factory = Facts.artifact_verified if verified else Facts.artifact_created
    for entry in run_workspace.delivery_staging.inspect_tree():
        if entry.kind is not ManagedTreeEntryKind.REGULAR_FILE:
            continue
        role = _ARTIFACT_ROLES.get(entry.relative_path)
        if (
            role is None
            and entry.relative_path.startswith("clips/")
            and entry.relative_path.endswith(".mp4")
        ):
            role = ArtifactRole.SHORT_VIDEO
        if role is not None:
            scope.record(factory(role, relative_path=entry.relative_path))


class _ProductionRunAssembly:
    __slots__ = (
        "_configuration",
        "_disclosure_sink",
        "_ledger",
        "_overwrite",
        "_run_started_at",
        "_run_workspace",
        "_source",
        "_speech_recognition",
        "_stepaudio_bridge",
        "_subtitle_optimization",
        "_text_bridge",
        "_topic_review",
        "_transcript",
        "_wall_clock",
    )

    def __init__(
        self,
        *,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
        speech_recognition: StepAudioSpeechRecognition,
        topic_review: TopicReview,
        subtitle_optimization: SubtitleOptimization,
        stepaudio_bridge: _StepAudioDiagnosticBridge,
        text_bridge: _TextModelDiagnosticBridge,
        ledger: _ProviderLedger,
        disclosure_sink: DisclosureSink | None,
        overwrite: bool,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._configuration = configuration
        self._run_workspace = run_workspace
        self._speech_recognition = speech_recognition
        self._topic_review = topic_review
        self._subtitle_optimization = subtitle_optimization
        self._stepaudio_bridge = stepaudio_bridge
        self._text_bridge = text_bridge
        self._ledger = ledger
        self._disclosure_sink = disclosure_sink
        self._overwrite = overwrite
        self._wall_clock = wall_clock
        self._run_started_at = wall_clock()
        self._source: SourceDescription | None = None
        self._transcript: CompleteTranscript | None = None

    def finalize_terminal_diagnostics(
        self,
        stage: StageDiagnostics,
        outcome: StageOutcome,
    ) -> None:
        if outcome not in {StageOutcome.FAILED, StageOutcome.INTERRUPTED}:
            raise ValueError("终态诊断只接受失败或中断")
        self._stepaudio_bridge.fail_unresolved(outcome)
        self._text_bridge.fail_unresolved(outcome)
        self._ledger.close_unresolved(stage)

    def preflight(
        self,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        cancellation.raise_if_cancelled()
        report = Readiness.check(self._readiness_request())
        if not isinstance(report, ReadinessReport):
            raise TypeError("Readiness 必须返回 ReadinessReport")
        self._ledger.declare(stage, report.provider_disclosures)
        if report.environment_fact is not None:
            if not isinstance(report.environment_fact, DiagnosticFact):
                raise TypeError("Readiness 环境投影必须形成类型化诊断事实")
            stage.scope(ErrorModule.READINESS).record(report.environment_fact)
        failures: list[Exception] = [
            _ReadinessFailure(issue) for issue in report.issues
        ]
        try:
            Publication.check_destination(
                self._run_workspace.published_delivery,
                overwrite=self._overwrite,
                cancellation=cancellation,
            )
        except CancellationRequested:
            raise
        except PublicationFailure as failure:
            failures.append(failure)
        _raise_preflight_failures(failures)
        cancellation.raise_if_cancelled()
        if self._disclosure_sink is not None:
            self._disclosure_sink(report.provider_disclosures)
        cancellation.raise_if_cancelled()
        return _StageWork(report, 2)

    def _readiness_request(self) -> ReadinessRequest:
        effective = self._configuration.effective
        transcription = effective.transcription_provider_config
        text = effective.text_model_provider_config
        return ReadinessRequest(
            run_workspace=self._run_workspace,
            subtitle_font=effective.subtitle_style.font,
            transcription=ProviderBinding(
                capability=ProviderCapability.TRANSCRIPTION,
                adapter=self._speech_recognition,
                adapter_id="stepaudio",
                provider_id="stepaudio",
                model_id=transcription.model,
                endpoint=transcription.endpoint,
            ),
            topic_review=ProviderBinding(
                capability=ProviderCapability.TOPIC_REVIEW,
                adapter=self._topic_review,
                adapter_id="stepfun",
                provider_id="stepfun",
                model_id=effective.topic_review.model,
                endpoint=text.endpoint,
            ),
            subtitle_optimization=ProviderBinding(
                capability=ProviderCapability.SUBTITLE_OPTIMIZATION,
                adapter=self._subtitle_optimization,
                adapter_id="stepfun",
                provider_id="stepfun",
                model_id=effective.subtitle_optimization.model,
                endpoint=text.endpoint,
            ),
        )

    def analyze_source(
        self,
        source: SourceFileCapability,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        description = SourceAnalysis.analyze(source, cancellation)
        self._source = description
        context = self._configuration.course_context
        stage.scope(ErrorModule.SOURCE_ANALYSIS).record(
            Facts.source(
                sha256=description.sha256,
                byte_length=description.byte_length,
                duration_ms=description.duration_ms,
                course_context_provided=context is not None,
                course_context_sha256=(None if context is None else context.sha256),
            )
        )
        return _StageWork(description, 1)

    def transcribe(
        self,
        source: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> TranscriptionResult:
        if not isinstance(source, SourceDescription):
            raise TypeError("生产转写阶段需要 SourceDescription")
        scope = stage.scope(ErrorModule.TRANSCRIPTION)
        self._stepaudio_bridge.activate(scope)
        try:
            result = self._speech_recognition.transcribe(
                TranscriptionRequest(
                    source=source,
                    temporary_workspace=self._run_workspace.temporary,
                    cancellation=cancellation,
                )
            )
        except TranscriptionFailure as failure:
            _record_transcription_execution(scope, failure.execution_facts)
            raise
        finally:
            self._stepaudio_bridge.deactivate()
        if not isinstance(result, TranscriptionResult):
            raise TypeError("StepAudio 必须返回 TranscriptionResult")
        _record_transcription_execution(scope, result.execution_facts)
        if not self._ledger.contacted(ProviderCapability.TRANSCRIPTION):
            self._ledger.resolve_zero(
                stage,
                ProviderCapability.TRANSCRIPTION,
                (
                    ZeroRequestReason.CACHE_HIT
                    if (
                        result.execution_facts.cache_use is CacheUse.HIT
                        or result.speech_presence is SpeechPresence.PRESENT
                    )
                    else ZeroRequestReason.NO_WORK
                ),
            )
        return result

    def plan_candidates(
        self,
        transcript: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(transcript, CompleteTranscript):
            raise TypeError("生产候选规划需要 CompleteTranscript")
        source = self._source
        if source is None:
            raise RuntimeError("候选规划缺少素材事实")
        cancellation.raise_if_cancelled()
        plan = ClipPlanning.prepare(
            source,
            transcript,
            self._configuration.course_context,
            self._configuration.effective.clip_policy,
        )
        self._transcript = transcript
        stage.scope(ErrorModule.CLIP_PLANNING)
        return _StageWork(plan, len(plan.candidates))

    def review_topics(
        self,
        plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(plan, CandidatePlan):
            raise TypeError("生产主题评审需要 CandidatePlan")
        scope = stage.scope(ErrorModule.TOPIC_REVIEW)
        self._text_bridge.activate(ProviderCapability.TOPIC_REVIEW, scope)
        try:
            result = self._topic_review.review(
                TopicReviewRequest(
                    candidate_plan=plan,
                    run_id=self._run_workspace.run_id,
                    cancellation=cancellation,
                )
            )
        except TopicReviewFailure as failure:
            self._record_topic_execution(scope, failure.execution_facts)
            raise
        finally:
            self._text_bridge.deactivate()
        if not isinstance(result, TopicReviewResult):
            raise TypeError("TopicReview 必须返回 TopicReviewResult")
        self._record_topic_execution(scope, result.execution_facts)
        facts = result.execution_facts
        if not self._ledger.contacted(ProviderCapability.TOPIC_REVIEW):
            self._ledger.resolve_zero(
                stage,
                ProviderCapability.TOPIC_REVIEW,
                _zero_reason(hit_count=facts.cache_hit_count),
            )
        delivery_plan = ClipPlanning.finalize(plan, result)
        return _StageWork(delivery_plan, len(plan.candidates))

    @staticmethod
    def _record_topic_execution(
        scope: DiagnosticScope,
        facts: TopicReviewExecutionFacts,
    ) -> None:
        _record_cache_facts(
            scope,
            CacheNamespace.TOPIC_REVIEW,
            hit_count=facts.cache_hit_count,
            miss_count=facts.cache_miss_count,
        )

    def build_delivery(
        self,
        reviewed_plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _DeliveryBuildWork:
        if not isinstance(reviewed_plan, DeliveryPlan):
            raise TypeError("生产交付构建需要 DeliveryPlan")
        transcript = self._transcript
        source = self._source
        if transcript is None or source is None:
            raise RuntimeError("交付构建缺少素材或完整转写")
        subtitle_scope = stage.scope(ErrorModule.SUBTITLE_OPTIMIZATION)
        self._text_bridge.activate(
            ProviderCapability.SUBTITLE_OPTIMIZATION,
            subtitle_scope,
        )
        try:
            subtitles = self._subtitle_optimization.optimize(
                SubtitleOptimizationRequest(
                    delivery_plan=reviewed_plan,
                    transcript=transcript,
                    run_id=self._run_workspace.run_id,
                    cancellation=cancellation,
                )
            )
        except SubtitleOptimizationFailure as failure:
            self._record_subtitle_execution(
                subtitle_scope,
                failure.execution_facts,
            )
            raise
        finally:
            self._text_bridge.deactivate()
        if not isinstance(subtitles, SubtitleOptimizationResult):
            raise TypeError("SubtitleOptimization 必须返回 SubtitleOptimizationResult")
        self._record_subtitle_execution(
            subtitle_scope,
            subtitles.execution_facts,
        )
        subtitle_facts = subtitles.execution_facts
        if not self._ledger.contacted(ProviderCapability.SUBTITLE_OPTIMIZATION):
            self._ledger.resolve_zero(
                stage,
                ProviderCapability.SUBTITLE_OPTIMIZATION,
                _zero_reason(hit_count=subtitle_facts.cache_hit_count),
            )
        delivery = DeliveryBuild.build(
            DeliveryBuildRequest(
                run_id=self._run_workspace.run_id,
                source=source,
                transcript=transcript,
                plan=reviewed_plan,
                subtitles=subtitles,
                staging_directory=self._run_workspace.delivery_staging,
                subtitle_style=self._configuration.effective.subtitle_style,
                application_version=_APPLICATION_VERSION,
                started_at=self._run_started_at,
                published_at=self._wall_clock(),
                cancellation=cancellation,
            )
        )
        _record_artifacts(
            stage.scope(ErrorModule.DELIVERY_BUILD),
            self._run_workspace,
            verified=False,
        )
        return _DeliveryBuildWork(
            delivery,
            ResultKind(reviewed_plan.result_kind.value),
            len(reviewed_plan.short_videos),
        )

    @staticmethod
    def _record_subtitle_execution(
        scope: DiagnosticScope,
        facts: SubtitleOptimizationExecutionFacts,
    ) -> None:
        _record_cache_facts(
            scope,
            CacheNamespace.SUBTITLE_OPTIMIZATION,
            hit_count=facts.cache_hit_count,
            miss_count=facts.cache_miss_count,
        )

    def verify_delivery(
        self,
        delivery: UnverifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        verified = DeliveryVerification.verify(delivery, cancellation)
        _record_artifacts(
            stage.scope(ErrorModule.DELIVERY_VERIFICATION),
            self._run_workspace,
            verified=True,
        )
        return _StageWork(verified, 1)

    def publish(
        self,
        delivery: VerifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
        commit: Callable[[PublishedDelivery, Callable[[], None]], None],
    ) -> _StageWork:
        stage.scope(ErrorModule.PUBLICATION)
        published = Publication.publish(
            delivery,
            published_directory=self._run_workspace.published_delivery,
            previous_directory=self._run_workspace.previous_delivery,
            overwrite=self._overwrite,
            cancellation=cancellation,
            commit=commit,
        )
        return _StageWork(published, 1)


class _ProductionAssemblyFactory:
    __slots__ = ("_disclosure_sink", "_wall_clock")

    def __init__(
        self,
        disclosure_sink: DisclosureSink | None,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._disclosure_sink = disclosure_sink
        self._wall_clock = wall_clock

    def create(
        self,
        *,
        request: LiveRunRequest,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
    ) -> _RunAssembly:
        effective = configuration.effective
        if effective.transcription_provider != "stepaudio":
            raise _fixed_provider_failure("transcription_provider")
        if effective.text_model_provider != "stepfun":
            raise _fixed_provider_failure("text_model_provider")
        cache = CacheRepository.initialize(
            run_workspace.cache,
            application_version=_APPLICATION_VERSION,
        )
        ledger = _ProviderLedger()
        stepaudio_bridge = _StepAudioDiagnosticBridge(ledger)
        text_bridge = _TextModelDiagnosticBridge(ledger)
        transcription_configuration = effective.transcription_provider_config
        text_configuration = effective.text_model_provider_config
        speech_recognition = StepAudioSpeechRecognition(
            StepAudioSettings(
                endpoint=transcription_configuration.endpoint,
                model=transcription_configuration.model,
                timeout_seconds=transcription_configuration.timeout_seconds,
                max_concurrency=transcription_configuration.max_concurrency,
            ),
            credential=os.environ.get(
                transcription_configuration.key_environment_variable,
                "",
            ),
            cache_repository=cache,
            event_sink=stepaudio_bridge,
        )
        text_model = StepFunTextModel(
            StepFunSettings(
                endpoint=text_configuration.endpoint,
                timeout_seconds=text_configuration.timeout_seconds,
                max_concurrency=text_configuration.max_concurrency,
            ),
            credential=os.environ.get(
                text_configuration.key_environment_variable,
                "",
            ),
            event_sink=text_bridge,
        )
        topic_review = TopicReview(
            text_model,
            cache,
            TopicReviewSettings(
                adapter_id="stepfun",
                generation=_generation_settings(effective.topic_review),
            ),
        )
        subtitle_optimization = SubtitleOptimization(
            text_model,
            cache,
            SubtitleOptimizationSettings(
                adapter_id="stepfun",
                generation=_generation_settings(effective.subtitle_optimization),
                window_max_chars=100,
                max_chars_per_line=(effective.subtitle_style.max_chars_per_line),
                max_lines=effective.subtitle_style.max_lines,
            ),
        )
        return _ProductionRunAssembly(
            configuration=configuration,
            run_workspace=run_workspace,
            speech_recognition=speech_recognition,
            topic_review=topic_review,
            subtitle_optimization=subtitle_optimization,
            stepaudio_bridge=stepaudio_bridge,
            text_bridge=text_bridge,
            ledger=ledger,
            disclosure_sink=self._disclosure_sink,
            overwrite=request.overwrite,
            wall_clock=self._wall_clock,
        )


def _fixed_provider_failure(field: str) -> Exception:
    from video_auto_editor.configuration import ConfigurationFailure

    return ConfigurationFailure(
        ErrorCode.CONFIG_VALUE_INVALID,
        {"field": field, "reason_code": "value.invalid_enum"},
    )


def _initialize_diagnostics(
    run_workspace: RunWorkspace | DiagnosticRunWorkspace,
    application_version: str,
    wall_clock: Callable[[], datetime],
    monotonic_clock: Callable[[], float],
) -> RunDiagnostics:
    return RunDiagnostics.initialize(
        run_workspace.diagnostics,
        application_version=application_version,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )


def compose_live_application(
    *,
    disclosure_sink: DisclosureSink | None = None,
) -> LiveApplication:
    """形成只含 StepAudio、共享 StepFun 与文件系统缓存的生产应用。"""
    if disclosure_sink is not None and not callable(disclosure_sink):
        raise TypeError("供应商披露接收端必须可调用")
    return LiveApplication._compose(
        assembly_factory=_ProductionAssemblyFactory(
            disclosure_sink,
            _utc_now,
        ),
        open_workspace=Workspace.open,
        open_diagnostic_workspace=Workspace.open_diagnostics,
        load_configuration=Configuration.load,
        initialize_diagnostics=_initialize_diagnostics,
        application_version=_APPLICATION_VERSION,
        wall_clock=_utc_now,
    )


__all__ = ["compose_live_application"]
