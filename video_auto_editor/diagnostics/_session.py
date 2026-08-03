"""运行诊断深模块的公开会话实现。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Final

from video_auto_editor.configuration import (
    ConfigurationDiagnosticProjection,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    RunError,
    RunStage,
)
from video_auto_editor.runtime.identity import OperationId, RunId
from video_auto_editor.workspace import WorkspaceFailure

from ._facts import (
    DiagnosticFact,
    _ArtifactFact,
    _CacheFact,
    _DeliveryStateFact,
    _EnvironmentFact,
    _ExternalRequestFact,
    _ExternalServiceFact,
    _ExternalServiceZeroRequestsFact,
    _FactKind,
    _RecoveredNoticeFact,
    _SourceFact,
    _TranscriptionExecutionFact,
)
from ._failure import (
    DiagnosticsFailure,
    _runtime_failure,
    _startup_failure,
)
from ._model import (
    ArtifactRole,
    CacheNamespace,
    CacheOutcome,
    DeliveryBuildState,
    DeliveryVerificationState,
    DiagnosticCompletion,
    DiagnosticFinalization,
    DiagnosticPackageSnapshot,
    ExternalRequestOutcome,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    ProviderCapability,
    ProviderTransport,
    PublicationState,
    RecoveredNoticeKind,
    ResultKind,
    RetryKind,
    StageOutcome,
    _DiagnosticTerminalState,
)
from ._store import _DiagnosticStore

_EVENT_SCHEMA_VERSION: Final = "run_event.v1"
_APPLICATION_VERSION = re.compile(
    r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_STABLE_REASON_CODE = re.compile(
    r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,7}"
)


class RunDiagnostics:
    """集中拥有一次直播拆条运行的事件顺序与诊断包。"""

    __slots__ = (
        "_active_stage",
        "_application_version",
        "_cache_stats",
        "_configuration",
        "_delivery",
        "_environment",
        "_errors",
        "_external_services",
        "_finished",
        "_interruption",
        "_lock",
        "_monotonic_clock",
        "_notices",
        "_operations",
        "_postcommit_incomplete",
        "_postcommit_proof",
        "_retry_counts",
        "_transcription_execution",
        "_transcription_retry_counts",
        "_transcription_transcript_cache_hit",
        "_run_started_at",
        "_run_id",
        "_sequence",
        "_started_timestamp",
        "_stages",
        "_store",
        "_source",
        "_wall_clock",
    )

    def __init__(self) -> None:
        raise TypeError("RunDiagnostics 只能通过诊断 Adapter 工厂创建")

    @classmethod
    def _start(
        cls,
        store: _DiagnosticStore,
        *,
        application_version: str,
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
    ) -> "RunDiagnostics":
        run_id = store.run_id
        if not isinstance(application_version, str):
            raise TypeError("应用版本必须是字符串")
        if _APPLICATION_VERSION.fullmatch(application_version) is None:
            raise ValueError("应用版本格式不合法")
        if not callable(wall_clock) or not callable(monotonic_clock):
            raise TypeError("诊断时钟必须可调用")

        timestamp = _timestamp(wall_clock())
        monotonic_value = _monotonic_value(monotonic_clock())

        event = {
            "schema_version": _EVENT_SCHEMA_VERSION,
            "timestamp": timestamp,
            "sequence": 1,
            "run_id": str(run_id),
            "level": "info",
            "event_code": "run.initialized",
            "stage": RunStage.INITIALIZED.value,
            "module": ErrorModule.APPLICATION.value,
            "message": "直播拆条运行已初始化。",
            "attributes": {
                "application_version": application_version,
                "release": {"status": "unknown"},
            },
        }
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        append_failure_reason: str | None = None
        try:
            store.append(encoded)
        except (OSError, WorkspaceFailure) as failure:
            append_failure_reason = _diagnostics_failure_reason(
                failure,
                default="diagnostics.append_failed",
                startup=True,
            )
        if append_failure_reason is not None:
            raise _startup_failure(
                operation="diagnostics.append",
                reason_code=append_failure_reason,
            )

        instance = object.__new__(cls)
        instance._store = store
        instance._run_id = run_id
        instance._application_version = application_version
        instance._cache_stats = {}
        instance._configuration = None
        instance._delivery = {
            "build_state": "not_started",
            "verification_state": "not_started",
            "publication_state": "not_started",
            "created_artifacts": {},
            "verified_artifacts": {},
            "published_delivery": None,
        }
        instance._environment = None
        instance._wall_clock = wall_clock
        instance._monotonic_clock = monotonic_clock
        instance._notices = {}
        instance._run_started_at = monotonic_value
        instance._started_timestamp = timestamp
        instance._sequence = 1
        instance._lock = RLock()
        instance._active_stage = None
        instance._operations = {}
        instance._postcommit_incomplete = False
        instance._postcommit_proof = None
        instance._retry_counts = {kind: 0 for kind in RetryKind}
        instance._transcription_execution = None
        instance._transcription_retry_counts = {
            kind: 0 for kind in RetryKind
        }
        instance._transcription_transcript_cache_hit = False
        instance._errors = {}
        instance._external_services = {}
        instance._finished = False
        instance._interruption = None
        instance._stages = {}
        instance._source = None
        return instance

    @property
    def run_id(self) -> RunId:
        """返回由 Workspace capability 绑定的既有运行标识。"""
        return self._run_id

    def snapshot(self) -> DiagnosticPackageSnapshot:
        """返回接收 Adapter 当前已接受的诊断包快照。"""
        with self._lock:
            snapshot_failed = False
            try:
                snapshot = self._store.snapshot()
            except (OSError, WorkspaceFailure):
                snapshot_failed = True
            if snapshot_failed:
                self._finished = True
                raise _runtime_failure(
                    operation="diagnostics.sync",
                    reason_code="diagnostics.file_sync_failed",
                )
            return snapshot

    def finish(self, outcome: DiagnosticCompletion) -> DiagnosticFinalization:
        """形成唯一运行终态并发布 ``run.json``。"""
        if not isinstance(outcome, DiagnosticCompletion):
            raise TypeError("诊断收尾必须使用 DiagnosticCompletion")
        with self._lock:
            if self._finished:
                raise RuntimeError("直播拆条运行诊断已经完成")
            if self._active_stage is not None:
                raise RuntimeError("活动阶段必须先完成")
            self._validate_outcome(outcome)
            if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
                if outcome.published_delivery is None:
                    raise TypeError("成功终态必须包含发布提交证明")
                # PublishedDelivery 是 authority-bearing capability；
                # 必须匹配同一次提交签发的确切实例，不能退化为值相等。
                if (
                    self._postcommit_proof is not None
                    and self._postcommit_proof
                    is not outcome.published_delivery
                ):
                    raise ValueError("成功终态与发布提交事实不一致")
                self._postcommit_proof = outcome.published_delivery
                if self._postcommit_incomplete:
                    self._finished = True
                    return DiagnosticFinalization._incomplete(None)
            # 终态发布即使中途失败也不可重试，否则可能生成第二个
            # run.completed 或让调用方在已损坏日志上继续追加。
            self._finished = True
            completed_at = _monotonic_value(self._monotonic_clock())
            ended_timestamp = _timestamp(self._wall_clock())
            duration_ms = _duration_ms(
                self._run_started_at,
                completed_at,
            )
            stage = self._terminal_stage(outcome)
            exit_code = _outcome_exit_code(outcome)
            result_kind = _result_kind_status(outcome)
            terminal_sequence = None
            try:
                terminal_sequence = self._append_event(
                    level="info",
                    event_code="run.completed",
                    stage=stage,
                    module=ErrorModule.APPLICATION,
                    message="直播拆条运行已结束。",
                    attributes={
                        "duration_ms": duration_ms,
                        "exit_code": exit_code,
                        "outcome": outcome.state.value,
                        "result_kind": result_kind,
                    },
                    timestamp=ended_timestamp,
                )
                if terminal_sequence is None:
                    return DiagnosticFinalization._incomplete(None)
                event_snapshot = self._snapshot_for_finalization()
                manifest = self._build_manifest(
                    outcome=outcome,
                    ended_timestamp=ended_timestamp,
                    duration_ms=duration_ms,
                    events=event_snapshot,
                )
                try:
                    encoded_manifest = (
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                except (TypeError, ValueError):
                    raise _runtime_failure(
                        operation="diagnostics.publish_manifest",
                        reason_code="diagnostics.serialization_failed",
                    ) from None
                self._publish_manifest(encoded_manifest)
            except DiagnosticsFailure:
                if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
                    return DiagnosticFinalization._incomplete(
                        terminal_sequence
                    )
                raise
            if terminal_sequence is None:
                raise RuntimeError("终态事件序号未形成")
            return DiagnosticFinalization._persisted(
                terminal_sequence
            )

    def _snapshot_for_finalization(self) -> bytes:
        snapshot_failed = False
        try:
            snapshot = self._store.snapshot()
        except (OSError, WorkspaceFailure):
            snapshot_failed = True
        if snapshot_failed:
            raise _runtime_failure(
                operation="diagnostics.sync",
                reason_code="diagnostics.file_sync_failed",
            )
        return snapshot.events

    def _publish_manifest(self, payload: bytes) -> None:
        publish_failure_reason: str | None = None
        try:
            self._store.publish_manifest(payload)
        except (OSError, WorkspaceFailure) as failure:
            publish_failure_reason = _diagnostics_failure_reason(
                failure,
                default="diagnostics.atomic_replace_failed",
                startup=False,
            )
        if publish_failure_reason is not None:
            raise _runtime_failure(
                operation="diagnostics.publish_manifest",
                reason_code=publish_failure_reason,
            )

    def enter_stage(
        self,
        stage: RunStage,
        module: ErrorModule,
    ) -> "DiagnosticScope":
        """进入单模块阶段；复杂阶段应使用 ``start_stage``。"""
        if not isinstance(module, ErrorModule):
            raise TypeError("诊断模块必须使用 ErrorModule")
        return self._start_stage(stage, owner_module=module).scope(module)

    def start_stage(self, stage: RunStage) -> "StageDiagnostics":
        """进入一个阶段并允许向实际参与的业务模块签发最小 scope。"""
        return self._start_stage(
            stage,
            owner_module=ErrorModule.APPLICATION,
        )

    def _start_stage(
        self,
        stage: RunStage,
        *,
        owner_module: ErrorModule,
    ) -> "StageDiagnostics":
        if not isinstance(stage, RunStage):
            raise TypeError("诊断阶段必须使用 RunStage")
        if not isinstance(owner_module, ErrorModule):
            raise TypeError("诊断模块必须使用 ErrorModule")
        with self._lock:
            self._assert_open()
            if self._postcommit_proof is not None:
                raise RuntimeError("发布提交后不能进入新的运行阶段")
            if self._active_stage is not None:
                raise RuntimeError("前一个阶段尚未完成")
            if stage in self._stages:
                raise RuntimeError("同一运行阶段不能重复进入")
            started_at = _monotonic_value(self._monotonic_clock())
            self._append_event(
                level="info",
                event_code="stage.started",
                stage=stage,
                module=owner_module,
                message="直播拆条运行阶段已开始。",
                attributes={},
            )
            stage_diagnostics = StageDiagnostics._create(
                owner=self,
                stage=stage,
                owner_module=owner_module,
                started_at=started_at,
            )
            self._active_stage = stage_diagnostics
            self._stages[stage] = stage_diagnostics
            return stage_diagnostics

    def _complete_stage(
        self,
        stage_diagnostics: "StageDiagnostics",
        outcome: StageOutcome,
        work_item_count: int,
    ) -> int | None:
        if not isinstance(outcome, StageOutcome):
            raise TypeError("阶段结果必须使用 StageOutcome")
        if not isinstance(work_item_count, int) or isinstance(
            work_item_count,
            bool,
        ):
            raise TypeError("阶段工作项数量必须是整数")
        if work_item_count < 0:
            raise ValueError("阶段工作项数量不能为负数")
        with self._lock:
            self._assert_open()
            if stage_diagnostics._completed:
                raise RuntimeError("阶段已经完成")
            if self._active_stage is not stage_diagnostics:
                raise TypeError("阶段诊断不属于当前活动阶段")
            if any(
                not operation._completed
                and operation._scope._stage_diagnostics is stage_diagnostics
                for operation in self._operations.values()
            ):
                raise RuntimeError("阶段仍有未完成的诊断操作")
            completed_at = _monotonic_value(self._monotonic_clock())
            duration_ms = _duration_ms(
                stage_diagnostics._started_at,
                completed_at,
            )
            sequence = self._append_event(
                level="info",
                event_code="stage.completed",
                stage=stage_diagnostics._stage,
                module=stage_diagnostics._owner_module,
                message="直播拆条运行阶段已完成。",
                attributes={
                    "duration_ms": duration_ms,
                    "outcome": outcome.value,
                    "work_item_count": work_item_count,
                },
            )
            stage_diagnostics._completed = True
            stage_diagnostics._outcome = outcome
            stage_diagnostics._duration_ms = duration_ms
            stage_diagnostics._work_item_count = work_item_count
            self._active_stage = None
            return sequence

    def _record_failure(
        self,
        scope: "DiagnosticScope",
        failure: object,
        *,
        operation: "DiagnosticOperation | None" = None,
    ) -> RunError:
        try:
            error_code = failure.error_code
            diagnostics = failure.diagnostics
        except AttributeError as exc:
            raise TypeError(
                "可记录失败必须提供类型化错误码与脱敏诊断"
            ) from exc
        if not isinstance(error_code, ErrorCode):
            raise TypeError("可记录失败必须使用 ErrorCode")
        if not isinstance(diagnostics, Mapping):
            raise TypeError("可记录失败必须提供脱敏诊断映射")
        with self._lock:
            self._assert_open()
            if self._postcommit_proof is not None:
                raise RuntimeError("发布提交后不能记录终态错误")
            self._assert_scope_active(scope)
            if operation is not None:
                self._assert_operation_active(operation)
                operation_id = operation._operation_id
            else:
                operation_id = None
            event_sequence = self._sequence + 1
            run_error = RunError.create(
                code=error_code,
                stage=scope._stage,
                module=scope._module,
                event_sequence=event_sequence,
                operation_id=operation_id,
                diagnostics=diagnostics,
            )
            actual_sequence = self._append_event(
                level="error",
                event_code="error.recorded",
                stage=scope._stage,
                module=scope._module,
                message=run_error.safe_message,
                attributes={
                    "category": run_error.category.value,
                    "diagnostics": _json_value(run_error.diagnostics),
                    "error_code": run_error.error_code.value,
                    "error_id": str(run_error.error_id),
                    "operator_action": run_error.operator_action.value,
                    "retryable_in_new_run": (
                        run_error.retryable_in_new_run
                    ),
                },
                operation_id=operation_id,
            )
            if actual_sequence != event_sequence:
                raise RuntimeError("错误事件序号分配不一致")
            self._errors[run_error.error_id] = run_error
            return run_error

    def _record_fact(
        self,
        scope: "DiagnosticScope",
        fact: DiagnosticFact,
        *,
        operation: "DiagnosticOperation | None" = None,
    ) -> int | None:
        if not isinstance(fact, DiagnosticFact):
            raise TypeError("业务模块只能记录 Facts 创建的诊断事实")
        fact._assert_authentic()
        with self._lock:
            self._assert_open()
            if (
                self._postcommit_proof is not None
                and fact._kind is not _FactKind.INTERRUPTION
            ):
                raise RuntimeError("发布提交后不能记录新的诊断事实")
            self._assert_scope_active(scope)
            if fact._kind is _FactKind.CONFIGURATION:
                if operation is not None:
                    raise ValueError("配置事实不能绑定诊断操作")
                if scope._module is not ErrorModule.CONFIGURATION:
                    raise ValueError("配置事实只能由 Configuration 记录")
                if self._configuration is not None:
                    raise RuntimeError("配置诊断事实已经记录")
                projection = fact._payload
                if not isinstance(
                    projection,
                    ConfigurationDiagnosticProjection,
                ):
                    raise TypeError("配置诊断事实类型不合法")
                serialized = _configuration_manifest(projection)
                sequence = self._append_event(
                    level="info",
                    event_code="configuration.observed",
                    stage=scope._stage,
                    module=scope._module,
                    message="生效配置的脱敏投影已记录。",
                    attributes=serialized,
                )
                self._configuration = projection
                return sequence
            if fact._kind is _FactKind.CACHE:
                if operation is None:
                    raise ValueError("缓存事实必须绑定缓存诊断操作")
                self._assert_operation_active(operation)
                if operation._kind not in {
                    OperationKind.CACHE_READ,
                    OperationKind.CACHE_WRITE,
                    OperationKind.CACHE_QUARANTINE,
                }:
                    raise ValueError("缓存事实只能绑定缓存诊断操作")
                cache_fact = fact._payload
                if not isinstance(cache_fact, _CacheFact):
                    raise TypeError("缓存诊断事实类型不合法")
                if (
                    scope._stage is RunStage.TRANSCRIPTION
                    and cache_fact.namespace is CacheNamespace.TRANSCRIPT
                    and cache_fact.outcome is CacheOutcome.HIT
                    and any(self._transcription_retry_counts.values())
                ):
                    raise ValueError("整场转写缓存命中时不得发生内部工作")
                observed_at = _monotonic_value(self._monotonic_clock())
                attributes: dict[str, object] = {
                    "duration_ms": _duration_ms(
                        operation._started_at,
                        observed_at,
                    ),
                    "namespace": cache_fact.namespace.value,
                    "outcome": cache_fact.outcome.value,
                }
                if cache_fact.singleflight_wait_ms is not None:
                    attributes["singleflight_wait_ms"] = (
                        cache_fact.singleflight_wait_ms
                    )
                if cache_fact.reason_code is not None:
                    attributes["reason_code"] = cache_fact.reason_code
                if cache_fact.quarantine_digest_prefix is not None:
                    attributes["quarantine_digest_prefix"] = (
                        cache_fact.quarantine_digest_prefix
                    )
                if cache_fact.error_code is not None:
                    attributes["error_code"] = cache_fact.error_code.value
                level = (
                    "error"
                    if cache_fact.outcome
                    is CacheOutcome.INFRASTRUCTURE_FAILED
                    else (
                        "warning"
                        if cache_fact.outcome
                        is CacheOutcome.CORRUPT_QUARANTINED
                        else "info"
                    )
                )
                sequence = self._append_event(
                    level=level,
                    event_code="cache.observed",
                    stage=scope._stage,
                    module=scope._module,
                    message="处理缓存操作事实已记录。",
                    attributes=attributes,
                    operation_id=operation._operation_id,
                    parent_operation_id=operation._parent_operation_id,
                )
                _update_cache_stats(self._cache_stats, cache_fact)
                if (
                    scope._stage is RunStage.TRANSCRIPTION
                    and cache_fact.namespace is CacheNamespace.TRANSCRIPT
                    and cache_fact.outcome is CacheOutcome.HIT
                ):
                    self._transcription_transcript_cache_hit = True
                return sequence
            if fact._kind is _FactKind.TRANSCRIPTION_EXECUTION:
                if operation is not None:
                    raise ValueError("转写聚合执行事实不能绑定诊断操作")
                if (
                    scope._stage is not RunStage.TRANSCRIPTION
                    or scope._module is not ErrorModule.TRANSCRIPTION
                ):
                    raise ValueError("转写聚合执行事实只能由语音识别阶段记录")
                if self._transcription_execution is not None:
                    raise RuntimeError("转写聚合执行事实已经记录")
                if any(
                    not candidate._completed
                    and candidate._scope._stage is scope._stage
                    for candidate in self._operations.values()
                ):
                    raise RuntimeError("转写聚合执行事实必须在内部操作完成后记录")
                execution_fact = fact._payload
                if not isinstance(
                    execution_fact,
                    _TranscriptionExecutionFact,
                ):
                    raise TypeError("转写聚合执行事实类型不合法")
                if (
                    self._transcription_transcript_cache_hit
                    and (
                        execution_fact.retry_count > 0
                        or execution_fact.recovery_count > 0
                        or any(self._transcription_retry_counts.values())
                    )
                ):
                    raise ValueError("整场转写缓存命中时不得发生内部工作")
                reported_counts = {
                    RetryKind.TRANSPORT_RETRY: execution_fact.retry_count,
                    RetryKind.COVERAGE_RECOVERY: (
                        execution_fact.recovery_count
                    ),
                }
                retry_deltas: dict[RetryKind, int] = {}
                for kind, reported_count in reported_counts.items():
                    observed_count = self._transcription_retry_counts[kind]
                    if reported_count < observed_count:
                        raise ValueError("转写聚合执行次数少于已记录内部操作")
                    retry_deltas[kind] = reported_count - observed_count
                sequence = self._append_event(
                    level="info",
                    event_code="transcription.execution_observed",
                    stage=scope._stage,
                    module=scope._module,
                    message="语音识别的中性聚合执行事实已记录。",
                    attributes={
                        "recovery_count": execution_fact.recovery_count,
                        "retry_count": execution_fact.retry_count,
                    },
                )
                for kind, retry_delta in retry_deltas.items():
                    self._retry_counts[kind] += retry_delta
                self._transcription_execution = execution_fact
                return sequence
            if fact._kind is _FactKind.SOURCE:
                if operation is not None:
                    raise ValueError("素材事实不能绑定诊断操作")
                if scope._module is not ErrorModule.SOURCE_ANALYSIS:
                    raise ValueError("素材事实只能由 SourceAnalysis 记录")
                if self._source is not None:
                    raise RuntimeError("素材诊断事实已经记录")
                source_fact = fact._payload
                if not isinstance(source_fact, _SourceFact):
                    raise TypeError("素材诊断事实类型不合法")
                sequence = self._append_event(
                    level="info",
                    event_code="source.observed",
                    stage=scope._stage,
                    module=scope._module,
                    message="素材的脱敏来源事实已记录。",
                    attributes={
                        "byte_length": source_fact.byte_length,
                        "course_context_provided": (
                            source_fact.course_context_provided
                        ),
                        "duration_ms": source_fact.duration_ms,
                    },
                )
                self._source = source_fact
                return sequence
            if fact._kind is _FactKind.ENVIRONMENT:
                if operation is not None:
                    raise ValueError("环境事实不能绑定诊断操作")
                if scope._module is not ErrorModule.READINESS:
                    raise ValueError("环境事实只能由 Readiness 记录")
                if self._environment is not None:
                    raise RuntimeError("环境诊断事实已经记录")
                environment_fact = fact._payload
                if not isinstance(environment_fact, _EnvironmentFact):
                    raise TypeError("环境诊断事实类型不合法")
                attributes = _environment_manifest(environment_fact)
                attributes.pop("status")
                attributes["application_version"] = (
                    self._application_version
                )
                sequence = self._append_event(
                    level=(
                        "info"
                        if environment_fact.preflight_outcome.value
                        == "succeeded"
                        else "error"
                    ),
                    event_code="environment.observed",
                    stage=scope._stage,
                    module=scope._module,
                    message="认证环境的脱敏预检事实已记录。",
                    attributes=attributes,
                )
                self._environment = environment_fact
                return sequence
            if fact._kind is _FactKind.INTERRUPTION:
                if operation is not None:
                    raise ValueError("中断事实不能绑定诊断操作")
                if scope._module is not ErrorModule.APPLICATION:
                    raise ValueError("中断事实只能由 Application 记录")
                signal = fact._payload
                if not isinstance(signal, InterruptionSignal):
                    raise TypeError("中断诊断事实类型不合法")
                if self._interruption is not None:
                    raise RuntimeError("受控中断已经记录")
                received_at = _timestamp(self._wall_clock())
                sequence = self._append_event(
                    level="warning",
                    event_code="interruption.requested",
                    stage=scope._stage,
                    module=scope._module,
                    message="应用已接受受控中断请求。",
                    attributes={"signal": signal.value},
                    timestamp=received_at,
                )
                self._interruption = {
                    "signal": signal,
                    "received_at": received_at,
                }
                return sequence
            if fact._kind is _FactKind.EXTERNAL_SERVICE:
                if operation is not None:
                    raise ValueError("供应商选择事实不能绑定诊断操作")
                service_fact = fact._payload
                if not isinstance(service_fact, _ExternalServiceFact):
                    raise TypeError("供应商选择事实类型不合法")
                _assert_provider_scope(scope, service_fact.capability)
                if service_fact.capability in self._external_services:
                    raise RuntimeError("同一供应商能力已经声明")
                endpoint = (
                    {"status": "not_applicable"}
                    if service_fact.endpoint_origin is None
                    else {
                        "status": "available",
                        "origin": service_fact.endpoint_origin,
                    }
                )
                sequence = self._append_event(
                    level="info",
                    event_code="external_service.selected",
                    stage=scope._stage,
                    module=scope._module,
                    message="供应商外发计划的脱敏事实已记录。",
                    attributes={
                        "adapter_id": service_fact.adapter_id,
                        "allowed_data_categories": [
                            category.value
                            for category
                            in service_fact.allowed_data_categories
                        ],
                        "capability": service_fact.capability.value,
                        "configuration_fingerprint": (
                            service_fact.configuration_fingerprint
                        ),
                        "endpoint": endpoint,
                        "model_id": service_fact.model_id,
                        "provider_id": service_fact.provider_id,
                        "transport": service_fact.transport.value,
                    },
                )
                self._external_services[service_fact.capability] = {
                    "selection": service_fact,
                    "requests": [],
                    "zero_request_reason": (
                        "deterministic_adapter"
                        if service_fact.transport
                        is ProviderTransport.LOCAL
                        else None
                    ),
                }
                return sequence
            if fact._kind is _FactKind.EXTERNAL_SERVICE_ZERO_REQUESTS:
                if operation is not None:
                    raise ValueError("零请求事实不能绑定诊断操作")
                zero_fact = fact._payload
                if not isinstance(
                    zero_fact,
                    _ExternalServiceZeroRequestsFact,
                ):
                    raise TypeError("供应商零请求事实类型不合法")
                _assert_provider_scope(scope, zero_fact.capability)
                service = self._external_services.get(zero_fact.capability)
                if service is None:
                    raise RuntimeError("必须先声明供应商能力")
                if service["requests"]:
                    raise RuntimeError("已经发生请求，不能声明零请求")
                if service["zero_request_reason"] is not None:
                    raise RuntimeError("供应商零请求事实已经记录")
                sequence = self._append_event(
                    level="info",
                    event_code="external_service.zero_requests",
                    stage=scope._stage,
                    module=scope._module,
                    message="供应商能力本次运行未发生远程请求。",
                    attributes={
                        "capability": zero_fact.capability.value,
                        "reason": zero_fact.reason.value,
                    },
                )
                service["zero_request_reason"] = zero_fact.reason.value
                return sequence
            if fact._kind is _FactKind.EXTERNAL_REQUEST:
                if operation is None:
                    raise ValueError("外部请求事实必须绑定诊断操作")
                self._assert_operation_active(operation)
                if operation._kind is not OperationKind.EXTERNAL_REQUEST:
                    raise ValueError("外部请求事实只能绑定外部请求操作")
                request_fact = fact._payload
                if not isinstance(request_fact, _ExternalRequestFact):
                    raise TypeError("外部请求事实类型不合法")
                _assert_provider_scope(scope, request_fact.capability)
                service = self._external_services.get(
                    request_fact.capability
                )
                if service is None:
                    raise RuntimeError("必须先声明供应商能力")
                selection = service["selection"]
                if (
                    not isinstance(selection, _ExternalServiceFact)
                    or selection.transport is not ProviderTransport.REMOTE
                ):
                    raise ValueError("本地 Adapter 不能记录远程请求")
                if service["zero_request_reason"] is not None:
                    raise RuntimeError("已经声明零请求，不能再记录请求")
                observed_at = _monotonic_value(self._monotonic_clock())
                duration_ms = _duration_ms(
                    operation._started_at,
                    observed_at,
                )
                request_id = (
                    {"status": "not_reported"}
                    if request_fact.remote_request_id is None
                    else {
                        "status": "reported",
                        "sha256": str(request_fact.remote_request_id),
                    }
                )
                token_usage = (
                    {"status": "not_reported"}
                    if request_fact.input_tokens is None
                    else {
                        "status": "reported",
                        "input_tokens": request_fact.input_tokens,
                        "output_tokens": request_fact.output_tokens,
                    }
                )
                sequence = self._append_event(
                    level=(
                        "info"
                        if request_fact.outcome
                        is ExternalRequestOutcome.SUCCEEDED
                        else "error"
                    ),
                    event_code="external_request.completed",
                    stage=scope._stage,
                    module=scope._module,
                    message="外部请求的脱敏结果已记录。",
                    attributes={
                        "attempt_count": request_fact.attempt_count,
                        "capability": request_fact.capability.value,
                        "duration_ms": duration_ms,
                        "outcome": request_fact.outcome.value,
                        "remote_request_id": request_id,
                        "token_usage": token_usage,
                    },
                    operation_id=operation._operation_id,
                    parent_operation_id=operation._parent_operation_id,
                )
                service["requests"].append(
                    {
                        "outcome": request_fact.outcome,
                        "attempt_count": request_fact.attempt_count,
                        "duration_ms": duration_ms,
                        "input_tokens": request_fact.input_tokens,
                        "output_tokens": request_fact.output_tokens,
                    }
                )
                return sequence
            if fact._kind is _FactKind.DELIVERY_STATE:
                if operation is not None:
                    raise ValueError("交付状态事实不能绑定诊断操作")
                delivery_fact = fact._payload
                if not isinstance(delivery_fact, _DeliveryStateFact):
                    raise TypeError("交付状态事实类型不合法")
                return self._record_delivery_state(scope, delivery_fact)
            if fact._kind in {
                _FactKind.ARTIFACT_CREATED,
                _FactKind.ARTIFACT_VERIFIED,
            }:
                artifact_fact = fact._payload
                if not isinstance(artifact_fact, _ArtifactFact):
                    raise TypeError("交付文件事实类型不合法")
                return self._record_artifact(
                    scope,
                    artifact_fact,
                    verified=(
                        fact._kind is _FactKind.ARTIFACT_VERIFIED
                    ),
                    operation=operation,
                )
            if fact._kind is _FactKind.RECOVERED_NOTICE:
                if operation is not None:
                    raise ValueError("已恢复事项不能绑定诊断操作")
                notice_fact = fact._payload
                if not isinstance(notice_fact, _RecoveredNoticeFact):
                    raise TypeError("已恢复事项类型不合法")
                _validate_recovered_notice(
                    notice_fact,
                    retry_counts=self._retry_counts,
                    cache_stats=self._cache_stats,
                    already_recorded=self._notices.get(
                        notice_fact.kind,
                        0,
                    ),
                )
                sequence = self._append_event(
                    level="warning",
                    event_code="notice.recorded",
                    stage=scope._stage,
                    module=scope._module,
                    message="本次运行的一项额外工作已恢复。",
                    attributes={
                        "count": notice_fact.count,
                        "kind": notice_fact.kind.value,
                    },
                )
                self._notices[notice_fact.kind] = (
                    self._notices.get(notice_fact.kind, 0)
                    + notice_fact.count
                )
                return sequence
            raise TypeError("未知诊断事实类型")

    def _record_delivery_state(
        self,
        scope: "DiagnosticScope",
        fact: _DeliveryStateFact,
    ) -> int | None:
        expected_module = {
            "build": ErrorModule.DELIVERY_BUILD,
            "verification": ErrorModule.DELIVERY_VERIFICATION,
            "publication": ErrorModule.PUBLICATION,
        }[fact.phase]
        if scope._module is not expected_module:
            raise ValueError("交付状态只能由对应业务模块记录")
        if (
            fact.published_delivery is not None
            and fact.published_delivery.run_id != self._run_id
        ):
            raise ValueError("发布提交证明必须属于同一次直播拆条运行")
        # COMMITTED 表达已经发生的不可逆外部事实。必须先保存提交证明，
        # 才能把紧随其后的事件写入失败按“提交后诊断不完整”处理；该路径
        # 不会发布 run.json，finish 只返回 incomplete，因而不会认证一份
        # 与事件日志不一致的完整诊断包。
        _advance_delivery_state(self._delivery, fact)
        if fact.published_delivery is not None:
            self._delivery["published_delivery"] = (
                fact.published_delivery
            )
            self._postcommit_proof = fact.published_delivery
        sequence = self._append_event(
            level=(
                "error"
                if fact.state == "failed"
                else (
                    "warning"
                    if fact.state in {"interrupted", "rolled_back"}
                    else "info"
                )
            ),
            event_code="delivery.state_changed",
            stage=scope._stage,
            module=scope._module,
            message="标准交付生命周期状态已更新。",
            attributes={"phase": fact.phase, "state": fact.state},
        )
        return sequence

    def _record_artifact(
        self,
        scope: "DiagnosticScope",
        fact: _ArtifactFact,
        *,
        verified: bool,
        operation: "DiagnosticOperation | None",
    ) -> int | None:
        if operation is not None:
            self._assert_operation_active(operation)
        if verified:
            if scope._module is not ErrorModule.DELIVERY_VERIFICATION:
                raise ValueError("产物验证事实只能由 DeliveryVerification 记录")
            if self._delivery["verification_state"] != "in_progress":
                raise RuntimeError("只有进行中的交付验证可以记录产物")
            created = self._delivery["created_artifacts"]
            if (
                not isinstance(created, dict)
                or created.get(fact.relative_path) is not fact.role
            ):
                raise ValueError("只能验证本次构建已经记录的同角色产物")
            artifacts = self._delivery["verified_artifacts"]
            event_code = "artifact.verified"
            message = "标准交付文件角色已通过独立验证。"
        else:
            if scope._module is not ErrorModule.DELIVERY_BUILD:
                raise ValueError("产物创建事实只能由 DeliveryBuild 记录")
            if self._delivery["build_state"] != "in_progress":
                raise RuntimeError("只有进行中的交付构建可以记录产物")
            artifacts = self._delivery["created_artifacts"]
            event_code = "artifact.created"
            message = "标准交付文件角色已创建。"
        if not isinstance(artifacts, dict):
            raise RuntimeError("交付文件状态不合法")
        if fact.relative_path in artifacts:
            raise RuntimeError("同一交付文件事实不能重复记录")
        sequence = self._append_event(
            level="info",
            event_code=event_code,
            stage=scope._stage,
            module=scope._module,
            message=message,
            attributes={
                "relative_path": fact.relative_path,
                "role": fact.role.value,
            },
            operation_id=(
                None if operation is None else operation._operation_id
            ),
            parent_operation_id=(
                None
                if operation is None
                else operation._parent_operation_id
            ),
        )
        artifacts[fact.relative_path] = fact.role
        return sequence

    def _validate_outcome(self, outcome: DiagnosticCompletion) -> None:
        _validate_delivery_terminal(self._delivery, outcome)
        errors = (outcome.primary_error, *outcome.associated_errors)
        if any(
            error is not None
            and self._errors.get(error.error_id) is not error
            for error in errors
        ):
            raise ValueError("终态错误必须由同一次直播拆条运行记录")
        if len({error.error_id for error in errors if error is not None}) != len(
            [error for error in errors if error is not None]
        ):
            raise ValueError("终态错误不能重复")
        if tuple(
            sorted(
                outcome.associated_errors,
                key=lambda error: error.event_sequence,
            )
        ) != outcome.associated_errors:
            raise ValueError("关联错误必须按事件序号排列")
        if outcome.state is _DiagnosticTerminalState.FAILED:
            if outcome.primary_error is None:
                raise TypeError("失败终态必须包含主错误")
            if any(
                error.event_sequence
                <= outcome.primary_error.event_sequence
                for error in outcome.associated_errors
            ):
                raise ValueError("主错误必须先于关联错误被记录")
            if outcome.published_delivery is not None:
                raise ValueError("失败终态不能包含发布提交证明")
            return
        if outcome.primary_error is not None:
            raise ValueError("成功或中断终态不能包含主错误")
        if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
            delivery = outcome.published_delivery
            if delivery is None:
                raise TypeError("成功终态必须包含 PublishedDelivery")
            if delivery.run_id != self._run_id:
                raise ValueError("发布提交证明必须属于同一次直播拆条运行")
            if not isinstance(outcome.result_kind, ResultKind):
                raise TypeError("成功终态必须包含合法结果种类")
            if outcome.associated_errors or outcome.recovery_incomplete:
                raise ValueError("成功终态不能包含终态错误")
            return
        if outcome.state is _DiagnosticTerminalState.INTERRUPTED:
            if outcome.published_delivery is not None:
                raise ValueError("中断终态不能包含发布提交证明")
            interruption = self._interruption
            if (
                interruption is None
                or interruption["signal"] is not outcome.interruption_signal
            ):
                raise ValueError("中断终态必须匹配已记录的中断请求")
            if (
                bool(outcome.associated_errors)
                != outcome.recovery_incomplete
            ):
                raise ValueError("中断清理错误与恢复完整性必须一致")
            return
        raise TypeError("未知运行终态")

    def _terminal_stage(self, outcome: DiagnosticCompletion) -> RunStage:
        if outcome.primary_error is not None:
            return outcome.primary_error.stage
        if self._stages:
            return next(reversed(self._stages))
        return RunStage.INITIALIZED

    def _build_manifest(
        self,
        *,
        outcome: DiagnosticCompletion,
        ended_timestamp: str,
        duration_ms: int,
        events: bytes,
    ) -> dict[str, object]:
        stages: dict[str, object] = {}
        for stage in RunStage:
            summary = self._stages.get(stage)
            if summary is None:
                stages[stage.value] = {"status": "not_started"}
            else:
                stages[stage.value] = summary._manifest_summary()

        return {
            "schema_version": "run_manifest.v1",
            "identity": {
                "run_id": str(self._run_id),
                "application_version": self._application_version,
                "release": {"status": "unknown"},
            },
            "lifecycle": {
                "started_at": self._started_timestamp,
                "ended_at": ended_timestamp,
                "duration_ms": duration_ms,
                "outcome": outcome.state.value,
                "exit_code": _outcome_exit_code(outcome),
                "result_kind": _result_kind_status(outcome),
                "interruption": self._interruption_manifest(outcome),
            },
            "source": (
                {"status": "not_observed"}
                if self._source is None
                else _source_manifest(self._source)
            ),
            "environment": (
                {"status": "not_observed"}
                if self._environment is None
                else {
                    **_environment_manifest(self._environment),
                    "application_version": self._application_version,
                }
            ),
            "configuration": (
                {"status": "not_observed"}
                if self._configuration is None
                else _configuration_manifest(self._configuration)
            ),
            "stages": stages,
            "operations": {
                "summaries": _operation_summaries(self._operations)
            },
            "retries_and_recovery": {
                kind.value: self._retry_counts[kind]
                for kind in RetryKind
            },
            "cache": _cache_manifest(self._cache_stats),
            "external_services": _external_services_manifest(
                self._external_services
            ),
            "delivery": _delivery_manifest(outcome, self._delivery),
            "notices": [
                {
                    "kind": kind.value,
                    "count": self._notices[kind],
                }
                for kind in RecoveredNoticeKind
                if kind in self._notices
            ],
            "errors": {
                "primary_error": (
                    {"status": "not_applicable"}
                    if outcome.primary_error is None
                    else _error_manifest(outcome.primary_error)
                ),
                "associated_errors": [
                    _error_manifest(error)
                    for error in outcome.associated_errors
                ],
                "recovery_incomplete": outcome.recovery_incomplete,
            },
            "event_log": {
                "path": "events.jsonl",
                "event_count": self._sequence,
                "byte_length": len(events),
                "sha256": "sha256:"
                + hashlib.sha256(events).hexdigest(),
            },
        }

    def _interruption_manifest(
        self,
        outcome: DiagnosticCompletion,
    ) -> dict[str, object]:
        if outcome.state is not _DiagnosticTerminalState.INTERRUPTED:
            return {"status": "not_applicable"}
        if (
            self._interruption is None
            or outcome.interruption_signal is None
            or outcome.cleanup_duration_ms is None
        ):
            raise RuntimeError("中断终态缺少已记录事实")
        return {
            "status": "available",
            "signal": outcome.interruption_signal.value,
            "received_at": self._interruption["received_at"],
            "cleanup_duration_ms": outcome.cleanup_duration_ms,
        }

    def _start_operation(
        self,
        scope: "DiagnosticScope",
        kind: OperationKind,
        *,
        item_index: int,
        item_count: int,
        parent: "DiagnosticOperation | None",
    ) -> "DiagnosticOperation":
        if not isinstance(kind, OperationKind):
            raise TypeError("操作类型必须使用 OperationKind")
        _positive_integer(item_index, field="操作工作项序号")
        _positive_integer(item_count, field="操作工作项总数")
        if item_index > item_count:
            raise ValueError("操作工作项序号不能大于总数")
        with self._lock:
            self._assert_open()
            if self._postcommit_proof is not None:
                raise RuntimeError("发布提交后不能开始新的诊断操作")
            self._assert_scope_active(scope)
            if (
                scope._stage is RunStage.TRANSCRIPTION
                and self._transcription_execution is not None
            ):
                raise RuntimeError("转写聚合执行事实形成后不能开始内部操作")
            parent_operation_id = None
            if parent is not None:
                if not isinstance(parent, DiagnosticOperation):
                    raise TypeError("父操作必须使用 DiagnosticOperation")
                if parent._owner is not self:
                    raise ValueError("父操作必须属于同一次直播拆条运行")
                if parent._completed:
                    raise RuntimeError("已完成操作不能成为新操作的父操作")
                parent_operation_id = parent._operation_id

            started_at = _monotonic_value(self._monotonic_clock())
            operation_id = OperationId.new()
            self._append_event(
                level="info",
                event_code="operation.started",
                stage=scope._stage,
                module=scope._module,
                message="诊断操作已开始。",
                attributes={
                    "item_count": item_count,
                    "item_index": item_index,
                    "operation_kind": kind.value,
                },
                operation_id=operation_id,
                parent_operation_id=parent_operation_id,
            )
            operation = DiagnosticOperation._create(
                owner=self,
                scope=scope,
                operation_id=operation_id,
                kind=kind,
                started_at=started_at,
                parent_operation_id=parent_operation_id,
            )
            self._operations[operation_id] = operation
            return operation

    def _schedule_retry(
        self,
        operation: "DiagnosticOperation",
        kind: RetryKind,
        *,
        next_attempt: int,
        reason_code: str,
        backoff_ms: int,
    ) -> int | None:
        if not isinstance(kind, RetryKind):
            raise TypeError("额外工作类别必须使用 RetryKind")
        _positive_integer(next_attempt, field="下一尝试序号")
        if next_attempt < 2:
            raise ValueError("下一尝试序号必须从 2 开始")
        if not isinstance(reason_code, str):
            raise TypeError("重试原因码必须是字符串")
        if _STABLE_REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("重试原因码格式不合法")
        _nonnegative_integer(backoff_ms, field="计划退避毫秒数")
        with self._lock:
            self._assert_open()
            self._assert_operation_active(operation)
            if (
                operation._scope._stage is RunStage.TRANSCRIPTION
                and self._transcription_execution is not None
            ):
                raise RuntimeError("转写聚合执行事实形成后不能再安排内部重试")
            if (
                operation._scope._stage is RunStage.TRANSCRIPTION
                and self._transcription_transcript_cache_hit
            ):
                raise ValueError("整场转写缓存命中时不得发生内部工作")
            if next_attempt != operation._last_attempt + 1:
                raise ValueError("下一尝试序号必须严格递增")
            sequence = self._append_event(
                level="warning",
                event_code="retry.scheduled",
                stage=operation._scope._stage,
                module=operation._scope._module,
                message="诊断操作已安排额外尝试。",
                attributes={
                    "backoff_ms": backoff_ms,
                    "next_attempt": next_attempt,
                    "reason_code": reason_code,
                    "retry_kind": kind.value,
                },
                operation_id=operation._operation_id,
                parent_operation_id=operation._parent_operation_id,
            )
            operation._last_attempt = next_attempt
            self._retry_counts[kind] += 1
            if operation._scope._stage is RunStage.TRANSCRIPTION:
                self._transcription_retry_counts[kind] += 1
            return sequence

    def _complete_operation(
        self,
        operation: "DiagnosticOperation",
        outcome: OperationOutcome,
        *,
        attempt_count: int,
    ) -> int | None:
        if not isinstance(outcome, OperationOutcome):
            raise TypeError("操作结果必须使用 OperationOutcome")
        _positive_integer(attempt_count, field="操作尝试次数")
        with self._lock:
            self._assert_open()
            self._assert_operation_active(operation)
            if attempt_count != operation._last_attempt:
                raise ValueError("操作尝试次数与已记录尝试不一致")
            for child in self._operations.values():
                if (
                    child._parent_operation_id == operation._operation_id
                    and not child._completed
                ):
                    raise RuntimeError("父操作不能先于活动子操作完成")
            completed_at = _monotonic_value(self._monotonic_clock())
            duration_ms = _duration_ms(
                operation._started_at,
                completed_at,
            )
            sequence = self._append_event(
                level="info",
                event_code="operation.completed",
                stage=operation._scope._stage,
                module=operation._scope._module,
                message="诊断操作已完成。",
                attributes={
                    "attempt_count": attempt_count,
                    "duration_ms": duration_ms,
                    "operation_kind": operation._kind.value,
                    "outcome": outcome.value,
                },
                operation_id=operation._operation_id,
                parent_operation_id=operation._parent_operation_id,
            )
            operation._completed = True
            operation._outcome = outcome
            operation._duration_ms = duration_ms
            return sequence

    def _assert_scope_active(self, scope: "DiagnosticScope") -> None:
        if (
            scope._owner is not self
            or self._active_stage is not scope._stage_diagnostics
            or scope._stage_diagnostics._scopes.get(scope._module)
            is not scope
        ):
            raise TypeError("诊断 scope 不属于当前活动阶段")
        if scope._stage_diagnostics._completed:
            raise RuntimeError("阶段已经完成")

    def _assert_operation_active(
        self,
        operation: "DiagnosticOperation",
    ) -> None:
        if (
            operation._owner is not self
            or self._operations.get(operation._operation_id) is not operation
        ):
            raise TypeError("诊断操作不属于当前直播拆条运行")
        self._assert_scope_active(operation._scope)
        if operation._completed:
            raise RuntimeError("诊断操作已经完成")

    def _assert_open(self) -> None:
        if self._finished:
            raise RuntimeError("直播拆条运行诊断已经完成")

    def _append_event(
        self,
        *,
        level: str,
        event_code: str,
        stage: RunStage,
        module: ErrorModule,
        message: str,
        attributes: dict[str, object],
        operation_id: OperationId | None = None,
        parent_operation_id: OperationId | None = None,
        timestamp: str | None = None,
    ) -> int | None:
        if self._postcommit_incomplete:
            return None
        sequence = self._sequence + 1
        event = {
            "schema_version": _EVENT_SCHEMA_VERSION,
            "timestamp": (
                _timestamp(self._wall_clock())
                if timestamp is None
                else timestamp
            ),
            "sequence": sequence,
            "run_id": str(self._run_id),
            "level": level,
            "event_code": event_code,
            "stage": stage.value,
            "module": module.value,
            "message": message,
            "attributes": attributes,
        }
        if operation_id is not None:
            event["operation_id"] = str(operation_id)
        if parent_operation_id is not None:
            event["parent_operation_id"] = str(parent_operation_id)
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )

        append_failure_reason: str | None = None
        try:
            self._store.append(encoded)
        except (OSError, WorkspaceFailure) as failure:
            append_failure_reason = _diagnostics_failure_reason(
                failure,
                default="diagnostics.append_failed",
                startup=False,
            )
        if append_failure_reason is not None:
            if self._postcommit_proof is not None:
                self._postcommit_incomplete = True
                return None
            self._finished = True
            raise _runtime_failure(
                operation="diagnostics.append",
                reason_code=append_failure_reason,
            )
        self._sequence = sequence
        return sequence


def _diagnostics_failure_reason(
    failure: OSError | WorkspaceFailure,
    *,
    default: str,
    startup: bool,
) -> str:
    if not isinstance(failure, WorkspaceFailure):
        return default
    workspace_reason = failure.diagnostics.get("reason_code")
    if workspace_reason == "filesystem.file_sync_failed":
        return "diagnostics.file_sync_failed"
    if workspace_reason == "filesystem.directory_sync_failed":
        return "diagnostics.directory_sync_failed"
    if not startup and workspace_reason in {
        "filesystem.atomic_replace_failed",
        "filesystem.cross_device",
    }:
        return "diagnostics.atomic_replace_failed"
    return default


class StageDiagnostics:
    """一次性拥有阶段配对并签发模块 scope 的 capability。"""

    __slots__ = (
        "_completed",
        "_owner",
        "_owner_module",
        "_outcome",
        "_scopes",
        "_stage",
        "_started_at",
        "_duration_ms",
        "_work_item_count",
    )

    def __init__(self) -> None:
        raise TypeError("StageDiagnostics 只能由 RunDiagnostics 创建")

    @classmethod
    def _create(
        cls,
        *,
        owner: RunDiagnostics,
        stage: RunStage,
        owner_module: ErrorModule,
        started_at: float,
    ) -> "StageDiagnostics":
        instance = object.__new__(cls)
        instance._owner = owner
        instance._stage = stage
        instance._owner_module = owner_module
        instance._started_at = started_at
        instance._completed = False
        instance._outcome = None
        instance._duration_ms = None
        instance._work_item_count = None
        instance._scopes = {}
        return instance

    @property
    def stage(self) -> RunStage:
        return self._stage

    def scope(self, module: ErrorModule) -> "DiagnosticScope":
        """向当前阶段实际参与的一个业务模块签发唯一 scope。"""
        if not isinstance(module, ErrorModule):
            raise TypeError("诊断模块必须使用 ErrorModule")
        with self._owner._lock:
            self._owner._assert_open()
            if (
                self._owner._active_stage is not self
                or self._completed
            ):
                raise RuntimeError("阶段已经完成")
            scope = self._scopes.get(module)
            if scope is None:
                scope = DiagnosticScope._create(
                    owner=self._owner,
                    stage_diagnostics=self,
                    module=module,
                )
                self._scopes[module] = scope
            return scope

    def complete(
        self,
        outcome: StageOutcome,
        *,
        work_item_count: int,
    ) -> int | None:
        """完成阶段；提交点后诊断失效时没有持久化序号。"""
        return self._owner._complete_stage(
            self,
            outcome,
            work_item_count,
        )

    def _manifest_summary(self) -> dict[str, object]:
        if (
            not self._completed
            or self._outcome is None
            or self._duration_ms is None
            or self._work_item_count is None
        ):
            return {"status": "in_progress"}
        return {
            "status": self._outcome.value,
            "duration_ms": self._duration_ms,
            "work_item_count": self._work_item_count,
        }


class DiagnosticScope:
    """只能由 RunDiagnostics 签发的阶段与模块绑定 scope。"""

    __slots__ = (
        "_completed",
        "_module",
        "_owner",
        "_stage",
        "_stage_diagnostics",
    )

    def __init__(self) -> None:
        raise TypeError("DiagnosticScope 只能由 RunDiagnostics 创建")

    @classmethod
    def _create(
        cls,
        *,
        owner: RunDiagnostics,
        stage_diagnostics: StageDiagnostics,
        module: ErrorModule,
    ) -> "DiagnosticScope":
        instance = object.__new__(cls)
        instance._owner = owner
        instance._stage_diagnostics = stage_diagnostics
        instance._stage = stage_diagnostics._stage
        instance._module = module
        return instance

    @property
    def run_id(self) -> RunId:
        return self._owner.run_id

    @property
    def stage(self) -> RunStage:
        return self._stage

    @property
    def module(self) -> ErrorModule:
        return self._module

    def complete(
        self,
        outcome: StageOutcome,
        *,
        work_item_count: int,
    ) -> int | None:
        """完成阶段；提交点后诊断失效时没有持久化序号。"""
        return self._stage_diagnostics.complete(
            outcome,
            work_item_count=work_item_count,
        )

    def start_operation(
        self,
        kind: OperationKind,
        *,
        item_index: int,
        item_count: int,
        parent: "DiagnosticOperation | None" = None,
    ) -> "DiagnosticOperation":
        """开始一个有成本、并发或可独立失败的诊断操作。"""
        return self._owner._start_operation(
            self,
            kind,
            item_index=item_index,
            item_count=item_count,
            parent=parent,
        )

    def record_failure(self, failure: object) -> RunError:
        """把类型化模块失败投影为带精确事件序号的 RunError。"""
        return self._owner._record_failure(self, failure)

    def record(self, fact: DiagnosticFact) -> int | None:
        """记录一个由 ``Facts`` 工厂创建的类型化安全事实。"""
        return self._owner._record_fact(self, fact)


class DiagnosticOperation:
    """只能由 DiagnosticScope 签发且恰好完成一次的操作。"""

    __slots__ = (
        "_completed",
        "_duration_ms",
        "_kind",
        "_last_attempt",
        "_operation_id",
        "_outcome",
        "_owner",
        "_parent_operation_id",
        "_scope",
        "_started_at",
    )

    def __init__(self) -> None:
        raise TypeError("DiagnosticOperation 只能由 DiagnosticScope 创建")

    @classmethod
    def _create(
        cls,
        *,
        owner: RunDiagnostics,
        scope: DiagnosticScope,
        operation_id: OperationId,
        kind: OperationKind,
        started_at: float,
        parent_operation_id: OperationId | None,
    ) -> "DiagnosticOperation":
        instance = object.__new__(cls)
        instance._owner = owner
        instance._scope = scope
        instance._operation_id = operation_id
        instance._kind = kind
        instance._started_at = started_at
        instance._parent_operation_id = parent_operation_id
        instance._last_attempt = 1
        instance._completed = False
        instance._duration_ms = None
        instance._outcome = None
        return instance

    @property
    def operation_id(self) -> OperationId:
        """返回与业务标识隔离的诊断操作标识。"""
        return self._operation_id

    def schedule_retry(
        self,
        kind: RetryKind,
        *,
        next_attempt: int,
        reason_code: str,
        backoff_ms: int,
    ) -> int | None:
        """在额外尝试发生前记录其类型和稳定调度事实。"""
        return self._owner._schedule_retry(
            self,
            kind,
            next_attempt=next_attempt,
            reason_code=reason_code,
            backoff_ms=backoff_ms,
        )

    def complete(
        self,
        outcome: OperationOutcome,
        *,
        attempt_count: int,
    ) -> int | None:
        """完成操作并使用诊断单调时钟记录耗时。"""
        return self._owner._complete_operation(
            self,
            outcome,
            attempt_count=attempt_count,
        )

    def record(self, fact: DiagnosticFact) -> int | None:
        """记录与当前诊断操作关联的类型化安全事实。"""
        return self._owner._record_fact(
            self._scope,
            fact,
            operation=self,
        )


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("UTC 时钟必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC 时钟必须返回带时区的 datetime")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _monotonic_value(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("单调时钟必须返回数字")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("单调时钟必须返回有限数字")
    return normalized


def _duration_ms(started_at: float, completed_at: float) -> int:
    if completed_at < started_at:
        raise RuntimeError("单调时钟发生倒退")
    return int((completed_at - started_at) * 1000)


def _positive_integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}必须是整数")
    if value < 1:
        raise ValueError(f"{field}必须是正整数")
    return value


def _nonnegative_integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}必须是整数")
    if value < 0:
        raise ValueError(f"{field}不能为负数")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, tuple):
        return [_json_value(nested) for nested in value]
    if isinstance(value, list):
        return [_json_value(nested) for nested in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("诊断值不属于严格 JSON 白名单")


def _error_manifest(error: RunError) -> dict[str, object]:
    operation = (
        {"status": "not_applicable"}
        if error.operation_id is None
        else {"status": "present", "operation_id": str(error.operation_id)}
    )
    return {
        "error_id": str(error.error_id),
        "error_code": error.error_code.value,
        "category": error.category.value,
        "stage": error.stage.value,
        "module": error.module.value,
        "operation": operation,
        "event_sequence": error.event_sequence,
        "safe_message": error.safe_message,
        "retryable_in_new_run": error.retryable_in_new_run,
        "operator_action": error.operator_action.value,
        "diagnostics": _json_value(error.diagnostics),
    }


def _outcome_exit_code(outcome: DiagnosticCompletion) -> int:
    if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
        return 0
    if outcome.state is _DiagnosticTerminalState.INTERRUPTED:
        if outcome.interruption_signal is None:
            raise TypeError("中断终态必须包含信号")
        return outcome.interruption_signal.exit_code
    if outcome.primary_error is None:
        raise TypeError("失败终态必须包含主错误")
    return int(outcome.primary_error.exit_code)


def _result_kind_status(outcome: DiagnosticCompletion) -> dict[str, object]:
    if outcome.state is not _DiagnosticTerminalState.SUCCEEDED:
        return {"status": "not_applicable"}
    if outcome.result_kind is None:
        raise TypeError("成功终态必须包含结果种类")
    return {
        "status": "available",
        "value": outcome.result_kind.value,
    }


def _source_manifest(fact: _SourceFact) -> dict[str, object]:
    context = (
        {
            "provided": True,
            "sha256": {
                "status": "available",
                "value": fact.course_context_sha256,
            },
        }
        if fact.course_context_sha256 is not None
        else {
            "provided": False,
            "sha256": {"status": "not_applicable"},
        }
    )
    return {
        "status": "available",
        "sha256": fact.sha256,
        "byte_length": fact.byte_length,
        "duration_ms": fact.duration_ms,
        "course_context": context,
    }


def _environment_manifest(fact: _EnvironmentFact) -> dict[str, object]:
    return {
        "status": "available",
        "certified_platform": fact.certified_platform.value,
        "python_version": str(fact.python_version),
        "ffmpeg_version": str(fact.ffmpeg_version),
        "ffprobe_version": str(fact.ffprobe_version),
        "font": {
            "family": fact.font_family,
            "available": fact.font_available,
        },
        "installation_fingerprint": fact.installation_fingerprint,
        "preflight_outcome": fact.preflight_outcome.value,
    }


def _assert_provider_scope(
    scope: DiagnosticScope,
    capability: ProviderCapability,
) -> None:
    expected_module = {
        ProviderCapability.TRANSCRIPTION: ErrorModule.TRANSCRIPTION,
        ProviderCapability.TOPIC_REVIEW: ErrorModule.TOPIC_REVIEW,
        ProviderCapability.SUBTITLE_OPTIMIZATION: (
            ErrorModule.SUBTITLE_OPTIMIZATION
        ),
    }[capability]
    if scope._module is not expected_module:
        raise ValueError("供应商事实只能由对应业务模块记录")


def _external_services_manifest(
    services: Mapping[ProviderCapability, dict[str, object]],
) -> dict[str, object]:
    if not services:
        return {"status": "not_observed"}
    summaries = []
    for capability in ProviderCapability:
        state = services.get(capability)
        if state is None:
            continue
        selection = state["selection"]
        requests = state["requests"]
        zero_reason = state["zero_request_reason"]
        if not isinstance(selection, _ExternalServiceFact):
            raise RuntimeError("供应商选择状态不合法")
        if not isinstance(requests, list):
            raise RuntimeError("供应商请求状态不合法")
        if requests:
            if zero_reason is not None:
                raise RuntimeError("供应商联系状态互相冲突")
            contact: dict[str, object] = {"status": "contacted"}
        else:
            if not isinstance(zero_reason, str):
                raise RuntimeError("零远程请求必须记录稳定原因")
            contact = {
                "status": "not_contacted",
                "reason": zero_reason,
            }
        durations = [
            request["duration_ms"]
            for request in requests
            if isinstance(request, dict)
        ]
        if len(durations) != len(requests) or any(
            not isinstance(duration, int) for duration in durations
        ):
            raise RuntimeError("供应商请求耗时状态不合法")
        reported = [
            request
            for request in requests
            if isinstance(request, dict)
            and request["input_tokens"] is not None
        ]
        if not requests:
            token_usage: dict[str, object] = {
                "status": "not_applicable"
            }
        elif not reported:
            token_usage = {"status": "not_reported"}
        else:
            token_usage = {
                "status": (
                    "reported"
                    if len(reported) == len(requests)
                    else "partially_reported"
                ),
                "input_tokens": sum(
                    request["input_tokens"] for request in reported
                ),
                "output_tokens": sum(
                    request["output_tokens"] for request in reported
                ),
            }
            if len(reported) != len(requests):
                token_usage["reported_request_count"] = len(reported)
        endpoint = (
            {"status": "not_applicable"}
            if selection.endpoint_origin is None
            else {
                "status": "available",
                "origin": selection.endpoint_origin,
            }
        )
        summaries.append(
            {
                "capability": capability.value,
                "adapter_id": selection.adapter_id,
                "provider_id": selection.provider_id,
                "model_id": selection.model_id,
                "configuration_fingerprint": (
                    selection.configuration_fingerprint
                ),
                "endpoint": endpoint,
                "transport": selection.transport.value,
                "purpose": capability.value,
                "allowed_data_categories": [
                    category.value
                    for category in selection.allowed_data_categories
                ],
                "contact": contact,
                "requests": {
                    "count": len(requests),
                    "succeeded": sum(
                        request["outcome"]
                        is ExternalRequestOutcome.SUCCEEDED
                        for request in requests
                    ),
                    "failed": sum(
                        request["outcome"]
                        is ExternalRequestOutcome.FAILED
                        for request in requests
                    ),
                    "attempt_count_total": sum(
                        request["attempt_count"]
                        for request in requests
                    ),
                    "duration_ms_total": sum(durations),
                    "duration_ms_max": max(durations, default=0),
                    "token_usage": token_usage,
                },
            }
        )
    return {"status": "observed", "services": summaries}


def _advance_delivery_state(
    delivery: dict[str, object],
    fact: _DeliveryStateFact,
) -> None:
    state_field = f"{fact.phase}_state"
    current = delivery[state_field]
    allowed = {
        "build": {
            "not_started": {"in_progress"},
            "in_progress": {
                DeliveryBuildState.COMPLETED.value,
                DeliveryBuildState.FAILED.value,
                DeliveryBuildState.INTERRUPTED.value,
            },
        },
        "verification": {
            "not_started": {"in_progress"},
            "in_progress": {
                DeliveryVerificationState.PASSED.value,
                DeliveryVerificationState.FAILED.value,
                DeliveryVerificationState.INTERRUPTED.value,
            },
        },
        "publication": {
            "not_started": {"in_progress"},
            "in_progress": {
                PublicationState.COMMITTED.value,
                PublicationState.ROLLED_BACK.value,
                PublicationState.FAILED.value,
            },
        },
    }[fact.phase]
    if fact.state not in allowed.get(str(current), set()):
        raise RuntimeError("交付生命周期状态转换不合法")
    if (
        fact.phase == "verification"
        and delivery["build_state"] != "completed"
    ):
        raise RuntimeError("交付构建完成后才能开始独立验证")
    if (
        fact.phase == "publication"
        and delivery["verification_state"] != "passed"
    ):
        raise RuntimeError("交付验证通过后才能开始发布")
    delivery[state_field] = fact.state


def _validate_delivery_terminal(
    delivery: dict[str, object],
    outcome: DiagnosticCompletion,
) -> None:
    states = (
        delivery["build_state"],
        delivery["verification_state"],
        delivery["publication_state"],
    )
    if "in_progress" in states:
        raise RuntimeError("终态清单不能包含进行中的交付生命周期")
    if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
        observed = states != (
            "not_started",
            "not_started",
            "not_started",
        )
        if observed and states != ("completed", "passed", "committed"):
            raise ValueError("成功终态与已记录交付生命周期不一致")
        recorded = delivery["published_delivery"]
        # 提交证明是 capability 身份，不是可互换的值对象。
        if (
            recorded is not None
            and recorded is not outcome.published_delivery
        ):
            raise ValueError("成功终态与发布提交事实不一致")
    elif delivery["publication_state"] == "committed":
        raise ValueError("越过发布提交点的运行不能失败或中断")


def _delivery_manifest(
    outcome: DiagnosticCompletion,
    delivery: dict[str, object],
) -> dict[str, object]:
    if outcome.state is _DiagnosticTerminalState.SUCCEEDED:
        manifest: dict[str, object] = {
            "build_state": "completed",
            "verification_state": "passed",
            "publication_state": "committed",
        }
    else:
        manifest = {
            "build_state": delivery["build_state"],
            "verification_state": delivery["verification_state"],
            "publication_state": delivery["publication_state"],
        }
    created = delivery["created_artifacts"]
    verified = delivery["verified_artifacts"]
    if not isinstance(created, dict) or not isinstance(verified, dict):
        raise RuntimeError("交付文件聚合状态不合法")
    if created or verified:
        manifest["artifacts"] = {
            "status": "observed",
            "created_by_role": _artifact_role_counts(created),
            "verified_by_role": _artifact_role_counts(verified),
        }
    return manifest


def _artifact_role_counts(
    artifacts: Mapping[str, ArtifactRole],
) -> dict[str, int]:
    return {
        role.value: sum(
            artifact_role is role
            for artifact_role in artifacts.values()
        )
        for role in ArtifactRole
        if role in artifacts.values()
    }


def _validate_recovered_notice(
    fact: _RecoveredNoticeFact,
    *,
    retry_counts: Mapping[RetryKind, int],
    cache_stats: Mapping[CacheNamespace, dict[str, int]],
    already_recorded: int,
) -> None:
    requested_total = already_recorded + fact.count
    retry_kind = {
        RecoveredNoticeKind.TRANSPORT_RETRY_SUCCEEDED: (
            RetryKind.TRANSPORT_RETRY
        ),
        RecoveredNoticeKind.SEMANTIC_RETRY_SUCCEEDED: (
            RetryKind.SEMANTIC_RETRY
        ),
        RecoveredNoticeKind.COVERAGE_RECOVERY_SUCCEEDED: (
            RetryKind.COVERAGE_RECOVERY
        ),
    }.get(fact.kind)
    if retry_kind is not None:
        if retry_counts[retry_kind] < requested_total:
            raise ValueError("已恢复事项数量不能超过已记录额外工作")
        return
    corrupt_count = sum(
        stats["corrupt_quarantined"]
        for stats in cache_stats.values()
    )
    if corrupt_count < requested_total:
        raise ValueError("缓存恢复事项必须对应已记录的损坏隔离")


def _configuration_manifest(
    projection: ConfigurationDiagnosticProjection,
) -> dict[str, object]:
    result = projection.result_configuration
    clip_policy = result.clip_policy
    return {
        "status": "available",
        "configuration_fingerprint": projection.configuration_fingerprint,
        "result_configuration": {
            "transcription_provider": result.transcription_provider,
            "transcription_model": result.transcription_model,
            "text_model_provider": result.text_model_provider,
            "topic_review": _text_generation_manifest(result.topic_review),
            "subtitle_optimization": _text_generation_manifest(
                result.subtitle_optimization
            ),
            "clip_policy": {
                "min_duration_seconds": clip_policy.min_duration_seconds,
                "target_duration_seconds": clip_policy.target_duration_seconds,
                "max_duration_seconds": clip_policy.max_duration_seconds,
                "max_clips": (
                    {"status": "unlimited"}
                    if clip_policy.max_clips is None
                    else {
                        "status": "limited",
                        "value": clip_policy.max_clips,
                    }
                ),
                "publish_ready_threshold": (
                    clip_policy.publish_ready_threshold
                ),
            },
            "subtitle_style": {
                "font": result.subtitle_style.font,
                "font_size": result.subtitle_style.font_size,
                "outline": result.subtitle_style.outline,
                "margin_bottom": result.subtitle_style.margin_bottom,
                "max_chars_per_line": (
                    result.subtitle_style.max_chars_per_line
                ),
                "max_lines": result.subtitle_style.max_lines,
            },
        },
        "runtime_policy": {
            "transcription_timeout_seconds": (
                projection.runtime_policy.transcription_timeout_seconds
            ),
            "transcription_max_concurrency": (
                projection.runtime_policy.transcription_max_concurrency
            ),
            "text_model_timeout_seconds": (
                projection.runtime_policy.text_model_timeout_seconds
            ),
            "text_model_max_concurrency": (
                projection.runtime_policy.text_model_max_concurrency
            ),
            "delivery_build_concurrency": (
                projection.runtime_policy.delivery_build_concurrency
            ),
        },
        "course_context": {
            "provided": projection.course_context.provided,
            "attribution_provided": (
                projection.course_context.attribution_provided
            ),
            "priority_topic_count": (
                projection.course_context.priority_topic_count
            ),
            "excluded_content_count": (
                projection.course_context.excluded_content_count
            ),
        },
    }


def _text_generation_manifest(settings: object) -> dict[str, object]:
    return {
        "model": settings.model,
        "temperature": settings.temperature,
        "reasoning_effort": settings.reasoning_effort,
        "max_output_tokens": settings.max_output_tokens,
    }


def _update_cache_stats(
    stats_by_namespace: dict[CacheNamespace, dict[str, int]],
    fact: _CacheFact,
) -> None:
    stats = stats_by_namespace.setdefault(
        fact.namespace,
        {
            "queries": 0,
            "hits": 0,
            "misses": 0,
            "corrupt_quarantined": 0,
            "writes_published": 0,
            "writes_already_present": 0,
            "infrastructure_failures": 0,
            "singleflight_wait_count": 0,
            "singleflight_wait_ms_total": 0,
        },
    )
    if fact.outcome in {
        CacheOutcome.HIT,
        CacheOutcome.MISS,
        CacheOutcome.CORRUPT_QUARANTINED,
    }:
        stats["queries"] += 1
    field_by_outcome = {
        CacheOutcome.HIT: "hits",
        CacheOutcome.MISS: "misses",
        CacheOutcome.CORRUPT_QUARANTINED: "corrupt_quarantined",
        CacheOutcome.WRITE_PUBLISHED: "writes_published",
        CacheOutcome.WRITE_ALREADY_PRESENT: "writes_already_present",
        CacheOutcome.INFRASTRUCTURE_FAILED: "infrastructure_failures",
    }
    stats[field_by_outcome[fact.outcome]] += 1
    if fact.singleflight_wait_ms is not None:
        stats["singleflight_wait_count"] += 1
        stats["singleflight_wait_ms_total"] += fact.singleflight_wait_ms


def _cache_manifest(
    stats_by_namespace: dict[CacheNamespace, dict[str, int]],
) -> dict[str, object]:
    if not stats_by_namespace:
        return {"status": "not_observed"}
    return {
        "status": "observed",
        "namespaces": {
            namespace.value: dict(stats_by_namespace[namespace])
            for namespace in CacheNamespace
            if namespace in stats_by_namespace
        },
    }


def _operation_summaries(
    operations: Mapping[OperationId, DiagnosticOperation],
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for kind in OperationKind:
        matching = [
            operation
            for operation in operations.values()
            if operation._kind is kind
        ]
        if not matching:
            continue
        if any(
            operation._outcome is None
            or operation._duration_ms is None
            for operation in matching
        ):
            raise RuntimeError("终态清单不能包含未完成诊断操作")
        durations = [
            operation._duration_ms
            for operation in matching
            if operation._duration_ms is not None
        ]
        summaries.append(
            {
                "operation_kind": kind.value,
                "count": len(matching),
                "outcomes": {
                    outcome.value: sum(
                        operation._outcome is outcome
                        for operation in matching
                    )
                    for outcome in OperationOutcome
                },
                "duration_ms_total": sum(durations),
                "duration_ms_max": max(durations),
            }
        )
    return summaries
