"""只供无密钥契约测试使用的确定性直播拆条组合根。"""

import hashlib
import json
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from video_auto_editor.application.live import (
    LiveApplication,
    LiveRunRequest,
    _DeliveryBuildWork,
    _RunAssembly,
    _StageWork,
)
from video_auto_editor.cache import CacheNamespace, CacheOutcome
from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.configuration import Configuration, LoadedConfiguration
from video_auto_editor.delivery.capability import (
    PublishedDelivery,
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.delivery.publication import Publication
from video_auto_editor.diagnostics import (
    ArtifactRole,
    DiagnosticsFailure,
    Facts,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    RunDiagnostics,
    StageOutcome,
)
from video_auto_editor.diagnostics._session import (
    DiagnosticScope,
    StageDiagnostics,
)
from video_auto_editor.diagnostics.collecting import _CollectingDiagnosticStore
from video_auto_editor.diagnostics.persistent import (
    initialize as initialize_persistent_diagnostics,
)
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
from video_auto_editor.runtime.identity import (
    RunId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    CompleteTranscript,
    ExecutionFacts,
    SpeechPresence,
    SpeechRecognition,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
)
from video_auto_editor.transcription.deterministic import (
    DeterministicSpeechRecognition,
    DeterministicTranscriptionScript,
)
from video_auto_editor.workspace import (
    DiagnosticRunWorkspace,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
    WorkspaceFailure,
)


@dataclass(frozen=True, slots=True)
class _DeterministicFact:
    kind: str
    payload: object | None = None


@dataclass(frozen=True, slots=True)
class ProductionFailureInjection:
    """只允许真实生产 failure 类型穿过顶层应用 seam。"""

    stage: RunStage
    failure_factory: Callable[[], Exception]

    def __post_init__(self) -> None:
        if not isinstance(self.stage, RunStage):
            raise TypeError("生产故障注入必须绑定 RunStage")
        if self.stage is RunStage.INITIALIZED:
            raise ValueError("初始化故障必须从 workspace 或诊断入口真实产生")
        if not callable(self.failure_factory):
            raise TypeError("生产故障注入必须提供 failure 工厂")

    def create_failure(self) -> Exception:
        failure = self.failure_factory()
        if not isinstance(failure, Exception):
            raise TypeError("生产故障工厂必须返回 Exception")
        if not type(failure).__module__.startswith("video_auto_editor."):
            raise TypeError("生产故障工厂不得返回测试专用异常")
        if not isinstance(getattr(failure, "error_code", None), ErrorCode):
            raise TypeError("生产故障必须携带稳定 ErrorCode")
        if not isinstance(getattr(failure, "diagnostics", None), Mapping):
            raise TypeError("生产故障必须携带安全诊断映射")
        return failure


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


def _record_transcription_execution_facts(
    scope: DiagnosticScope,
    facts: ExecutionFacts,
) -> None:
    """只把中性执行事实翻译为严格诊断事件。"""
    if not isinstance(facts, ExecutionFacts):
        raise TypeError("转写阶段只能记录 ExecutionFacts")
    cache_operation = scope.start_operation(
        OperationKind.CACHE_READ,
        item_index=1,
        item_count=1,
    )
    cache_operation.record(
        Facts.cache(
            CacheNamespace.TRANSCRIPT,
            (
                CacheOutcome.HIT
                if facts.cache_use is CacheUse.HIT
                else CacheOutcome.MISS
            ),
        )
    )
    cache_operation.complete(
        OperationOutcome.SUCCEEDED,
        attempt_count=1,
    )
    scope.record(
        Facts.transcription_execution(
            retry_count=facts.retry_count,
            recovery_count=facts.recovery_count,
        )
    )


def _effective_result_kind(
    configured: ResultKind,
    speech_presence: SpeechPresence,
) -> ResultKind:
    if not isinstance(configured, ResultKind):
        raise TypeError("确定性结果必须使用 ResultKind")
    if not isinstance(speech_presence, SpeechPresence):
        raise TypeError("确定性结果必须包含 SpeechPresence")
    if speech_presence is SpeechPresence.ABSENT:
        return ResultKind.EMPTY
    return configured


class _DuplicateManifestField(ValueError):
    pass


def _strict_manifest_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateManifestField(key)
        value[key] = item
    return value


def _valid_deterministic_manifest(
    value: object,
    *,
    run_id: RunId,
    result_kind: ResultKind,
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "result_kind",
        "run_id",
        "schema_version",
        "speech_presence",
        "transcript_chunk_ids",
        "transcript_id",
    }:
        return False
    if value.get("run_id") != str(run_id):
        return False
    if value.get("schema_version") != "deterministic.v1":
        return False
    try:
        TranscriptId(value["transcript_id"])
        chunk_ids = tuple(
            TranscriptChunkId(item)
            for item in value["transcript_chunk_ids"]
        )
        speech_presence = SpeechPresence(value["speech_presence"])
    except (TypeError, ValueError):
        return False
    if len(chunk_ids) != len(set(chunk_ids)):
        return False
    if value.get("result_kind") != result_kind.value:
        return False
    if speech_presence is SpeechPresence.PRESENT:
        return bool(chunk_ids)
    return not chunk_ids


def _default_transcription_script(
    *,
    include_transcription_retry_and_recovery: bool,
) -> DeterministicTranscriptionScript:
    scripted_internal_work_count = int(
        include_transcription_retry_and_recovery
    )
    return DeterministicTranscriptionScript.succeed(
        TranscriptionResult(
            chunks=(
                TranscriptionChunk(
                    start_ms=0,
                    end_ms=900,
                    text="确定性忠实转写文本",
                ),
            ),
            speech_presence=SpeechPresence.PRESENT,
            execution_facts=ExecutionFacts(
                cache_use=CacheUse.MISS,
                retry_count=scripted_internal_work_count,
                recovery_count=scripted_internal_work_count,
            ),
        )
    )


class _DeterministicRunAssembly:
    __slots__ = (
        "_built_manifest_bytes",
        "_built_result_kind",
        "_course_context_sha256",
        "_failure_injection",
        "_failure_stage",
        "_interrupt_after_preflight_return",
        "_interruption_signal",
        "_invalid_result_stage",
        "_invalid_work_count_stage",
        "_overwrite",
        "_postcommit_cancellation",
        "_postcommit_effect_failure",
        "_result_kind",
        "_run_workspace",
        "_source_analyzer",
        "_speech_recognition",
        "_subtitle_failure",
        "_terminal_diagnostics_failure",
        "_terminal_diagnostics_observer",
        "_unexpected_stage",
    )

    def __init__(
        self,
        run_workspace: RunWorkspace,
        source_analyzer: _SourceAnalyzer,
        speech_recognition: SpeechRecognition,
        configuration: LoadedConfiguration,
        result_kind: ResultKind,
        subtitle_failure: bool,
        failure_stage: RunStage | None,
        failure_injection: ProductionFailureInjection | None,
        unexpected_stage: RunStage | None,
        invalid_result_stage: RunStage | None,
        invalid_work_count_stage: RunStage | None,
        postcommit_cancellation: bool,
        postcommit_effect_failure: bool,
        terminal_diagnostics_failure: bool,
        terminal_diagnostics_observer: Callable[
            [StageDiagnostics, StageOutcome],
            None,
        ]
        | None,
        interrupt_after_preflight_return: bool,
        interruption_signal: InterruptionSignal,
        overwrite: bool,
    ) -> None:
        self._run_workspace = run_workspace
        self._source_analyzer = source_analyzer
        self._speech_recognition = speech_recognition
        self._built_manifest_bytes: bytes | None = None
        self._built_result_kind: ResultKind | None = None
        self._course_context_sha256 = (
            None
            if configuration.course_context is None
            else configuration.course_context.sha256
        )
        self._result_kind = result_kind
        self._subtitle_failure = subtitle_failure
        self._failure_stage = failure_stage
        self._failure_injection = failure_injection
        self._unexpected_stage = unexpected_stage
        self._invalid_result_stage = invalid_result_stage
        self._invalid_work_count_stage = invalid_work_count_stage
        self._postcommit_cancellation = postcommit_cancellation
        self._postcommit_effect_failure = postcommit_effect_failure
        self._terminal_diagnostics_failure = (
            terminal_diagnostics_failure
        )
        self._terminal_diagnostics_observer = (
            terminal_diagnostics_observer
        )
        self._interrupt_after_preflight_return = (
            interrupt_after_preflight_return
        )
        self._interruption_signal = interruption_signal
        self._overwrite = overwrite

    def finalize_terminal_diagnostics(
        self,
        stage: StageDiagnostics,
        outcome: StageOutcome,
    ) -> None:
        if not isinstance(stage, StageDiagnostics):
            raise TypeError("终态诊断收尾必须绑定活动阶段")
        if outcome not in {
            StageOutcome.FAILED,
            StageOutcome.INTERRUPTED,
        }:
            raise ValueError("终态诊断收尾只接受失败或中断")
        observer = self._terminal_diagnostics_observer
        if observer is not None:
            observer(stage, outcome)
        if self._terminal_diagnostics_failure:
            raise DiagnosticsFailure(
                ErrorCode.DIAGNOSTICS_WRITE_FAILED,
                {
                    "operation": "diagnostics.append",
                    "reason_code": "diagnostics.append_failed",
                },
            )

    def _before_work(self, stage: RunStage) -> None:
        injection = self._failure_injection
        if injection is not None and stage is injection.stage:
            raise injection.create_failure()
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
        Publication.check_destination(
            self._run_workspace.published_delivery,
            overwrite=self._overwrite,
            cancellation=cancellation,
        )
        self._before_work(RunStage.PREFLIGHT)
        readiness = self._speech_recognition.check_readiness()
        if not readiness.ready:
            raise _DeterministicFailure(
                ErrorCode.INTERNAL_UNEXPECTED,
                ErrorModule.TRANSCRIPTION,
            )
        work = self._work(
            RunStage.PREFLIGHT,
            _DeterministicFact("readiness"),
            1,
        )
        if self._interrupt_after_preflight_return:
            os.kill(
                os.getpid(),
                (
                    signal.SIGINT
                    if self._interruption_signal
                    is InterruptionSignal.SIGINT
                    else signal.SIGTERM
                ),
            )
        return work

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
    ) -> TranscriptionResult:
        if not isinstance(source, SourceDescription):
            raise TypeError("确定性语音识别需要素材事实")
        scope = stage.scope(ErrorModule.TRANSCRIPTION)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.TRANSCRIPTION)
        request = TranscriptionRequest(
            source=source,
            temporary_workspace=self._run_workspace.temporary,
            cancellation=cancellation,
        )
        try:
            result = self._speech_recognition.transcribe(request)
        except TranscriptionFailure as failure:
            _record_transcription_execution_facts(
                scope,
                failure.execution_facts,
            )
            raise
        if not isinstance(result, TranscriptionResult):
            raise TypeError(
                "语音识别 Adapter 必须返回 TranscriptionResult"
            )
        _record_transcription_execution_facts(
            scope,
            result.execution_facts,
        )
        return result

    def plan_candidates(
        self,
        transcript: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if not isinstance(transcript, CompleteTranscript):
            raise TypeError("确定性候选规划需要完整转写文本")
        stage.scope(ErrorModule.CLIP_PLANNING)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.CANDIDATE_PLANNING)
        return self._work(
            RunStage.CANDIDATE_PLANNING,
            _DeterministicFact("candidate_plan", transcript),
            (
                0
                if transcript.speech_presence is SpeechPresence.ABSENT
                else 1
            ),
        )

    def review_topics(
        self,
        plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _StageWork:
        if (
            not isinstance(plan, _DeterministicFact)
            or plan.kind != "candidate_plan"
            or not isinstance(plan.payload, CompleteTranscript)
        ):
            raise TypeError("确定性主题评审需要候选规划事实")
        stage.scope(ErrorModule.TOPIC_REVIEW)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.TOPIC_REVIEW)
        return self._work(
            RunStage.TOPIC_REVIEW,
            _DeterministicFact("reviewed_plan", plan.payload),
            (
                0
                if plan.payload.speech_presence is SpeechPresence.ABSENT
                else 1
            ),
        )

    def build_delivery(
        self,
        reviewed_plan: object,
        stage: StageDiagnostics,
        cancellation: CancellationToken,
    ) -> _DeliveryBuildWork:
        if (
            not isinstance(reviewed_plan, _DeterministicFact)
            or reviewed_plan.kind != "reviewed_plan"
            or not isinstance(
                reviewed_plan.payload,
                CompleteTranscript,
            )
        ):
            raise TypeError("确定性交付构建需要完整评审事实")
        transcript = reviewed_plan.payload
        result_kind = _effective_result_kind(
            self._result_kind,
            transcript.speech_presence,
        )
        delivery_scope = stage.scope(ErrorModule.DELIVERY_BUILD)
        cancellation.raise_if_cancelled()
        self._before_work(RunStage.DELIVERY_BUILD)
        if self._invalid_result_stage is RunStage.DELIVERY_BUILD:
            return self._work(
                RunStage.DELIVERY_BUILD,
                _DeterministicFact("invalid_delivery"),
                1,
            )  # type: ignore[return-value]
        if result_kind is ResultKind.CLIPS:
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
        deterministic_manifest = {
            "result_kind": result_kind.value,
            "run_id": str(self._run_workspace.run_id),
            "schema_version": "deterministic.v1",
            "speech_presence": transcript.speech_presence.value,
            "transcript_chunk_ids": [
                str(chunk.transcript_chunk_id)
                for chunk in transcript.chunks
            ],
            "transcript_id": str(transcript.transcript_id),
        }
        manifest_bytes = (
            json.dumps(
                deterministic_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._run_workspace.delivery_staging.location(
            "manifest.json"
        ).publish_bytes_atomically(manifest_bytes)
        self._built_manifest_bytes = manifest_bytes
        self._built_result_kind = result_kind
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
                result_kind,
                "invalid",  # type: ignore[arg-type]
            )
        return _DeliveryBuildWork(
            delivery,
            result_kind,
            1 if result_kind is ResultKind.CLIPS else 0,
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
        try:
            manifest_bytes = delivery.managed_directory.location(
                "manifest.json"
            ).read_bytes()
            manifest = json.loads(
                manifest_bytes,
                object_pairs_hook=_strict_manifest_object,
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            _DuplicateManifestField,
            RecursionError,
            WorkspaceFailure,
        ):
            raise _DeterministicFailure(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorModule.DELIVERY_VERIFICATION,
            ) from None
        built_manifest_bytes = self._built_manifest_bytes
        built_result_kind = self._built_result_kind
        if (
            not isinstance(built_manifest_bytes, bytes)
            or manifest_bytes != built_manifest_bytes
            or not isinstance(built_result_kind, ResultKind)
        ):
            raise _DeterministicFailure(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorModule.DELIVERY_VERIFICATION,
            )
        if not _valid_deterministic_manifest(
            manifest,
            run_id=self._run_workspace.run_id,
            result_kind=built_result_kind,
        ):
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
        try:
            tree = delivery.managed_directory.inspect_tree()
            snapshot = hashlib.sha256(b"delivery_snapshot.v1\0")
            for entry in tree:
                if entry.byte_length is None:
                    continue
                contents = delivery.managed_directory.location(
                    entry.relative_path
                ).read_bytes()
                path_bytes = entry.relative_path.encode("utf-8")
                snapshot.update(len(path_bytes).to_bytes(8, "big"))
                snapshot.update(path_bytes)
                snapshot.update(len(contents).to_bytes(8, "big"))
                snapshot.update(hashlib.sha256(contents).digest())
            final_tree = delivery.managed_directory.inspect_tree()
        except (OSError, WorkspaceFailure):
            raise _DeterministicFailure(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorModule.DELIVERY_VERIFICATION,
            ) from None
        if final_tree != tree:
            raise _DeterministicFailure(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorModule.DELIVERY_VERIFICATION,
            )
        verified = VerifiedDelivery._from_verification(
            delivery,
            verification_snapshot="sha256:" + snapshot.hexdigest(),
            verification_tree=tree,
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
            [PublishedDelivery, Callable[[], None]],
            None,
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
        if self._invalid_work_count_stage is RunStage.PUBLISHING:
            return self._work(RunStage.PUBLISHING, delivery, 1)
        published = Publication.publish(
            delivery,
            published_directory=self._run_workspace.published_delivery,
            previous_directory=self._run_workspace.previous_delivery,
            overwrite=self._overwrite,
            cancellation=cancellation,
            commit=commit,
        )
        work = self._work(RunStage.PUBLISHING, published, 1)
        if self._postcommit_cancellation:
            raise CancellationRequested(signal.SIGTERM)
        if self._postcommit_effect_failure:
            raise RuntimeError("deterministic postcommit effect failure")
        return work


class _DeterministicAssemblyFactory:
    __slots__ = (
        "_failure_injection",
        "_failure_stage",
        "_interrupt_after_preflight_return",
        "_interruption_signal",
        "_invalid_result_stage",
        "_invalid_work_count_stage",
        "_postcommit_cancellation",
        "_postcommit_effect_failure",
        "_result_kind",
        "_source_analyzer",
        "_subtitle_failure",
        "_terminal_diagnostics_failure",
        "_terminal_diagnostics_observer",
        "_transcription_script",
        "_unexpected_stage",
    )

    def __init__(
        self,
        source_analyzer: _SourceAnalyzer,
        transcription_script: DeterministicTranscriptionScript,
        result_kind: ResultKind,
        subtitle_failure: bool,
        failure_stage: RunStage | None,
        failure_injection: ProductionFailureInjection | None,
        unexpected_stage: RunStage | None,
        invalid_result_stage: RunStage | None,
        invalid_work_count_stage: RunStage | None,
        postcommit_cancellation: bool,
        postcommit_effect_failure: bool,
        terminal_diagnostics_failure: bool,
        terminal_diagnostics_observer: Callable[
            [StageDiagnostics, StageOutcome],
            None,
        ]
        | None,
        interrupt_after_preflight_return: bool,
        interruption_signal: InterruptionSignal,
    ) -> None:
        self._source_analyzer = source_analyzer
        self._transcription_script = transcription_script
        self._result_kind = result_kind
        self._subtitle_failure = subtitle_failure
        self._failure_stage = failure_stage
        self._failure_injection = failure_injection
        self._unexpected_stage = unexpected_stage
        self._invalid_result_stage = invalid_result_stage
        self._invalid_work_count_stage = invalid_work_count_stage
        self._postcommit_cancellation = postcommit_cancellation
        self._postcommit_effect_failure = postcommit_effect_failure
        self._terminal_diagnostics_failure = (
            terminal_diagnostics_failure
        )
        self._terminal_diagnostics_observer = (
            terminal_diagnostics_observer
        )
        self._interrupt_after_preflight_return = (
            interrupt_after_preflight_return
        )
        self._interruption_signal = interruption_signal

    def create(
        self,
        *,
        request: LiveRunRequest,
        configuration: LoadedConfiguration,
        run_workspace: RunWorkspace,
    ) -> _RunAssembly:
        return _DeterministicRunAssembly(
            run_workspace,
            self._source_analyzer,
            DeterministicSpeechRecognition(
                self._transcription_script
            ),
            configuration,
            self._result_kind,
            self._subtitle_failure,
            self._failure_stage,
            self._failure_injection,
            self._unexpected_stage,
            self._invalid_result_stage,
            self._invalid_work_count_stage,
            self._postcommit_cancellation,
            self._postcommit_effect_failure,
            self._terminal_diagnostics_failure,
            self._terminal_diagnostics_observer,
            self._interrupt_after_preflight_return,
            self._interruption_signal,
            request.overwrite,
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
        self._memory = _CollectingDiagnosticStore(run_id)

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
        if self._failure == "startup":
            raise DiagnosticsFailure(
                ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
                {
                    "component": "run_diagnostics",
                    "operation": "diagnostics.initialize",
                    "reason_code": "diagnostics.open_failed",
                },
            )
        if self._failure is None:
            return initialize_persistent_diagnostics(
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
    transcription_script: DeterministicTranscriptionScript | None = None,
    result_kind: ResultKind = ResultKind.CLIPS,
    failure_stage: RunStage | None = None,
    failure_injection: ProductionFailureInjection | None = None,
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
    terminal_diagnostics_failure: bool = False,
    terminal_diagnostics_observer: Callable[
        [StageDiagnostics, StageOutcome],
        None,
    ]
    | None = None,
    interrupt_after_preflight_return: bool = False,
    interrupt_during_workspace_open: bool = False,
    interrupt_during_run_id_factory: bool = False,
) -> LiveApplication:
    """装配只使用本地确定性模块的应用实例。"""
    if source_analyzer is not None and not callable(source_analyzer):
        raise TypeError("确定性素材分析器必须可调用")
    if (
        transcription_script is not None
        and not isinstance(
            transcription_script,
            DeterministicTranscriptionScript,
        )
    ):
        raise TypeError("确定性语音识别必须使用预编排脚本")
    if not isinstance(result_kind, ResultKind):
        raise TypeError("确定性结果必须使用 ResultKind")
    if failure_stage is not None and failure_stage not in _FAILURES:
        raise ValueError("确定性故障只能注入非初始化业务阶段")
    if failure_injection is not None and not isinstance(
        failure_injection,
        ProductionFailureInjection,
    ):
        raise TypeError("生产故障注入必须使用 ProductionFailureInjection")
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
        "startup",
        "precommit",
        "postcommit",
        "finalization",
    }:
        raise ValueError(
            "诊断故障只能发生在初始化、提交点前、提交点后或终态收尾"
        )
    if not isinstance(subtitle_failure, bool):
        raise TypeError("字幕优化故障选项必须是布尔值")
    resolved_transcription_script = (
        _default_transcription_script(
            include_transcription_retry_and_recovery=(
                emit_non_stage_events
            )
        )
        if transcription_script is None
        else transcription_script
    )
    scripted_result = resolved_transcription_script.result
    if subtitle_failure and (
        result_kind is ResultKind.EMPTY
        or (
            scripted_result is not None
            and _effective_result_kind(
                result_kind,
                scripted_result.speech_presence,
            )
            is ResultKind.EMPTY
        )
    ):
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
    if invalid_work_count_stage is RunStage.TRANSCRIPTION:
        raise ValueError("转写工作项数量由应用从完整结果派生，不能由组合根注入")
    if not isinstance(postcommit_control_failure, bool):
        raise TypeError("提交点后控制故障选项必须是布尔值")
    if not isinstance(postcommit_cancellation, bool):
        raise TypeError("提交点后取消异常选项必须是布尔值")
    if not isinstance(postcommit_effect_failure, bool):
        raise TypeError("提交点后模块故障选项必须是布尔值")
    if not isinstance(terminal_diagnostics_failure, bool):
        raise TypeError("终态诊断故障选项必须是布尔值")
    if terminal_diagnostics_observer is not None and not callable(
        terminal_diagnostics_observer
    ):
        raise TypeError("终态诊断观察器必须可调用")
    if not isinstance(interrupt_after_preflight_return, bool):
        raise TypeError("预检返回后中断选项必须是布尔值")
    if not isinstance(interrupt_during_workspace_open, bool):
        raise TypeError("workspace 打开中断选项必须是布尔值")
    if not isinstance(interrupt_during_run_id_factory, bool):
        raise TypeError("运行标识生成中断选项必须是布尔值")
    selected_injections = sum(
        value is not None
        for value in (
            failure_stage,
            failure_injection,
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
            int(interrupt_after_preflight_return),
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
            resolved_transcription_script,
            result_kind,
            subtitle_failure,
            failure_stage,
            failure_injection,
            unexpected_stage,
            invalid_result_stage,
            invalid_work_count_stage,
            postcommit_cancellation,
            postcommit_effect_failure,
            terminal_diagnostics_failure,
            terminal_diagnostics_observer,
            interrupt_after_preflight_return,
            interruption_signal,
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
