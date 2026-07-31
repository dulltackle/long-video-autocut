"""从消费者视角验证未发布标准交付物。"""

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from enum import Enum
from time import monotonic
from typing import Any

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.runtime.identity import (
    BusinessId,
    CandidateId,
    PlanId,
    RunId,
    SeriesId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.runtime.result import ResultKind
from video_auto_editor.workspace import (
    ManagedBinaryFile,
    ManagedDirectoryCapability,
    ManagedTreeEntry,
    ManagedTreeEntryKind,
    WorkspaceFailure,
)

from .capability import UnverifiedDelivery, VerifiedDelivery

_MANIFEST_FIELDS = frozenset(
    {
        "application_version",
        "documents",
        "execution",
        "files",
        "published_at",
        "result_kind",
        "run_id",
        "schema_version",
        "source",
        "started_at",
        "terminal_state",
    }
)
_APPLICATION_VERSION = re.compile(
    r"[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_RFC3339_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z"
)
_ARTIFACT_ROLES = frozenset(
    {
        "clip_plan",
        "faithful_transcript",
        "faithful_transcript_rendering",
        "human_report",
        "short_video_catalog",
        "short_video_media",
    }
)
_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/x-subrip",
        "text/markdown",
        "video/mp4",
    }
)
_DOCUMENT_FIELDS = {
    "transcript": frozenset({"path", "transcript_id"}),
    "transcript_rendering": frozenset({"path", "transcript_id"}),
    "plan": frozenset({"path", "plan_id"}),
    "metadata": frozenset({"path"}),
    "report": frozenset({"path"}),
}
_EXECUTION_COUNT_FIELDS = frozenset(
    {
        "cache_hit_count",
        "cache_miss_count",
        "model_request_count",
        "semantic_retry_count",
        "short_video_count",
        "transport_attempt_count",
        "transport_retry_count",
        "window_count",
    }
)
_FILE_FIELDS = frozenset({"byte_length", "media_type", "path", "role", "sha256"})
_REVIEW_FIELDS = frozenset(
    {
        "boundary_fix_end_ms",
        "boundary_fix_start_ms",
        "boundary_fix_suggestion",
        "export_decision",
        "keywords",
        "learning_value",
        "needs_human_review",
        "publish_ready_score",
        "reject_reason",
        "share_value",
        "summary",
        "title",
        "topic_complete",
        "topic_name",
    }
)
_REJECTION_REASONS = frozenset(
    {
        "boundary_remedy_invalid",
        "excluded_content",
        "max_clips_limit",
        "needs_human_review",
        "publish_ready_score_below_threshold",
        "review_rejected",
        "topic_incomplete",
    }
)
_FIXED_ARTIFACTS = {
    "metadata.json": ("short_video_catalog", "application/json"),
    "plan.json": ("clip_plan", "application/json"),
    "report.md": ("human_report", "text/markdown"),
    "transcript.json": ("faithful_transcript", "application/json"),
    "transcript.srt": (
        "faithful_transcript_rendering",
        "application/x-subrip",
    ),
}
_FIXED_DOCUMENT_PATHS = {
    "metadata": "metadata.json",
    "plan": "plan.json",
    "report": "report.md",
    "transcript": "transcript.json",
    "transcript_rendering": "transcript.srt",
}
_MEDIA_DURATION_TOLERANCE_MS = 250
_PROBE_POLL_SECONDS = 0.05
_PROBE_TIMEOUT_SECONDS = 30.0
_PROBE_STOP_SECONDS = 0.5
_MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
_MAX_PROBE_ERROR_BYTES = 1024 * 1024
_PROBE_IO_BYTES = 64 * 1024
_FILE_READ_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _ContentFact:
    byte_length: int
    digest: bytes

    @property
    def sha256(self) -> str:
        return "sha256:" + self.digest.hex()


class DeliveryVerificationFailure(RuntimeError):
    """不暴露交付正文、路径或媒体工具原始输出的验证失败。"""

    __slots__ = (
        "category",
        "diagnostics",
        "error_code",
        "retryable_in_new_run",
        "safe_message",
    )

    def __init__(self, diagnostics: Mapping[str, Any]) -> None:
        definition = get_error_definition(ErrorCode.DELIVERY_VERIFICATION_FAILED)
        self.error_code = ErrorCode.DELIVERY_VERIFICATION_FAILED
        self.category: ErrorCategory = definition.category
        self.safe_message = definition.safe_message
        self.retryable_in_new_run = definition.retryable_in_new_run
        self.diagnostics = freeze_error_diagnostics(
            self.error_code,
            diagnostics,
        )
        super().__init__(self.safe_message)


class _DuplicateJsonField(ValueError):
    pass


class _SchemaInvalid(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


class _DeliveryManifestParseFailure(ValueError):
    __slots__ = ("reason",)

    def __init__(self, reason: "DeliveryManifestReadReason") -> None:
        self.reason = reason
        super().__init__(reason.value)


class DeliveryManifestReadState(str, Enum):
    """交付运行清单的封闭读取状态。"""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"


class DeliveryManifestReadReason(str, Enum):
    """不回显清单正文或解析异常的稳定读取原因。"""

    VALID = "valid"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_UNREADABLE = "manifest_unreadable"
    MANIFEST_SYMLINK = "manifest_symlink"
    MANIFEST_ENCODING_INVALID = "manifest_encoding_invalid"
    MANIFEST_JSON_INVALID = "manifest_json_invalid"
    MANIFEST_DUPLICATE_FIELD = "manifest_duplicate_field"
    MANIFEST_NON_FINITE_NUMBER = "manifest_non_finite_number"
    MANIFEST_SCHEMA_INVALID = "manifest_schema_invalid"
    MANIFEST_RUN_ID_MISMATCH = "manifest_run_id_mismatch"
    MANIFEST_RESULT_KIND_MISMATCH = "manifest_result_kind_mismatch"


@dataclass(frozen=True, slots=True)
class DeliveryManifestSummary:
    """只投影交付运行清单中的长期审计摘要。"""

    run_id: RunId
    result_kind: ResultKind
    application_version: str
    started_at: str
    published_at: str
    source_sha256: str
    source_byte_length: int
    source_duration_ms: int
    short_video_count: int
    file_count: int


@dataclass(frozen=True, slots=True)
class DeliveryManifestReadResult:
    """严格读取交付运行清单后的封闭结果。"""

    state: DeliveryManifestReadState
    reason: DeliveryManifestReadReason
    summary: DeliveryManifestSummary | None


class DeliveryManifestReader:
    """复用交付验证 schema，只读取可长期审计的清单摘要。"""

    __slots__ = ()

    @classmethod
    def read(
        cls,
        manifest: bytes | None,
        *,
        expected_run_id: RunId,
        expected_result_kind: ResultKind | None = None,
    ) -> DeliveryManifestReadResult:
        if manifest is not None and not isinstance(manifest, bytes):
            raise TypeError("交付运行清单快照必须是 bytes 或 None")
        if not isinstance(expected_run_id, RunId):
            raise TypeError("交付运行清单必须绑定 RunId")
        if expected_result_kind is not None and not isinstance(
            expected_result_kind,
            ResultKind,
        ):
            raise TypeError("预期交付结果必须使用 ResultKind")
        if manifest is None:
            return DeliveryManifestReadResult(
                DeliveryManifestReadState.INCOMPLETE,
                DeliveryManifestReadReason.MANIFEST_MISSING,
                None,
            )
        try:
            document = _parse_delivery_manifest_bytes(manifest)
        except _DeliveryManifestParseFailure as failure:
            return _corrupt_delivery_manifest(failure.reason)
        try:
            _validate_manifest_paths(document)
        except DeliveryVerificationFailure:
            return _corrupt_delivery_manifest(
                DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
            )
        try:
            _validate_manifest_summary_identities(document)
        except _SchemaInvalid:
            return _corrupt_delivery_manifest(
                DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
            )
        if not _manifest_result_summary_is_consistent(document):
            return _corrupt_delivery_manifest(
                DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
            )
        if document["run_id"] != str(expected_run_id):
            return _corrupt_delivery_manifest(
                DeliveryManifestReadReason.MANIFEST_RUN_ID_MISMATCH
            )
        result_kind = ResultKind(document["result_kind"])
        if (
            expected_result_kind is not None
            and result_kind is not expected_result_kind
        ):
            return _corrupt_delivery_manifest(
                DeliveryManifestReadReason.MANIFEST_RESULT_KIND_MISMATCH
            )
        source = document["source"]
        execution = document["execution"]["subtitle_optimization"]
        return DeliveryManifestReadResult(
            DeliveryManifestReadState.COMPLETE,
            DeliveryManifestReadReason.VALID,
            DeliveryManifestSummary(
                run_id=expected_run_id,
                result_kind=result_kind,
                application_version=document["application_version"],
                started_at=document["started_at"],
                published_at=document["published_at"],
                source_sha256=source["sha256"],
                source_byte_length=source["byte_length"],
                source_duration_ms=source["duration_ms"],
                short_video_count=execution["short_video_count"],
                file_count=len(document["files"]),
            ),
        )


def _reject_non_finite_json_number(_value: str) -> None:
    raise _NonFiniteJsonNumber


def _corrupt_delivery_manifest(
    reason: DeliveryManifestReadReason,
) -> DeliveryManifestReadResult:
    return DeliveryManifestReadResult(
        DeliveryManifestReadState.CORRUPT,
        reason,
        None,
    )


def _parse_delivery_manifest_bytes(
    manifest: bytes,
) -> dict[str, Any]:
    """严格解析并验证交付运行清单的共享 schema 部分。"""
    try:
        document = json.loads(
            manifest,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_non_finite_json_number,
        )
    except UnicodeDecodeError:
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_ENCODING_INVALID
        ) from None
    except _DuplicateJsonField:
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_DUPLICATE_FIELD
        ) from None
    except _NonFiniteJsonNumber:
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_NON_FINITE_NUMBER
        ) from None
    except (ValueError, RecursionError):
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_JSON_INVALID
        ) from None
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
        )
    try:
        _validate_manifest_schema(document)
    except _SchemaInvalid:
        raise _DeliveryManifestParseFailure(
            DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
        ) from None
    return document


class DeliveryVerification:
    """验证标准交付物并签发只可由本模块形成的发布能力。"""

    __slots__ = ()

    @classmethod
    def verify(
        cls,
        delivery: UnverifiedDelivery,
        cancellation: CancellationToken,
    ) -> VerifiedDelivery:
        """读取未发布交付，不修改内容，验证后形成内容快照。"""
        if not isinstance(delivery, UnverifiedDelivery):
            raise TypeError("交付验证只接受 UnverifiedDelivery")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("交付验证必须绑定根取消令牌")
        return _without_sensitive_exception_context(
            lambda: cls._verify(delivery, cancellation)
        )

    @classmethod
    def _verify(
        cls,
        delivery: UnverifiedDelivery,
        cancellation: CancellationToken,
    ) -> VerifiedDelivery:
        cancellation.raise_if_cancelled()
        tree_entries = _inspect_delivery_tree(delivery.managed_directory)
        manifest_bytes = _read_delivery_file(
            delivery.managed_directory,
            "manifest.json",
            artifact_role="delivery_manifest",
        )
        try:
            manifest = _parse_delivery_manifest_bytes(manifest_bytes)
        except _DeliveryManifestParseFailure:
            raise _verification_failure(
                operation="delivery.verify_schema",
                artifact_role="delivery_manifest",
                reason_code="verification.schema_invalid",
            ) from None
        if manifest.get("run_id") != str(delivery.run_id):
            raise _verification_failure(
                operation="delivery.verify_schema",
                artifact_role="delivery_manifest",
                reason_code="verification.run_id_mismatch",
            )
        _validate_manifest_paths(manifest)
        transcript_bytes = _read_delivery_file(
            delivery.managed_directory,
            "transcript.json",
            artifact_role="faithful_transcript",
        )
        transcript = _json_document(
            transcript_bytes,
            artifact_role="faithful_transcript",
            validator=_validate_transcript_schema,
        )
        plan_bytes = _read_delivery_file(
            delivery.managed_directory,
            "plan.json",
            artifact_role="clip_plan",
        )
        plan = _json_document(
            plan_bytes,
            artifact_role="clip_plan",
            validator=_validate_plan_schema,
        )
        metadata_bytes = _read_delivery_file(
            delivery.managed_directory,
            "metadata.json",
            artifact_role="short_video_catalog",
        )
        metadata = _json_document(
            metadata_bytes,
            artifact_role="short_video_catalog",
            validator=_validate_metadata_schema,
        )
        report_bytes = _read_delivery_file(
            delivery.managed_directory,
            "report.md",
            artifact_role="human_report",
        )
        for document, artifact_role in (
            (transcript, "faithful_transcript"),
            (plan, "clip_plan"),
            (metadata, "short_video_catalog"),
        ):
            if document["run_id"] != manifest["run_id"]:
                raise _verification_failure(
                    operation="delivery.verify_schema",
                    artifact_role=artifact_role,
                    reason_code="verification.run_id_mismatch",
                )
        _validate_identities(
            manifest=manifest,
            transcript=transcript,
            plan=plan,
            metadata=metadata,
        )
        _validate_references(
            manifest=manifest,
            transcript=transcript,
            plan=plan,
            metadata=metadata,
        )
        _validate_result_kind(
            manifest=manifest,
            plan=plan,
            metadata=metadata,
        )
        _validate_tree_entries(tree_entries, manifest)
        loaded_contents = {
            "metadata.json": metadata_bytes,
            "plan.json": plan_bytes,
            "report.md": report_bytes,
            "transcript.json": transcript_bytes,
        }
        short_video_by_media_path = {
            short_video["media"]["path"]: short_video
            for short_video in metadata["short_videos"]
        }
        content_facts = {
            "manifest.json": _content_fact(manifest_bytes),
        }
        transcript_rendering: bytes | None = None
        for artifact in manifest["files"]:
            cancellation.raise_if_cancelled()
            path = artifact["path"]
            if artifact["role"] == "short_video_media":
                short_video = short_video_by_media_path[path]
                fact = _verify_media_file(
                    delivery.managed_directory,
                    path,
                    artifact=artifact,
                    expected_duration_ms=short_video["duration_ms"],
                    cancellation=cancellation,
                )
            elif path in loaded_contents:
                fact = _content_fact(loaded_contents[path])
                _assert_artifact_content(fact, artifact)
            elif path == "transcript.srt":
                transcript_rendering = _read_delivery_file(
                    delivery.managed_directory,
                    path,
                    artifact_role=artifact["role"],
                )
                fact = _content_fact(transcript_rendering)
                _assert_artifact_content(fact, artifact)
            else:
                fact = _hash_delivery_file(
                    delivery.managed_directory,
                    path,
                    artifact_role=artifact["role"],
                    cancellation=cancellation,
                )
                _assert_artifact_content(fact, artifact)
            content_facts[path] = fact
        _validate_human_report(
            report_bytes,
            manifest=manifest,
            plan=plan,
        )
        if transcript_rendering is None:
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role="faithful_transcript_rendering",
                reason_code="verification.file_set_mismatch",
            )
        expected_transcript_rendering = _render_transcript(transcript)
        if transcript_rendering != expected_transcript_rendering:
            raise _verification_failure(
                operation="delivery.verify_transcript",
                artifact_role="faithful_transcript_rendering",
                reason_code="verification.transcript_mismatch",
            )
        final_tree_entries = _inspect_delivery_tree(delivery.managed_directory)
        if final_tree_entries != tree_entries:
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role="delivery_manifest",
                reason_code="verification.file_set_mismatch",
            )
        snapshot = hashlib.sha256(b"delivery_snapshot.v1\0")
        for path in sorted(content_facts):
            path_bytes = path.encode("utf-8")
            fact = content_facts[path]
            snapshot.update(len(path_bytes).to_bytes(8, "big"))
            snapshot.update(path_bytes)
            snapshot.update(fact.byte_length.to_bytes(8, "big"))
            snapshot.update(fact.digest)
        cancellation.raise_if_cancelled()
        try:
            return VerifiedDelivery._from_verification(
                delivery,
                verification_snapshot="sha256:" + snapshot.hexdigest(),
                verification_tree=final_tree_entries,
            )
        except WorkspaceFailure as failure:
            raise _file_access_failure(
                failure,
                artifact_role="delivery_manifest",
            ) from None


def _without_sensitive_exception_context(
    effect: Callable[[], VerifiedDelivery],
) -> VerifiedDelivery:
    """在公共边界复制安全事实，丢弃不可信正文、路径与工具异常链。"""
    try:
        return effect()
    except DeliveryVerificationFailure as failure:
        diagnostics = failure.diagnostics
    raise DeliveryVerificationFailure(diagnostics) from None


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonField(key)
        result[key] = value
    return result


def _inspect_delivery_tree(
    directory: ManagedDirectoryCapability,
) -> tuple[ManagedTreeEntry, ...]:
    try:
        return directory.inspect_tree()
    except WorkspaceFailure as failure:
        raise _file_access_failure(
            failure,
            artifact_role="delivery_manifest",
        ) from None


def _validate_human_report(
    contents: bytes,
    *,
    manifest: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    try:
        text = contents.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _verification_failure(
            operation="delivery.verify_schema",
            artifact_role="human_report",
            reason_code="verification.schema_invalid",
        ) from None
    if text != _render_human_report(manifest=manifest, plan=plan):
        raise _verification_failure(
            operation="delivery.verify_schema",
            artifact_role="human_report",
            reason_code="verification.schema_invalid",
        )


def _render_human_report(
    *,
    manifest: dict[str, Any],
    plan: dict[str, Any],
) -> str:
    if manifest["result_kind"] == "empty":
        outcome = (
            "本次运行成功完成，形成有效空结果；没有候选满足发布条件，短视频集合为空。"
        )
    else:
        outcome = f"本次运行成功完成，共形成 {plan['published_count']} 条短视频。"
    return (
        "# 直播拆条报告\n\n"
        f"- 运行标识：`{manifest['run_id']}`\n"
        f"- 结果类型：`{manifest['result_kind']}`\n"
        f"- 候选数量：{plan['candidate_count']}\n"
        f"- 发布数量：{plan['published_count']}\n\n"
        "## 结果说明\n\n"
        f"{outcome}\n"
    )


def _read_delivery_file(
    directory: ManagedDirectoryCapability,
    path: str,
    *,
    artifact_role: str,
) -> bytes:
    try:
        return directory.location(path).read_bytes()
    except WorkspaceFailure as failure:
        raise _file_access_failure(
            failure,
            artifact_role=artifact_role,
        ) from None
    except OSError:
        raise _file_access_failure(
            None,
            artifact_role=artifact_role,
        ) from None


def _content_fact(contents: bytes) -> _ContentFact:
    return _ContentFact(
        byte_length=len(contents),
        digest=hashlib.sha256(contents).digest(),
    )


def _hash_stream(
    stream: ManagedBinaryFile,
    cancellation: CancellationToken,
) -> _ContentFact:
    digest = hashlib.sha256()
    byte_length = 0
    while True:
        cancellation.raise_if_cancelled()
        chunk = stream.read(_FILE_READ_BYTES)
        if not chunk:
            break
        byte_length += len(chunk)
        digest.update(chunk)
    cancellation.raise_if_cancelled()
    return _ContentFact(
        byte_length=byte_length,
        digest=digest.digest(),
    )


def _hash_delivery_file(
    directory: ManagedDirectoryCapability,
    path: str,
    *,
    artifact_role: str,
    cancellation: CancellationToken,
) -> _ContentFact:
    try:
        return directory.location(path).use_binary(
            "rb",
            lambda stream: _hash_stream(stream, cancellation),
        )
    except WorkspaceFailure as failure:
        raise _file_access_failure(
            failure,
            artifact_role=artifact_role,
        ) from None
    except OSError:
        raise _file_access_failure(
            None,
            artifact_role=artifact_role,
        ) from None


def _assert_artifact_content(
    fact: _ContentFact,
    artifact: dict[str, Any],
) -> None:
    if fact.byte_length != artifact["byte_length"]:
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role=artifact["role"],
            reason_code="verification.length_mismatch",
        )
    if fact.sha256 != artifact["sha256"]:
        raise _verification_failure(
            operation="delivery.verify_digest",
            artifact_role=artifact["role"],
            reason_code="verification.digest_mismatch",
        )


def _file_access_failure(
    failure: WorkspaceFailure | None,
    *,
    artifact_role: str,
) -> DeliveryVerificationFailure:
    symlink_present = (
        failure is not None
        and failure.diagnostics.get("reason_code") == "workspace.symlink_encountered"
    )
    return _verification_failure(
        operation="delivery.verify_files",
        artifact_role=(artifact_role if symlink_present else "delivery_manifest"),
        reason_code=(
            "verification.symlink_present"
            if symlink_present
            else "verification.file_set_mismatch"
        ),
    )


def _validate_manifest_schema(manifest: dict[str, Any]) -> None:
    if manifest["schema_version"] != "delivery_manifest.v1":
        raise _SchemaInvalid
    _string(manifest["run_id"])
    if manifest["terminal_state"] != "succeeded":
        raise _SchemaInvalid
    if manifest["result_kind"] not in {"clips", "empty"}:
        raise _SchemaInvalid
    started_at = _utc_timestamp(manifest["started_at"])
    published_at = _utc_timestamp(manifest["published_at"])
    if published_at < started_at:
        raise _SchemaInvalid
    application_version = _string(manifest["application_version"])
    if _APPLICATION_VERSION.fullmatch(application_version) is None:
        raise _SchemaInvalid

    source = _object(
        manifest["source"],
        frozenset({"byte_length", "duration_ms", "sha256"}),
    )
    if _SHA256.fullmatch(_string(source["sha256"])) is None:
        raise _SchemaInvalid
    _positive_integer(source["byte_length"])
    _positive_integer(source["duration_ms"])

    documents = _object(
        manifest["documents"],
        frozenset(_DOCUMENT_FIELDS),
    )
    for name, fields in _DOCUMENT_FIELDS.items():
        document = _object(documents[name], fields)
        _string(document["path"], nonempty=True)
        if "transcript_id" in fields:
            _string(document["transcript_id"], nonempty=True)
        if "plan_id" in fields:
            _string(document["plan_id"], nonempty=True)
    execution = _object(
        manifest["execution"],
        frozenset({"subtitle_optimization"}),
    )
    subtitle_optimization = _object(
        execution["subtitle_optimization"],
        _EXECUTION_COUNT_FIELDS,
    )
    for field in _EXECUTION_COUNT_FIELDS:
        _nonnegative_integer(subtitle_optimization[field])
    if (
        subtitle_optimization["semantic_retry_count"]
        > subtitle_optimization["model_request_count"]
        or subtitle_optimization["transport_retry_count"]
        > subtitle_optimization["transport_attempt_count"]
        or subtitle_optimization["cache_hit_count"]
        + subtitle_optimization["cache_miss_count"]
        != subtitle_optimization["window_count"]
        or subtitle_optimization["model_request_count"]
        < subtitle_optimization["cache_miss_count"]
        or subtitle_optimization["window_count"]
        < subtitle_optimization["short_video_count"]
    ):
        raise _SchemaInvalid

    files = _array(manifest["files"])
    for value in files:
        artifact = _object(value, _FILE_FIELDS)
        _string(artifact["path"], nonempty=True)
        if _string(artifact["role"]) not in _ARTIFACT_ROLES:
            raise _SchemaInvalid
        if _string(artifact["media_type"]) not in _MEDIA_TYPES:
            raise _SchemaInvalid
        _nonnegative_integer(artifact["byte_length"])
        if _SHA256.fullmatch(_string(artifact["sha256"])) is None:
            raise _SchemaInvalid


def _validate_manifest_summary_identities(
    manifest: dict[str, Any],
) -> None:
    documents = manifest["documents"]
    try:
        RunId(manifest["run_id"])
        transcript_id = TranscriptId(
            documents["transcript"]["transcript_id"]
        )
        rendering_transcript_id = TranscriptId(
            documents["transcript_rendering"]["transcript_id"]
        )
        PlanId(documents["plan"]["plan_id"])
    except ValueError:
        raise _SchemaInvalid from None
    if transcript_id != rendering_transcript_id:
        raise _SchemaInvalid


def _validate_manifest_paths(manifest: dict[str, Any]) -> None:
    documents = manifest["documents"]
    for name, expected_path in _FIXED_DOCUMENT_PATHS.items():
        path = documents[name]["path"]
        _safe_delivery_path(
            path,
            artifact_role="delivery_manifest",
        )
        if path != expected_path:
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role="delivery_manifest",
                reason_code="verification.file_set_mismatch",
            )

    paths = [artifact["path"] for artifact in manifest["files"]]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role="delivery_manifest",
            reason_code="verification.file_set_mismatch",
        )
    for artifact in manifest["files"]:
        path = artifact["path"]
        _safe_delivery_path(
            path,
            artifact_role="delivery_manifest",
        )
        if path.lower().endswith(".srt") and path != "transcript.srt":
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role="delivery_manifest",
                reason_code="verification.sidecar_subtitle_present",
            )
        expected_contract = _FIXED_ARTIFACTS.get(path)
        if expected_contract is None:
            if (
                not path.startswith("clips/")
                or not path.endswith(".mp4")
                or path.count("/") != 1
            ):
                raise _verification_failure(
                    operation="delivery.verify_files",
                    artifact_role="delivery_manifest",
                    reason_code="verification.file_set_mismatch",
                )
            expected_contract = ("short_video_media", "video/mp4")
        if (
            artifact["role"],
            artifact["media_type"],
        ) != expected_contract:
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role="delivery_manifest",
                reason_code="verification.file_set_mismatch",
            )
    if not set(_FIXED_ARTIFACTS) <= set(paths):
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role="delivery_manifest",
            reason_code="verification.file_set_mismatch",
        )


def _validate_tree_entries(
    entries: tuple[ManagedTreeEntry, ...],
    manifest: dict[str, Any],
) -> None:
    directories = {
        entry.relative_path
        for entry in entries
        if entry.kind is ManagedTreeEntryKind.DIRECTORY
    }
    if directories != {"clips"}:
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role="delivery_manifest",
            reason_code="verification.file_set_mismatch",
        )
    files = {
        entry.relative_path: entry
        for entry in entries
        if entry.kind is ManagedTreeEntryKind.REGULAR_FILE
    }
    unexpected_sidecars = {
        path
        for path in files
        if path.lower().endswith(".srt") and path != "transcript.srt"
    }
    if unexpected_sidecars:
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role="delivery_manifest",
            reason_code="verification.sidecar_subtitle_present",
        )
    artifacts = {artifact["path"]: artifact for artifact in manifest["files"]}
    if set(files) != {"manifest.json", *artifacts}:
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role="delivery_manifest",
            reason_code="verification.file_set_mismatch",
        )
    for path, artifact in artifacts.items():
        if files[path].byte_length != artifact["byte_length"]:
            raise _verification_failure(
                operation="delivery.verify_files",
                artifact_role=artifact["role"],
                reason_code="verification.length_mismatch",
            )


def _safe_delivery_path(
    path: str,
    *,
    artifact_role: str,
) -> None:
    encoded = path.encode("utf-8")
    parts = path.split("/")
    if (
        not path
        or len(encoded) > 4096
        or path.startswith("/")
        or path.endswith("/")
        or "//" in path
        or "\\" in path
        or "\x00" in path
        or unicodedata.normalize("NFC", path) != path
        or any(
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > 255
            or not part.isprintable()
            for part in parts
        )
    ):
        raise _verification_failure(
            operation="delivery.verify_files",
            artifact_role=artifact_role,
            reason_code="verification.path_unsafe",
        )


def _validate_transcript_schema(transcript: dict[str, Any]) -> None:
    _object(
        transcript,
        frozenset(
            {
                "chunks",
                "run_id",
                "schema_version",
                "source_duration_ms",
                "speech_presence",
                "transcript_id",
            }
        ),
    )
    if transcript["schema_version"] != "transcript.v1":
        raise _SchemaInvalid
    _string(transcript["run_id"])
    _string(transcript["transcript_id"], nonempty=True)
    speech_presence = _string(transcript["speech_presence"])
    if speech_presence not in {"present", "absent"}:
        raise _SchemaInvalid
    source_duration_ms = _positive_integer(transcript["source_duration_ms"])
    chunks = _array(transcript["chunks"])
    if (speech_presence == "present") != bool(chunks):
        raise _SchemaInvalid

    previous_end = 0
    for value in chunks:
        chunk = _object_with_optional(
            value,
            required=frozenset(
                {
                    "end_ms",
                    "start_ms",
                    "text",
                    "transcript_chunk_id",
                }
            ),
            optional=frozenset({"char_spans_ms"}),
        )
        _string(chunk["transcript_chunk_id"], nonempty=True)
        start_ms = _nonnegative_integer(chunk["start_ms"])
        end_ms = _positive_integer(chunk["end_ms"])
        if not previous_end <= start_ms < end_ms <= source_duration_ms:
            raise _SchemaInvalid
        previous_end = end_ms
        text = _string(chunk["text"])
        if not text.strip():
            raise _SchemaInvalid
        if "char_spans_ms" not in chunk:
            continue
        spans = _array(chunk["char_spans_ms"])
        if len(spans) != len(text):
            raise _SchemaInvalid
        previous_span_end = start_ms
        for value in spans:
            span = _object(
                value,
                frozenset({"end_ms", "start_ms"}),
            )
            span_start = _nonnegative_integer(span["start_ms"])
            span_end = _positive_integer(span["end_ms"])
            if not (previous_span_end <= span_start < span_end <= end_ms):
                raise _SchemaInvalid
            previous_span_end = span_end


def _validate_plan_schema(plan: dict[str, Any]) -> None:
    _object(
        plan,
        frozenset(
            {
                "candidate_count",
                "candidates",
                "plan_id",
                "published_count",
                "result_kind",
                "run_id",
                "schema_version",
                "transcript_id",
            }
        ),
    )
    if plan["schema_version"] != "clip_plan.v1":
        raise _SchemaInvalid
    _string(plan["run_id"])
    _string(plan["plan_id"], nonempty=True)
    _string(plan["transcript_id"], nonempty=True)
    if plan["result_kind"] not in {"clips", "empty"}:
        raise _SchemaInvalid
    candidates = _array(plan["candidates"])
    if _nonnegative_integer(plan["candidate_count"]) != len(candidates):
        raise _SchemaInvalid
    published_count = _nonnegative_integer(plan["published_count"])

    actual_published_count = 0
    for value in candidates:
        candidate = _object(
            value,
            frozenset(
                {
                    "boundary_remedy",
                    "candidate_id",
                    "final_range",
                    "initial_range",
                    "review",
                    "selection",
                    "transcript_chunk_ids",
                }
            ),
        )
        _string(candidate["candidate_id"], nonempty=True)
        transcript_chunk_ids = _array(candidate["transcript_chunk_ids"])
        if not transcript_chunk_ids:
            raise _SchemaInvalid
        for identifier in transcript_chunk_ids:
            _string(identifier, nonempty=True)
        initial_start, initial_end = _material_range(candidate["initial_range"])
        final_start, final_end = _material_range(candidate["final_range"])
        remedy = _object(
            candidate["boundary_remedy"],
            frozenset(
                {
                    "requested_end_ms",
                    "requested_start_ms",
                    "status",
                    "suggestion",
                }
            ),
        )
        if remedy["status"] not in {"not_needed", "applied", "invalid"}:
            raise _SchemaInvalid
        _string(remedy["suggestion"])
        requested_start = _nullable_nonnegative_integer(remedy["requested_start_ms"])
        requested_end = _nullable_nonnegative_integer(remedy["requested_end_ms"])
        if (requested_start is None) != (requested_end is None):
            raise _SchemaInvalid
        if (
            requested_start is not None
            and requested_end is not None
            and requested_start >= requested_end
        ):
            raise _SchemaInvalid

        review = _object(candidate["review"], _REVIEW_FIELDS)
        for field in ("topic_name", "title", "summary"):
            if not _string(review[field]).strip():
                raise _SchemaInvalid
        for field in (
            "boundary_fix_suggestion",
            "reject_reason",
        ):
            _string(review[field])
        _boolean(review["topic_complete"])
        _boolean(review["needs_human_review"])
        _bounded_integer(review["learning_value"], maximum=10)
        _bounded_integer(review["share_value"], maximum=10)
        _bounded_integer(
            review["publish_ready_score"],
            maximum=100,
        )
        if review["export_decision"] not in {
            "needs_review",
            "publish_ready",
            "reject",
        }:
            raise _SchemaInvalid
        keywords = _array(review["keywords"])
        if any(not _string(keyword).strip() for keyword in keywords):
            raise _SchemaInvalid
        boundary_fix_start = _nullable_nonnegative_integer(
            review["boundary_fix_start_ms"]
        )
        boundary_fix_end = _nullable_nonnegative_integer(review["boundary_fix_end_ms"])
        if (boundary_fix_start is None) != (boundary_fix_end is None):
            raise _SchemaInvalid
        if (
            boundary_fix_start is not None
            and boundary_fix_end is not None
            and boundary_fix_start >= boundary_fix_end
        ):
            raise _SchemaInvalid

        selection = candidate["selection"]
        if not isinstance(selection, dict):
            raise _SchemaInvalid
        if selection.get("outcome") == "published":
            _object(
                selection,
                frozenset({"outcome", "short_video_id"}),
            )
            _string(selection["short_video_id"], nonempty=True)
            actual_published_count += 1
        elif selection.get("outcome") == "rejected":
            _object(
                selection,
                frozenset(
                    {
                        "human_review_reason",
                        "needs_human_review",
                        "outcome",
                        "reason_code",
                    }
                ),
            )
            if selection["reason_code"] not in _REJECTION_REASONS:
                raise _SchemaInvalid
            _boolean(selection["needs_human_review"])
            _string(selection["human_review_reason"])
        else:
            raise _SchemaInvalid

        if remedy["status"] == "not_needed" and (
            remedy["suggestion"]
            or requested_start is not None
            or (initial_start, initial_end) != (final_start, final_end)
        ):
            raise _SchemaInvalid
        if remedy["status"] == "applied" and (
            requested_start is None
            or requested_end is None
            or (requested_start, requested_end) != (final_start, final_end)
            or final_start > initial_start
            or final_end < initial_end
            or (initial_start, initial_end) == (final_start, final_end)
        ):
            raise _SchemaInvalid
        if remedy["status"] == "invalid" and (
            (initial_start, initial_end) != (final_start, final_end)
        ):
            raise _SchemaInvalid

    if published_count != actual_published_count:
        raise _SchemaInvalid


def _validate_metadata_schema(metadata: dict[str, Any]) -> None:
    _object(
        metadata,
        frozenset(
            {
                "result_kind",
                "run_id",
                "schema_version",
                "series",
                "short_videos",
            }
        ),
    )
    if metadata["schema_version"] != "short_video_catalog.v1":
        raise _SchemaInvalid
    _string(metadata["run_id"])
    if metadata["result_kind"] not in {"clips", "empty"}:
        raise _SchemaInvalid
    for value in _array(metadata["short_videos"]):
        short_video = _object(
            value,
            frozenset(
                {
                    "duration_ms",
                    "end_ms",
                    "keywords",
                    "media",
                    "short_video_id",
                    "source_candidate_id",
                    "start_ms",
                    "subtitles",
                    "summary",
                    "title",
                    "topic_name",
                }
            ),
        )
        _string(short_video["short_video_id"], nonempty=True)
        _string(short_video["source_candidate_id"], nonempty=True)
        for field in ("summary", "title", "topic_name"):
            if not _string(short_video[field]).strip():
                raise _SchemaInvalid
        keywords = _array(short_video["keywords"])
        if any(not _string(keyword).strip() for keyword in keywords):
            raise _SchemaInvalid
        start_ms = _nonnegative_integer(short_video["start_ms"])
        end_ms = _positive_integer(short_video["end_ms"])
        duration_ms = _positive_integer(short_video["duration_ms"])
        if start_ms >= end_ms or duration_ms != end_ms - start_ms:
            raise _SchemaInvalid
        media = _object(
            short_video["media"],
            frozenset(
                {
                    "audio_required",
                    "container",
                    "path",
                    "video_required",
                }
            ),
        )
        _string(media["path"], nonempty=True)
        if (
            media["container"] != "mp4"
            or media["audio_required"] is not True
            or media["video_required"] is not True
        ):
            raise _SchemaInvalid
        subtitles = _object(
            short_video["subtitles"],
            frozenset({"kind"}),
        )
        if subtitles["kind"] != "burned_in":
            raise _SchemaInvalid

    for value in _array(metadata["series"]):
        series = _object(
            value,
            frozenset({"series_id", "short_video_ids", "topic"}),
        )
        _string(series["series_id"], nonempty=True)
        if not _string(series["topic"]).strip():
            raise _SchemaInvalid
        short_video_ids = _array(series["short_video_ids"])
        if len(short_video_ids) < 2:
            raise _SchemaInvalid
        for identifier in short_video_ids:
            _string(identifier, nonempty=True)


def _validate_identities(
    *,
    manifest: dict[str, Any],
    transcript: dict[str, Any],
    plan: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    _business_id(
        manifest["run_id"],
        RunId,
        artifact_role="delivery_manifest",
    )
    documents = manifest["documents"]
    _business_id(
        documents["transcript"]["transcript_id"],
        TranscriptId,
        artifact_role="delivery_manifest",
    )
    _business_id(
        documents["transcript_rendering"]["transcript_id"],
        TranscriptId,
        artifact_role="delivery_manifest",
    )
    _business_id(
        documents["plan"]["plan_id"],
        PlanId,
        artifact_role="delivery_manifest",
    )

    _business_id(
        transcript["transcript_id"],
        TranscriptId,
        artifact_role="faithful_transcript",
    )
    transcript_chunk_ids: set[str] = set()
    for chunk in transcript["chunks"]:
        identifier = chunk["transcript_chunk_id"]
        _business_id(
            identifier,
            TranscriptChunkId,
            artifact_role="faithful_transcript",
        )
        _add_unique(
            transcript_chunk_ids,
            identifier,
            artifact_role="faithful_transcript",
        )

    _business_id(
        plan["plan_id"],
        PlanId,
        artifact_role="clip_plan",
    )
    _business_id(
        plan["transcript_id"],
        TranscriptId,
        artifact_role="clip_plan",
    )
    candidate_ids: set[str] = set()
    selected_short_video_ids: set[str] = set()
    for candidate in plan["candidates"]:
        candidate_id = candidate["candidate_id"]
        _business_id(
            candidate_id,
            CandidateId,
            artifact_role="clip_plan",
        )
        _add_unique(
            candidate_ids,
            candidate_id,
            artifact_role="clip_plan",
        )
        referenced_chunk_ids: set[str] = set()
        for identifier in candidate["transcript_chunk_ids"]:
            _business_id(
                identifier,
                TranscriptChunkId,
                artifact_role="clip_plan",
            )
            _add_unique(
                referenced_chunk_ids,
                identifier,
                artifact_role="clip_plan",
            )
        selection = candidate["selection"]
        if selection["outcome"] == "published":
            short_video_id = selection["short_video_id"]
            _business_id(
                short_video_id,
                ShortVideoId,
                artifact_role="clip_plan",
            )
            _add_unique(
                selected_short_video_ids,
                short_video_id,
                artifact_role="clip_plan",
            )

    short_video_ids: set[str] = set()
    for short_video in metadata["short_videos"]:
        short_video_id = short_video["short_video_id"]
        _business_id(
            short_video_id,
            ShortVideoId,
            artifact_role="short_video_catalog",
        )
        _add_unique(
            short_video_ids,
            short_video_id,
            artifact_role="short_video_catalog",
        )
        _business_id(
            short_video["source_candidate_id"],
            CandidateId,
            artifact_role="short_video_catalog",
        )

    series_ids: set[str] = set()
    for series in metadata["series"]:
        series_id = series["series_id"]
        _business_id(
            series_id,
            SeriesId,
            artifact_role="short_video_catalog",
        )
        _add_unique(
            series_ids,
            series_id,
            artifact_role="short_video_catalog",
        )
        member_ids: set[str] = set()
        for identifier in series["short_video_ids"]:
            _business_id(
                identifier,
                ShortVideoId,
                artifact_role="short_video_catalog",
            )
            _add_unique(
                member_ids,
                identifier,
                artifact_role="short_video_catalog",
            )


def _validate_references(
    *,
    manifest: dict[str, Any],
    transcript: dict[str, Any],
    plan: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    documents = manifest["documents"]
    transcript_id = transcript["transcript_id"]
    if (
        documents["transcript"]["transcript_id"] != transcript_id
        or documents["transcript_rendering"]["transcript_id"] != transcript_id
    ):
        raise _reference_failure("delivery_manifest")
    if documents["plan"]["plan_id"] != plan["plan_id"]:
        raise _reference_failure("delivery_manifest")
    if plan["transcript_id"] != transcript_id:
        raise _reference_failure("clip_plan")
    source_duration_ms = manifest["source"]["duration_ms"]
    if transcript["source_duration_ms"] != source_duration_ms:
        raise _reference_failure("faithful_transcript")

    transcript_chunk_ids = {
        chunk["transcript_chunk_id"] for chunk in transcript["chunks"]
    }
    candidate_by_id: dict[str, dict[str, Any]] = {}
    published_by_candidate: dict[str, str] = {}
    for candidate in plan["candidates"]:
        candidate_id = candidate["candidate_id"]
        candidate_by_id[candidate_id] = candidate
        if not set(candidate["transcript_chunk_ids"]) <= transcript_chunk_ids:
            raise _reference_failure("clip_plan")
        for range_name in ("initial_range", "final_range"):
            if candidate[range_name]["end_ms"] > source_duration_ms:
                raise _reference_failure("clip_plan")
        remedy = candidate["boundary_remedy"]
        review = candidate["review"]
        if (
            remedy["suggestion"] != review["boundary_fix_suggestion"]
            or remedy["requested_start_ms"] != review["boundary_fix_start_ms"]
            or remedy["requested_end_ms"] != review["boundary_fix_end_ms"]
        ):
            raise _reference_failure("clip_plan")
        selection = candidate["selection"]
        if selection["outcome"] == "published":
            if (
                review["export_decision"] != "publish_ready"
                or not review["topic_complete"]
                or review["needs_human_review"]
                or review["reject_reason"]
                or remedy["status"] == "invalid"
            ):
                raise _reference_failure("clip_plan")
            published_by_candidate[candidate_id] = selection["short_video_id"]

    ordered_candidates = sorted(
        plan["candidates"],
        key=_candidate_material_order,
    )
    candidate_position = {
        candidate["candidate_id"]: position
        for position, candidate in enumerate(ordered_candidates)
    }
    short_video_by_source: dict[str, str] = {}
    short_video_by_id: dict[str, dict[str, Any]] = {}
    source_position_by_short_video_id: dict[str, int] = {}
    for short_video in metadata["short_videos"]:
        short_video_id = short_video["short_video_id"]
        source_candidate_id = short_video["source_candidate_id"]
        source_candidate = candidate_by_id.get(source_candidate_id)
        if source_candidate is None:
            raise _reference_failure("short_video_catalog")
        if source_candidate_id in short_video_by_source:
            raise _reference_failure("short_video_catalog")
        short_video_by_source[source_candidate_id] = short_video_id
        short_video_by_id[short_video_id] = short_video
        source_position_by_short_video_id[short_video_id] = candidate_position[
            source_candidate_id
        ]
        review = source_candidate["review"]
        final_range = source_candidate["final_range"]
        if (
            short_video["topic_name"] != review["topic_name"]
            or short_video["title"] != review["title"]
            or short_video["summary"] != review["summary"]
            or short_video["keywords"] != review["keywords"]
            or short_video["start_ms"] != final_range["start_ms"]
            or short_video["end_ms"] != final_range["end_ms"]
            or short_video["media"]["path"] != f"clips/{short_video_id}.mp4"
        ):
            raise _reference_failure("short_video_catalog")
        if short_video["end_ms"] > source_duration_ms:
            raise _reference_failure("short_video_catalog")

    if published_by_candidate != short_video_by_source:
        raise _reference_failure("short_video_catalog")
    manifest_media_paths = {
        artifact["path"]
        for artifact in manifest["files"]
        if artifact["role"] == "short_video_media"
    }
    metadata_media_paths = {
        short_video["media"]["path"] for short_video in metadata["short_videos"]
    }
    if manifest_media_paths != metadata_media_paths:
        raise _reference_failure("delivery_manifest")

    assigned_short_video_ids: set[str] = set()
    actual_series: list[tuple[str, tuple[str, ...]]] = []
    for series in metadata["series"]:
        member_ids = series["short_video_ids"]
        if not set(member_ids) <= set(short_video_by_id):
            raise _reference_failure("short_video_catalog")
        if assigned_short_video_ids.intersection(member_ids):
            raise _reference_failure("short_video_catalog")
        member_topics = {
            _canonical_text(short_video_by_id[identifier]["topic_name"])
            for identifier in member_ids
        }
        if member_topics != {_canonical_text(series["topic"])}:
            raise _reference_failure("short_video_catalog")
        positions = tuple(
            source_position_by_short_video_id[identifier] for identifier in member_ids
        )
        if positions != tuple(range(positions[0], positions[0] + len(positions))):
            raise _reference_failure("short_video_catalog")
        assigned_short_video_ids.update(member_ids)
        actual_series.append(
            (
                _canonical_text(series["topic"]),
                tuple(member_ids),
            )
        )
    if set(actual_series) != set(_expected_same_topic_series(plan)):
        raise _reference_failure("short_video_catalog")


def _validate_result_kind(
    *,
    manifest: dict[str, Any],
    plan: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    if not _manifest_result_summary_is_consistent(manifest):
        raise _result_kind_failure("delivery_manifest")
    result_kind = manifest["result_kind"]
    if {
        result_kind,
        plan["result_kind"],
        metadata["result_kind"],
    } != {result_kind}:
        raise _result_kind_failure("delivery_manifest")
    published_candidates = [
        candidate
        for candidate in plan["candidates"]
        if candidate["selection"]["outcome"] == "published"
    ]
    short_videos = metadata["short_videos"]
    series = metadata["series"]
    if plan["published_count"] != len(short_videos):
        raise _result_kind_failure("clip_plan")
    subtitle_count = manifest["execution"]["subtitle_optimization"]["short_video_count"]
    if subtitle_count != len(short_videos):
        raise _result_kind_failure("delivery_manifest")
    if result_kind == "empty":
        execution_counts = manifest["execution"]["subtitle_optimization"]
        if (
            published_candidates
            or short_videos
            or series
            or any(execution_counts.values())
        ):
            raise _result_kind_failure("delivery_manifest")
    elif not published_candidates or not short_videos:
        raise _result_kind_failure("delivery_manifest")


def _manifest_result_summary_is_consistent(
    manifest: dict[str, Any],
) -> bool:
    counts = manifest["execution"]["subtitle_optimization"]
    short_video_count = counts["short_video_count"]
    media_count = sum(
        artifact["role"] == "short_video_media"
        for artifact in manifest["files"]
    )
    if short_video_count != media_count:
        return False
    if manifest["result_kind"] == "empty":
        return not any(counts.values())
    return short_video_count > 0


def _result_kind_failure(
    artifact_role: str,
) -> DeliveryVerificationFailure:
    return _verification_failure(
        operation="delivery.verify_references",
        artifact_role=artifact_role,
        reason_code="verification.result_kind_mismatch",
    )


def _reference_failure(
    artifact_role: str,
) -> DeliveryVerificationFailure:
    return _verification_failure(
        operation="delivery.verify_references",
        artifact_role=artifact_role,
        reason_code="verification.reference_dangling",
    )


def _canonical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _expected_same_topic_series(
    plan: dict[str, Any],
) -> list[tuple[str, tuple[str, ...]]]:
    expected: list[tuple[str, tuple[str, ...]]] = []
    run_topic = ""
    run_short_video_ids: list[str] = []

    def flush() -> None:
        if len(run_short_video_ids) >= 2:
            expected.append((run_topic, tuple(run_short_video_ids)))

    for candidate in sorted(
        plan["candidates"],
        key=_candidate_material_order,
    ):
        selection = candidate["selection"]
        if selection["outcome"] != "published":
            flush()
            run_topic = ""
            run_short_video_ids = []
            continue
        topic = _canonical_text(candidate["review"]["topic_name"])
        if run_short_video_ids and topic != run_topic:
            flush()
            run_short_video_ids = []
        if not run_short_video_ids:
            run_topic = topic
        run_short_video_ids.append(selection["short_video_id"])
    flush()
    return expected


def _candidate_material_order(
    candidate: dict[str, Any],
) -> tuple[int, int, str]:
    final_range = candidate["final_range"]
    return (
        final_range["start_ms"],
        final_range["end_ms"],
        candidate["candidate_id"],
    )


def _render_transcript(transcript: dict[str, Any]) -> bytes:
    body = "".join(
        (
            f"{index}\n"
            f"{_srt_timestamp(chunk['start_ms'])} --> "
            f"{_srt_timestamp(chunk['end_ms'])}\n"
            f"{chunk['text']}\n\n"
        )
        for index, chunk in enumerate(transcript["chunks"], start=1)
    )
    try:
        return body.encode("utf-8", errors="strict")
    except UnicodeError:
        raise _verification_failure(
            operation="delivery.verify_schema",
            artifact_role="faithful_transcript",
            reason_code="verification.schema_invalid",
        ) from None


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _verify_media_file(
    directory: ManagedDirectoryCapability,
    path: str,
    *,
    artifact: dict[str, Any],
    expected_duration_ms: int,
    cancellation: CancellationToken,
) -> _ContentFact:
    def verify(stream: ManagedBinaryFile) -> _ContentFact:
        fact = _hash_stream(stream, cancellation)
        _assert_artifact_content(fact, artifact)
        stream.seek(0)
        _probe_media_stream(
            stream,
            expected_duration_ms=expected_duration_ms,
            cancellation=cancellation,
        )
        return fact

    try:
        return directory.location(path).use_binary("rb", verify)
    except WorkspaceFailure as failure:
        raise _file_access_failure(
            failure,
            artifact_role=artifact["role"],
        ) from None
    except OSError:
        raise _file_access_failure(
            None,
            artifact_role=artifact["role"],
        ) from None


def _probe_media_stream(
    stream: ManagedBinaryFile,
    *,
    expected_duration_ms: int,
    cancellation: CancellationToken,
) -> None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-count_packets",
        "-count_frames",
        "-show_entries",
        (
            "format=duration,format_name:"
            "stream=codec_type,nb_read_packets,nb_read_frames"
        ),
        "-of",
        "json",
        "pipe:0",
    ]
    cancellation.raise_if_cancelled()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                "LC_ALL": "C",
                "PATH": os.environ.get("PATH", os.defpath),
            },
        )
    except OSError:
        raise _media_failure("verification.media_invalid") from None
    if process.stdin is None or process.stdout is None or process.stderr is None:
        _stop_probe(process)
        _close_probe_pipes(process)
        raise _media_failure("verification.media_invalid")

    selector: selectors.BaseSelector | None = None
    stdout = bytearray()
    stderr_length = 0
    input_chunk = memoryview(b"")
    deadline = monotonic() + _PROBE_TIMEOUT_SECONDS
    try:
        selector = selectors.DefaultSelector()
        for pipe, event, label in (
            (process.stdin, selectors.EVENT_WRITE, "stdin"),
            (process.stdout, selectors.EVENT_READ, "stdout"),
            (process.stderr, selectors.EVENT_READ, "stderr"),
        ):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe.fileno(), event, label)
        while selector.get_map():
            cancellation.raise_if_cancelled()
            if monotonic() >= deadline:
                raise _media_failure("verification.media_invalid")
            for key, _event in selector.select(_PROBE_POLL_SECONDS):
                if key.data == "stdin":
                    if not input_chunk:
                        input_bytes = stream.read(_FILE_READ_BYTES)
                        if not input_bytes:
                            _close_probe_pipe(
                                selector,
                                process.stdin,
                            )
                            continue
                        input_chunk = memoryview(input_bytes)
                    try:
                        written = os.write(key.fd, input_chunk)
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        _close_probe_pipe(selector, process.stdin)
                        continue
                    if written <= 0:
                        raise _media_failure("verification.media_invalid")
                    input_chunk = input_chunk[written:]
                    continue
                try:
                    output = os.read(key.fd, _PROBE_IO_BYTES)
                except BlockingIOError:
                    continue
                if not output:
                    pipe = process.stdout if key.data == "stdout" else process.stderr
                    _close_probe_pipe(selector, pipe)
                    continue
                if key.data == "stdout":
                    stdout.extend(output)
                    if len(stdout) > _MAX_PROBE_OUTPUT_BYTES:
                        raise _media_failure("verification.media_invalid")
                else:
                    stderr_length += len(output)
                    if stderr_length > _MAX_PROBE_ERROR_BYTES:
                        raise _media_failure("verification.media_invalid")
        try:
            return_code = process.wait(timeout=_PROBE_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            raise _media_failure("verification.media_invalid") from None
        if return_code != 0 or not stdout or stderr_length:
            raise _media_failure("verification.media_invalid")
    except (OSError, ValueError):
        _stop_probe(process)
        raise _media_failure("verification.media_invalid") from None
    except BaseException:
        _stop_probe(process)
        raise
    finally:
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
        _close_probe_pipes(process)

    try:
        probe = json.loads(
            bytes(stdout),
            object_pairs_hook=_strict_json_object,
        )
        probe_object = _object_with_optional(
            probe,
            required=frozenset({"format", "streams"}),
            optional=frozenset({"programs"}),
        )
        streams = [
            _object(
                stream,
                frozenset(
                    {
                        "codec_type",
                        "nb_read_frames",
                        "nb_read_packets",
                    }
                ),
            )
            for stream in _array(probe_object["streams"])
        ]
        stream_types = {_string(stream["codec_type"]) for stream in streams}
        required_stream_counts = [
            (
                _positive_decimal_integer_string(stream["nb_read_packets"]),
                _positive_decimal_integer_string(stream["nb_read_frames"]),
            )
            for stream in streams
            if stream["codec_type"] in {"audio", "video"}
        ]
        format_value = _object(
            probe_object["format"],
            frozenset({"duration", "format_name"}),
        )
        format_names = set(_string(format_value["format_name"]).split(","))
        duration = Decimal(_string(format_value["duration"]))
        if "mp4" not in format_names or not duration.is_finite() or duration <= 0:
            raise _SchemaInvalid
        duration_delta_ms = abs(duration * 1000 - Decimal(expected_duration_ms))
    except (ValueError, DecimalException, RecursionError):
        raise _media_failure("verification.media_invalid") from None
    if not {"audio", "video"} <= stream_types:
        raise _media_failure("verification.stream_missing")
    if len(required_stream_counts) < 2:
        raise _media_failure("verification.media_invalid")
    if duration_delta_ms > _MEDIA_DURATION_TOLERANCE_MS:
        raise _media_failure("verification.duration_mismatch")


def _close_probe_pipe(
    selector: selectors.BaseSelector,
    pipe: Any,
) -> None:
    try:
        selector.unregister(pipe.fileno())
    except (KeyError, OSError, ValueError):
        pass
    try:
        pipe.close()
    except OSError:
        pass


def _stop_probe(process: subprocess.Popen[bytes]) -> None:
    try:
        running = process.poll() is None
    except OSError:
        running = True
    if running:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass
    try:
        process.wait(timeout=_PROBE_STOP_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=_PROBE_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _close_probe_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _media_failure(reason_code: str) -> DeliveryVerificationFailure:
    return _verification_failure(
        operation="delivery.verify_media",
        artifact_role="short_video_media",
        reason_code=reason_code,
    )


def _business_id(
    value: str,
    identity_type: type[BusinessId],
    *,
    artifact_role: str,
) -> BusinessId:
    try:
        return identity_type(value)
    except (TypeError, ValueError):
        raise _verification_failure(
            operation="delivery.verify_references",
            artifact_role=artifact_role,
            reason_code="verification.identity_invalid",
        ) from None


def _add_unique(
    values: set[str],
    value: str,
    *,
    artifact_role: str,
) -> None:
    if value in values:
        raise _verification_failure(
            operation="delivery.verify_references",
            artifact_role=artifact_role,
            reason_code="verification.identity_duplicate",
        )
    values.add(value)


def _json_document(
    contents: bytes,
    *,
    artifact_role: str,
    validator: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    try:
        document = json.loads(
            contents,
            object_pairs_hook=_strict_json_object,
        )
        if not isinstance(document, dict):
            raise _SchemaInvalid
        validator(document)
        return document
    except (ValueError, RecursionError):
        raise _verification_failure(
            operation="delivery.verify_schema",
            artifact_role=artifact_role,
            reason_code="verification.schema_invalid",
        ) from None


def _object(
    value: Any,
    fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _SchemaInvalid
    return value


def _object_with_optional(
    value: Any,
    *,
    required: frozenset[str],
    optional: frozenset[str],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not set(value) <= required | optional
    ):
        raise _SchemaInvalid
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise _SchemaInvalid
    return value


def _string(value: Any, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _SchemaInvalid
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise _SchemaInvalid from None
    return value


def _positive_decimal_integer_string(value: Any) -> int:
    text = _string(value, nonempty=True)
    if not text.isascii() or not text.isdecimal():
        raise _SchemaInvalid
    result = int(text)
    if result <= 0:
        raise _SchemaInvalid
    return result


def _nonnegative_integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _SchemaInvalid
    return value


def _positive_integer(value: Any) -> int:
    result = _nonnegative_integer(value)
    if result == 0:
        raise _SchemaInvalid
    return result


def _nullable_nonnegative_integer(value: Any) -> int | None:
    if value is None:
        return None
    return _nonnegative_integer(value)


def _bounded_integer(value: Any, *, maximum: int) -> int:
    result = _nonnegative_integer(value)
    if result > maximum:
        raise _SchemaInvalid
    return result


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _SchemaInvalid
    return value


def _material_range(value: Any) -> tuple[int, int]:
    range_value = _object(
        value,
        frozenset({"end_ms", "start_ms"}),
    )
    start_ms = _nonnegative_integer(range_value["start_ms"])
    end_ms = _positive_integer(range_value["end_ms"])
    if start_ms >= end_ms:
        raise _SchemaInvalid
    return start_ms, end_ms


def _utc_timestamp(value: Any) -> int:
    text = _string(value)
    if _RFC3339_UTC.fullmatch(text) is None:
        raise _SchemaInvalid
    without_zone = text.removesuffix("Z")
    if "." in without_zone:
        seconds_text, fraction = without_zone.rsplit(".", 1)
    else:
        seconds_text = without_zone
        fraction = ""
    try:
        parsed = datetime.fromisoformat(seconds_text + "+00:00")
    except ValueError:
        raise _SchemaInvalid from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _SchemaInvalid
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    whole_seconds = delta.days * 86_400 + delta.seconds
    fractional_nanoseconds = int(fraction.ljust(9, "0")) if fraction else 0
    return whole_seconds * 1_000_000_000 + fractional_nanoseconds


def _verification_failure(
    *,
    operation: str,
    artifact_role: str,
    reason_code: str,
) -> DeliveryVerificationFailure:
    return DeliveryVerificationFailure(
        {
            "operation": operation,
            "artifact_role": artifact_role,
            "reason_code": reason_code,
        }
    )
