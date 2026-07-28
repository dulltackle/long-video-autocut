"""根据已验证素材与完整转写形成候选方案。"""

from dataclasses import dataclass
from typing import Protocol

from video_auto_editor.configuration._model import ClipPolicy, CourseContext
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription

from ._model import (
    CandidatePlan,
    CandidateReviewContext,
    ClipCandidate,
    TranscriptExcerpt,
)


class _CompleteTranscript(Protocol):
    """候选规划消费的最小完整转写结构。"""

    @property
    def transcript_id(self) -> TranscriptId:
        ...

    @property
    def chunks(self) -> tuple[object, ...]:
        ...


@dataclass(frozen=True, slots=True)
class _TranscriptChunkSnapshot:
    transcript_chunk_id: TranscriptChunkId
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    initial_start_ms: int
    initial_end_ms: int
    preceding_chunks: tuple[_TranscriptChunkSnapshot, ...]
    candidate_chunks: tuple[_TranscriptChunkSnapshot, ...]
    following_chunks: tuple[_TranscriptChunkSnapshot, ...]


class ClipPlanning:
    """以一个入口隐藏候选生成与初始边界算法。"""

    __slots__ = ()

    @classmethod
    def prepare(
        cls,
        source_analysis: SourceDescription,
        transcript: _CompleteTranscript,
        course_context: CourseContext | None,
        clip_policy: ClipPolicy,
    ) -> CandidatePlan:
        """形成新的不可变候选方案。"""
        if not isinstance(source_analysis, SourceDescription):
            raise TypeError("候选规划只接受已验证素材描述")
        transcript_id = getattr(transcript, "transcript_id", None)
        if not isinstance(transcript_id, TranscriptId):
            raise TypeError("候选规划只接受带类型化标识的完整转写")
        chunks = _snapshot_transcript_chunks(
            getattr(transcript, "chunks", None),
            source_analysis.duration_ms,
        )
        context_snapshot = _snapshot_course_context(course_context)
        policy = _validate_clip_policy(clip_policy)
        specifications = _build_candidate_specs(chunks, policy)
        return CandidatePlan._from_preparation(
            plan_id=PlanId.new(),
            transcript_id=transcript_id,
            source_duration_ms=source_analysis.duration_ms,
            course_context=context_snapshot,
            clip_policy=policy,
            candidates=tuple(
                ClipCandidate(
                    candidate_id=CandidateId.new(),
                    initial_start_ms=specification.initial_start_ms,
                    initial_end_ms=specification.initial_end_ms,
                    transcript_chunk_ids=tuple(
                        chunk.transcript_chunk_id
                        for chunk in specification.candidate_chunks
                    ),
                    review_context=CandidateReviewContext(
                        preceding_chunks=tuple(
                            _to_excerpt(chunk)
                            for chunk in specification.preceding_chunks
                        ),
                        candidate_chunks=tuple(
                            _to_excerpt(chunk)
                            for chunk in specification.candidate_chunks
                        ),
                        following_chunks=tuple(
                            _to_excerpt(chunk)
                            for chunk in specification.following_chunks
                        ),
                    ),
                )
                for specification in specifications
            ),
        )


def _snapshot_transcript_chunks(
    chunks: object,
    source_duration_ms: int,
) -> tuple[_TranscriptChunkSnapshot, ...]:
    if not isinstance(chunks, tuple):
        raise TypeError("完整转写的文本块集合必须不可变")
    snapshots = []
    identifiers = set()
    for chunk in chunks:
        transcript_chunk_id = getattr(
            chunk,
            "transcript_chunk_id",
            None,
        )
        if not isinstance(transcript_chunk_id, TranscriptChunkId):
            raise TypeError("转写文本块必须使用类型化标识")
        if transcript_chunk_id in identifiers:
            raise ValueError("完整转写不能包含重复文本块标识")
        identifiers.add(transcript_chunk_id)
        start_ms = getattr(chunk, "start_ms", None)
        end_ms = getattr(chunk, "end_ms", None)
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
        ):
            raise TypeError("转写文本块时间必须使用整数毫秒")
        if not 0 <= start_ms < end_ms <= source_duration_ms:
            raise ValueError("转写文本块必须位于素材时间范围内")
        text = getattr(chunk, "text", None)
        if not isinstance(text, str):
            raise TypeError("转写文本块正文必须是字符串")
        if not text.strip():
            raise ValueError("转写文本块正文不能为空")
        snapshots.append(
            _TranscriptChunkSnapshot(
                transcript_chunk_id=transcript_chunk_id,
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )
    return tuple(
        sorted(
            snapshots,
            key=lambda chunk: (
                chunk.start_ms,
                chunk.end_ms,
                chunk.text,
                str(chunk.transcript_chunk_id),
            ),
        )
    )


def _validate_clip_policy(clip_policy: object) -> ClipPolicy:
    if not isinstance(clip_policy, ClipPolicy):
        raise TypeError("候选规划只接受严格校验后的短视频策略")
    duration_values = (
        clip_policy.min_duration_seconds,
        clip_policy.target_duration_seconds,
        clip_policy.max_duration_seconds,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in duration_values
    ):
        raise TypeError("短视频策略时长必须是整数秒")
    if any(value <= 0 for value in duration_values):
        raise ValueError("短视频策略时长必须是正整数")
    if any(value > 3_600 for value in duration_values):
        raise ValueError("短视频策略时长超出认证范围")
    if not (
        clip_policy.min_duration_seconds
        <= clip_policy.target_duration_seconds
        <= clip_policy.max_duration_seconds
    ):
        raise ValueError("短视频策略必须满足最短、目标、最长时长顺序")
    max_clips = clip_policy.max_clips
    if max_clips is not None and (
        not isinstance(max_clips, int)
        or isinstance(max_clips, bool)
        or max_clips <= 0
        or max_clips > 1_000
    ):
        raise ValueError("短视频策略数量上限超出认证范围")
    threshold = clip_policy.publish_ready_threshold
    if (
        not isinstance(threshold, int)
        or isinstance(threshold, bool)
        or not 0 <= threshold <= 100
    ):
        raise ValueError("短视频策略发布就绪门槛必须位于 0 到 100")
    return clip_policy


def _snapshot_course_context(
    course_context: object,
) -> CourseContext | None:
    if course_context is None:
        return None
    if not isinstance(course_context, CourseContext):
        raise TypeError("候选规划只接受严格校验后的课程上下文")
    for value, field_name in (
        (course_context.schema_version, "schema_version"),
        (course_context.sha256, "sha256"),
        (course_context.course_topic, "course_topic"),
    ):
        if not isinstance(value, str):
            raise TypeError(f"课程上下文 {field_name} 必须是字符串")
    attribution = course_context.attribution
    if attribution is not None and not isinstance(attribution, str):
        raise TypeError("课程上下文 attribution 必须是字符串")
    priority_topics = _snapshot_context_strings(
        course_context.priority_topics,
        "priority_topics",
    )
    excluded_content = _snapshot_context_strings(
        course_context.excluded_content,
        "excluded_content",
    )
    return CourseContext(
        schema_version=course_context.schema_version,
        sha256=course_context.sha256,
        course_topic=course_context.course_topic,
        attribution=attribution,
        priority_topics=priority_topics,
        excluded_content=excluded_content,
    )


def _snapshot_context_strings(
    values: object,
    field_name: str,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"课程上下文 {field_name} 必须是字符串集合")
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"课程上下文 {field_name} 必须只含字符串")
    return tuple(values)


def _build_candidate_specs(
    chunks: tuple[_TranscriptChunkSnapshot, ...],
    clip_policy: ClipPolicy,
) -> tuple[_CandidateSpec, ...]:
    minimum_ms = clip_policy.min_duration_seconds * 1_000
    target_ms = clip_policy.target_duration_seconds * 1_000
    maximum_ms = clip_policy.max_duration_seconds * 1_000
    specifications = []
    start_position = 0

    while start_position < len(chunks):
        start_ms = chunks[start_position].start_ms
        end_ms = start_ms
        end_position = start_position - 1

        for position in range(start_position, len(chunks)):
            proposed_end_ms = max(end_ms, chunks[position].end_ms)
            if proposed_end_ms - start_ms > maximum_ms:
                break
            end_ms = proposed_end_ms
            end_position = position
            if end_ms - start_ms >= target_ms:
                while (
                    end_position + 1 < len(chunks)
                    and chunks[end_position + 1].start_ms >= start_ms
                    and chunks[end_position + 1].end_ms <= end_ms
                ):
                    end_position += 1
                remaining_chunks = chunks[end_position + 1 :]
                if remaining_chunks:
                    remaining_end_ms = max(
                        chunk.end_ms for chunk in remaining_chunks
                    )
                    remaining_span_ms = (
                        remaining_end_ms
                        - remaining_chunks[0].start_ms
                    )
                    if (
                        remaining_end_ms > end_ms
                        and remaining_span_ms < minimum_ms
                        and remaining_end_ms - start_ms
                        <= maximum_ms
                    ):
                        end_ms = remaining_end_ms
                        end_position = len(chunks) - 1
                break

        if (
            end_position >= start_position
            and minimum_ms <= end_ms - start_ms <= maximum_ms
        ):
            candidate_chunks = chunks[
                start_position : end_position + 1
            ]
            specifications.append(
                _CandidateSpec(
                    initial_start_ms=start_ms,
                    initial_end_ms=end_ms,
                    preceding_chunks=_preceding_context(
                        chunks,
                        start_position,
                        start_ms,
                    ),
                    candidate_chunks=candidate_chunks,
                    following_chunks=_following_context(
                        chunks,
                        end_position,
                        end_ms,
                    ),
                )
            )
            start_position = end_position + 1
            continue

        start_position += 1

    return tuple(
        sorted(
            specifications,
            key=lambda item: (
                item.initial_start_ms,
                item.initial_end_ms,
                tuple(
                    str(chunk.transcript_chunk_id)
                    for chunk in item.candidate_chunks
                ),
            ),
        )
    )


def _preceding_context(
    chunks: tuple[_TranscriptChunkSnapshot, ...],
    start_position: int,
    candidate_start_ms: int,
) -> tuple[_TranscriptChunkSnapshot, ...]:
    preceding = tuple(
        chunk
        for chunk in chunks[:start_position]
        if chunk.end_ms <= candidate_start_ms
    )
    return preceding[-1:]


def _following_context(
    chunks: tuple[_TranscriptChunkSnapshot, ...],
    end_position: int,
    candidate_end_ms: int,
) -> tuple[_TranscriptChunkSnapshot, ...]:
    following = tuple(
        chunk
        for chunk in chunks[end_position + 1 :]
        if chunk.start_ms >= candidate_end_ms
    )
    return following[:1]


def _to_excerpt(
    chunk: _TranscriptChunkSnapshot,
) -> TranscriptExcerpt:
    return TranscriptExcerpt(
        transcript_chunk_id=chunk.transcript_chunk_id,
        start_ms=chunk.start_ms,
        end_ms=chunk.end_ms,
        text=chunk.text,
    )
