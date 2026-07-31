"""在受管暂存目录构建版本化标准交付物。"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from video_auto_editor.clip_planning import (
    FinalCandidate,
    PublishedSelection,
    RejectedSelection,
)
from video_auto_editor.runtime.cancellation import CancellationRequested
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.result import ResultKind
from video_auto_editor.workspace import WorkspaceFailure

from ._media import BuiltMedia, export_short_videos
from ._model import DeliveryBuildFailure, DeliveryBuildRequest
from .capability import UnverifiedDelivery


_ARTIFACTS = (
    ("metadata.json", "short_video_catalog", "application/json"),
    ("plan.json", "clip_plan", "application/json"),
    ("report.md", "human_report", "text/markdown"),
    ("transcript.json", "faithful_transcript", "application/json"),
    (
        "transcript.srt",
        "faithful_transcript_rendering",
        "application/x-subrip",
    ),
)


@dataclass(frozen=True, slots=True)
class _ArtifactFact:
    path: str
    role: str
    media_type: str
    byte_length: int
    sha256: str


class DeliveryBuild:
    """以一个入口隐藏文档渲染、媒体导出与完整清单形成。"""

    __slots__ = ()

    @classmethod
    def build(cls, request: DeliveryBuildRequest) -> UnverifiedDelivery:
        """只在受管暂存目录形成未验证交付 capability。"""
        if not isinstance(request, DeliveryBuildRequest):
            raise TypeError("交付构建只接受 DeliveryBuildRequest")
        request.cancellation.raise_if_cancelled()
        safe_failure: DeliveryBuildFailure
        try:
            return cls._build(request)
        except CancellationRequested:
            raise
        except DeliveryBuildFailure as failure:
            safe_failure = DeliveryBuildFailure(
                failure.error_code,
                failure.diagnostics,
            )
        except (WorkspaceFailure, FileExistsError):
            safe_failure = DeliveryBuildFailure(
                ErrorCode.DELIVERY_BUILD_FAILED,
                {
                    "artifact_role": "delivery_manifest",
                    "reason_code": "delivery.write_failed",
                },
            )
        raise safe_failure from None

    @classmethod
    def _build(cls, request: DeliveryBuildRequest) -> UnverifiedDelivery:
        staging = request.staging_directory
        documents = _build_documents(request)
        try:
            staging.location("clips").mkdir()
        except (WorkspaceFailure, FileExistsError):
            raise DeliveryBuildFailure(
                ErrorCode.DELIVERY_BUILD_FAILED,
                {
                    "artifact_role": "short_video_media",
                    "reason_code": "delivery.write_failed",
                },
            ) from None
        try:
            built_media = export_short_videos(request)
        except (WorkspaceFailure, FileExistsError):
            raise DeliveryBuildFailure(
                ErrorCode.DELIVERY_BUILD_FAILED,
                {
                    "artifact_role": "short_video_media",
                    "reason_code": "delivery.write_failed",
                },
            ) from None
        artifacts = [_media_artifact(item) for item in built_media]
        for path, role, _media_type in _ARTIFACTS:
            request.cancellation.raise_if_cancelled()
            contents = documents[path]
            try:
                staging.location(path).publish_bytes_atomically(contents)
            except (WorkspaceFailure, FileExistsError):
                raise DeliveryBuildFailure(
                    ErrorCode.DELIVERY_BUILD_FAILED,
                    {
                        "artifact_role": role,
                        "reason_code": "delivery.write_failed",
                    },
                ) from None
            artifacts.append(
                _ArtifactFact(
                    path=path,
                    role=role,
                    media_type=_media_type,
                    byte_length=len(contents),
                    sha256=(
                        "sha256:"
                        + hashlib.sha256(contents).hexdigest()
                    ),
                )
            )

        request.cancellation.raise_if_cancelled()
        manifest = _manifest_document(request, tuple(artifacts))
        manifest_bytes = _json_bytes(manifest, "delivery_manifest")
        try:
            staging.location("manifest.json").publish_bytes_atomically(
                manifest_bytes
            )
        except (WorkspaceFailure, FileExistsError):
            raise DeliveryBuildFailure(
                ErrorCode.DELIVERY_BUILD_FAILED,
                {
                    "artifact_role": "delivery_manifest",
                    "reason_code": "delivery.write_failed",
                },
            ) from None
        request.cancellation.raise_if_cancelled()
        return UnverifiedDelivery._from_build(request.run_id, staging)


def _build_documents(request: DeliveryBuildRequest) -> dict[str, bytes]:
    return {
        "metadata.json": _json_bytes(
            _metadata_document(request),
            "short_video_catalog",
        ),
        "plan.json": _json_bytes(_plan_document(request), "clip_plan"),
        "report.md": _report_bytes(request),
        "transcript.json": _json_bytes(
            _transcript_document(request),
            "faithful_transcript",
        ),
        "transcript.srt": _transcript_srt_bytes(request),
    }


def _transcript_document(request: DeliveryBuildRequest) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    for chunk in request.transcript.chunks:
        item: dict[str, Any] = {
            "transcript_chunk_id": str(chunk.transcript_chunk_id),
            "start_ms": chunk.start_ms,
            "end_ms": chunk.end_ms,
            "text": chunk.text,
        }
        if chunk.character_spans is not None:
            item["char_spans_ms"] = [
                {"start_ms": span.start_ms, "end_ms": span.end_ms}
                for span in chunk.character_spans
            ]
        chunks.append(item)
    return {
        "schema_version": "transcript.v1",
        "run_id": str(request.run_id),
        "transcript_id": str(request.transcript.transcript_id),
        "speech_presence": request.transcript.speech_presence.value,
        "source_duration_ms": request.source.duration_ms,
        "chunks": chunks,
    }


def _plan_document(request: DeliveryBuildRequest) -> dict[str, Any]:
    return {
        "schema_version": "clip_plan.v1",
        "run_id": str(request.run_id),
        "plan_id": str(request.plan.plan_id),
        "transcript_id": str(request.plan.transcript_id),
        "result_kind": request.plan.result_kind.value,
        "candidate_count": len(request.plan.candidates),
        "published_count": len(request.plan.short_videos),
        "candidates": [
            _candidate_document(candidate)
            for candidate in request.plan.candidates
        ],
    }


def _candidate_document(candidate: FinalCandidate) -> dict[str, Any]:
    review = candidate.review
    remedy = candidate.boundary_remedy
    if isinstance(candidate.selection, PublishedSelection):
        selection: Mapping[str, Any] = {
            "outcome": "published",
            "short_video_id": str(candidate.selection.short_video_id),
        }
    elif isinstance(candidate.selection, RejectedSelection):
        selection = {
            "outcome": "rejected",
            "reason_code": candidate.selection.reason_code.value,
            "needs_human_review": (
                candidate.selection.needs_human_review
            ),
            "human_review_reason": (
                candidate.selection.human_review_reason
            ),
        }
    else:
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "operation": "delivery.serialize",
                "artifact_role": "clip_plan",
                "reason_code": "delivery.invariant_violation",
            },
        )
    return {
        "candidate_id": str(candidate.candidate_id),
        "transcript_chunk_ids": [
            str(identifier)
            for identifier in candidate.transcript_chunk_ids
        ],
        "initial_range": {
            "start_ms": candidate.initial_start_ms,
            "end_ms": candidate.initial_end_ms,
        },
        "final_range": {
            "start_ms": candidate.final_start_ms,
            "end_ms": candidate.final_end_ms,
        },
        "boundary_remedy": {
            "status": remedy.status.value,
            "suggestion": remedy.suggestion,
            "requested_start_ms": remedy.requested_start_ms,
            "requested_end_ms": remedy.requested_end_ms,
        },
        "review": {
            "topic_name": review.topic_name,
            "topic_complete": review.topic_complete,
            "learning_value": review.learning_value,
            "share_value": review.share_value,
            "publish_ready_score": review.publish_ready_score,
            "export_decision": review.export_decision.value,
            "title": review.title,
            "summary": review.summary,
            "keywords": list(review.keywords),
            "needs_human_review": review.needs_human_review,
            "reject_reason": review.reject_reason,
            "boundary_fix_suggestion": (
                review.boundary_fix_suggestion
            ),
            "boundary_fix_start_ms": review.boundary_fix_start_ms,
            "boundary_fix_end_ms": review.boundary_fix_end_ms,
        },
        "selection": selection,
    }


def _metadata_document(request: DeliveryBuildRequest) -> dict[str, Any]:
    return {
        "schema_version": "short_video_catalog.v1",
        "run_id": str(request.run_id),
        "result_kind": request.plan.result_kind.value,
        "short_videos": [
            {
                "short_video_id": str(item.short_video_id),
                "source_candidate_id": str(item.source_candidate_id),
                "topic_name": item.topic_name,
                "title": item.title,
                "summary": item.summary,
                "keywords": list(item.keywords),
                "start_ms": item.final_start_ms,
                "end_ms": item.final_end_ms,
                "duration_ms": item.duration_ms,
                "media": {
                    "path": f"clips/{item.short_video_id}.mp4",
                    "container": "mp4",
                    "video_required": True,
                    "audio_required": True,
                },
                "subtitles": {"kind": "burned_in"},
            }
            for item in request.plan.short_videos
        ],
        "series": [
            {
                "series_id": str(series.series_id),
                "topic": series.topic,
                "short_video_ids": [
                    str(identifier)
                    for identifier in series.short_video_ids
                ],
            }
            for series in request.plan.series
        ],
    }


def _manifest_document(
    request: DeliveryBuildRequest,
    artifacts: tuple[_ArtifactFact, ...],
) -> dict[str, Any]:
    facts = request.subtitles.execution_facts
    files = [
        {
            "path": artifact.path,
            "role": artifact.role,
            "media_type": artifact.media_type,
            "byte_length": artifact.byte_length,
            "sha256": artifact.sha256,
        }
        for artifact in sorted(artifacts, key=lambda item: item.path)
    ]
    return {
        "schema_version": "delivery_manifest.v1",
        "run_id": str(request.run_id),
        "terminal_state": "succeeded",
        "result_kind": request.plan.result_kind.value,
        "started_at": _timestamp(request.started_at),
        "published_at": _timestamp(request.published_at),
        "application_version": request.application_version,
        "source": {
            "sha256": request.source.sha256,
            "byte_length": request.source.byte_length,
            "duration_ms": request.source.duration_ms,
        },
        "documents": {
            "transcript": {
                "path": "transcript.json",
                "transcript_id": str(request.transcript.transcript_id),
            },
            "transcript_rendering": {
                "path": "transcript.srt",
                "transcript_id": str(request.transcript.transcript_id),
            },
            "plan": {
                "path": "plan.json",
                "plan_id": str(request.plan.plan_id),
            },
            "metadata": {"path": "metadata.json"},
            "report": {"path": "report.md"},
        },
        "execution": {
            "subtitle_optimization": {
                "short_video_count": facts.short_video_count,
                "window_count": facts.window_count,
                "model_request_count": facts.model_request_count,
                "cache_hit_count": facts.cache_hit_count,
                "cache_miss_count": facts.cache_miss_count,
                "semantic_retry_count": facts.semantic_retry_count,
                "transport_attempt_count": facts.transport_attempt_count,
                "transport_retry_count": facts.transport_retry_count,
            }
        },
        "files": files,
    }


def _transcript_srt_bytes(request: DeliveryBuildRequest) -> bytes:
    try:
        body = "".join(
            (
                f"{index}\n"
                f"{_srt_timestamp(chunk.start_ms)} --> "
                f"{_srt_timestamp(chunk.end_ms)}\n"
                f"{chunk.text}\n\n"
            )
            for index, chunk in enumerate(
                request.transcript.chunks,
                start=1,
            )
        )
        return body.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "operation": "delivery.render_transcript",
                "artifact_role": "faithful_transcript_rendering",
                "reason_code": "delivery.render_failed",
            },
        ) from None


def _report_bytes(request: DeliveryBuildRequest) -> bytes:
    try:
        if request.plan.result_kind is ResultKind.EMPTY:
            outcome = (
                "本次运行成功完成，形成有效空结果；"
                "没有候选满足发布条件，短视频集合为空。"
            )
        else:
            outcome = (
                "本次运行成功完成，"
                f"共形成 {len(request.plan.short_videos)} 条短视频。"
            )
        report = (
            "# 直播拆条报告\n\n"
            f"- 运行标识：`{request.run_id}`\n"
            f"- 结果类型：`{request.plan.result_kind.value}`\n"
            f"- 候选数量：{len(request.plan.candidates)}\n"
            f"- 发布数量：{len(request.plan.short_videos)}\n\n"
            "## 结果说明\n\n"
            f"{outcome}\n"
        )
        return report.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "operation": "delivery.render_report",
                "artifact_role": "human_report",
                "reason_code": "delivery.render_failed",
            },
        ) from None


def _json_bytes(document: Mapping[str, Any], role: str) -> bytes:
    try:
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "operation": "delivery.serialize",
                "artifact_role": role,
                "reason_code": "delivery.serialization_failed",
            },
        ) from None


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _media_artifact(item: BuiltMedia) -> _ArtifactFact:
    return _ArtifactFact(
        path=item.path,
        role="short_video_media",
        media_type="video/mp4",
        byte_length=item.byte_length,
        sha256=item.sha256,
    )
