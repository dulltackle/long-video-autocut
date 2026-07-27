"""固定推进直播拆条运行生命周期的顶层应用。"""

import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from os import PathLike
from pathlib import Path
from time import monotonic
from types import MappingProxyType
from typing import NoReturn, Protocol, runtime_checkable

from video_auto_editor.configuration import LoadedConfiguration
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    DeliveryBuildState,
    DeliveryVerificationState,
    DiagnosticsFailure,
    Facts,
    InterruptionSignal,
    PublicationState,
    ResultKind,
    RunDiagnostics,
    RunOutcome,
    StageOutcome,
)
from video_auto_editor.diagnostics._session import StageDiagnostics
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
    SignalCoordinator,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    ExitCode,
    InternalLocation,
    RunError,
    RunStage,
    get_error_definition,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.workspace import (
    DiagnosticRunWorkspace,
    ManagedDirectoryCapability,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
    WorkspaceFailure,
)


@dataclass(frozen=True, slots=True, init=False)
class LiveRunRequest:
    """一次直播拆条运行的不可变公共请求。"""

    source: Path = field(repr=False)
    workspace_dir: Path | None = field(default=None, repr=False)
    overwrite: bool = False

    def __init__(
        self,
        source: PathLike[str] | str,
        *,
        workspace_dir: PathLike[str] | str | None = None,
        overwrite: bool = False,
    ) -> None:
        if not isinstance(overwrite, bool):
            raise TypeError("覆盖发布选项必须是布尔值")
        object.__setattr__(self, "source", Path(source))
        object.__setattr__(
            self,
            "workspace_dir",
            None if workspace_dir is None else Path(workspace_dir),
        )
        object.__setattr__(self, "overwrite", overwrite)


class LiveRunState(str, Enum):
    """直播拆条入口向调用方公开的封闭终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True, init=False)
class LiveRunOutcome:
    """只向 CLI 与调度器公开稳定运行事实的类型化终态。"""

    run_id: RunId
    state: LiveRunState
    exit_code: ExitCode
    result_kind: ResultKind | None
    primary_error_code: ErrorCode | None
    primary_error: RunError | None
    associated_errors: tuple[RunError, ...]
    recovery_incomplete: bool
    interruption_signal: InterruptionSignal | None
    diagnostics_incomplete: bool

    def __new__(cls) -> "LiveRunOutcome":
        raise TypeError("LiveRunOutcome 只能由 LiveApplication 创建")

    @classmethod
    def _from_run_outcome(
        cls,
        run_id: RunId,
        outcome: RunOutcome,
        *,
        diagnostics_incomplete: bool,
        recovery_incomplete: bool = False,
    ) -> "LiveRunOutcome":
        if outcome.state.value == LiveRunState.SUCCEEDED.value:
            exit_code = ExitCode.SUCCESS
        elif outcome.state.value == LiveRunState.INTERRUPTED.value:
            signal = outcome.interruption_signal
            if signal is None:
                raise TypeError("中断终态缺少信号")
            exit_code = ExitCode(signal.exit_code)
        else:
            error = outcome.primary_error
            if error is None:
                raise TypeError("失败终态缺少主错误")
            exit_code = error.exit_code

        instance = object.__new__(cls)
        object.__setattr__(instance, "run_id", run_id)
        object.__setattr__(
            instance,
            "state",
            LiveRunState(outcome.state.value),
        )
        object.__setattr__(instance, "exit_code", exit_code)
        object.__setattr__(instance, "result_kind", outcome.result_kind)
        object.__setattr__(
            instance,
            "primary_error_code",
            (
                None
                if outcome.primary_error is None
                else outcome.primary_error.error_code
            ),
        )
        object.__setattr__(
            instance,
            "primary_error",
            outcome.primary_error,
        )
        object.__setattr__(
            instance,
            "associated_errors",
            outcome.associated_errors,
        )
        object.__setattr__(
            instance,
            "recovery_incomplete",
            outcome.recovery_incomplete or recovery_incomplete,
        )
        object.__setattr__(
            instance,
            "interruption_signal",
            outcome.interruption_signal,
        )
        object.__setattr__(
            instance,
            "diagnostics_incomplete",
            diagnostics_incomplete,
        )
        return instance

    @classmethod
    def _failed_without_diagnostics(
        cls,
        run_id: RunId,
        error_code: ErrorCode,
        *,
        recovery_incomplete: bool,
    ) -> "LiveRunOutcome":
        instance = object.__new__(cls)
        object.__setattr__(instance, "run_id", run_id)
        object.__setattr__(
            instance,
            "state",
            LiveRunState.FAILED,
        )
        object.__setattr__(
            instance,
            "exit_code",
            get_error_definition(error_code).exit_code,
        )
        object.__setattr__(instance, "result_kind", None)
        object.__setattr__(
            instance,
            "primary_error_code",
            error_code,
        )
        object.__setattr__(instance, "primary_error", None)
        object.__setattr__(instance, "associated_errors", ())
        object.__setattr__(
            instance,
            "recovery_incomplete",
            recovery_incomplete,
        )
        object.__setattr__(instance, "interruption_signal", None)
        object.__setattr__(
            instance,
            "diagnostics_incomplete",
            True,
        )
        return instance

    def _with_incomplete_recovery(self) -> "LiveRunOutcome":
        if self.recovery_incomplete and self.diagnostics_incomplete:
            return self
        instance = object.__new__(type(self))
        object.__setattr__(instance, "run_id", self.run_id)
        object.__setattr__(instance, "state", self.state)
        object.__setattr__(instance, "exit_code", self.exit_code)
        object.__setattr__(instance, "result_kind", self.result_kind)
        object.__setattr__(
            instance,
            "primary_error_code",
            self.primary_error_code,
        )
        object.__setattr__(
            instance,
            "primary_error",
            self.primary_error,
        )
        object.__setattr__(
            instance,
            "associated_errors",
            self.associated_errors,
        )
        object.__setattr__(instance, "recovery_incomplete", True)
        object.__setattr__(
            instance,
            "interruption_signal",
            self.interruption_signal,
        )
        object.__setattr__(instance, "diagnostics_incomplete", True)
        return instance

    def _with_incomplete_diagnostics(self) -> "LiveRunOutcome":
        if self.diagnostics_incomplete:
            return self
        instance = object.__new__(type(self))
        object.__setattr__(instance, "run_id", self.run_id)
        object.__setattr__(instance, "state", self.state)
        object.__setattr__(instance, "exit_code", self.exit_code)
        object.__setattr__(instance, "result_kind", self.result_kind)
        object.__setattr__(
            instance,
            "primary_error_code",
            self.primary_error_code,
        )
        object.__setattr__(
            instance,
            "primary_error",
            self.primary_error,
        )
        object.__setattr__(
            instance,
            "associated_errors",
            self.associated_errors,
        )
        object.__setattr__(
            instance,
            "recovery_incomplete",
            self.recovery_incomplete,
        )
        object.__setattr__(
            instance,
            "interruption_signal",
            self.interruption_signal,
        )
        object.__setattr__(instance, "diagnostics_incomplete", True)
        return instance


@dataclass(frozen=True, slots=True)
class _StageWork:
    value: object
    work_item_count: int

    def __post_init__(self) -> None:
        _validate_work_item_count(self.work_item_count)


@dataclass(frozen=True, slots=True)
class _DeliveryBuildWork:
    delivery: UnverifiedDelivery
    result_kind: ResultKind
    work_item_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, UnverifiedDelivery):
            raise TypeError("交付构建结果必须包含 UnverifiedDelivery")
        if not isinstance(self.result_kind, ResultKind):
            raise TypeError("交付构建结果必须包含 ResultKind")
        _validate_work_item_count(self.work_item_count)


@dataclass(slots=True)
class _CommitState:
    run_id: RunId
    published_directory: ManagedDirectoryCapability
    result_kind: ResultKind | None = None
    published_delivery: PublishedDelivery | None = None
    interruption_recorded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("发布提交状态必须绑定 RunId")
        if not isinstance(
            self.published_directory,
            ManagedDirectoryCapability,
        ):
            raise TypeError("发布提交状态必须绑定最终交付目录 capability")
        self.published_directory._assert_bound_to_run(self.run_id)

    def prepare(self, result_kind: ResultKind) -> None:
        if not isinstance(result_kind, ResultKind):
            raise TypeError("发布提交必须绑定 ResultKind")
        if self.result_kind is not None:
            raise RuntimeError("发布提交结果种类已经准备")
        self.result_kind = result_kind

    def capture(self, delivery: PublishedDelivery) -> None:
        self.validate(delivery)
        self.published_delivery = delivery

    def discard_capture(self) -> None:
        self.published_delivery = None

    def validate(self, delivery: PublishedDelivery) -> None:
        if not isinstance(delivery, PublishedDelivery):
            raise TypeError("发布提交必须包含 PublishedDelivery")
        if delivery.run_id != self.run_id:
            raise ValueError("发布提交证明必须属于当前运行")
        if delivery.managed_directory is not self.published_directory:
            raise ValueError("发布提交证明必须绑定当前最终交付目录")
        if self.result_kind is None:
            raise RuntimeError("发布提交前必须准备结果种类")
        if self.published_delivery is not None:
            raise RuntimeError("发布提交证明只能形成一次")

    @property
    def committed(self) -> bool:
        return self.published_delivery is not None

    def outcome(self) -> RunOutcome:
        delivery = self.published_delivery
        result_kind = self.result_kind
        if delivery is None or result_kind is None:
            raise RuntimeError("直播拆条运行尚未越过发布提交点")
        return RunOutcome.succeeded(
            delivery,
            result_kind=result_kind,
        )

    def mark_interruption_recorded(self) -> None:
        if not self.committed:
            raise RuntimeError("只能在发布提交后记录延迟中断")
        self.interruption_recorded = True


@runtime_checkable
class _RunAssembly(Protocol):
    def preflight(
        self,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def analyze_source(
        self,
        source: SourceFileCapability,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def transcribe(
        self,
        source: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def plan_candidates(
        self,
        transcript: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def review_topics(
        self,
        plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def build_delivery(
        self,
        reviewed_plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _DeliveryBuildWork: ...

    def verify_delivery(
        self,
        delivery: UnverifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork: ...

    def publish(
        self,
        delivery: VerifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
        commit: Callable[
            [_StageWork, Callable[[], None]],
            _StageWork,
        ],
    ) -> _StageWork: ...


class _RunAssemblyFactory(Protocol):
    def create(
        self,
        *,
        request: LiveRunRequest,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
    ) -> _RunAssembly: ...


class _ExecutionControl(Protocol):
    def stage_started(
        self,
        stage: RunStage,
        cancellation: CancellationSource,
    ) -> None: ...

    def publication_committed(
        self,
        cancellation: CancellationSource,
    ) -> None: ...


class _NoExecutionControl:
    def stage_started(
        self,
        stage: RunStage,
        cancellation: CancellationSource,
    ) -> None:
        del stage, cancellation

    def publication_committed(
        self,
        cancellation: CancellationSource,
    ) -> None:
        del cancellation


_STAGE_ORDER = (
    RunStage.PREFLIGHT,
    RunStage.SOURCE_ANALYSIS,
    RunStage.TRANSCRIPTION,
    RunStage.CANDIDATE_PLANNING,
    RunStage.TOPIC_REVIEW,
    RunStage.DELIVERY_BUILD,
    RunStage.DELIVERY_VERIFICATION,
    RunStage.PUBLISHING,
)


class _LifecycleCursor:
    __slots__ = ("_index", "_terminated")

    def __init__(self) -> None:
        self._index = 0
        self._terminated = False

    def enter(self, stage: RunStage) -> None:
        if self._terminated:
            raise RuntimeError("直播拆条运行已经终止")
        if (
            self._index >= len(_STAGE_ORDER)
            or _STAGE_ORDER[self._index] is not stage
        ):
            raise RuntimeError("直播拆条运行阶段必须严格单向推进")
        self._index += 1

    def complete(self) -> None:
        if self._index != len(_STAGE_ORDER):
            raise RuntimeError("直播拆条运行未完成全部非终止阶段")
        self._terminated = True

    def terminate(self) -> None:
        if self._terminated:
            raise RuntimeError("直播拆条运行已经终止")
        self._terminated = True


class _StageTerminated(Exception):
    __slots__ = ("outcome",)

    def __init__(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        super().__init__("直播拆条运行阶段已经形成终态")


class _ApplicationFailure(RuntimeError):
    __slots__ = ("diagnostics", "error_code")

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, object],
    ) -> None:
        self.error_code = error_code
        self.diagnostics = MappingProxyType(dict(diagnostics))
        super().__init__("直播拆条运行发生稳定公共失败")


_OpenWorkspace = Callable[[Path, Path | None], Workspace]
_LoadConfiguration = Callable[[Path], LoadedConfiguration]
_AuditableRunWorkspace = RunWorkspace | DiagnosticRunWorkspace
_InitializeDiagnostics = Callable[
    [
        _AuditableRunWorkspace,
        str,
        Callable[[], datetime],
        Callable[[], float],
    ],
    RunDiagnostics,
]
_CleanupRun = Callable[[_AuditableRunWorkspace], None]


def _validate_work_item_count(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("阶段工作项数量必须是整数")
    if value < 0:
        raise ValueError("阶段工作项数量不能为负数")


class LiveApplication:
    """直播拆条运行的唯一顶层业务入口。"""

    __slots__ = (
        "_application_version",
        "_assembly_factory",
        "_cleanup_run",
        "_control",
        "_initialize_diagnostics",
        "_load_configuration",
        "_monotonic_clock",
        "_open_diagnostic_workspace",
        "_open_workspace",
        "_run_id_factory",
        "_wall_clock",
    )

    def __init__(self) -> None:
        raise TypeError("LiveApplication 只能由组合根装配")

    @classmethod
    def _compose(
        cls,
        *,
        assembly_factory: _RunAssemblyFactory,
        open_workspace: _OpenWorkspace,
        open_diagnostic_workspace: _OpenWorkspace,
        load_configuration: _LoadConfiguration,
        initialize_diagnostics: _InitializeDiagnostics,
        application_version: str,
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float] = monotonic,
        run_id_factory: Callable[[], RunId] = RunId.new,
        control: _ExecutionControl | None = None,
        cleanup_run: _CleanupRun | None = None,
    ) -> "LiveApplication":
        instance = object.__new__(cls)
        instance._assembly_factory = assembly_factory
        instance._open_workspace = open_workspace
        instance._open_diagnostic_workspace = open_diagnostic_workspace
        instance._load_configuration = load_configuration
        instance._initialize_diagnostics = initialize_diagnostics
        instance._application_version = application_version
        instance._wall_clock = wall_clock
        instance._monotonic_clock = monotonic_clock
        instance._run_id_factory = run_id_factory
        instance._control = (
            _NoExecutionControl() if control is None else control
        )
        instance._cleanup_run = (
            (lambda run_workspace: run_workspace.cleanup())
            if cleanup_run is None
            else cleanup_run
        )
        return instance

    def execute(self, request: LiveRunRequest) -> LiveRunOutcome:
        """执行一次固定生命周期并返回不泄漏模块细节的终态。"""
        if not isinstance(request, LiveRunRequest):
            raise TypeError("直播拆条应用只接受 LiveRunRequest")
        cancellation = CancellationSource(clock=self._monotonic_clock)
        run_id: RunId | None = None
        run_workspace: _AuditableRunWorkspace | None = None
        commit_state: _CommitState | None = None
        projected_outcome: LiveRunOutcome | None = None
        teardown_incomplete = False

        def best_effort_cleanup() -> None:
            target = run_workspace
            if target is not None:
                self._cleanup_run(target)

        coordinator = SignalCoordinator(
            cancellation,
            best_effort_cleanup=best_effort_cleanup,
        )
        try:
            coordinator.install()
            candidate_run_id = self._run_id_factory()
            if not isinstance(candidate_run_id, RunId):
                raise TypeError("运行标识工厂必须返回 RunId")
            run_id = candidate_run_id
            try:
                workspace = self._open_workspace(
                    request.source,
                    request.workspace_dir,
                )
            except WorkspaceFailure as source_failure:
                if source_failure.error_code not in {
                    ErrorCode.INPUT_MISSING,
                    ErrorCode.INPUT_UNREADABLE,
                }:
                    raise
                workspace = self._open_diagnostic_workspace(
                    request.source,
                    request.workspace_dir,
                )
                run_workspace = workspace.acquire_diagnostic_run(run_id)
                projected_outcome = (
                    self._execute_workspace_input_failure(
                        run_id=run_id,
                        run_workspace=run_workspace,
                        failure=source_failure,
                        cancellation=cancellation.token,
                    )
                )
            else:
                run_workspace = workspace.acquire_run(run_id)
                commit_state = _CommitState(
                    run_id,
                    run_workspace.published_delivery,
                )
                projected_outcome = self._execute_acquired_run(
                    request=request,
                    run_id=run_id,
                    workspace=workspace,
                    run_workspace=run_workspace,
                    cancellation=cancellation,
                    commit_state=commit_state,
                )
        except Exception as failure:
            outcome_run_id = run_id if run_id is not None else RunId.new()
            if run_workspace is None or commit_state is None:
                recordable_failure = _recordable_failure(failure)
                error_code = getattr(
                    recordable_failure,
                    "error_code",
                    ErrorCode.INTERNAL_UNEXPECTED,
                )
                if not isinstance(error_code, ErrorCode):
                    error_code = ErrorCode.INTERNAL_UNEXPECTED
                recovery_incomplete = (
                    error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
                )
                if run_workspace is not None:
                    try:
                        self._cleanup_run(run_workspace)
                    except Exception:
                        recovery_incomplete = True
                projected_outcome = (
                    LiveRunOutcome._failed_without_diagnostics(
                        outcome_run_id,
                        error_code,
                        recovery_incomplete=recovery_incomplete,
                    )
                )
            else:
                projected_outcome = self._fallback_outcome(
                    outcome_run_id,
                    run_workspace,
                    commit_state,
                    failure,
                )
        finally:
            try:
                coordinator.complete_cleanup()
            except Exception:
                teardown_incomplete = True
            if run_workspace is not None:
                try:
                    run_workspace.close()
                except Exception:
                    teardown_incomplete = True
                    try:
                        run_workspace.close()
                    except Exception:
                        pass
            if (
                projected_outcome is not None
                and commit_state is not None
                and commit_state.committed
                and cancellation.token.cancelled
                and not commit_state.interruption_recorded
            ):
                projected_outcome = (
                    projected_outcome._with_incomplete_diagnostics()
                )
            try:
                coordinator.restore()
            except Exception:
                teardown_incomplete = True
            if (
                projected_outcome is not None
                and commit_state is not None
                and commit_state.committed
                and cancellation.token.cancelled
                and not commit_state.interruption_recorded
            ):
                projected_outcome = (
                    projected_outcome._with_incomplete_diagnostics()
                )
        if projected_outcome is None:
            raise RuntimeError("直播拆条运行没有形成类型化终态")
        if teardown_incomplete:
            projected_outcome = (
                projected_outcome._with_incomplete_recovery()
            )
        return projected_outcome

    def _execute_workspace_input_failure(
        self,
        *,
        run_id: RunId,
        run_workspace: DiagnosticRunWorkspace,
        failure: WorkspaceFailure,
        cancellation: CancellationToken,
    ) -> LiveRunOutcome:
        diagnostics = self._initialize_diagnostics(
            run_workspace,
            self._application_version,
            self._wall_clock,
            self._monotonic_clock,
        )
        stage = diagnostics.start_stage(RunStage.INITIALIZED)
        cleanup_started = self._monotonic_clock()
        cleanup_failure = self._attempt_cleanup(run_workspace)
        cleanup_completed = self._monotonic_clock()
        cleanup_duration_ms = max(
            0,
            int((cleanup_completed - cleanup_started) * 1000),
        )
        signal_number = cancellation.signal_number
        if signal_number in {signal.SIGINT, signal.SIGTERM}:
            interruption_signal = self._record_interruption(
                stage,
                RunStage.INITIALIZED,
                signal_number,
            )
            associated_errors, recovery_incomplete = (
                self._record_cleanup_failure(stage, cleanup_failure)
            )
            stage.complete(
                StageOutcome.INTERRUPTED,
                work_item_count=0,
            )
            run_outcome = RunOutcome.interrupted(
                interruption_signal,
                cleanup_duration_ms=cleanup_duration_ms,
                associated_errors=associated_errors,
                recovery_incomplete=recovery_incomplete,
            )
        else:
            primary_error = stage.scope(
                ErrorModule.WORKSPACE
            ).record_failure(failure)
            associated_errors, recovery_incomplete = (
                self._record_cleanup_failure(stage, cleanup_failure)
            )
            stage.complete(StageOutcome.FAILED, work_item_count=0)
            run_outcome = RunOutcome.failed(
                primary_error,
                associated_errors=associated_errors,
                recovery_incomplete=recovery_incomplete,
            )
        try:
            finalization = diagnostics.finish(run_outcome)
        except Exception:
            return LiveRunOutcome._from_run_outcome(
                run_id,
                run_outcome,
                diagnostics_incomplete=True,
            )
        return LiveRunOutcome._from_run_outcome(
            run_id,
            run_outcome,
            diagnostics_incomplete=finalization.diagnostics_incomplete,
        )

    def _execute_acquired_run(
        self,
        *,
        request: LiveRunRequest,
        run_id: RunId,
        workspace: Workspace,
        run_workspace: RunWorkspace,
        cancellation: CancellationSource,
        commit_state: _CommitState,
    ) -> LiveRunOutcome:
        cursor = _LifecycleCursor()
        diagnostics = self._initialize_diagnostics(
            run_workspace,
            self._application_version,
            self._wall_clock,
            self._monotonic_clock,
        )
        source_capability = workspace.source
        if source_capability is None:
            raise RuntimeError("直播拆条运行缺少素材 capability")

        postcommit_cleanup_incomplete = False
        try:
            assembly_work = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.PREFLIGHT,
                lambda stage, token: self._prepare_run(
                    request=request,
                    source=source_capability,
                    run_workspace=run_workspace,
                    stage=stage,
                    cancellation=token,
                ),
            )
            assembly = assembly_work.value
            source = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.SOURCE_ANALYSIS,
                lambda stage, token: assembly.analyze_source(
                    source_capability,
                    stage,
                    token,
                ),
                expected_value=SourceDescription,
            )
            transcript = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.TRANSCRIPTION,
                lambda stage, token: assembly.transcribe(
                    source.value,
                    stage,
                    token,
                ),
            )
            plan = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.CANDIDATE_PLANNING,
                lambda stage, token: assembly.plan_candidates(
                    transcript.value,
                    stage,
                    token,
                ),
            )
            reviewed_plan = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.TOPIC_REVIEW,
                lambda stage, token: assembly.review_topics(
                    plan.value,
                    stage,
                    token,
                ),
            )
            built = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.DELIVERY_BUILD,
                lambda stage, token: assembly.build_delivery(
                    reviewed_plan.value,
                    stage,
                    token,
                ),
                expected_result=_DeliveryBuildWork,
            )
            if not isinstance(built, _DeliveryBuildWork):
                raise RuntimeError("交付构建结果类型验证失效")
            commit_state.prepare(built.result_kind)
            verified = self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.DELIVERY_VERIFICATION,
                lambda stage, token: assembly.verify_delivery(
                    built.delivery,
                    stage,
                    token,
                ),
                expected_value=VerifiedDelivery,
            )
            if not isinstance(verified.value, VerifiedDelivery):
                raise RuntimeError("交付验证结果类型验证失效")
            self._run_stage(
                cursor,
                diagnostics,
                cancellation,
                run_workspace,
                RunStage.PUBLISHING,
                lambda stage, token: assembly.publish(
                    verified.value,
                    stage,
                    token,
                    lambda work, effect: self._capture_publication(
                        cancellation,
                        commit_state,
                        work,
                        effect,
                    ),
                ),
                expected_value=PublishedDelivery,
                commit_state=commit_state,
            )
            cursor.complete()
            run_outcome = commit_state.outcome()
            try:
                self._cleanup_run(run_workspace)
            except Exception:
                # 发布提交证明优先；清理失败不能撤销完整可见的交付。
                postcommit_cleanup_incomplete = True
        except _StageTerminated as terminal:
            run_outcome = terminal.outcome
        if postcommit_cleanup_incomplete:
            return LiveRunOutcome._from_run_outcome(
                run_id,
                run_outcome,
                diagnostics_incomplete=True,
                recovery_incomplete=True,
            )
        try:
            finalization = diagnostics.finish(run_outcome)
        except Exception:
            return LiveRunOutcome._from_run_outcome(
                run_id,
                run_outcome,
                diagnostics_incomplete=True,
            )
        return LiveRunOutcome._from_run_outcome(
            run_id,
            run_outcome,
            diagnostics_incomplete=finalization.diagnostics_incomplete,
        )

    def _fallback_outcome(
        self,
        run_id: RunId,
        run_workspace: RunWorkspace,
        commit_state: _CommitState,
        failure: Exception,
    ) -> LiveRunOutcome:
        if commit_state.committed:
            recovery_incomplete = False
            try:
                self._cleanup_run(run_workspace)
            except Exception:
                recovery_incomplete = True
            return LiveRunOutcome._from_run_outcome(
                run_id,
                commit_state.outcome(),
                diagnostics_incomplete=True,
                recovery_incomplete=recovery_incomplete,
            )

        recovery_incomplete = False
        try:
            self._cleanup_run(run_workspace)
        except Exception:
            recovery_incomplete = True
        recordable_failure = _recordable_failure(failure)
        error_code = getattr(
            recordable_failure,
            "error_code",
            ErrorCode.INTERNAL_UNEXPECTED,
        )
        if not isinstance(error_code, ErrorCode):
            error_code = ErrorCode.INTERNAL_UNEXPECTED
        return LiveRunOutcome._failed_without_diagnostics(
            run_id,
            error_code,
            recovery_incomplete=recovery_incomplete,
        )

    @staticmethod
    def _capture_publication(
        cancellation: CancellationSource,
        commit_state: _CommitState,
        work: _StageWork,
        effect: Callable[[], None],
    ) -> _StageWork:
        if not isinstance(work, _StageWork):
            raise TypeError("发布提交回调只接受类型化发布阶段结果")
        if not isinstance(work.value, PublishedDelivery):
            raise TypeError("发布提交回调必须包含 PublishedDelivery")
        if not callable(effect):
            raise TypeError("发布提交回调必须包含受管原子效果")
        commit_state.validate(work.value)
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            {signal.SIGINT, signal.SIGTERM},
        )
        try:
            cancellation.token.raise_if_cancelled()
            commit_state.capture(work.value)
            try:
                effect()
            except Exception:
                commit_state.discard_capture()
                raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return work

    def _run_stage(
        self,
        cursor: _LifecycleCursor,
        diagnostics: RunDiagnostics,
        cancellation: CancellationSource,
        run_workspace: RunWorkspace,
        stage: RunStage,
        effect: Callable[
            [StageDiagnostics, CancellationToken],
            _StageWork | _DeliveryBuildWork,
        ],
        *,
        expected_result: type[_StageWork] | type[_DeliveryBuildWork] = (
            _StageWork
        ),
        expected_value: type[object] | None = None,
        commit_state: _CommitState | None = None,
    ) -> _StageWork | _DeliveryBuildWork:
        cursor.enter(stage)
        stage_diagnostics = diagnostics.start_stage(stage)
        result: _StageWork | _DeliveryBuildWork | None = None
        try:
            self._record_delivery_started(stage_diagnostics, stage)
            self._control.stage_started(stage, cancellation)
            cancellation.token.raise_if_cancelled()
            result = effect(stage_diagnostics, cancellation.token)
            if not isinstance(result, expected_result):
                raise TypeError("业务阶段返回了错误的类型化阶段结果")
            if expected_value is not None and not isinstance(
                result.value,
                expected_value,
            ):
                raise TypeError("业务阶段返回值不满足阶段契约")
            if commit_state is not None and not commit_state.committed:
                raise TypeError("发布阶段没有调用提交证明回调")
            self._record_delivery_succeeded(
                stage_diagnostics,
                stage,
                result,
            )
            if commit_state is not None:
                try:
                    self._control.publication_committed(cancellation)
                except Exception:
                    # 提交点后的信号或控制故障不能撤销成功。
                    pass
                signal_number = cancellation.token.signal_number
                if signal_number in {signal.SIGINT, signal.SIGTERM}:
                    stage_diagnostics.scope(
                        ErrorModule.APPLICATION
                    ).record(
                        Facts.interruption(
                            _interruption_signal(signal_number)
                        )
                    )
                    commit_state.mark_interruption_recorded()
            stage_diagnostics.complete(
                StageOutcome.SUCCEEDED,
                work_item_count=result.work_item_count,
            )
            return result
        except DiagnosticsFailure:
            if commit_state is not None and commit_state.committed:
                if result is None:
                    raise RuntimeError("提交证明缺少发布阶段结果")
                return result
            raise
        except CancellationRequested as interruption:
            if commit_state is not None and commit_state.committed:
                if result is None:
                    raise RuntimeError("提交证明缺少发布阶段结果")
                return result
            self._terminate_interrupted_stage(
                cursor,
                stage_diagnostics,
                run_workspace,
                stage,
                interruption.signal_number,
            )
        except Exception as failure:
            if commit_state is not None and commit_state.committed:
                if result is None:
                    raise RuntimeError("提交证明缺少发布阶段结果")
                return result
            signal_number = cancellation.token.signal_number
            if signal_number in {signal.SIGINT, signal.SIGTERM}:
                self._terminate_interrupted_stage(
                    cursor,
                    stage_diagnostics,
                    run_workspace,
                    stage,
                    signal_number,
                )
            self._record_delivery_failed(stage_diagnostics, stage)
            recordable_failure = _recordable_failure(failure)
            error_module = _failure_module(
                recordable_failure,
                stage,
            )
            primary_error = stage_diagnostics.scope(
                error_module
            ).record_failure(recordable_failure)
            associated_errors, recovery_incomplete = (
                self._cleanup_before_terminal(
                    stage_diagnostics,
                    run_workspace,
                )
            )
            stage_diagnostics.complete(
                StageOutcome.FAILED,
                work_item_count=0,
            )
            cursor.terminate()
            raise _StageTerminated(
                RunOutcome.failed(
                    primary_error,
                    associated_errors=associated_errors,
                    recovery_incomplete=recovery_incomplete,
                )
            ) from None

    def _terminate_interrupted_stage(
        self,
        cursor: _LifecycleCursor,
        stage_diagnostics: StageDiagnostics,
        run_workspace: RunWorkspace,
        stage: RunStage,
        signal_number: int | None,
    ) -> NoReturn:
        run_outcome = self._complete_interrupted_stage(
            stage_diagnostics,
            run_workspace,
            stage,
            signal_number,
        )
        cursor.terminate()
        raise _StageTerminated(run_outcome) from None

    def _complete_interrupted_stage(
        self,
        stage_diagnostics: StageDiagnostics,
        run_workspace: RunWorkspace,
        stage: RunStage,
        signal_number: int | None,
    ) -> RunOutcome:
        interruption_signal = self._record_interruption(
            stage_diagnostics,
            stage,
            signal_number,
        )
        cleanup_started = self._monotonic_clock()
        cleanup_failure = self._attempt_cleanup(run_workspace)
        associated_errors, recovery_incomplete = (
            self._record_cleanup_failure(
                stage_diagnostics,
                cleanup_failure,
            )
        )
        cleanup_completed = self._monotonic_clock()
        cleanup_duration_ms = max(
            0,
            int((cleanup_completed - cleanup_started) * 1000),
        )
        stage_diagnostics.complete(
            StageOutcome.INTERRUPTED,
            work_item_count=0,
        )
        return RunOutcome.interrupted(
            interruption_signal,
            cleanup_duration_ms=cleanup_duration_ms,
            associated_errors=associated_errors,
            recovery_incomplete=recovery_incomplete,
        )

    def _record_interruption(
        self,
        stage_diagnostics: StageDiagnostics,
        stage: RunStage,
        signal_number: int | None,
    ) -> InterruptionSignal:
        if signal_number not in {signal.SIGINT, signal.SIGTERM}:
            raise _ApplicationFailure(
                ErrorCode.INTERNAL_UNEXPECTED,
                {
                    "source_module": (
                        "video_auto_editor.application.live"
                    ),
                    "function": "_run_stage",
                    "line": 1,
                },
            ) from None
        interruption_signal = _interruption_signal(signal_number)
        self._record_delivery_interrupted(
            stage_diagnostics,
            stage,
        )
        stage_diagnostics.scope(ErrorModule.APPLICATION).record(
            Facts.interruption(interruption_signal)
        )
        return interruption_signal

    @staticmethod
    def _record_delivery_started(
        stage_diagnostics: StageDiagnostics,
        stage: RunStage,
    ) -> None:
        if stage is RunStage.DELIVERY_BUILD:
            stage_diagnostics.scope(ErrorModule.DELIVERY_BUILD).record(
                Facts.delivery_build(DeliveryBuildState.IN_PROGRESS)
            )
        elif stage is RunStage.DELIVERY_VERIFICATION:
            stage_diagnostics.scope(
                ErrorModule.DELIVERY_VERIFICATION
            ).record(
                Facts.delivery_verification(
                    DeliveryVerificationState.IN_PROGRESS
                )
            )
        elif stage is RunStage.PUBLISHING:
            stage_diagnostics.scope(ErrorModule.PUBLICATION).record(
                Facts.publication(PublicationState.IN_PROGRESS)
            )

    @staticmethod
    def _record_delivery_succeeded(
        stage_diagnostics: StageDiagnostics,
        stage: RunStage,
        result: _StageWork | _DeliveryBuildWork,
    ) -> None:
        if stage is RunStage.DELIVERY_BUILD:
            stage_diagnostics.scope(ErrorModule.DELIVERY_BUILD).record(
                Facts.delivery_build(DeliveryBuildState.COMPLETED)
            )
        elif stage is RunStage.DELIVERY_VERIFICATION:
            stage_diagnostics.scope(
                ErrorModule.DELIVERY_VERIFICATION
            ).record(
                Facts.delivery_verification(
                    DeliveryVerificationState.PASSED
                )
            )
        elif stage is RunStage.PUBLISHING:
            if not isinstance(result.value, PublishedDelivery):
                raise TypeError("发布阶段缺少 PublishedDelivery")
            stage_diagnostics.scope(ErrorModule.PUBLICATION).record(
                Facts.publication(
                    PublicationState.COMMITTED,
                    published_delivery=result.value,
                )
            )

    @staticmethod
    def _record_delivery_interrupted(
        stage_diagnostics: StageDiagnostics,
        stage: RunStage,
    ) -> None:
        if stage is RunStage.DELIVERY_BUILD:
            stage_diagnostics.scope(ErrorModule.DELIVERY_BUILD).record(
                Facts.delivery_build(DeliveryBuildState.INTERRUPTED)
            )
        elif stage is RunStage.DELIVERY_VERIFICATION:
            stage_diagnostics.scope(
                ErrorModule.DELIVERY_VERIFICATION
            ).record(
                Facts.delivery_verification(
                    DeliveryVerificationState.INTERRUPTED
                )
            )
        elif stage is RunStage.PUBLISHING:
            stage_diagnostics.scope(ErrorModule.PUBLICATION).record(
                Facts.publication(PublicationState.ROLLED_BACK)
            )

    @staticmethod
    def _record_delivery_failed(
        stage_diagnostics: StageDiagnostics,
        stage: RunStage,
    ) -> None:
        if stage is RunStage.DELIVERY_BUILD:
            stage_diagnostics.scope(ErrorModule.DELIVERY_BUILD).record(
                Facts.delivery_build(DeliveryBuildState.FAILED)
            )
        elif stage is RunStage.DELIVERY_VERIFICATION:
            stage_diagnostics.scope(
                ErrorModule.DELIVERY_VERIFICATION
            ).record(
                Facts.delivery_verification(
                    DeliveryVerificationState.FAILED
                )
            )
        elif stage is RunStage.PUBLISHING:
            stage_diagnostics.scope(ErrorModule.PUBLICATION).record(
                Facts.publication(PublicationState.FAILED)
            )

    def _prepare_run(
        self,
        *,
        request: LiveRunRequest,
        source: SourceFileCapability,
        run_workspace: RunWorkspace,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        configuration = self._load_configuration(source.path)
        stage.scope(ErrorModule.CONFIGURATION).record(
            Facts.configuration(
                configuration.diagnostic_projection
            )
        )
        assembly = self._assembly_factory.create(
            request=request,
            configuration=configuration,
            run_workspace=run_workspace,
        )
        if not isinstance(assembly, _RunAssembly):
            raise TypeError("运行组合必须满足固定阶段级接口")
        preflight = assembly.preflight(stage, cancellation)
        if not isinstance(preflight, _StageWork):
            raise TypeError("聚合预检必须返回类型化阶段结果")
        return _StageWork(assembly, preflight.work_item_count)

    def _cleanup_before_terminal(
        self,
        stage: StageDiagnostics,
        run_workspace: _AuditableRunWorkspace,
    ) -> tuple[tuple[RunError, ...], bool]:
        return self._record_cleanup_failure(
            stage,
            self._attempt_cleanup(run_workspace),
        )

    def _attempt_cleanup(
        self,
        run_workspace: _AuditableRunWorkspace,
    ) -> Exception | None:
        try:
            self._cleanup_run(run_workspace)
        except Exception as failure:
            return _recordable_failure(failure)
        return None

    @staticmethod
    def _record_cleanup_failure(
        stage: StageDiagnostics,
        cleanup_failure: Exception | None,
    ) -> tuple[tuple[RunError, ...], bool]:
        if cleanup_failure is None:
            return (), False
        module = _failure_module(
            cleanup_failure,
            stage.stage,
        )
        cleanup_error = stage.scope(module).record_failure(
            cleanup_failure
        )
        return (cleanup_error,), True


_STAGE_MODULES = {
    RunStage.PREFLIGHT: ErrorModule.READINESS,
    RunStage.SOURCE_ANALYSIS: ErrorModule.SOURCE_ANALYSIS,
    RunStage.TRANSCRIPTION: ErrorModule.TRANSCRIPTION,
    RunStage.CANDIDATE_PLANNING: ErrorModule.CLIP_PLANNING,
    RunStage.TOPIC_REVIEW: ErrorModule.TOPIC_REVIEW,
    RunStage.DELIVERY_BUILD: ErrorModule.DELIVERY_BUILD,
    RunStage.DELIVERY_VERIFICATION: ErrorModule.DELIVERY_VERIFICATION,
    RunStage.PUBLISHING: ErrorModule.PUBLICATION,
}


def _failure_module(
    failure: Exception,
    stage: RunStage,
) -> ErrorModule:
    declared = getattr(failure, "module", None)
    if isinstance(declared, ErrorModule):
        return declared
    error_code = getattr(failure, "error_code", None)
    if not isinstance(error_code, ErrorCode):
        return _STAGE_MODULES[stage]
    value = error_code.value
    if value.startswith("config."):
        return ErrorModule.CONFIGURATION
    if value.startswith("environment."):
        return (
            ErrorModule.RUN_DIAGNOSTICS
            if error_code is ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE
            else ErrorModule.READINESS
        )
    if value.startswith("input."):
        return ErrorModule.SOURCE_ANALYSIS
    if value.startswith("transcription."):
        return ErrorModule.TRANSCRIPTION
    if value.startswith("media."):
        return _STAGE_MODULES[stage]
    if value.startswith("topic_review."):
        return ErrorModule.TOPIC_REVIEW
    if value.startswith("subtitle_optimization."):
        return ErrorModule.SUBTITLE_OPTIMIZATION
    if value.startswith("cache."):
        return ErrorModule.CACHE
    if value.startswith("diagnostics."):
        return ErrorModule.RUN_DIAGNOSTICS
    if value.startswith("workspace."):
        return ErrorModule.WORKSPACE
    if value.startswith("delivery."):
        return _STAGE_MODULES[stage]
    if value.startswith("publication."):
        return ErrorModule.PUBLICATION
    return ErrorModule.APPLICATION


def _interruption_signal(
    signal_number: int | None,
) -> InterruptionSignal:
    if signal_number == signal.SIGINT:
        return InterruptionSignal.SIGINT
    if signal_number == signal.SIGTERM:
        return InterruptionSignal.SIGTERM
    raise ValueError("受控中断必须来自 SIGINT 或 SIGTERM")


def _recordable_failure(failure: Exception) -> Exception:
    if (
        isinstance(getattr(failure, "error_code", None), ErrorCode)
        and isinstance(getattr(failure, "diagnostics", None), Mapping)
    ):
        return failure
    source_module = "video_auto_editor.application.live"
    function = "execute"
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
        location = InternalLocation.from_runtime(
            source_module=source_module,
            function=function,
            line=line,
        )
    except (TypeError, ValueError):
        location = InternalLocation.from_runtime(
            source_module="video_auto_editor.application.live",
            function="execute",
            line=1,
        )
    return _ApplicationFailure(
        ErrorCode.INTERNAL_UNEXPECTED,
        {
            "source_module": location.source_module,
            "function": location.function,
            "line": location.line,
        },
    )
