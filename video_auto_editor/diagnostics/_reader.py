"""运行诊断包的严格、只读完整性边界。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from ipaddress import IPv6Address
from typing import Any, Final
from urllib.parse import urlsplit

from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ErrorCode,
    ErrorModule,
    ExitCode,
    RemoteRequestId,
    RunError,
    RunStage,
    get_error_definition,
)
from video_auto_editor.runtime.identity import (
    ErrorId,
    OperationId,
    RunId,
    ShortVideoId,
)

from ._model import (
    ArtifactRole,
    CacheNamespace,
    CacheOutcome,
    CertifiedPlatform,
    DeliveryBuildState,
    DeliveryVerificationState,
    DiagnosticPackageSnapshot,
    ExternalDataCategory,
    ExternalRequestOutcome,
    InterruptionSignal,
    OperationKind,
    OperationOutcome,
    PreflightOutcome,
    ProviderCapability,
    ProviderTransport,
    PublicationState,
    RecoveredNoticeKind,
    ResultKind,
    RetryKind,
    RunTerminalState,
    StageOutcome,
    ZeroRequestReason,
)


class DiagnosticPackageReadState(str, Enum):
    """诊断包读取后的闭合完整性状态。"""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"


class DiagnosticPackageReadReason(str, Enum):
    """不携带文件内容或解析异常文本的闭合读取原因。"""

    VALID = "valid"
    EVENT_LOG_MISSING = "event_log_missing"
    EVENT_TAIL_TRUNCATED = "event_tail_truncated"
    TERMINAL_EVENT_MISSING = "terminal_event_missing"
    MANIFEST_MISSING = "manifest_missing"
    EVENT_ENCODING_INVALID = "event_encoding_invalid"
    EVENT_JSON_INVALID = "event_json_invalid"
    EVENT_DUPLICATE_FIELD = "event_duplicate_field"
    EVENT_NON_FINITE_NUMBER = "event_non_finite_number"
    EVENT_SCHEMA_INVALID = "event_schema_invalid"
    EVENT_CODE_UNKNOWN = "event_code_unknown"
    EVENT_FIELD_UNKNOWN = "event_field_unknown"
    EVENT_SEQUENCE_MISMATCH = "event_sequence_mismatch"
    EVENT_RUN_ID_MISMATCH = "event_run_id_mismatch"
    MANIFEST_ENCODING_INVALID = "manifest_encoding_invalid"
    MANIFEST_JSON_INVALID = "manifest_json_invalid"
    MANIFEST_DUPLICATE_FIELD = "manifest_duplicate_field"
    MANIFEST_NON_FINITE_NUMBER = "manifest_non_finite_number"
    MANIFEST_SCHEMA_INVALID = "manifest_schema_invalid"
    MANIFEST_FIELD_UNKNOWN = "manifest_field_unknown"
    MANIFEST_RUN_ID_MISMATCH = "manifest_run_id_mismatch"
    MANIFEST_EVENT_COUNT_MISMATCH = "manifest_event_count_mismatch"
    MANIFEST_EVENT_BYTE_LENGTH_MISMATCH = (
        "manifest_event_byte_length_mismatch"
    )
    MANIFEST_EVENT_DIGEST_MISMATCH = "manifest_event_digest_mismatch"


@dataclass(frozen=True, slots=True)
class DiagnosticPackageReadResult:
    """仅披露已验证标识和聚合数量的诊断包读取结果。"""

    state: DiagnosticPackageReadState
    reason: DiagnosticPackageReadReason
    run_id: RunId | None
    event_count: int


@dataclass(frozen=True, slots=True)
class _EventSchema:
    required_attributes: frozenset[str]
    optional_attributes: frozenset[str] = frozenset()
    operation: str = "forbidden"
    parent_operation_allowed: bool = False


_BASE_EVENT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "timestamp",
        "sequence",
        "run_id",
        "level",
        "event_code",
        "stage",
        "module",
        "message",
        "attributes",
    }
)
_OPTIONAL_EVENT_FIELDS: Final = frozenset(
    {"operation_id", "parent_operation_id"}
)
_EVENT_SCHEMAS: Final = {
    "run.initialized": _EventSchema(
        frozenset({"application_version", "release"})
    ),
    "run.completed": _EventSchema(
        frozenset(
            {"duration_ms", "exit_code", "outcome", "result_kind"}
        )
    ),
    "stage.started": _EventSchema(frozenset()),
    "stage.completed": _EventSchema(
        frozenset({"duration_ms", "outcome", "work_item_count"})
    ),
    "operation.started": _EventSchema(
        frozenset({"item_count", "item_index", "operation_kind"}),
        operation="required",
        parent_operation_allowed=True,
    ),
    "operation.completed": _EventSchema(
        frozenset(
            {
                "attempt_count",
                "duration_ms",
                "operation_kind",
                "outcome",
            }
        ),
        operation="required",
        parent_operation_allowed=True,
    ),
    "retry.scheduled": _EventSchema(
        frozenset(
            {"backoff_ms", "next_attempt", "reason_code", "retry_kind"}
        ),
        operation="required",
        parent_operation_allowed=True,
    ),
    "error.recorded": _EventSchema(
        frozenset(
            {
                "category",
                "diagnostics",
                "error_code",
                "error_id",
                "operator_action",
                "retryable_in_new_run",
            }
        ),
        operation="optional",
    ),
    "configuration.observed": _EventSchema(
        frozenset(
            {
                "status",
                "configuration_fingerprint",
                "result_configuration",
                "runtime_policy",
                "course_context",
            }
        )
    ),
    "cache.observed": _EventSchema(
        frozenset({"duration_ms", "namespace", "outcome"}),
        frozenset(
            {
                "singleflight_wait_ms",
                "reason_code",
                "quarantine_digest_prefix",
                "error_code",
            }
        ),
        operation="required",
        parent_operation_allowed=True,
    ),
    "transcription.execution_observed": _EventSchema(
        frozenset({"recovery_count", "retry_count"})
    ),
    "source.observed": _EventSchema(
        frozenset(
            {
                "byte_length",
                "course_context_provided",
                "duration_ms",
            }
        )
    ),
    "environment.observed": _EventSchema(
        frozenset(
            {
                "certified_platform",
                "python_version",
                "application_version",
                "ffmpeg_version",
                "ffprobe_version",
                "font",
                "installation_fingerprint",
                "preflight_outcome",
            }
        )
    ),
    "external_service.selected": _EventSchema(
        frozenset(
            {
                "capability",
                "adapter_id",
                "provider_id",
                "model_id",
                "configuration_fingerprint",
                "endpoint",
                "allowed_data_categories",
                "transport",
            }
        )
    ),
    "external_service.zero_requests": _EventSchema(
        frozenset({"capability", "reason"})
    ),
    "external_request.completed": _EventSchema(
        frozenset(
            {
                "capability",
                "outcome",
                "attempt_count",
                "duration_ms",
                "remote_request_id",
                "token_usage",
            }
        ),
        operation="required",
        parent_operation_allowed=True,
    ),
    "delivery.state_changed": _EventSchema(
        frozenset({"phase", "state"})
    ),
    "artifact.created": _EventSchema(
        frozenset({"role", "relative_path"}),
        operation="optional",
        parent_operation_allowed=True,
    ),
    "artifact.verified": _EventSchema(
        frozenset({"role", "relative_path"}),
        operation="optional",
        parent_operation_allowed=True,
    ),
    "notice.recorded": _EventSchema(frozenset({"kind", "count"})),
    "interruption.requested": _EventSchema(
        frozenset({"signal"})
    ),
}
_MANIFEST_FIELDS: Final = frozenset(
    {
        "schema_version",
        "identity",
        "lifecycle",
        "source",
        "environment",
        "configuration",
        "stages",
        "operations",
        "retries_and_recovery",
        "cache",
        "external_services",
        "delivery",
        "notices",
        "errors",
        "event_log",
    }
)
_IDENTITY_FIELDS: Final = frozenset(
    {"run_id", "application_version", "release"}
)
_LIFECYCLE_FIELDS: Final = frozenset(
    {
        "started_at",
        "ended_at",
        "duration_ms",
        "outcome",
        "exit_code",
        "result_kind",
        "interruption",
    }
)
_EVENT_LOG_FIELDS: Final = frozenset(
    {"path", "event_count", "byte_length", "sha256"}
)
_CONFIGURATION_FIELDS: Final = frozenset(
    {
        "status",
        "configuration_fingerprint",
        "result_configuration",
        "runtime_policy",
        "course_context",
    }
)
_RESULT_CONFIGURATION_FIELDS: Final = frozenset(
    {
        "transcription_provider",
        "transcription_model",
        "text_model_provider",
        "topic_review",
        "subtitle_optimization",
        "clip_policy",
        "subtitle_style",
    }
)
_TEXT_GENERATION_FIELDS: Final = frozenset(
    {"model", "temperature", "reasoning_effort", "max_output_tokens"}
)
_CLIP_POLICY_FIELDS: Final = frozenset(
    {
        "min_duration_seconds",
        "target_duration_seconds",
        "max_duration_seconds",
        "max_clips",
        "publish_ready_threshold",
    }
)
_SUBTITLE_STYLE_FIELDS: Final = frozenset(
    {
        "font",
        "font_size",
        "outline",
        "margin_bottom",
        "max_chars_per_line",
        "max_lines",
    }
)
_RUNTIME_POLICY_FIELDS: Final = frozenset(
    {
        "transcription_timeout_seconds",
        "transcription_max_concurrency",
        "text_model_timeout_seconds",
        "text_model_max_concurrency",
        "delivery_build_concurrency",
    }
)
_COURSE_CONTEXT_FIELDS: Final = frozenset(
    {
        "provided",
        "attribution_provided",
        "priority_topic_count",
        "excluded_content_count",
    }
)
_OPERATION_SUMMARY_FIELDS: Final = frozenset(
    {
        "operation_kind",
        "count",
        "outcomes",
        "duration_ms_total",
        "duration_ms_max",
    }
)
_CACHE_STAT_FIELDS: Final = frozenset(
    {
        "queries",
        "hits",
        "misses",
        "corrupt_quarantined",
        "writes_published",
        "writes_already_present",
        "infrastructure_failures",
        "singleflight_wait_count",
        "singleflight_wait_ms_total",
    }
)
_DELIVERY_FIELDS: Final = frozenset(
    {"build_state", "verification_state", "publication_state"}
)
_ERRORS_FIELDS: Final = frozenset(
    {"primary_error", "associated_errors", "recovery_incomplete"}
)
_ERROR_FIELDS: Final = frozenset(
    {
        "error_id",
        "error_code",
        "category",
        "stage",
        "module",
        "operation",
        "event_sequence",
        "safe_message",
        "retryable_in_new_run",
        "operator_action",
        "diagnostics",
    }
)
_TIMESTAMP: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
_SHA256: Final = re.compile(r"sha256:[0-9a-f]{64}")
_APPLICATION_VERSION: Final = re.compile(
    r"[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_FONT_FAMILY: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}")
_MODEL_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_EVENT_MESSAGES: Final = {
    "run.initialized": "直播拆条运行已初始化。",
    "run.completed": "直播拆条运行已结束。",
    "stage.started": "直播拆条运行阶段已开始。",
    "stage.completed": "直播拆条运行阶段已完成。",
    "operation.started": "诊断操作已开始。",
    "retry.scheduled": "诊断操作已安排额外尝试。",
    "operation.completed": "诊断操作已完成。",
    "configuration.observed": "生效配置的脱敏投影已记录。",
    "cache.observed": "处理缓存操作事实已记录。",
    "transcription.execution_observed": (
        "语音识别的中性聚合执行事实已记录。"
    ),
    "source.observed": "素材的脱敏来源事实已记录。",
    "environment.observed": "认证环境的脱敏预检事实已记录。",
    "interruption.requested": "应用已接受受控中断请求。",
    "external_service.selected": "供应商外发计划的脱敏事实已记录。",
    "external_service.zero_requests": (
        "供应商能力本次运行未发生远程请求。"
    ),
    "external_request.completed": "外部请求的脱敏结果已记录。",
    "delivery.state_changed": "标准交付生命周期状态已更新。",
    "artifact.created": "标准交付文件角色已创建。",
    "artifact.verified": "标准交付文件角色已通过独立验证。",
    "notice.recorded": "本次运行的一项额外工作已恢复。",
}


class _DuplicateField(ValueError):
    pass


class _NonFiniteNumber(ValueError):
    pass


class _EncodingInvalid(ValueError):
    pass


class _JsonInvalid(ValueError):
    pass


class DiagnosticPackageReader:
    """逐行验证事件，并把缺失与损坏明确分开。"""

    @classmethod
    def read(
        cls,
        snapshot: DiagnosticPackageSnapshot,
    ) -> DiagnosticPackageReadResult:
        if not isinstance(snapshot, DiagnosticPackageSnapshot):
            raise TypeError("诊断包读取器必须接收 DiagnosticPackageSnapshot")
        if not isinstance(snapshot.events, bytes):
            raise TypeError("诊断事件快照必须是 bytes")
        if snapshot.manifest is not None and not isinstance(
            snapshot.manifest,
            bytes,
        ):
            raise TypeError("诊断清单快照必须是 bytes 或 None")
        if not snapshot.events:
            return _incomplete(
                DiagnosticPackageReadReason.EVENT_LOG_MISSING,
                run_id=None,
                event_count=0,
            )

        complete_lines, tail_truncated = _event_lines(snapshot.events)
        parsed_events: list[dict[str, Any]] = []
        expected_run_id: RunId | None = None
        for expected_sequence, line in enumerate(complete_lines, start=1):
            try:
                parsed = _load_strict_json(line)
            except _EncodingInvalid:
                return _corrupt(
                    DiagnosticPackageReadReason.EVENT_ENCODING_INVALID,
                    len(parsed_events),
                )
            except _DuplicateField:
                return _corrupt(
                    DiagnosticPackageReadReason.EVENT_DUPLICATE_FIELD,
                    len(parsed_events),
                )
            except _NonFiniteNumber:
                return _corrupt(
                    DiagnosticPackageReadReason.EVENT_NON_FINITE_NUMBER,
                    len(parsed_events),
                )
            except _JsonInvalid:
                return _corrupt(
                    DiagnosticPackageReadReason.EVENT_JSON_INVALID,
                    len(parsed_events),
                )
            try:
                validation = _validate_event(
                    parsed,
                    expected_sequence=expected_sequence,
                    expected_run_id=expected_run_id,
                )
            except Exception:
                return _corrupt(
                    DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID,
                    len(parsed_events),
                )
            if isinstance(validation, DiagnosticPackageReadReason):
                return _corrupt(validation, len(parsed_events))
            event, event_run_id = validation
            if expected_run_id is None:
                expected_run_id = event_run_id
            parsed_events.append(event)

        if tail_truncated:
            return _incomplete(
                DiagnosticPackageReadReason.EVENT_TAIL_TRUNCATED,
                run_id=expected_run_id,
                event_count=len(parsed_events),
            )
        if not parsed_events:
            return _incomplete(
                DiagnosticPackageReadReason.EVENT_LOG_MISSING,
                run_id=None,
                event_count=0,
            )
        terminal_indexes = [
            index
            for index, event in enumerate(parsed_events)
            if event["event_code"] == "run.completed"
        ]
        if not terminal_indexes:
            return _incomplete(
                DiagnosticPackageReadReason.TERMINAL_EVENT_MISSING,
                run_id=expected_run_id,
                event_count=len(parsed_events),
            )
        if terminal_indexes != [len(parsed_events) - 1]:
            return _corrupt(
                DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID,
                len(parsed_events),
            )
        if parsed_events[0]["event_code"] != "run.initialized":
            return _corrupt(
                DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID,
                len(parsed_events),
            )
        try:
            timeline_reason = _validate_event_timeline(parsed_events)
        except Exception:
            timeline_reason = (
                DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            )
        if timeline_reason is not None:
            return _corrupt(timeline_reason, len(parsed_events))
        if snapshot.manifest is None:
            return _incomplete(
                DiagnosticPackageReadReason.MANIFEST_MISSING,
                run_id=expected_run_id,
                event_count=len(parsed_events),
            )

        try:
            manifest = _load_strict_json(snapshot.manifest)
        except _EncodingInvalid:
            return _corrupt(
                DiagnosticPackageReadReason.MANIFEST_ENCODING_INVALID,
                len(parsed_events),
            )
        except _DuplicateField:
            return _corrupt(
                DiagnosticPackageReadReason.MANIFEST_DUPLICATE_FIELD,
                len(parsed_events),
            )
        except _NonFiniteNumber:
            return _corrupt(
                DiagnosticPackageReadReason.MANIFEST_NON_FINITE_NUMBER,
                len(parsed_events),
            )
        except _JsonInvalid:
            return _corrupt(
                DiagnosticPackageReadReason.MANIFEST_JSON_INVALID,
                len(parsed_events),
            )
        try:
            manifest_validation = _validate_manifest(
                manifest,
                run_id=expected_run_id,
                events=snapshot.events,
                event_count=len(parsed_events),
                parsed_events=parsed_events,
            )
        except Exception:
            manifest_validation = (
                DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
            )
        if manifest_validation is not None:
            return _corrupt(manifest_validation, len(parsed_events))
        return DiagnosticPackageReadResult(
            state=DiagnosticPackageReadState.COMPLETE,
            reason=DiagnosticPackageReadReason.VALID,
            run_id=expected_run_id,
            event_count=len(parsed_events),
        )


def _validate_event_timeline(
    events: list[dict[str, Any]],
) -> DiagnosticPackageReadReason | None:
    active_stage: dict[str, Any] | None = None
    completed_stages: set[str] = set()
    operations: dict[str, dict[str, Any]] = {}
    operation_attempts: dict[str, int] = {}
    active_operations: set[str] = set()
    transcription_execution_seen = False
    last_stage = RunStage.INITIALIZED.value
    for index, event in enumerate(events):
        code = event["event_code"]
        if code == "run.initialized":
            if (
                index != 0
                or event["stage"] != RunStage.INITIALIZED.value
                or event["module"] != ErrorModule.APPLICATION.value
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            continue
        if code == "run.completed":
            if (
                index != len(events) - 1
                or active_stage is not None
                or active_operations
                or event["module"] != ErrorModule.APPLICATION.value
                or (
                    event["attributes"]["outcome"]
                    != RunTerminalState.FAILED.value
                    and event["stage"] != last_stage
                )
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            continue
        if code == "stage.started":
            if (
                active_stage is not None
                or event["stage"] in completed_stages
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            active_stage = event
            last_stage = event["stage"]
            continue
        if code == "stage.completed":
            if (
                active_stage is None
                or event["stage"] != active_stage["stage"]
                or event["module"] != active_stage["module"]
                or active_operations
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            completed_stages.add(event["stage"])
            active_stage = None
            continue
        if active_stage is None or event["stage"] != active_stage["stage"]:
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
        if code == "transcription.execution_observed":
            if (
                transcription_execution_seen
                or event["stage"] != RunStage.TRANSCRIPTION.value
                or event["module"] != ErrorModule.TRANSCRIPTION.value
                or active_operations
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            transcription_execution_seen = True
            continue
        operation_id = event.get("operation_id")
        if code == "operation.started":
            if (
                (
                    transcription_execution_seen
                    and event["stage"] == RunStage.TRANSCRIPTION.value
                )
                or
                operation_id in operations
                or (
                    event.get("parent_operation_id") is not None
                    and event["parent_operation_id"]
                    not in active_operations
                )
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            operations[operation_id] = event
            operation_attempts[operation_id] = 1
            active_operations.add(operation_id)
            continue
        if code == "operation.completed":
            started = operations.get(operation_id)
            if (
                operation_id not in active_operations
                or started is None
                or event["stage"] != started["stage"]
                or event["module"] != started["module"]
                or event.get("parent_operation_id")
                != started.get("parent_operation_id")
                or event["attributes"]["operation_kind"]
                != started["attributes"]["operation_kind"]
                or event["attributes"]["attempt_count"]
                != operation_attempts.get(operation_id)
                or any(
                    operation.get("parent_operation_id") == operation_id
                    and child_id in active_operations
                    for child_id, operation in operations.items()
                )
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            active_operations.remove(operation_id)
            continue
        if operation_id is not None and operation_id not in active_operations:
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
        if (
            operation_id is not None
            and (
                event["stage"] != operations[operation_id]["stage"]
                or event["module"] != operations[operation_id]["module"]
            )
        ):
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
        if (
            operation_id is not None
            and _EVENT_SCHEMAS[code].parent_operation_allowed
            and event.get("parent_operation_id")
            != operations[operation_id].get("parent_operation_id")
        ):
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
        if code == "retry.scheduled":
            if (
                transcription_execution_seen
                and event["stage"] == RunStage.TRANSCRIPTION.value
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            next_attempt = event["attributes"]["next_attempt"]
            if next_attempt != operation_attempts[operation_id] + 1:
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            operation_attempts[operation_id] = next_attempt
        if operation_id is not None:
            operation_kind = operations[operation_id]["attributes"][
                "operation_kind"
            ]
            if (
                code == "external_request.completed"
                and operation_kind
                != OperationKind.EXTERNAL_REQUEST.value
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
            if (
                code == "cache.observed"
                and operation_kind
                not in {
                    OperationKind.CACHE_READ.value,
                    OperationKind.CACHE_WRITE.value,
                    OperationKind.CACHE_QUARANTINE.value,
                }
            ):
                return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if active_stage is not None or active_operations:
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    return None


def _event_lines(payload: bytes) -> tuple[list[bytes], bool]:
    parts = payload.split(b"\n")
    if parts[-1] == b"":
        return parts[:-1], False
    return parts[:-1], True


def _load_strict_json(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _EncodingInvalid from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateField:
        raise
    except _NonFiniteNumber:
        raise
    except (RecursionError, ValueError) as exc:
        raise _JsonInvalid from exc
    if not _finite_json_tree(parsed):
        raise _NonFiniteNumber
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise _DuplicateField
        parsed[key] = value
    return parsed


def _reject_non_finite(_value: str) -> None:
    raise _NonFiniteNumber


def _finite_json_tree(value: Any) -> bool:
    remaining = [value]
    while remaining:
        current = remaining.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
        elif isinstance(current, dict):
            remaining.extend(current.values())
        elif isinstance(current, list):
            remaining.extend(current)
        elif current is not None and not isinstance(
            current,
            (str, int, bool),
        ):
            return False
    return True


def _validate_event(
    value: Any,
    *,
    expected_sequence: int,
    expected_run_id: RunId | None,
) -> (
    tuple[dict[str, Any], RunId]
    | DiagnosticPackageReadReason
):
    if not isinstance(value, dict):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    fields = frozenset(value)
    allowed = _BASE_EVENT_FIELDS | _OPTIONAL_EVENT_FIELDS
    if fields.difference(allowed):
        return DiagnosticPackageReadReason.EVENT_FIELD_UNKNOWN
    if not _BASE_EVENT_FIELDS.issubset(fields):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if value["schema_version"] != "run_event.v1":
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if (
        not isinstance(value["timestamp"], str)
        or not _valid_timestamp(value["timestamp"])
    ):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    sequence = value["sequence"]
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence != expected_sequence
    ):
        return DiagnosticPackageReadReason.EVENT_SEQUENCE_MISMATCH
    try:
        event_run_id = RunId(value["run_id"])
    except (TypeError, ValueError):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if expected_run_id is not None and event_run_id != expected_run_id:
        return DiagnosticPackageReadReason.EVENT_RUN_ID_MISMATCH
    if (
        not isinstance(value["level"], str)
        or value["level"] not in {"info", "warning", "error"}
    ):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    try:
        RunStage(value["stage"])
        ErrorModule(value["module"])
    except (TypeError, ValueError):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if not _nonempty_string(value["message"]):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    event_code = value["event_code"]
    if not isinstance(event_code, str) or event_code not in _EVENT_SCHEMAS:
        return DiagnosticPackageReadReason.EVENT_CODE_UNKNOWN
    attributes = value["attributes"]
    if not isinstance(attributes, dict) or _contains_null(attributes):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    schema = _EVENT_SCHEMAS[event_code]
    attribute_fields = frozenset(attributes)
    allowed_attributes = (
        schema.required_attributes | schema.optional_attributes
    )
    if attribute_fields.difference(allowed_attributes):
        return DiagnosticPackageReadReason.EVENT_FIELD_UNKNOWN
    if not schema.required_attributes.issubset(attribute_fields):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    operation_id = value.get("operation_id")
    if schema.operation == "required" and operation_id is None:
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if schema.operation == "forbidden" and operation_id is not None:
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if operation_id is not None:
        try:
            OperationId(operation_id)
        except (TypeError, ValueError):
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    parent_operation_id = value.get("parent_operation_id")
    if parent_operation_id is not None:
        if (
            not schema.parent_operation_allowed
            or operation_id is None
        ):
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
        try:
            OperationId(parent_operation_id)
        except (TypeError, ValueError):
            return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if not _validate_event_attributes(event_code, attributes):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if not _valid_event_level_and_message(value):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    if event_code == "error.recorded" and not _valid_error_event(value):
        return DiagnosticPackageReadReason.EVENT_SCHEMA_INVALID
    return value, event_run_id


def _validate_event_attributes(
    event_code: str,
    attributes: dict[str, Any],
) -> bool:
    if event_code == "run.initialized":
        return (
            isinstance(attributes["application_version"], str)
            and _APPLICATION_VERSION.fullmatch(
                attributes["application_version"]
            )
            is not None
            and attributes["release"] == {"status": "unknown"}
        )
    if event_code == "run.completed":
        return (
            _nonnegative_int(attributes["duration_ms"])
            and _enum_value(attributes["exit_code"], ExitCode)
            and _enum_value(
                attributes["outcome"],
                RunTerminalState,
            )
            and _valid_result_kind(
                attributes["result_kind"],
                outcome=attributes["outcome"],
            )
        )
    if event_code == "stage.started":
        return not attributes
    if event_code == "stage.completed":
        return (
            _nonnegative_int(attributes["duration_ms"])
            and attributes["outcome"]
            in {"succeeded", "failed", "interrupted"}
            and _nonnegative_int(attributes["work_item_count"])
        )
    if event_code == "operation.started":
        return (
            _positive_int(attributes["item_count"])
            and _positive_int(attributes["item_index"])
            and attributes["item_index"] <= attributes["item_count"]
            and _enum_value(attributes["operation_kind"], OperationKind)
        )
    if event_code == "operation.completed":
        return (
            _positive_int(attributes["attempt_count"])
            and _nonnegative_int(attributes["duration_ms"])
            and _enum_value(attributes["operation_kind"], OperationKind)
            and _enum_value(attributes["outcome"], OperationOutcome)
        )
    if event_code == "retry.scheduled":
        return (
            _nonnegative_int(attributes["backoff_ms"])
            and _positive_int(attributes["next_attempt"])
            and attributes["next_attempt"] >= 2
            and _stable_string(attributes["reason_code"])
            and _enum_value(attributes["retry_kind"], RetryKind)
        )
    if event_code == "error.recorded":
        try:
            definition = get_error_definition(
                ErrorCode(attributes["error_code"])
            )
        except (TypeError, ValueError, KeyError):
            return False
        return (
            attributes["category"] == definition.category.value
            and isinstance(attributes["diagnostics"], dict)
            and _uuid4_string(attributes["error_id"])
            and attributes["operator_action"]
            == definition.operator_action.value
            and attributes["retryable_in_new_run"]
            is definition.retryable_in_new_run
        )
    if event_code == "configuration.observed":
        return _validate_configuration_manifest(attributes) is None
    if event_code == "cache.observed":
        basic_valid = (
            _nonnegative_int(attributes["duration_ms"])
            and _enum_value(attributes["namespace"], CacheNamespace)
            and _enum_value(attributes["outcome"], CacheOutcome)
            and (
                "singleflight_wait_ms" not in attributes
                or _nonnegative_int(attributes["singleflight_wait_ms"])
            )
            and (
                "reason_code" not in attributes
                or _stable_string(attributes["reason_code"])
            )
            and (
                "quarantine_digest_prefix" not in attributes
                or (
                    isinstance(
                        attributes["quarantine_digest_prefix"],
                        str,
                    )
                    and re.fullmatch(
                        r"sha256:[0-9a-f]{8,16}",
                        attributes["quarantine_digest_prefix"],
                    )
                    is not None
                )
            )
            and (
                "error_code" not in attributes
                or attributes["error_code"]
                == ErrorCode.CACHE_INFRASTRUCTURE_FAILED.value
            )
        )
        if not basic_valid:
            return False
        outcome = CacheOutcome(attributes["outcome"])
        fault_fields = {
            "reason_code",
            "quarantine_digest_prefix",
            "error_code",
        }.intersection(attributes)
        if outcome is CacheOutcome.CORRUPT_QUARANTINED:
            return (
                fault_fields
                == {"reason_code", "quarantine_digest_prefix"}
            )
        if outcome is CacheOutcome.INFRASTRUCTURE_FAILED:
            return (
                "error_code" in fault_fields
                and "quarantine_digest_prefix" not in fault_fields
            )
        return not fault_fields
    return _validate_future_event_attributes(event_code, attributes)


def _valid_event_level_and_message(event: dict[str, Any]) -> bool:
    code = event["event_code"]
    attributes = event["attributes"]
    if code == "error.recorded":
        expected_level = "error"
    elif code in {
        "retry.scheduled",
        "interruption.requested",
        "notice.recorded",
    }:
        expected_level = "warning"
    elif code == "cache.observed":
        outcome = attributes["outcome"]
        expected_level = (
            "error"
            if outcome == CacheOutcome.INFRASTRUCTURE_FAILED.value
            else (
                "warning"
                if outcome == CacheOutcome.CORRUPT_QUARANTINED.value
                else "info"
            )
        )
    elif code == "environment.observed":
        expected_level = (
            "info"
            if attributes["preflight_outcome"]
            == PreflightOutcome.SUCCEEDED.value
            else "error"
        )
    elif code == "external_request.completed":
        expected_level = (
            "info"
            if attributes["outcome"]
            == ExternalRequestOutcome.SUCCEEDED.value
            else "error"
        )
    elif code == "delivery.state_changed":
        state = attributes["state"]
        expected_level = (
            "error"
            if state == "failed"
            else (
                "warning"
                if state in {"interrupted", "rolled_back"}
                else "info"
            )
        )
    else:
        expected_level = "info"
    if event["level"] != expected_level:
        return False
    expected_message = _EVENT_MESSAGES.get(code)
    return (
        expected_message is None
        or event["message"] == expected_message
    )


def _valid_error_event(event: dict[str, Any]) -> bool:
    attributes = event["attributes"]
    try:
        ErrorId(attributes["error_id"])
        operation_id = (
            None
            if "operation_id" not in event
            else OperationId(event["operation_id"])
        )
        reconstructed = RunError.create(
            code=attributes["error_code"],
            stage=event["stage"],
            module=event["module"],
            operation_id=operation_id,
            event_sequence=event["sequence"],
            diagnostics=_rehydrate_error_diagnostics(
                attributes["diagnostics"]
            ),
        )
    except (TypeError, ValueError, KeyError):
        return False
    return (
        attributes["category"] == reconstructed.category.value
        and attributes["operator_action"]
        == reconstructed.operator_action.value
        and attributes["retryable_in_new_run"]
        is reconstructed.retryable_in_new_run
        and event["message"] == reconstructed.safe_message
    )


def _validate_future_event_attributes(
    event_code: str,
    attributes: dict[str, Any],
) -> bool:
    if event_code == "source.observed":
        return (
            _nonnegative_int(attributes["byte_length"])
            and _nonnegative_int(attributes["duration_ms"])
            and isinstance(
                attributes["course_context_provided"],
                bool,
            )
        )
    if event_code == "transcription.execution_observed":
        return (
            _nonnegative_int(attributes["recovery_count"])
            and _nonnegative_int(attributes["retry_count"])
        )
    if event_code == "environment.observed":
        font = attributes["font"]
        return (
            _enum_value(
                attributes["certified_platform"],
                CertifiedPlatform,
            )
            and all(
                _valid_detected_version(attributes[field])
                for field in {
                    "python_version",
                    "ffmpeg_version",
                    "ffprobe_version",
                }
            )
            and isinstance(attributes["application_version"], str)
            and _APPLICATION_VERSION.fullmatch(
                attributes["application_version"]
            )
            is not None
            and isinstance(font, dict)
            and frozenset(font) == {"family", "available"}
            and isinstance(font["family"], str)
            and _FONT_FAMILY.fullmatch(font["family"]) is not None
            and isinstance(font["available"], bool)
            and _sha256(attributes["installation_fingerprint"])
            and _enum_value(
                attributes["preflight_outcome"],
                PreflightOutcome,
            )
        )
    if event_code == "external_service.selected":
        return (
            _enum_value(attributes["capability"], ProviderCapability)
            and all(
                _stable_identifier(attributes[field])
                for field in {"adapter_id", "provider_id", "model_id"}
            )
            and _sha256(attributes["configuration_fingerprint"])
            and _enum_value(attributes["transport"], ProviderTransport)
            and _valid_endpoint_status(
                attributes["endpoint"],
                transport=ProviderTransport(attributes["transport"]),
            )
            and _valid_external_categories(
                capability=ProviderCapability(
                    attributes["capability"]
                ),
                transport=ProviderTransport(attributes["transport"]),
                categories=attributes["allowed_data_categories"],
            )
        )
    if event_code == "external_service.zero_requests":
        return (
            _enum_value(attributes["capability"], ProviderCapability)
            and _enum_value(attributes["reason"], ZeroRequestReason)
        )
    if event_code == "external_request.completed":
        return (
            _enum_value(attributes["capability"], ProviderCapability)
            and _enum_value(
                attributes["outcome"],
                ExternalRequestOutcome,
            )
            and _positive_int(attributes["attempt_count"])
            and _nonnegative_int(attributes["duration_ms"])
            and _valid_remote_request_id_status(
                attributes["remote_request_id"]
            )
            and _valid_token_usage_status(
                attributes["token_usage"]
            )
        )
    if event_code == "delivery.state_changed":
        phase = attributes["phase"]
        state = attributes["state"]
        return (
            (phase == "build" and _enum_value(state, DeliveryBuildState))
            or (
                phase == "verification"
                and _enum_value(state, DeliveryVerificationState)
            )
            or (
                phase == "publication"
                and _enum_value(state, PublicationState)
            )
        )
    if event_code in {"artifact.created", "artifact.verified"}:
        try:
            role = ArtifactRole(attributes["role"])
        except (TypeError, ValueError):
            return False
        return _valid_artifact_path(
            role,
            attributes["relative_path"],
        )
    if event_code == "notice.recorded":
        return (
            _enum_value(attributes["kind"], RecoveredNoticeKind)
            and _positive_int(attributes["count"])
        )
    if event_code == "interruption.requested":
        return _enum_value(attributes["signal"], InterruptionSignal)
    return False


def _validate_manifest(
    value: Any,
    *,
    run_id: RunId | None,
    events: bytes,
    event_count: int,
    parsed_events: list[dict[str, Any]],
) -> DiagnosticPackageReadReason | None:
    if not isinstance(value, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    fields = frozenset(value)
    if fields.difference(_MANIFEST_FIELDS):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    if fields != _MANIFEST_FIELDS:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if value["schema_version"] != "run_manifest.v1":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if _contains_null(value):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    identity = value["identity"]
    lifecycle = value["lifecycle"]
    event_log = value["event_log"]
    terminal_event = parsed_events[-1]
    if not isinstance(identity, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if frozenset(identity).difference(_IDENTITY_FIELDS):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    if frozenset(identity) != _IDENTITY_FIELDS:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    try:
        manifest_run_id = RunId(identity["run_id"])
    except (TypeError, ValueError):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if run_id is None or manifest_run_id != run_id:
        return DiagnosticPackageReadReason.MANIFEST_RUN_ID_MISMATCH
    if (
        not isinstance(identity["application_version"], str)
        or _APPLICATION_VERSION.fullmatch(
            identity["application_version"]
        )
        is None
        or identity["release"] != {"status": "unknown"}
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not isinstance(lifecycle, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if frozenset(lifecycle).difference(_LIFECYCLE_FIELDS):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    if frozenset(lifecycle) != _LIFECYCLE_FIELDS:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not _valid_lifecycle(lifecycle, terminal_event):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    for validator, section in (
        (_validate_source_manifest, value["source"]),
        (_validate_environment_manifest, value["environment"]),
        (_validate_configuration_manifest, value["configuration"]),
        (_validate_stages_manifest, value["stages"]),
        (_validate_operations_manifest, value["operations"]),
        (
            _validate_retries_manifest,
            value["retries_and_recovery"],
        ),
        (_validate_cache_manifest, value["cache"]),
        (
            _validate_external_services_manifest,
            value["external_services"],
        ),
        (_validate_delivery_manifest, value["delivery"]),
        (_validate_notices_manifest, value["notices"]),
        (_validate_errors_manifest, value["errors"]),
    ):
        section_reason = validator(section)
        if section_reason is not None:
            return section_reason
    if not _errors_match_lifecycle(value["errors"], lifecycle):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not isinstance(event_log, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if frozenset(event_log).difference(_EVENT_LOG_FIELDS):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    if frozenset(event_log) != _EVENT_LOG_FIELDS:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if event_log["path"] != "events.jsonl":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if (
        not _positive_int(event_log["event_count"])
        or not _positive_int(event_log["byte_length"])
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if event_log["event_count"] != event_count:
        return DiagnosticPackageReadReason.MANIFEST_EVENT_COUNT_MISMATCH
    if event_log["byte_length"] != len(events):
        return (
            DiagnosticPackageReadReason.MANIFEST_EVENT_BYTE_LENGTH_MISMATCH
        )
    expected_digest = "sha256:" + hashlib.sha256(events).hexdigest()
    if (
        not isinstance(event_log["sha256"], str)
        or _SHA256.fullmatch(event_log["sha256"]) is None
        or event_log["sha256"] != expected_digest
    ):
        return DiagnosticPackageReadReason.MANIFEST_EVENT_DIGEST_MISMATCH
    if not _manifest_matches_events(value, parsed_events):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _manifest_matches_events(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    initialized = events[0]
    terminal = events[-1]
    initialized_attributes = initialized["attributes"]
    lifecycle = manifest["lifecycle"]
    if (
        manifest["identity"]["application_version"]
        != initialized_attributes["application_version"]
        or manifest["identity"]["release"]
        != initialized_attributes["release"]
        or lifecycle["started_at"] != initialized["timestamp"]
        or lifecycle["ended_at"] != terminal["timestamp"]
        or not _interruption_matches_events(lifecycle, events)
        or (
            lifecycle["outcome"] == RunTerminalState.FAILED.value
            and terminal["stage"]
            != manifest["errors"]["primary_error"]["stage"]
        )
    ):
        return False
    expected_stages = _stages_from_events(events)
    expected_operations = _operations_from_events(events)
    expected_retries = _retries_from_events(events)
    expected_cache = _cache_from_events(events)
    expected_external_services = _external_services_from_events(events)
    expected_delivery = _delivery_from_events(lifecycle["outcome"], events)
    if (
        expected_stages is None
        or expected_operations is None
        or expected_retries is None
        or expected_cache is None
        or expected_external_services is None
        or expected_delivery is None
    ):
        return False
    return (
        _source_matches_events(manifest["source"], events)
        and _environment_matches_events(
            manifest["environment"],
            events,
        )
        and _configuration_matches_events(
            manifest["configuration"],
            events,
        )
        and manifest["stages"] == expected_stages
        and manifest["operations"] == expected_operations
        and manifest["retries_and_recovery"] == expected_retries
        and manifest["cache"] == expected_cache
        and manifest["external_services"] == expected_external_services
        and manifest["delivery"] == expected_delivery
        and manifest["notices"] == _notices_from_events(events)
        and _notices_supported_by_events(events)
        and _errors_match_events(
            manifest["errors"],
            lifecycle["outcome"],
            events,
        )
    )


def _events_with_code(
    events: list[dict[str, Any]],
    event_code: str,
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event["event_code"] == event_code
    ]


def _interruption_matches_events(
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    interruption_events = _events_with_code(
        events,
        "interruption.requested",
    )
    if len(interruption_events) > 1:
        return False
    if lifecycle["outcome"] != RunTerminalState.INTERRUPTED.value:
        return True
    if len(interruption_events) != 1:
        return False
    event = interruption_events[0]
    return (
        lifecycle["interruption"]["signal"]
        == event["attributes"]["signal"]
        and lifecycle["interruption"]["received_at"]
        == event["timestamp"]
    )


def _source_matches_events(
    manifest_source: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    source_events = _events_with_code(events, "source.observed")
    if not source_events:
        return manifest_source == {"status": "not_observed"}
    if len(source_events) != 1 or manifest_source["status"] != "available":
        return False
    attributes = source_events[0]["attributes"]
    return (
        manifest_source["byte_length"] == attributes["byte_length"]
        and manifest_source["duration_ms"] == attributes["duration_ms"]
        and manifest_source["course_context"]["provided"]
        is attributes["course_context_provided"]
    )


def _environment_matches_events(
    manifest_environment: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    environment_events = _events_with_code(
        events,
        "environment.observed",
    )
    if not environment_events:
        return manifest_environment == {"status": "not_observed"}
    if len(environment_events) != 1:
        return False
    return manifest_environment == {
        "status": "available",
        **environment_events[0]["attributes"],
    }


def _configuration_matches_events(
    manifest_configuration: dict[str, Any],
    events: list[dict[str, Any]],
) -> bool:
    configuration_events = _events_with_code(
        events,
        "configuration.observed",
    )
    if not configuration_events:
        return manifest_configuration == {"status": "not_observed"}
    return (
        len(configuration_events) == 1
        and manifest_configuration
        == configuration_events[0]["attributes"]
    )


def _stages_from_events(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    completed: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_code"] != "stage.completed":
            continue
        if event["stage"] in completed:
            return None
        attributes = event["attributes"]
        completed[event["stage"]] = {
            "status": attributes["outcome"],
            "duration_ms": attributes["duration_ms"],
            "work_item_count": attributes["work_item_count"],
        }
    return {
        stage.value: completed.get(
            stage.value,
            {"status": "not_started"},
        )
        for stage in RunStage
    }


def _operations_from_events(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]] | None:
    completed = _events_with_code(events, "operation.completed")
    summaries: list[dict[str, Any]] = []
    for kind in OperationKind:
        matching = [
            event
            for event in completed
            if event["attributes"]["operation_kind"] == kind.value
        ]
        if not matching:
            continue
        durations = [
            event["attributes"]["duration_ms"]
            for event in matching
        ]
        summaries.append(
            {
                "operation_kind": kind.value,
                "count": len(matching),
                "outcomes": {
                    outcome.value: sum(
                        event["attributes"]["outcome"]
                        == outcome.value
                        for event in matching
                    )
                    for outcome in OperationOutcome
                },
                "duration_ms_total": sum(durations),
                "duration_ms_max": max(durations),
            }
        )
    return {"summaries": summaries}


def _retries_from_events(
    events: list[dict[str, Any]],
) -> dict[str, int] | None:
    retry_events = _events_with_code(events, "retry.scheduled")
    retry_counts = {
        kind.value: sum(
            event["attributes"]["retry_kind"] == kind.value
            for event in retry_events
        )
        for kind in RetryKind
    }
    execution_events = _events_with_code(
        events,
        "transcription.execution_observed",
    )
    if len(execution_events) > 1:
        return None
    if not execution_events:
        if _whole_transcript_hit_conflicts_with_retries(
            events,
            retry_events,
        ):
            return None
        return retry_counts
    execution = execution_events[0]["attributes"]
    if (
        execution["retry_count"] > 0
        or execution["recovery_count"] > 0
    ) and _whole_transcript_cache_hit(events):
        return None
    if _whole_transcript_hit_conflicts_with_retries(
        events,
        retry_events,
    ):
        return None
    aggregate_counts = {
        RetryKind.TRANSPORT_RETRY.value: execution["retry_count"],
        RetryKind.COVERAGE_RECOVERY.value: execution["recovery_count"],
    }
    for kind, reported_count in aggregate_counts.items():
        observed_count = sum(
            event["stage"] == RunStage.TRANSCRIPTION.value
            and event["attributes"]["retry_kind"] == kind
            for event in retry_events
        )
        if reported_count < observed_count:
            return None
        retry_counts[kind] += reported_count - observed_count
    return retry_counts


def _whole_transcript_cache_hit(
    events: list[dict[str, Any]],
) -> bool:
    return any(
        event["stage"] == RunStage.TRANSCRIPTION.value
        and event["attributes"]["namespace"]
        == CacheNamespace.TRANSCRIPT.value
        and event["attributes"]["outcome"] == CacheOutcome.HIT.value
        for event in _events_with_code(events, "cache.observed")
    )


def _whole_transcript_hit_conflicts_with_retries(
    events: list[dict[str, Any]],
    retry_events: list[dict[str, Any]],
) -> bool:
    return _whole_transcript_cache_hit(events) and any(
        event["stage"] == RunStage.TRANSCRIPTION.value
        for event in retry_events
    )


def _cache_from_events(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    cache_events = _events_with_code(events, "cache.observed")
    if not cache_events:
        return {"status": "not_observed"}
    stats_by_namespace: dict[str, dict[str, int]] = {}
    for event in cache_events:
        attributes = event["attributes"]
        namespace = attributes["namespace"]
        stats = stats_by_namespace.setdefault(
            namespace,
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
        outcome = attributes["outcome"]
        if outcome in {
            CacheOutcome.HIT.value,
            CacheOutcome.MISS.value,
            CacheOutcome.CORRUPT_QUARANTINED.value,
        }:
            stats["queries"] += 1
        field_by_outcome = {
            CacheOutcome.HIT.value: "hits",
            CacheOutcome.MISS.value: "misses",
            CacheOutcome.CORRUPT_QUARANTINED.value: (
                "corrupt_quarantined"
            ),
            CacheOutcome.WRITE_PUBLISHED.value: "writes_published",
            CacheOutcome.WRITE_ALREADY_PRESENT.value: (
                "writes_already_present"
            ),
            CacheOutcome.INFRASTRUCTURE_FAILED.value: (
                "infrastructure_failures"
            ),
        }
        stats[field_by_outcome[outcome]] += 1
        if "singleflight_wait_ms" in attributes:
            stats["singleflight_wait_count"] += 1
            stats["singleflight_wait_ms_total"] += attributes[
                "singleflight_wait_ms"
            ]
    return {
        "status": "observed",
        "namespaces": {
            namespace.value: stats_by_namespace[namespace.value]
            for namespace in CacheNamespace
            if namespace.value in stats_by_namespace
        },
    }


def _external_services_from_events(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    selected_events = _events_with_code(
        events,
        "external_service.selected",
    )
    if not selected_events:
        if any(
            event["event_code"]
            in {
                "external_service.zero_requests",
                "external_request.completed",
            }
            for event in events
        ):
            return None
        return {"status": "not_observed"}
    state_by_capability: dict[str, dict[str, Any]] = {}
    for event in selected_events:
        attributes = event["attributes"]
        capability = attributes["capability"]
        if capability in state_by_capability:
            return None
        state_by_capability[capability] = {
            "selection": attributes,
            "requests": [],
            "zero_reason": (
                ZeroRequestReason.DETERMINISTIC_ADAPTER.value
                if attributes["transport"]
                == ProviderTransport.LOCAL.value
                else None
            ),
        }
    for event in events:
        event_code = event["event_code"]
        if event_code not in {
            "external_service.zero_requests",
            "external_request.completed",
        }:
            continue
        attributes = event["attributes"]
        capability = attributes["capability"]
        state = state_by_capability.get(capability)
        if state is None:
            return None
        if event_code == "external_service.zero_requests":
            if state["requests"] or state["zero_reason"] is not None:
                return None
            state["zero_reason"] = attributes["reason"]
            continue
        selection = state["selection"]
        if (
            selection["transport"] != ProviderTransport.REMOTE.value
            or state["zero_reason"] is not None
        ):
            return None
        state["requests"].append(attributes)
    services: list[dict[str, Any]] = []
    for capability in ProviderCapability:
        state = state_by_capability.get(capability.value)
        if state is None:
            continue
        selection = state["selection"]
        requests = state["requests"]
        zero_reason = state["zero_reason"]
        if not requests and zero_reason is None:
            return None
        reported = [
            request
            for request in requests
            if request["token_usage"]["status"] == "reported"
        ]
        if not requests:
            token_usage: dict[str, Any] = {
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
                    request["token_usage"]["input_tokens"]
                    for request in reported
                ),
                "output_tokens": sum(
                    request["token_usage"]["output_tokens"]
                    for request in reported
                ),
            }
            if len(reported) != len(requests):
                token_usage["reported_request_count"] = len(reported)
        durations = [
            request["duration_ms"]
            for request in requests
        ]
        services.append(
            {
                "capability": capability.value,
                "adapter_id": selection["adapter_id"],
                "provider_id": selection["provider_id"],
                "model_id": selection["model_id"],
                "configuration_fingerprint": selection[
                    "configuration_fingerprint"
                ],
                "endpoint": selection["endpoint"],
                "transport": selection["transport"],
                "purpose": capability.value,
                "allowed_data_categories": selection[
                    "allowed_data_categories"
                ],
                "contact": (
                    {"status": "contacted"}
                    if requests
                    else {
                        "status": "not_contacted",
                        "reason": zero_reason,
                    }
                ),
                "requests": {
                    "count": len(requests),
                    "succeeded": sum(
                        request["outcome"]
                        == ExternalRequestOutcome.SUCCEEDED.value
                        for request in requests
                    ),
                    "failed": sum(
                        request["outcome"]
                        == ExternalRequestOutcome.FAILED.value
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
    return {"status": "observed", "services": services}


def _delivery_from_events(
    outcome: str,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    states = {
        "build": "not_started",
        "verification": "not_started",
        "publication": "not_started",
    }
    allowed_transitions = {
        "build": {
            "not_started": {"in_progress"},
            "in_progress": {"completed", "failed", "interrupted"},
        },
        "verification": {
            "not_started": {"in_progress"},
            "in_progress": {"passed", "failed", "interrupted"},
        },
        "publication": {
            "not_started": {"in_progress"},
            "in_progress": {"committed", "rolled_back", "failed"},
        },
    }
    created: dict[str, str] = {}
    verified: dict[str, str] = {}
    delivery_observed = False
    for event in events:
        event_code = event["event_code"]
        attributes = event["attributes"]
        if event_code == "delivery.state_changed":
            phase = attributes["phase"]
            state = attributes["state"]
            if state not in allowed_transitions[phase].get(
                states[phase],
                set(),
            ):
                return None
            if (
                phase == "verification"
                and states["build"] != "completed"
            ):
                return None
            if (
                phase == "publication"
                and states["verification"] != "passed"
            ):
                return None
            states[phase] = state
            delivery_observed = True
        elif event_code == "artifact.created":
            path = attributes["relative_path"]
            if states["build"] != "in_progress" or path in created:
                return None
            created[path] = attributes["role"]
        elif event_code == "artifact.verified":
            path = attributes["relative_path"]
            if (
                states["verification"] != "in_progress"
                or path in verified
                or created.get(path) != attributes["role"]
            ):
                return None
            verified[path] = attributes["role"]
    state_values = tuple(states.values())
    if "in_progress" in state_values:
        return None
    successful_states = ("completed", "passed", "committed")
    if outcome == RunTerminalState.SUCCEEDED.value:
        if delivery_observed and state_values != successful_states:
            return None
        delivery: dict[str, Any] = {
            "build_state": successful_states[0],
            "verification_state": successful_states[1],
            "publication_state": successful_states[2],
        }
    else:
        if states["publication"] == "committed":
            return None
        delivery = {
            "build_state": states["build"],
            "verification_state": states["verification"],
            "publication_state": states["publication"],
        }
    if created or verified:
        delivery["artifacts"] = {
            "status": "observed",
            "created_by_role": _artifact_counts(created),
            "verified_by_role": _artifact_counts(verified),
        }
    return delivery


def _artifact_counts(artifacts: dict[str, str]) -> dict[str, int]:
    return {
        role.value: sum(
            artifact_role == role.value
            for artifact_role in artifacts.values()
        )
        for role in ArtifactRole
        if role.value in artifacts.values()
    }


def _notices_from_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts = {
        kind.value: sum(
            event["attributes"]["count"]
            for event in events
            if (
                event["event_code"] == "notice.recorded"
                and event["attributes"]["kind"] == kind.value
            )
        )
        for kind in RecoveredNoticeKind
    }
    return [
        {"kind": kind.value, "count": counts[kind.value]}
        for kind in RecoveredNoticeKind
        if counts[kind.value]
    ]


def _notices_supported_by_events(
    events: list[dict[str, Any]],
) -> bool:
    notices = {
        notice["kind"]: notice["count"]
        for notice in _notices_from_events(events)
    }
    retry_counts = _retries_from_events(events)
    if retry_counts is None:
        return False
    retry_support = {
        RecoveredNoticeKind.TRANSPORT_RETRY_SUCCEEDED.value: (
            RetryKind.TRANSPORT_RETRY.value
        ),
        RecoveredNoticeKind.SEMANTIC_RETRY_SUCCEEDED.value: (
            RetryKind.SEMANTIC_RETRY.value
        ),
        RecoveredNoticeKind.COVERAGE_RECOVERY_SUCCEEDED.value: (
            RetryKind.COVERAGE_RECOVERY.value
        ),
    }
    if any(
        notices.get(notice_kind, 0)
        > retry_counts[retry_kind]
        for notice_kind, retry_kind in retry_support.items()
    ):
        return False
    corrupt_count = sum(
        event["attributes"]["outcome"]
        == CacheOutcome.CORRUPT_QUARANTINED.value
        for event in events
        if event["event_code"] == "cache.observed"
    )
    return (
        notices.get(
            RecoveredNoticeKind.CACHE_CORRUPTION_RECOVERED.value,
            0,
        )
        <= corrupt_count
    )


def _errors_match_events(
    manifest_errors: dict[str, Any],
    outcome: str,
    events: list[dict[str, Any]],
) -> bool:
    error_events = _events_with_code(events, "error.recorded")
    projected_by_id = {
        projection["error_id"]: projection
        for projection in (
            _error_from_event(event)
            for event in error_events
        )
    }
    if len(projected_by_id) != len(error_events):
        return False
    primary = manifest_errors["primary_error"]
    associated = manifest_errors["associated_errors"]
    referenced = (
        associated
        if _is_not_applicable(primary)
        else [primary, *associated]
    )
    if any(
        projected_by_id.get(error["error_id"]) != error
        for error in referenced
    ):
        return False
    if outcome == RunTerminalState.SUCCEEDED.value:
        return (
            _is_not_applicable(primary)
            and not associated
            and not manifest_errors["recovery_incomplete"]
        )
    if outcome == RunTerminalState.INTERRUPTED.value:
        return (
            _is_not_applicable(primary)
            and bool(associated)
            is manifest_errors["recovery_incomplete"]
        )
    return (
        not _is_not_applicable(primary)
        and all(
            error["event_sequence"] > primary["event_sequence"]
            for error in associated
        )
    )


def _error_from_event(event: dict[str, Any]) -> dict[str, Any]:
    attributes = event["attributes"]
    return {
        "error_id": attributes["error_id"],
        "error_code": attributes["error_code"],
        "category": attributes["category"],
        "stage": event["stage"],
        "module": event["module"],
        "operation": (
            {"status": "not_applicable"}
            if "operation_id" not in event
            else {
                "status": "present",
                "operation_id": event["operation_id"],
            }
        ),
        "event_sequence": event["sequence"],
        "safe_message": event["message"],
        "retryable_in_new_run": attributes[
            "retryable_in_new_run"
        ],
        "operator_action": attributes["operator_action"],
        "diagnostics": attributes["diagnostics"],
    }


def _validate_source_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    status_reason = _status_variant(value)
    if status_reason is not None:
        return status_reason
    if value["status"] == "not_observed":
        return _closed_fields(value, frozenset({"status"}))
    if value["status"] != "available":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    reason = _closed_fields(
        value,
        frozenset(
            {
                "status",
                "sha256",
                "byte_length",
                "duration_ms",
                "course_context",
            }
        ),
    )
    if reason is not None:
        return reason
    course_context = value["course_context"]
    reason = _closed_fields(
        course_context,
        frozenset({"provided", "sha256"}),
    )
    if reason is not None:
        return reason
    if (
        not _sha256(value["sha256"])
        or not _nonnegative_int(value["byte_length"])
        or not _nonnegative_int(value["duration_ms"])
        or not isinstance(course_context["provided"], bool)
        or not _valid_digest_status(
            course_context["sha256"],
            provided=course_context["provided"],
        )
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_environment_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    status_reason = _status_variant(value)
    if status_reason is not None:
        return status_reason
    if value["status"] == "not_observed":
        return _closed_fields(value, frozenset({"status"}))
    if value["status"] != "available":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    fields = frozenset(
        {
            "status",
            "certified_platform",
            "python_version",
            "application_version",
            "ffmpeg_version",
            "ffprobe_version",
            "font",
            "installation_fingerprint",
            "preflight_outcome",
        }
    )
    reason = _closed_fields(value, fields)
    if reason is not None:
        return reason
    font = value["font"]
    if (
        not all(
            _valid_detected_version(value[field])
            for field in {
                "python_version",
                "ffmpeg_version",
                "ffprobe_version",
            }
        )
        or not isinstance(value["application_version"], str)
        or _APPLICATION_VERSION.fullmatch(
            value["application_version"]
        )
        is None
        or not _enum_value(
            value["certified_platform"],
            CertifiedPlatform,
        )
        or not isinstance(font, dict)
        or frozenset(font) != {"family", "available"}
        or not isinstance(font["family"], str)
        or _FONT_FAMILY.fullmatch(font["family"]) is None
        or not isinstance(font["available"], bool)
        or not _sha256(value["installation_fingerprint"])
        or not _enum_value(
            value["preflight_outcome"],
            PreflightOutcome,
        )
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_configuration_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    status_reason = _status_variant(value)
    if status_reason is not None:
        return status_reason
    if value["status"] == "not_observed":
        return _closed_fields(value, frozenset({"status"}))
    if value["status"] != "available":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    reason = _closed_fields(value, _CONFIGURATION_FIELDS)
    if reason is not None:
        return reason
    if not _sha256(value["configuration_fingerprint"]):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    for validator, nested in (
        (
            _validate_result_configuration,
            value["result_configuration"],
        ),
        (_validate_runtime_policy, value["runtime_policy"]),
        (
            _validate_configuration_course_context,
            value["course_context"],
        ),
    ):
        nested_reason = validator(nested)
        if nested_reason is not None:
            return nested_reason
    return None


def _validate_result_configuration(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _RESULT_CONFIGURATION_FIELDS)
    if reason is not None:
        return reason
    if (
        value["transcription_provider"] != "stepaudio"
        or value["text_model_provider"] != "stepfun"
        or not _valid_model_name(value["transcription_model"])
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    for field in {"topic_review", "subtitle_optimization"}:
        reason = _validate_text_generation(value[field])
        if reason is not None:
            return reason
    reason = _validate_clip_policy(value["clip_policy"])
    if reason is not None:
        return reason
    return _validate_subtitle_style(value["subtitle_style"])


def _validate_text_generation(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _TEXT_GENERATION_FIELDS)
    if reason is not None:
        return reason
    temperature = value["temperature"]
    if (
        not _valid_model_name(value["model"])
        or not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not 0.0 <= float(temperature) <= 2.0
        or not isinstance(value["reasoning_effort"], str)
        or value["reasoning_effort"]
        not in {"none", "low", "medium", "high"}
        or not _integer_between(value["max_output_tokens"], 1, 65_536)
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_clip_policy(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _CLIP_POLICY_FIELDS)
    if reason is not None:
        return reason
    durations = tuple(
        value[field]
        for field in {
            "min_duration_seconds",
            "target_duration_seconds",
            "max_duration_seconds",
        }
    )
    if not all(_integer_between(duration, 1, 3600) for duration in durations):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not (
        value["min_duration_seconds"]
        <= value["target_duration_seconds"]
        <= value["max_duration_seconds"]
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    max_clips = value["max_clips"]
    reason = _closed_fields(
        max_clips,
        (
            frozenset({"status"})
            if isinstance(max_clips, dict)
            and max_clips.get("status") == "unlimited"
            else frozenset({"status", "value"})
        ),
    )
    if reason is not None:
        return reason
    if max_clips["status"] == "limited":
        if not _integer_between(max_clips["value"], 1, 1000):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    elif max_clips["status"] != "unlimited":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not _integer_between(value["publish_ready_threshold"], 0, 100):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_subtitle_style(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _SUBTITLE_STYLE_FIELDS)
    if reason is not None:
        return reason
    if (
        not _nonempty_string(value["font"])
        or not _integer_between(value["font_size"], 8, 200)
        or not _integer_between(value["outline"], 0, 20)
        or not _integer_between(value["margin_bottom"], 0, 1000)
        or not _integer_between(value["max_chars_per_line"], 1, 100)
        or not _integer_between(value["max_lines"], 1, 2)
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_runtime_policy(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _RUNTIME_POLICY_FIELDS)
    if reason is not None:
        return reason
    if (
        not _integer_between(
            value["transcription_timeout_seconds"],
            1,
            600,
        )
        or not _integer_between(
            value["transcription_max_concurrency"],
            1,
            32,
        )
        or not _integer_between(
            value["text_model_timeout_seconds"],
            1,
            600,
        )
        or not _integer_between(
            value["text_model_max_concurrency"],
            1,
            32,
        )
        or not _integer_between(
            value["delivery_build_concurrency"],
            1,
            32,
        )
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_configuration_course_context(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _COURSE_CONTEXT_FIELDS)
    if reason is not None:
        return reason
    if (
        not isinstance(value["provided"], bool)
        or not isinstance(value["attribution_provided"], bool)
        or not _nonnegative_int(value["priority_topic_count"])
        or not _nonnegative_int(value["excluded_content_count"])
        or (
            not value["provided"]
            and (
                value["attribution_provided"]
                or value["priority_topic_count"] != 0
                or value["excluded_content_count"] != 0
            )
        )
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_stages_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    expected = frozenset(stage.value for stage in RunStage)
    reason = _closed_fields(value, expected)
    if reason is not None:
        return reason
    for summary in value.values():
        if not isinstance(summary, dict) or not isinstance(
            summary.get("status"),
            str,
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if summary["status"] == "not_started":
            reason = _closed_fields(summary, frozenset({"status"}))
        elif _enum_value(summary["status"], StageOutcome):
            reason = _closed_fields(
                summary,
                frozenset(
                    {"status", "duration_ms", "work_item_count"}
                ),
            )
            if (
                reason is None
                and (
                    not _nonnegative_int(summary["duration_ms"])
                    or not _nonnegative_int(summary["work_item_count"])
                )
            ):
                reason = (
                    DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
                )
        else:
            reason = DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if reason is not None:
            return reason
    return None


def _validate_operations_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, frozenset({"summaries"}))
    if reason is not None:
        return reason
    summaries = value["summaries"]
    if not isinstance(summaries, list):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    seen: set[OperationKind] = set()
    indexes: list[int] = []
    kinds = tuple(OperationKind)
    for summary in summaries:
        reason = _closed_fields(summary, _OPERATION_SUMMARY_FIELDS)
        if reason is not None:
            return reason
        try:
            kind = OperationKind(summary["operation_kind"])
        except (TypeError, ValueError):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if kind in seen:
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        seen.add(kind)
        indexes.append(kinds.index(kind))
        count = summary["count"]
        outcomes = summary["outcomes"]
        reason = _closed_fields(
            outcomes,
            frozenset(outcome.value for outcome in OperationOutcome),
        )
        if reason is not None:
            return reason
        if (
            not _positive_int(count)
            or not all(
                _nonnegative_int(outcome_count)
                for outcome_count in outcomes.values()
            )
            or sum(outcomes.values()) != count
            or not _nonnegative_int(summary["duration_ms_total"])
            or not _nonnegative_int(summary["duration_ms_max"])
            or summary["duration_ms_max"]
            > summary["duration_ms_total"]
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if indexes != sorted(indexes):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_retries_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(
        value,
        frozenset(kind.value for kind in RetryKind),
    )
    if reason is not None:
        return reason
    if not all(_nonnegative_int(count) for count in value.values()):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_cache_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    status_reason = _status_variant(value)
    if status_reason is not None:
        return status_reason
    if value["status"] == "not_observed":
        return _closed_fields(value, frozenset({"status"}))
    if value["status"] != "observed":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    reason = _closed_fields(value, frozenset({"status", "namespaces"}))
    if reason is not None:
        return reason
    namespaces = value["namespaces"]
    if not isinstance(namespaces, dict) or not namespaces:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    allowed = frozenset(namespace.value for namespace in CacheNamespace)
    if frozenset(namespaces).difference(allowed):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    for stats in namespaces.values():
        reason = _closed_fields(stats, _CACHE_STAT_FIELDS)
        if reason is not None:
            return reason
        if not all(_nonnegative_int(count) for count in stats.values()):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if stats["queries"] != (
            stats["hits"]
            + stats["misses"]
            + stats["corrupt_quarantined"]
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_external_services_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    status_reason = _status_variant(value)
    if status_reason is not None:
        return status_reason
    if value["status"] == "not_observed":
        return _closed_fields(value, frozenset({"status"}))
    if value["status"] != "observed":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    reason = _closed_fields(value, frozenset({"status", "services"}))
    if reason is not None:
        return reason
    services = value["services"]
    if not isinstance(services, list) or not services:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    expected_fields = frozenset(
        {
            "capability",
            "adapter_id",
            "provider_id",
            "model_id",
            "configuration_fingerprint",
            "endpoint",
            "transport",
            "purpose",
            "allowed_data_categories",
            "contact",
            "requests",
        }
    )
    seen: set[ProviderCapability] = set()
    capability_order = tuple(ProviderCapability)
    indexes: list[int] = []
    for service in services:
        reason = _closed_fields(service, expected_fields)
        if reason is not None:
            return reason
        try:
            capability = ProviderCapability(service["capability"])
            transport = ProviderTransport(service["transport"])
        except (TypeError, ValueError):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if capability in seen:
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        seen.add(capability)
        indexes.append(capability_order.index(capability))
        categories = service["allowed_data_categories"]
        if (
            service["purpose"] != capability.value
            or not all(
                _stable_identifier(service[field])
                for field in {"adapter_id", "provider_id", "model_id"}
            )
            or not _sha256(service["configuration_fingerprint"])
            or not _valid_endpoint_status(
                service["endpoint"],
                transport=transport,
            )
            or not _valid_external_categories(
                capability=capability,
                transport=transport,
                categories=categories,
            )
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        request_reason = _validate_external_requests_summary(
            service["requests"],
            contact=service["contact"],
        )
        if request_reason is not None:
            return request_reason
    if indexes != sorted(indexes):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_external_requests_summary(
    value: Any,
    *,
    contact: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(
        value,
        frozenset(
            {
                "count",
                "succeeded",
                "failed",
                "attempt_count_total",
                "duration_ms_total",
                "duration_ms_max",
                "token_usage",
            }
        ),
    )
    if reason is not None:
        return reason
    counters = {
        "count",
        "succeeded",
        "failed",
        "attempt_count_total",
        "duration_ms_total",
        "duration_ms_max",
    }
    if not all(_nonnegative_int(value[field]) for field in counters):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    count = value["count"]
    if (
        value["succeeded"] + value["failed"] != count
        or value["duration_ms_max"] > value["duration_ms_total"]
        or value["attempt_count_total"] < count
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if count == 0:
        if (
            value["attempt_count_total"] != 0
            or value["duration_ms_total"] != 0
            or value["duration_ms_max"] != 0
            or not _valid_not_contacted(contact)
            or not _is_not_applicable(value["token_usage"])
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        return None
    if contact != {"status": "contacted"}:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not _valid_aggregate_token_usage(value["token_usage"], count=count):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _valid_not_contacted(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and frozenset(value) == {"status", "reason"}
        and value["status"] == "not_contacted"
        and _enum_value(value["reason"], ZeroRequestReason)
    )


def _valid_aggregate_token_usage(value: Any, *, count: int) -> bool:
    if value == {"status": "not_reported"}:
        return True
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    expected = {"status", "input_tokens", "output_tokens"}
    if status == "partially_reported":
        expected.add("reported_request_count")
    if frozenset(value) != expected:
        return False
    if (
        not isinstance(status, str)
        or status not in {"reported", "partially_reported"}
        or not _nonnegative_int(value["input_tokens"])
        or not _nonnegative_int(value["output_tokens"])
    ):
        return False
    if status == "partially_reported":
        return _integer_between(
            value["reported_request_count"],
            1,
            count - 1,
        )
    return True


def _validate_delivery_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    if not isinstance(value, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    expected = (
        _DELIVERY_FIELDS | {"artifacts"}
        if "artifacts" in value
        else _DELIVERY_FIELDS
    )
    reason = _closed_fields(value, frozenset(expected))
    if reason is not None:
        return reason
    if (
        not isinstance(value["build_state"], str)
        or value["build_state"]
        not in {
            "not_started",
            "completed",
            "failed",
            "interrupted",
        }
        or not isinstance(value["verification_state"], str)
        or value["verification_state"]
        not in {"not_started", "passed", "failed", "interrupted"}
        or not isinstance(value["publication_state"], str)
        or value["publication_state"]
        not in {"not_started", "committed", "rolled_back", "failed"}
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if "artifacts" in value:
        reason = _validate_artifact_summary(value["artifacts"])
        if reason is not None:
            return reason
    return None


def _validate_artifact_summary(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(
        value,
        frozenset(
            {"status", "created_by_role", "verified_by_role"}
        ),
    )
    if reason is not None:
        return reason
    if value["status"] != "observed":
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    created = value["created_by_role"]
    verified = value["verified_by_role"]
    allowed = frozenset(role.value for role in ArtifactRole)
    for counts in (created, verified):
        if not isinstance(counts, dict):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if frozenset(counts).difference(allowed):
            return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
        if not all(_positive_int(count) for count in counts.values()):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not created and not verified:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if any(
        count > created.get(role, 0)
        for role, count in verified.items()
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_notices_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    if not isinstance(value, list):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    kinds: set[RecoveredNoticeKind] = set()
    indexes: list[int] = []
    kind_order = tuple(RecoveredNoticeKind)
    for notice in value:
        reason = _closed_fields(notice, frozenset({"kind", "count"}))
        if reason is not None:
            return reason
        try:
            kind = RecoveredNoticeKind(notice["kind"])
        except (TypeError, ValueError):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        if kind in kinds or not _positive_int(notice["count"]):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        kinds.add(kind)
        indexes.append(kind_order.index(kind))
    if indexes != sorted(indexes):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _validate_errors_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _ERRORS_FIELDS)
    if reason is not None:
        return reason
    primary = value["primary_error"]
    associated = value["associated_errors"]
    if not isinstance(associated, list) or not isinstance(
        value["recovery_incomplete"],
        bool,
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if _is_not_applicable(primary):
        primary_id = None
    else:
        reason = _validate_error_manifest(primary)
        if reason is not None:
            return reason
        primary_id = primary["error_id"]
    seen_ids = {primary_id} if primary_id is not None else set()
    previous_sequence = 0
    for error in associated:
        reason = _validate_error_manifest(error)
        if reason is not None:
            return reason
        if (
            error["error_id"] in seen_ids
            or error["event_sequence"] <= previous_sequence
        ):
            return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
        seen_ids.add(error["error_id"])
        previous_sequence = error["event_sequence"]
    return None


def _validate_error_manifest(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    reason = _closed_fields(value, _ERROR_FIELDS)
    if reason is not None:
        return reason
    try:
        ErrorId(value["error_id"])
        operation = value["operation"]
        if _is_not_applicable(operation):
            operation_id = None
        else:
            reason = _closed_fields(
                operation,
                frozenset({"status", "operation_id"}),
            )
            if reason is not None:
                return reason
            if operation["status"] != "present":
                return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
            operation_id = OperationId(operation["operation_id"])
        reconstructed = RunError.create(
            code=value["error_code"],
            stage=value["stage"],
            module=value["module"],
            operation_id=operation_id,
            event_sequence=value["event_sequence"],
            diagnostics=_rehydrate_error_diagnostics(
                value["diagnostics"]
            ),
        )
    except (TypeError, ValueError, KeyError):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if (
        value["category"] != reconstructed.category.value
        or value["safe_message"] != reconstructed.safe_message
        or value["retryable_in_new_run"]
        is not reconstructed.retryable_in_new_run
        or value["operator_action"] != reconstructed.operator_action.value
    ):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _errors_match_lifecycle(
    errors: dict[str, Any],
    lifecycle: dict[str, Any],
) -> bool:
    primary = errors["primary_error"]
    outcome = lifecycle["outcome"]
    if outcome == RunTerminalState.FAILED.value:
        if _is_not_applicable(primary):
            return False
        try:
            reconstructed = RunError.create(
                code=primary["error_code"],
                stage=primary["stage"],
                module=primary["module"],
                event_sequence=primary["event_sequence"],
                diagnostics=_rehydrate_error_diagnostics(
                    primary["diagnostics"]
                ),
            )
        except (TypeError, ValueError):
            return False
        return int(reconstructed.exit_code) == lifecycle["exit_code"]
    return (
        _is_not_applicable(primary)
        and (
            (
                outcome == RunTerminalState.SUCCEEDED.value
                and lifecycle["exit_code"] == int(ExitCode.SUCCESS)
            )
            or (
                outcome == RunTerminalState.INTERRUPTED.value
                and lifecycle["exit_code"]
                == InterruptionSignal(
                    lifecycle["interruption"]["signal"]
                ).exit_code
            )
        )
    )


def _rehydrate_error_diagnostics(
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    hydrated = dict(diagnostics)
    remote_request_id = hydrated.get("remote_request_id")
    if remote_request_id is not None:
        if not _sha256(remote_request_id):
            raise ValueError("远端请求标识摘要不合法")
        hydrated["remote_request_id"] = str.__new__(
            RemoteRequestId,
            remote_request_id,
        )
    return hydrated


def _closed_fields(
    value: Any,
    expected: frozenset[str],
) -> DiagnosticPackageReadReason | None:
    if not isinstance(value, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    actual = frozenset(value)
    if actual.difference(expected):
        return DiagnosticPackageReadReason.MANIFEST_FIELD_UNKNOWN
    if actual != expected:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _status_variant(
    value: Any,
) -> DiagnosticPackageReadReason | None:
    if not isinstance(value, dict):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if "status" not in value:
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    if not isinstance(value["status"], str):
        return DiagnosticPackageReadReason.MANIFEST_SCHEMA_INVALID
    return None


def _valid_digest_status(value: Any, *, provided: bool) -> bool:
    if provided:
        return (
            isinstance(value, dict)
            and frozenset(value) == {"status", "value"}
            and value["status"] == "available"
            and _sha256(value["value"])
        )
    return _is_not_applicable(value)


def _is_not_applicable(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value == {"status": "not_applicable"}
    )


def _valid_lifecycle(
    lifecycle: dict[str, Any],
    terminal_event: dict[str, Any],
) -> bool:
    terminal = terminal_event["attributes"]
    try:
        ExitCode(lifecycle["exit_code"])
    except (TypeError, ValueError):
        return False
    outcome = lifecycle["outcome"]
    interruption = lifecycle["interruption"]
    if outcome == RunTerminalState.INTERRUPTED.value:
        interruption_valid = _valid_interruption_manifest(interruption)
    else:
        interruption_valid = _is_not_applicable(interruption)
    return (
        isinstance(lifecycle["started_at"], str)
        and _TIMESTAMP.fullmatch(lifecycle["started_at"]) is not None
        and isinstance(lifecycle["ended_at"], str)
        and _TIMESTAMP.fullmatch(lifecycle["ended_at"]) is not None
        and _nonnegative_int(lifecycle["duration_ms"])
        and lifecycle["duration_ms"] == terminal["duration_ms"]
        and _enum_value(outcome, RunTerminalState)
        and outcome == terminal["outcome"]
        and _int_not_bool(lifecycle["exit_code"])
        and lifecycle["exit_code"] == terminal["exit_code"]
        and _valid_result_kind(lifecycle["result_kind"], outcome=outcome)
        and lifecycle["result_kind"] == terminal["result_kind"]
        and interruption_valid
    )


def _valid_result_kind(value: Any, *, outcome: str) -> bool:
    if outcome != RunTerminalState.SUCCEEDED.value:
        return _is_not_applicable(value)
    return (
        isinstance(value, dict)
        and frozenset(value) == {"status", "value"}
        and value["status"] == "available"
        and _enum_value(value["value"], ResultKind)
    )


def _valid_interruption_manifest(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and frozenset(value)
        == {"status", "signal", "received_at", "cleanup_duration_ms"}
        and value["status"] == "available"
        and _enum_value(value["signal"], InterruptionSignal)
        and isinstance(value["received_at"], str)
        and _TIMESTAMP.fullmatch(value["received_at"]) is not None
        and _nonnegative_int(value["cleanup_duration_ms"])
    )


def _status_object(
    value: Any,
    allowed: set[str] | None = None,
) -> bool:
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    if frozenset(value).difference({"status", "value"}):
        return False
    status = value["status"]
    if not _nonempty_string(status):
        return False
    if allowed is not None and status not in allowed:
        return False
    if status in {"available", "present"}:
        return "value" in value
    return "value" not in value


def _valid_endpoint_status(
    value: Any,
    *,
    transport: ProviderTransport,
) -> bool:
    if transport is ProviderTransport.LOCAL:
        return _is_not_applicable(value)
    if (
        not isinstance(value, dict)
        or frozenset(value) != {"status", "origin"}
        or value["status"] != "available"
        or not isinstance(value["origin"], str)
    ):
        return False
    try:
        parsed = urlsplit(value["origin"])
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query != ""
        or parsed.fragment != ""
    ):
        return False
    host = parsed.hostname.lower()
    if ":" in host:
        try:
            host = f"[{IPv6Address(host).compressed}]"
        except ValueError:
            return False
    normalized_port = "" if port in {None, 443} else f":{port}"
    return value["origin"] == f"https://{host}{normalized_port}"


def _valid_external_categories(
    *,
    capability: ProviderCapability,
    transport: ProviderTransport,
    categories: Any,
) -> bool:
    if not isinstance(categories, list):
        return False
    if transport is ProviderTransport.LOCAL:
        return categories == []
    allowed = {
        ProviderCapability.TRANSCRIPTION: {
            ExternalDataCategory.AUDIO_SHARD.value,
        },
        ProviderCapability.TOPIC_REVIEW: {
            ExternalDataCategory.CANDIDATE_TRANSCRIPT.value,
            ExternalDataCategory.COURSE_CONTEXT.value,
            ExternalDataCategory.BUSINESS_CONSTRAINTS.value,
        },
        ProviderCapability.SUBTITLE_OPTIMIZATION: {
            ExternalDataCategory.SUBTITLE_WINDOW.value,
            ExternalDataCategory.FIXED_INSTRUCTIONS.value,
        },
    }[capability]
    return (
        bool(categories)
        and all(isinstance(category, str) for category in categories)
        and len(categories) == len(set(categories))
        and set(categories).issubset(allowed)
        and categories == sorted(categories)
    )


def _valid_remote_request_id_status(value: Any) -> bool:
    return (
        value == {"status": "not_reported"}
        or (
            isinstance(value, dict)
            and frozenset(value) == {"status", "sha256"}
            and value["status"] == "reported"
            and _sha256(value["sha256"])
        )
    )


def _valid_token_usage_status(value: Any) -> bool:
    return (
        value == {"status": "not_reported"}
        or (
            isinstance(value, dict)
            and frozenset(value)
            == {"status", "input_tokens", "output_tokens"}
            and value["status"] == "reported"
            and _nonnegative_int(value["input_tokens"])
            and _nonnegative_int(value["output_tokens"])
        )
    )


def _contains_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_null(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_contains_null(nested) for nested in value)
    return False


def _valid_timestamp(value: str) -> bool:
    if _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _valid_detected_version(value: Any) -> bool:
    try:
        DetectedVersion.from_readiness(value)
    except (TypeError, ValueError):
        return False
    return True


def _valid_model_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _MODEL_NAME.fullmatch(value) is not None
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512


def _stable_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(
            r"[a-z][a-z0-9_]{0,31}"
            r"(?:\.[a-z][a-z0-9_]{0,31}){0,7}",
            value,
        )
        is not None
    )


def _stable_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value)
        is not None
    )


def _int_not_bool(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any) -> bool:
    return _int_not_bool(value) and value >= 1


def _nonnegative_int(value: Any) -> bool:
    return _int_not_bool(value) and value >= 0


def _integer_between(value: Any, minimum: int, maximum: int) -> bool:
    return _int_not_bool(value) and minimum <= value <= maximum


def _enum_value(value: Any, enum_type: type[Enum]) -> bool:
    try:
        enum_type(value)
    except (TypeError, ValueError):
        return False
    return True


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _uuid4_string(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        OperationId(value)
    except ValueError:
        return False
    return True


def _valid_artifact_path(role: ArtifactRole, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    fixed = {
        ArtifactRole.MANIFEST: "manifest.json",
        ArtifactRole.TRANSCRIPT_JSON: "transcript.json",
        ArtifactRole.TRANSCRIPT_SRT: "transcript.srt",
        ArtifactRole.PLAN: "plan.json",
        ArtifactRole.METADATA: "metadata.json",
        ArtifactRole.REPORT: "report.md",
    }
    if role is not ArtifactRole.SHORT_VIDEO:
        return value == fixed[role]
    if not value.startswith("clips/") or not value.endswith(".mp4"):
        return False
    try:
        ShortVideoId(value[len("clips/") : -len(".mp4")])
    except (TypeError, ValueError):
        return False
    return True


def _incomplete(
    reason: DiagnosticPackageReadReason,
    *,
    run_id: RunId | None,
    event_count: int,
) -> DiagnosticPackageReadResult:
    return DiagnosticPackageReadResult(
        state=DiagnosticPackageReadState.INCOMPLETE,
        reason=reason,
        run_id=run_id,
        event_count=event_count,
    )


def _corrupt(
    reason: DiagnosticPackageReadReason,
    event_count: int,
) -> DiagnosticPackageReadResult:
    return DiagnosticPackageReadResult(
        state=DiagnosticPackageReadState.CORRUPT,
        reason=reason,
        run_id=None,
        event_count=event_count,
    )
