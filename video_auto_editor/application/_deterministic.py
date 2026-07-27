"""只供无密钥契约测试使用的确定性直播拆条组合根。"""

import hashlib
import json
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from video_auto_editor.configuration import Configuration, LoadedConfiguration
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.diagnostics import (
    ArtifactRole,
    CacheNamespace,
    CacheOutcome,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    RecoveredNoticeKind,
    ResultKind,
    RetryKind,
    RunDiagnostics,
)
from video_auto_editor.diagnostics._session import StageDiagnostics
from video_auto_editor.diagnostics._store import _MemoryDiagnosticStore
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
    CancellationToken,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    RunStage,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.workspace import (
    DiagnosticRunWorkspace,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
)

from .live import (
    LiveApplication,
    LiveRunRequest,
    _DeliveryBuildWork,
    _RunAssembly,
    _StageWork,
)


@dataclass(frozen=True, slots=True)
class _DeterministicFact:
    kind: str


_SourceAnalyzer = Callable[
    [SourceFileCapability, CancellationToken],
    SourceDescription,
]


def _deterministic_source_analyzer(
    source: SourceFileCapability,
    cancellation: CancellationToken,
) -> SourceDescription:
    """形成不启动媒体工具的确定性素材事实。"""
    cancellation.raise_if_cancelled()
    contents = source.path.read_bytes()
    cancellation.raise_if_cancelled()
    return SourceDescription._from_analysis(
        source_file=source,
        sha256=f"sha256:{hashlib.sha256(contents).hexdigest()}",
        byte_length=len(contents),
        duration_ms=1_000,
    )


class _DeterministicRunAssembly:
    __slots__ = (
        "_course_context_sha256",
        "_emit_non_stage_events",
        "_failure_stage",
        "_invalid_result_stage",
        "_invalid_work_count_stage",
        "_postcommit_cancellation",
        "_postcommit_effect_failure",
        "_result_kind",
        "_run_workspace",
        "_source_analyzer",
        "_subtitle_failure",
        "_unexpected_stage",
    )

    def __init__(
        self,
        run_workspace: RunWorkspace,
        source_analyzer: _SourceAnalyzer,
        configuration: LoadedConfiguration,
        result_kind: ResultKind,
        emit_non_stage_events: bool,
        subtitle_failure: bool,
        failure_stage: RunStage | None,
        unexpected_stage: RunStage | None,
        invalid_result_stage: RunStage | None,
        invalid_work_count_stage: RunStage | None,
        postcommit_cancellation: bool,
        postcommit_effect_failure: bool,
    ) -> None:
        self._run_workspace = run_workspace
        self._source_analyzer = source_analyzer
        self._course_context_sha256 = (
            None
            if configuration.course_context is None
            else configuration.course_context.sha256
        )
        self._result_kind = result_kind
        self._emit_non_stage_events = emit_non_stage_events
        self._subtitle_failure = subtitle_failure
        self._failure_stage = failure_stage
        self._unexpected_stage = unexpected_stage
        self._invalid_result_stage = invalid_result_stage
        self._invalid_work_count_stage = invalid_work_count_stage
        self._postcommit_cancellation = postcommit_cancellation
        self._postcommit_effect_failure = postcommit_effect_failure

    def _before_work(self, stage: RunStage) -> None:
        if stage is self._failure_stage:
            error_code, module = _FAILURES[stage]
            raise _DeterministicFailure(error_code, module)
        if stage is self._unexpected_stage:
            raise RuntimeError("secret exception detail")

    def _work(
        self,
        stage: RunStage,
        value: object,
        work_item_count: int,
    ) -> _StageWork:
        if stage is self._invalid_work_count_stage:
            return _StageWork(value, "invalid")  # type: ignore[arg-type]
        return _StageWork(value, work_item_count)

    def preflight(
        self,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        stage.scope(ErrorModule.READINESS)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.PREFLIGHT)
        return self._work(
            RunStage.PREFLIGHT,
            _DeterministicFact("readiness"),
            1,
        )

    def analyze_source(
        self,
        source: SourceFileCapability,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        scope = stage.scope(ErrorModule.SOURCE_ANALYSIS)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.SOURCE_ANALYSIS)
        description = self._source_analyzer(source, cancellation)
        context_digest = self._course_context_sha256
        scope.record(
            Facts.source(
                sha256=description.sha256,
                byte_length=description.byte_length,
                duration_ms=description.duration_ms,
                course_context_provided=context_digest is not None,
                course_context_sha256=context_digest,
            )
        )
        return self._work(
            RunStage.SOURCE_ANALYSIS,
            description,
            1,
        )

    def transcribe(
        self,
        source: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(source, SourceDescription):
            raise TypeError("确定性语音识别需要素材事实")
        scope = stage.scope(ErrorModule.TRANSCRIPTION)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.TRANSCRIPTION)
        if self._emit_non_stage_events:
            cache_operation = scope.start_operation(
                OperationKind.CACHE_READ,
                item_index=1,
                item_count=1,
            )
            cache_operation.record(
                Facts.cache(
                    CacheNamespace.TRANSCRIPTION_SHARD,
                    CacheOutcome.HIT,
                )
            )
            cache_operation.complete(
                OperationOutcome.SUCCEEDED,
                attempt_count=1,
            )
            recovery_operation = scope.start_operation(
                OperationKind.COVERAGE_RECOVERY,
                item_index=1,
                item_count=1,
            )
            recovery_operation.schedule_retry(
                RetryKind.COVERAGE_RECOVERY,
                next_attempt=2,
                reason_code="coverage.gap_detected",
                backoff_ms=0,
            )
            recovery_operation.complete(
                OperationOutcome.SUCCEEDED,
                attempt_count=2,
            )
            scope.record(
                Facts.recovered(
                    RecoveredNoticeKind.COVERAGE_RECOVERY_SUCCEEDED
                )
            )
        return self._work(
            RunStage.TRANSCRIPTION,
            _DeterministicFact("transcript"),
            1,
        )

    def plan_candidates(
        self,
        transcript: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(transcript, _DeterministicFact):
            raise TypeError("确定性候选规划需要转写事实")
        stage.scope(ErrorModule.CLIP_PLANNING)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.CANDIDATE_PLANNING)
        return self._work(
            RunStage.CANDIDATE_PLANNING,
            _DeterministicFact("candidate_plan"),
            1,
        )

    def review_topics(
        self,
        plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(plan, _DeterministicFact):
            raise TypeError("确定性主题评审需要候选规划事实")
        stage.scope(ErrorModule.TOPIC_REVIEW)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.TOPIC_REVIEW)
        return self._work(
            RunStage.TOPIC_REVIEW,
            _DeterministicFact("reviewed_plan"),
            1,
        )

    def build_delivery(
        self,
        reviewed_plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _DeliveryBuildWork:
        if not isinstance(reviewed_plan, _DeterministicFact):
            raise TypeError("确定性交付构建需要完整评审事实")
        delivery_scope = stage.scope(ErrorModule.DELIVERY_BUILD)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.DELIVERY_BUILD)
        if self._invalid_result_stage is RunStage.DELIVERY_BUILD:
            return self._work(
                RunStage.DELIVERY_BUILD,
                _DeterministicFact("invalid_delivery"),
                1,
            )  # type: ignore[return-value]
        if self._result_kind is ResultKind.CLIPS:
            subtitle_scope = stage.scope(
                ErrorModule.SUBTITLE_OPTIMIZATION
            )
            subtitle_operation = subtitle_scope.start_operation(
                OperationKind.SUBTITLE_WINDOW,
                item_index=1,
                item_count=1,
            )
            if self._subtitle_failure:
                subtitle_operation.complete(
                    OperationOutcome.FAILED,
                    attempt_count=1,
                )
                raise _DeterministicFailure(
                    ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID,
                    ErrorModule.SUBTITLE_OPTIMIZATION,
                )
            subtitle_operation.complete(
                OperationOutcome.SUCCEEDED,
                attempt_count=1,
            )
        self._run_workspace.delivery_staging.location(
            "manifest.json"
        ).publish_bytes_atomically(
            (
                '{"result_kind":"'
                + self._result_kind.value
                + '","run_id":"'
                + str(self._run_workspace.run_id)
                + '","schema_version":"deterministic.v1"}\n'
            ).encode("utf-8")
        )
        delivery_scope.record(
            Facts.artifact_created(
                ArtifactRole.MANIFEST,
                relative_path="manifest.json",
            )
        )
        delivery = UnverifiedDelivery._from_build(
            self._run_workspace.run_id,
            self._run_workspace.delivery_staging,
        )
        if self._invalid_work_count_stage is RunStage.DELIVERY_BUILD:
            return _DeliveryBuildWork(
                delivery,
                self._result_kind,
                "invalid",  # type: ignore[arg-type]
            )
        return _DeliveryBuildWork(
            delivery,
            self._result_kind,
            1 if self._result_kind is ResultKind.CLIPS else 0,
        )

    def verify_delivery(
        self,
        delivery: UnverifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        scope = stage.scope(ErrorModule.DELIVERY_VERIFICATION)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.DELIVERY_VERIFICATION)
        if (
            self._invalid_result_stage
            is RunStage.DELIVERY_VERIFICATION
        ):
            return self._work(
                RunStage.DELIVERY_VERIFICATION,
                _DeterministicFact("invalid_verification"),
                1,
            )
        manifest = json.loads(
            delivery.managed_directory.location(
                "manifest.json"
            ).read_bytes()
        )
        if manifest != {
            "result_kind": self._result_kind.value,
            "run_id": str(self._run_workspace.run_id),
            "schema_version": "deterministic.v1",
        }:
            raise _DeterministicFailure(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorModule.DELIVERY_VERIFICATION,
            )
        scope.record(
            Facts.artifact_verified(
                ArtifactRole.MANIFEST,
                relative_path="manifest.json",
            )
        )
        verified = VerifiedDelivery._from_verification(
            delivery,
            verification_snapshot="deterministic-v1",
        )
        return self._work(
            RunStage.DELIVERY_VERIFICATION,
            verified,
            1,
        )

    def publish(
        self,
        delivery: VerifiedDelivery,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
        commit: Callable[
            [_StageWork, Callable[[], None]],
            _StageWork,
        ],
    ) -> _StageWork:
        stage.scope(ErrorModule.PUBLICATION)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.PUBLISHING)
        if self._invalid_result_stage is RunStage.PUBLISHING:
            return self._work(
                RunStage.PUBLISHING,
                _DeterministicFact("invalid_publication"),
                1,
            )
        manifest = delivery.managed_directory.location(
            "manifest.json"
        ).read_bytes()
        published = PublishedDelivery._from_publication(
            delivery,
            published_directory=self._run_workspace.published_delivery,
        )
        work = self._work(RunStage.PUBLISHING, published, 1)
        marker = self._run_workspace.published_delivery.location(
            "manifest.json"
        )

        def publish_marker() -> None:
            try:
                marker.publish_bytes_atomically(manifest)
            except Exception:
                # 受管原子写可能在重命名已可见、父目录耐久同步失败
                # 后报告失败。当前 run_id 唯一标记若已完整可见，则
                # 逻辑提交点已经越过；真实耐久回滚属于 Publication。
                try:
                    visible_manifest = marker.read_bytes()
                except Exception:
                    raise _DeterministicFailure(
                        ErrorCode.PUBLICATION_COMMIT_FAILED,
                        ErrorModule.PUBLICATION,
                    ) from None
                if visible_manifest != manifest:
                    raise _DeterministicFailure(
                        ErrorCode.PUBLICATION_COMMIT_FAILED,
                        ErrorModule.PUBLICATION,
                    ) from None

        # 先形成全部内存返回值，再把唯一可见写入交给应用拥有的短提交
        # 临界区；回调会把物理提交与提交证明捕获绑定为一个边界。
        committed = commit(work, publish_marker)
        if self._postcommit_cancellation:
            raise CancellationRequested(signal.SIGTERM)
        if self._postcommit_effect_failure:
            raise RuntimeError("deterministic postcommit effect failure")
        return committed


class _DeterministicAssemblyFactory:
    __slots__ = (
        "_emit_non_stage_events",
        "_failure_stage",
        "_invalid_result_stage",
        "_invalid_work_count_stage",
        "_postcommit_cancellation",
        "_postcommit_effect_failure",
        "_result_kind",
        "_source_analyzer",
        "_subtitle_failure",
        "_unexpected_stage",
    )

    def __init__(
        self,
        source_analyzer: _SourceAnalyzer,
        result_kind: ResultKind,
        emit_non_stage_events: bool,
        subtitle_failure: bool,
        failure_stage: RunStage | None,
        unexpected_stage: RunStage | None,
        invalid_result_stage: RunStage | None,
        invalid_work_count_stage: RunStage | None,
        postcommit_cancellation: bool,
        postcommit_effect_failure: bool,
    ) -> None:
        self._source_analyzer = source_analyzer
        self._result_kind = result_kind
        self._emit_non_stage_events = emit_non_stage_events
        self._subtitle_failure = subtitle_failure
        self._failure_stage = failure_stage
        self._unexpected_stage = unexpected_stage
        self._invalid_result_stage = invalid_result_stage
        self._invalid_work_count_stage = invalid_work_count_stage
        self._postcommit_cancellation = postcommit_cancellation
        self._postcommit_effect_failure = postcommit_effect_failure

    def create(
        self,
        *,
        request: LiveRunRequest,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
    ) -> _RunAssembly:
        del request
        return _DeterministicRunAssembly(
            run_workspace,
            self._source_analyzer,
            configuration,
            self._result_kind,
            self._emit_non_stage_events,
            self._subtitle_failure,
            self._failure_stage,
            self._unexpected_stage,
            self._invalid_result_stage,
            self._invalid_work_count_stage,
            self._postcommit_cancellation,
            self._postcommit_effect_failure,
        )


class _DeterministicFailure(RuntimeError):
    __slots__ = ("diagnostics", "error_code", "module")

    def __init__(
        self,
        error_code: ErrorCode,
        module: ErrorModule,
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.error_code = error_code
        self.module = module
        self.diagnostics = MappingProxyType(
            {} if diagnostics is None else dict(diagnostics)
        )
        super().__init__("确定性阶段故障")


_FAILURES = {
    RunStage.PREFLIGHT: (
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        ErrorModule.READINESS,
    ),
    RunStage.SOURCE_ANALYSIS: (
        ErrorCode.INPUT_MEDIA_INVALID,
        ErrorModule.SOURCE_ANALYSIS,
    ),
    RunStage.TRANSCRIPTION: (
        ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
        ErrorModule.TRANSCRIPTION,
    ),
    RunStage.CANDIDATE_PLANNING: (
        ErrorCode.MEDIA_PROCESSING_FAILED,
        ErrorModule.CLIP_PLANNING,
    ),
    RunStage.TOPIC_REVIEW: (
        ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID,
        ErrorModule.TOPIC_REVIEW,
    ),
    RunStage.DELIVERY_BUILD: (
        ErrorCode.DELIVERY_BUILD_FAILED,
        ErrorModule.DELIVERY_BUILD,
    ),
    RunStage.DELIVERY_VERIFICATION: (
        ErrorCode.DELIVERY_VERIFICATION_FAILED,
        ErrorModule.DELIVERY_VERIFICATION,
    ),
    RunStage.PUBLISHING: (
        ErrorCode.PUBLICATION_COMMIT_FAILED,
        ErrorModule.PUBLICATION,
    ),
}


class _DeterministicControl:
    __slots__ = (
        "_deliver_signal_through_os",
        "_interrupt_after_commit",
        "_interruption_signal",
        "_interruption_stage",
        "_postcommit_control_failure",
    )

    def __init__(
        self,
        interruption_stage: RunStage | None,
        interruption_signal: InterruptionSignal,
        interrupt_after_commit: bool,
        deliver_signal_through_os: bool,
        postcommit_control_failure: bool,
    ) -> None:
        self._interruption_stage = interruption_stage
        self._interruption_signal = interruption_signal
        self._interrupt_after_commit = interrupt_after_commit
        self._deliver_signal_through_os = deliver_signal_through_os
        self._postcommit_control_failure = postcommit_control_failure

    def stage_started(
        self,
        stage: RunStage,
        cancellation: CancellationSource,
    ) -> None:
        if stage is self._interruption_stage:
            signal_number = (
                signal.SIGINT
                if self._interruption_signal is InterruptionSignal.SIGINT
                else signal.SIGTERM
            )
            if self._deliver_signal_through_os:
                os.kill(os.getpid(), signal_number)
            else:
                cancellation.request(signal_number)

    def publication_committed(
        self,
        cancellation: CancellationSource,
    ) -> None:
        if self._interrupt_after_commit:
            cancellation.request(
                signal.SIGINT
                if self._interruption_signal
                is InterruptionSignal.SIGINT
                else signal.SIGTERM
            )
        if self._postcommit_control_failure:
            raise RuntimeError("deterministic postcommit control failure")


class _DeterministicCleanup:
    __slots__ = ("_fail", "_fail_before_delete")

    def __init__(self, fail: bool, fail_before_delete: bool) -> None:
        self._fail = fail
        self._fail_before_delete = fail_before_delete

    def __call__(
        self,
        run_workspace: RunWorkspace | DiagnosticRunWorkspace,
    ) -> None:
        if self._fail_before_delete:
            raise _DeterministicFailure(
                ErrorCode.WORKSPACE_CLEANUP_FAILED,
                ErrorModule.WORKSPACE,
                {
                    "operation": "workspace.cleanup",
                    "reason_code": "workspace.directory_sync_failed",
                },
            )
        run_workspace.cleanup()
        if self._fail:
            raise _DeterministicFailure(
                ErrorCode.WORKSPACE_CLEANUP_FAILED,
                ErrorModule.WORKSPACE,
                {
                    "operation": "workspace.cleanup",
                    "reason_code": "workspace.directory_sync_failed",
                },
            )


class _SignalDuringWorkspaceOpen:
    __slots__ = ("_signal_number",)

    def __init__(self, interruption_signal: InterruptionSignal) -> None:
        self._signal_number = (
            signal.SIGINT
            if interruption_signal is InterruptionSignal.SIGINT
            else signal.SIGTERM
        )

    def __call__(self, source, workspace_dir) -> Workspace:
        os.kill(os.getpid(), self._signal_number)
        return Workspace.open(source, workspace_dir)


class _SignalDuringRunIdFactory:
    __slots__ = ("_signal_number",)

    def __init__(self, interruption_signal: InterruptionSignal) -> None:
        self._signal_number = (
            signal.SIGINT
            if interruption_signal is InterruptionSignal.SIGINT
            else signal.SIGTERM
        )

    def __call__(self) -> RunId:
        os.kill(os.getpid(), self._signal_number)
        return RunId.new()


class _FailingDiagnosticStore:
    __slots__ = ("_failure", "_memory", "run_id")

    def __init__(self, run_id, failure: str) -> None:
        self.run_id = run_id
        self._failure = failure
        self._memory = _MemoryDiagnosticStore(run_id)

    def append(self, payload: bytes) -> None:
        event = json.loads(payload)
        if (
            self._failure == "precommit"
            and event["event_code"] == "stage.started"
            and event["stage"] == "source_analysis"
        ):
            raise OSError("deterministic precommit failure")
        if (
            self._failure == "postcommit"
            and event["event_code"] == "delivery.state_changed"
            and event["attributes"] == {
                "phase": "publication",
                "state": "committed",
            }
        ):
            raise OSError("deterministic postcommit failure")
        self._memory.append(payload)

    def snapshot(self):
        return self._memory.snapshot()

    def publish_manifest(self, payload: bytes) -> None:
        if self._failure == "finalization":
            raise OSError("deterministic finalization failure")
        self._memory.publish_manifest(payload)


class _DeterministicDiagnosticsInitializer:
    __slots__ = ("_failure",)

    def __init__(self, failure: str | None) -> None:
        self._failure = failure

    def __call__(
        self,
        run_workspace: RunWorkspace | DiagnosticRunWorkspace,
        application_version: str,
        wall_clock,
        monotonic_clock,
    ) -> RunDiagnostics:
        if self._failure is None:
            return RunDiagnostics.initialize(
                run_workspace.diagnostics,
                application_version=application_version,
                wall_clock=wall_clock,
                monotonic_clock=monotonic_clock,
            )
        return RunDiagnostics._start(
            _FailingDiagnosticStore(
                run_workspace.run_id,
                self._failure,
            ),
            application_version=application_version,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )


def compose_deterministic_live_application(
    *,
    source_analyzer: _SourceAnalyzer | None = None,
    result_kind: ResultKind = ResultKind.CLIPS,
    failure_stage: RunStage | None = None,
    interruption_stage: RunStage | None = None,
    interruption_signal: InterruptionSignal = InterruptionSignal.SIGINT,
    interrupt_after_commit: bool = False,
    emit_non_stage_events: bool = False,
    cleanup_failure: bool = False,
    cleanup_failure_before_delete: bool = False,
    diagnostics_failure: str | None = None,
    unexpected_stage: RunStage | None = None,
    subtitle_failure: bool = False,
    deliver_signal_through_os: bool = False,
    invalid_result_stage: RunStage | None = None,
    invalid_work_count_stage: RunStage | None = None,
    postcommit_control_failure: bool = False,
    postcommit_cancellation: bool = False,
    postcommit_effect_failure: bool = False,
    interrupt_during_workspace_open: bool = False,
    interrupt_during_run_id_factory: bool = False,
) -> LiveApplication:
    """装配只使用本地确定性模块的应用实例。"""
    if source_analyzer is not None and not callable(source_analyzer):
        raise TypeError("确定性素材分析器必须可调用")
    if not isinstance(result_kind, ResultKind):
        raise TypeError("确定性结果必须使用 ResultKind")
    if failure_stage is not None and failure_stage not in _FAILURES:
        raise ValueError("确定性故障只能注入非初始化业务阶段")
    if (
        interruption_stage is not None
        and interruption_stage not in _FAILURES
    ):
        raise ValueError("确定性中断只能注入非初始化业务阶段")
    if not isinstance(interruption_signal, InterruptionSignal):
        raise TypeError("确定性中断必须使用 InterruptionSignal")
    if not isinstance(interrupt_after_commit, bool):
        raise TypeError("提交点后中断选项必须是布尔值")
    if not isinstance(deliver_signal_through_os, bool):
        raise TypeError("真实信号投递选项必须是布尔值")
    if deliver_signal_through_os and interruption_stage is None:
        raise ValueError("真实信号投递必须绑定中断阶段")
    if not isinstance(emit_non_stage_events, bool):
        raise TypeError("非阶段事件选项必须是布尔值")
    if not isinstance(cleanup_failure, bool):
        raise TypeError("清理故障选项必须是布尔值")
    if not isinstance(cleanup_failure_before_delete, bool):
        raise TypeError("清理前故障选项必须是布尔值")
    if cleanup_failure and cleanup_failure_before_delete:
        raise ValueError("清理故障位置只能选择一个")
    if diagnostics_failure not in {
        None,
        "precommit",
        "postcommit",
        "finalization",
    }:
        raise ValueError("诊断故障只能发生在提交点前、提交点后或终态收尾")
    if not isinstance(subtitle_failure, bool):
        raise TypeError("字幕优化故障选项必须是布尔值")
    if subtitle_failure and result_kind is ResultKind.EMPTY:
        raise ValueError("有效空结果没有字幕优化工作项")
    if unexpected_stage is not None and unexpected_stage not in _FAILURES:
        raise ValueError("未知异常只能注入非初始化业务阶段")
    if invalid_result_stage not in {
        None,
        RunStage.DELIVERY_BUILD,
        RunStage.DELIVERY_VERIFICATION,
        RunStage.PUBLISHING,
    }:
        raise ValueError("错误阶段结果只能注入交付阶段")
    if (
        invalid_work_count_stage is not None
        and invalid_work_count_stage not in _FAILURES
    ):
        raise ValueError("错误工作项数量只能注入非初始化业务阶段")
    if not isinstance(postcommit_control_failure, bool):
        raise TypeError("提交点后控制故障选项必须是布尔值")
    if not isinstance(postcommit_cancellation, bool):
        raise TypeError("提交点后取消异常选项必须是布尔值")
    if not isinstance(postcommit_effect_failure, bool):
        raise TypeError("提交点后模块故障选项必须是布尔值")
    if not isinstance(interrupt_during_workspace_open, bool):
        raise TypeError("workspace 打开中断选项必须是布尔值")
    if not isinstance(interrupt_during_run_id_factory, bool):
        raise TypeError("运行标识生成中断选项必须是布尔值")
    selected_injections = sum(
        value is not None
        for value in (
            failure_stage,
            interruption_stage,
            unexpected_stage,
            invalid_result_stage,
            invalid_work_count_stage,
        )
    ) + sum(
        (
            int(interrupt_after_commit),
            int(subtitle_failure),
            int(postcommit_control_failure),
            int(postcommit_cancellation),
            int(postcommit_effect_failure),
            int(interrupt_during_workspace_open),
            int(interrupt_during_run_id_factory),
        )
    )
    if selected_injections > 1:
        raise ValueError("确定性故障与中断不能同时注入")
    return LiveApplication._compose(
        assembly_factory=_DeterministicAssemblyFactory(
            (
                _deterministic_source_analyzer
                if source_analyzer is None
                else source_analyzer
            ),
            result_kind,
            emit_non_stage_events,
            subtitle_failure,
            failure_stage,
            unexpected_stage,
            invalid_result_stage,
            invalid_work_count_stage,
            postcommit_cancellation,
            postcommit_effect_failure,
        ),
        open_workspace=(
            _SignalDuringWorkspaceOpen(interruption_signal)
            if interrupt_during_workspace_open
            else Workspace.open
        ),
        open_diagnostic_workspace=Workspace.open_diagnostics,
        load_configuration=Configuration.load,
        initialize_diagnostics=_DeterministicDiagnosticsInitializer(
            diagnostics_failure
        ),
        application_version="4.7.0",
        wall_clock=lambda: datetime.now(timezone.utc),
        run_id_factory=(
            _SignalDuringRunIdFactory(interruption_signal)
            if interrupt_during_run_id_factory
            else RunId.new
        ),
        control=_DeterministicControl(
            interruption_stage,
            interruption_signal,
            interrupt_after_commit,
            deliver_signal_through_os,
            postcommit_control_failure,
        ),
        cleanup_run=_DeterministicCleanup(
            cleanup_failure,
            cleanup_failure_before_delete,
        ),
    )
