"""ClipPlanning 深模块拥有的不可变业务事实。"""

from dataclasses import dataclass, field
from typing import TypeVar

from video_auto_editor.configuration._model import ClipPolicy, CourseContext
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
    TranscriptChunkId,
    TranscriptId,
)

_CandidatePlanT = TypeVar("_CandidatePlanT", bound="CandidatePlan")


@dataclass(frozen=True, slots=True)
class TranscriptExcerpt:
    """供主题评审消费的单个忠实转写文本块快照。"""

    transcript_chunk_id: TranscriptChunkId
    start_ms: int
    end_ms: int
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CandidateReviewContext:
    """候选正文及其紧邻的前后转写上下文。"""

    preceding_chunks: tuple[TranscriptExcerpt, ...]
    candidate_chunks: tuple[TranscriptExcerpt, ...]
    following_chunks: tuple[TranscriptExcerpt, ...]


@dataclass(frozen=True, slots=True)
class ClipCandidate:
    """等待主题评审的候选片段及其初始素材边界。"""

    candidate_id: CandidateId
    initial_start_ms: int
    initial_end_ms: int
    transcript_chunk_ids: tuple[TranscriptChunkId, ...]
    review_context: CandidateReviewContext


@dataclass(frozen=True, slots=True, init=False)
class CandidatePlan:
    """候选规划阶段形成的不可变拆条方案。"""

    plan_id: PlanId
    transcript_id: TranscriptId
    source_duration_ms: int
    course_context: CourseContext | None = field(repr=False)
    clip_policy: ClipPolicy = field(repr=False)
    candidates: tuple[ClipCandidate, ...]

    def __new__(  # noqa: PYI019 - Python 3.10 不提供 typing.Self
        cls: type[_CandidatePlanT],
        *_args: object,
        **_kwargs: object,
    ) -> _CandidatePlanT:
        raise TypeError("CandidatePlan 只能由 ClipPlanning 创建")

    @classmethod
    def _from_preparation(  # noqa: PYI019 - Python 3.10 不提供 typing.Self
        cls: type[_CandidatePlanT],
        *,
        plan_id: PlanId,
        transcript_id: TranscriptId,
        source_duration_ms: int,
        course_context: CourseContext | None,
        clip_policy: ClipPolicy,
        candidates: tuple[ClipCandidate, ...],
    ) -> _CandidatePlanT:
        if not isinstance(plan_id, PlanId):
            raise TypeError("候选方案必须使用 PlanId")
        if not isinstance(transcript_id, TranscriptId):
            raise TypeError("候选方案必须引用 TranscriptId")
        if (
            not isinstance(source_duration_ms, int)
            or isinstance(source_duration_ms, bool)
            or source_duration_ms <= 0
        ):
            raise ValueError("候选方案素材时长必须是正整数毫秒")
        if course_context is not None and not isinstance(
            course_context,
            CourseContext,
        ):
            raise TypeError("候选方案课程上下文类型不合法")
        if not isinstance(clip_policy, ClipPolicy):
            raise TypeError("候选方案短视频策略类型不合法")
        if not isinstance(candidates, tuple) or any(
            not isinstance(candidate, ClipCandidate)
            for candidate in candidates
        ):
            raise TypeError("候选方案只能包含不可变候选片段")
        candidate_ids = tuple(
            candidate.candidate_id for candidate in candidates
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("候选方案的候选标识必须唯一")

        instance = object.__new__(cls)
        object.__setattr__(instance, "plan_id", plan_id)
        object.__setattr__(instance, "transcript_id", transcript_id)
        object.__setattr__(
            instance,
            "source_duration_ms",
            source_duration_ms,
        )
        object.__setattr__(
            instance,
            "course_context",
            course_context,
        )
        object.__setattr__(instance, "clip_policy", clip_policy)
        object.__setattr__(instance, "candidates", candidates)
        return instance
