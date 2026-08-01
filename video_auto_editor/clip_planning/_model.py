"""ClipPlanning 深模块拥有的不可变业务事实。"""

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, TypeVar

from video_auto_editor.configuration import ClipPolicy, CourseContext
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
    SeriesId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)

_CandidatePlanT = TypeVar("_CandidatePlanT", bound="CandidatePlan")
_DeliveryPlanT = TypeVar("_DeliveryPlanT", bound="DeliveryPlan")


class ResultKind(str, Enum):
    """短视频规划形成的两类合法交付结果。"""

    CLIPS = "clips"
    EMPTY = "empty"


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
    _transcript_chunks: tuple[TranscriptExcerpt, ...] = field(
        repr=False,
    )

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
        transcript_chunks: tuple[TranscriptExcerpt, ...],
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
        if not isinstance(transcript_chunks, tuple) or any(
            not isinstance(chunk, TranscriptExcerpt)
            for chunk in transcript_chunks
        ):
            raise TypeError("候选方案只能保留不可变转写文本块")
        transcript_chunk_ids = tuple(
            chunk.transcript_chunk_id for chunk in transcript_chunks
        )
        if len(transcript_chunk_ids) != len(set(transcript_chunk_ids)):
            raise ValueError("候选方案的转写文本块标识必须唯一")
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
        object.__setattr__(
            instance,
            "_transcript_chunks",
            transcript_chunks,
        )
        return instance


class ReviewRecommendation(str, Enum):
    """主题评审允许返回的封闭导出建议。"""

    PUBLISH_READY = "publish_ready"
    NEEDS_REVIEW = "needs_review"
    REJECT = "reject"


class RejectionReason(str, Enum):
    """最终候选淘汰的稳定业务原因。"""

    EXCLUDED_CONTENT = "excluded_content"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    TOPIC_INCOMPLETE = "topic_incomplete"
    PUBLISH_READY_SCORE_BELOW_THRESHOLD = (
        "publish_ready_score_below_threshold"
    )
    REVIEW_REJECTED = "review_rejected"
    BOUNDARY_REMEDY_INVALID = "boundary_remedy_invalid"
    MAX_CLIPS_LIMIT = "max_clips_limit"


class BoundaryRemedyStatus(str, Enum):
    """候选边界补救允许形成的封闭状态。"""

    NOT_NEEDED = "not_needed"
    APPLIED = "applied"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BoundaryRemedy:
    """边界补救请求及其确定结果。"""

    status: BoundaryRemedyStatus
    suggestion: str = field(repr=False)
    requested_start_ms: int | None
    requested_end_ms: int | None


class TopicReviewItem(Protocol):
    """归并单个候选评审所需的完整结构接口。

    `reject_reason` 与 `boundary_fix_suggestion` 始终存在；无对应事实时
    使用空字符串，补救范围不存在时两个毫秒值同时为 `None`。
    """

    @property
    def candidate_id(self) -> CandidateId:
        ...

    @property
    def topic_name(self) -> str:
        ...

    @property
    def topic_complete(self) -> bool:
        ...

    @property
    def learning_value(self) -> int:
        ...

    @property
    def share_value(self) -> int:
        ...

    @property
    def publish_ready_score(self) -> int:
        ...

    @property
    def export_decision(self) -> str:
        ...

    @property
    def title(self) -> str:
        ...

    @property
    def summary(self) -> str:
        ...

    @property
    def keywords(self) -> tuple[str, ...]:
        ...

    @property
    def needs_human_review(self) -> bool:
        ...

    @property
    def reject_reason(self) -> str:
        ...

    @property
    def boundary_fix_suggestion(self) -> str:
        ...

    @property
    def boundary_fix_start_ms(self) -> int | None:
        ...

    @property
    def boundary_fix_end_ms(self) -> int | None:
        ...


class CompleteTopicReview(Protocol):
    """完整主题评审向 ClipPlanning 提供的最小结构接口。"""

    @property
    def reviews(self) -> tuple[TopicReviewItem, ...]:
        ...


@dataclass(frozen=True, slots=True)
class TopicReviewSnapshot:
    """归并进交付方案的完整主题评审快照。"""

    candidate_id: CandidateId
    topic_name: str = field(repr=False)
    topic_complete: bool
    learning_value: int
    share_value: int
    publish_ready_score: int
    export_decision: ReviewRecommendation
    title: str = field(repr=False)
    summary: str = field(repr=False)
    keywords: tuple[str, ...] = field(repr=False)
    needs_human_review: bool
    reject_reason: str = field(repr=False)
    boundary_fix_suggestion: str = field(repr=False)
    boundary_fix_start_ms: int | None
    boundary_fix_end_ms: int | None


@dataclass(frozen=True, slots=True)
class RejectedSelection:
    """候选未发布时的判别联合分支。"""

    reason_code: RejectionReason
    needs_human_review: bool
    human_review_reason: str = field(repr=False)
    outcome: Literal["rejected"] = field(
        default="rejected",
        init=False,
    )


@dataclass(frozen=True, slots=True)
class PublishedSelection:
    """候选已发布时的判别联合分支。"""

    short_video_id: ShortVideoId
    outcome: Literal["published"] = field(
        default="published",
        init=False,
    )


@dataclass(frozen=True, slots=True)
class FinalCandidate:
    """完整评审后带唯一选择结果的候选事实。"""

    candidate_id: CandidateId
    initial_start_ms: int
    initial_end_ms: int
    final_start_ms: int
    final_end_ms: int
    transcript_chunk_ids: tuple[TranscriptChunkId, ...]
    boundary_remedy: BoundaryRemedy = field(repr=False)
    review: TopicReviewSnapshot = field(repr=False)
    selection: PublishedSelection | RejectedSelection


@dataclass(frozen=True, slots=True)
class ShortVideo:
    """交付构建可以直接消费的发布就绪短视频事实。"""

    short_video_id: ShortVideoId
    source_candidate_id: CandidateId
    topic_name: str = field(repr=False)
    title: str = field(repr=False)
    summary: str = field(repr=False)
    keywords: tuple[str, ...] = field(repr=False)
    final_start_ms: int
    final_end_ms: int

    @property
    def duration_ms(self) -> int:
        """返回最终素材范围的整数毫秒时长。"""
        return self.final_end_ms - self.final_start_ms


@dataclass(frozen=True, slots=True)
class SameTopicSeries:
    """连续多条同主题短视频的唯一有序关系。"""

    series_id: SeriesId
    topic: str = field(repr=False)
    short_video_ids: tuple[ShortVideoId, ...]


@dataclass(frozen=True, slots=True, init=False)
class DeliveryPlan:
    """完整主题评审后形成的不可变交付方案。"""

    plan_id: PlanId
    transcript_id: TranscriptId
    source_duration_ms: int
    result_kind: ResultKind
    candidates: tuple[FinalCandidate, ...]
    short_videos: tuple[ShortVideo, ...]
    series: tuple[SameTopicSeries, ...]

    def __new__(  # noqa: PYI019 - Python 3.10 不提供 typing.Self
        cls: type[_DeliveryPlanT],
        *_args: object,
        **_kwargs: object,
    ) -> _DeliveryPlanT:
        raise TypeError("DeliveryPlan 只能由 ClipPlanning 创建")

    @classmethod
    def _from_finalization(  # noqa: PYI019 - Python 3.10 不提供 typing.Self
        cls: type[_DeliveryPlanT],
        *,
        plan_id: PlanId,
        transcript_id: TranscriptId,
        source_duration_ms: int,
        result_kind: ResultKind,
        candidates: tuple[FinalCandidate, ...],
        short_videos: tuple[ShortVideo, ...],
        series: tuple[SameTopicSeries, ...],
    ) -> _DeliveryPlanT:
        _validate_delivery_plan(
            plan_id=plan_id,
            transcript_id=transcript_id,
            source_duration_ms=source_duration_ms,
            result_kind=result_kind,
            candidates=candidates,
            short_videos=short_videos,
            series=series,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "plan_id", plan_id)
        object.__setattr__(instance, "transcript_id", transcript_id)
        object.__setattr__(
            instance,
            "source_duration_ms",
            source_duration_ms,
        )
        object.__setattr__(instance, "result_kind", result_kind)
        object.__setattr__(instance, "candidates", candidates)
        object.__setattr__(instance, "short_videos", short_videos)
        object.__setattr__(instance, "series", series)
        return instance


def _validate_delivery_plan(
    *,
    plan_id: object,
    transcript_id: object,
    source_duration_ms: object,
    result_kind: object,
    candidates: object,
    short_videos: object,
    series: object,
) -> None:
    if not isinstance(plan_id, PlanId):
        raise TypeError("交付方案必须使用 PlanId")
    if not isinstance(transcript_id, TranscriptId):
        raise TypeError("交付方案必须引用 TranscriptId")
    if (
        not isinstance(source_duration_ms, int)
        or isinstance(source_duration_ms, bool)
        or source_duration_ms <= 0
    ):
        raise ValueError("交付方案素材时长必须是正整数毫秒")
    if not isinstance(result_kind, ResultKind):
        raise TypeError("交付方案必须使用 ResultKind")
    if not isinstance(candidates, tuple) or any(
        not isinstance(candidate, FinalCandidate)
        for candidate in candidates
    ):
        raise TypeError("交付方案只能包含不可变最终候选")
    if not isinstance(short_videos, tuple) or any(
        not isinstance(short_video, ShortVideo)
        for short_video in short_videos
    ):
        raise TypeError("交付方案只能包含不可变短视频事实")
    if not isinstance(series, tuple) or any(
        not isinstance(item, SameTopicSeries) for item in series
    ):
        raise TypeError("交付方案只能包含不可变同主题系列")

    candidate_by_id: dict[CandidateId, FinalCandidate] = {}
    candidate_position_by_id: dict[CandidateId, int] = {}
    published_by_candidate: dict[CandidateId, ShortVideoId] = {}
    for position, candidate in enumerate(candidates):
        _validate_final_candidate(candidate, source_duration_ms)
        if candidate.candidate_id in candidate_by_id:
            raise ValueError("交付方案的候选标识必须唯一")
        candidate_by_id[candidate.candidate_id] = candidate
        candidate_position_by_id[candidate.candidate_id] = position
        if isinstance(candidate.selection, PublishedSelection):
            published_by_candidate[candidate.candidate_id] = (
                candidate.selection.short_video_id
            )

    short_video_by_source: dict[CandidateId, ShortVideoId] = {}
    short_video_ids: set[ShortVideoId] = set()
    for short_video in short_videos:
        _validate_short_video(short_video, source_duration_ms)
        if short_video.short_video_id in short_video_ids:
            raise ValueError("交付方案的短视频标识必须唯一")
        short_video_ids.add(short_video.short_video_id)
        if short_video.source_candidate_id in short_video_by_source:
            raise ValueError("每个候选最多形成一条短视频")
        short_video_by_source[short_video.source_candidate_id] = (
            short_video.short_video_id
        )
        source_candidate = candidate_by_id.get(
            short_video.source_candidate_id
        )
        if source_candidate is None:
            raise ValueError("短视频引用了未知候选标识")
        if (
            short_video.final_start_ms
            != source_candidate.final_start_ms
            or short_video.final_end_ms
            != source_candidate.final_end_ms
            or short_video.topic_name
            != source_candidate.review.topic_name
            or short_video.title != source_candidate.review.title
            or short_video.summary != source_candidate.review.summary
            or short_video.keywords != source_candidate.review.keywords
        ):
            raise ValueError("短视频事实必须与来源候选一致")

    if published_by_candidate != short_video_by_source:
        raise ValueError("发布候选必须与短视频一一互引")
    published_candidate_by_short_video_id = {
        short_video_id: candidate_by_id[candidate_id]
        for candidate_id, short_video_id in published_by_candidate.items()
    }
    source_position_by_short_video_id = {
        short_video_id: candidate_position_by_id[candidate_id]
        for candidate_id, short_video_id in published_by_candidate.items()
    }
    _validate_series(
        series,
        short_video_ids,
        published_candidate_by_short_video_id,
        source_position_by_short_video_id,
    )
    if result_kind is ResultKind.EMPTY:
        if short_videos or series or published_by_candidate:
            raise ValueError("有效空结果不能包含已发布短视频或系列")
    elif not short_videos:
        raise ValueError("短视频结果必须至少包含一条短视频")


def _validate_final_candidate(
    candidate: FinalCandidate,
    source_duration_ms: int,
) -> None:
    if not isinstance(candidate.candidate_id, CandidateId):
        raise TypeError("最终候选必须使用 CandidateId")
    _validate_time_range(
        candidate.initial_start_ms,
        candidate.initial_end_ms,
        source_duration_ms,
        "候选初始范围",
    )
    _validate_time_range(
        candidate.final_start_ms,
        candidate.final_end_ms,
        source_duration_ms,
        "候选最终范围",
    )
    if (
        not isinstance(candidate.transcript_chunk_ids, tuple)
        or not candidate.transcript_chunk_ids
        or any(
            not isinstance(identifier, TranscriptChunkId)
            for identifier in candidate.transcript_chunk_ids
        )
        or len(candidate.transcript_chunk_ids)
        != len(set(candidate.transcript_chunk_ids))
    ):
        raise ValueError("最终候选必须引用唯一转写文本块标识")
    if (
        not isinstance(candidate.review, TopicReviewSnapshot)
        or candidate.review.candidate_id != candidate.candidate_id
    ):
        raise ValueError("最终候选必须引用同一候选的完整主题评审")
    _validate_topic_review_snapshot(candidate.review)
    if not isinstance(candidate.boundary_remedy, BoundaryRemedy):
        raise TypeError("最终候选必须包含边界补救事实")
    _validate_boundary_facts(candidate)
    if isinstance(candidate.selection, PublishedSelection):
        if (
            candidate.selection.outcome != "published"
            or not isinstance(
                candidate.selection.short_video_id,
                ShortVideoId,
            )
        ):
            raise ValueError("发布选择分支不合法")
    elif isinstance(candidate.selection, RejectedSelection):
        if (
            candidate.selection.outcome != "rejected"
            or not isinstance(
                candidate.selection.reason_code,
                RejectionReason,
            )
            or not isinstance(
                candidate.selection.needs_human_review,
                bool,
            )
            or not isinstance(
                candidate.selection.human_review_reason,
                str,
            )
        ):
            raise ValueError("淘汰选择分支不合法")
    else:
        raise TypeError("最终候选必须包含发布或淘汰选择")


def _validate_boundary_facts(candidate: FinalCandidate) -> None:
    remedy = candidate.boundary_remedy
    review = candidate.review
    if (
        remedy.suggestion != review.boundary_fix_suggestion
        or remedy.requested_start_ms != review.boundary_fix_start_ms
        or remedy.requested_end_ms != review.boundary_fix_end_ms
    ):
        raise ValueError("边界补救事实必须与主题评审一致")
    initial_range = (
        candidate.initial_start_ms,
        candidate.initial_end_ms,
    )
    final_range = (
        candidate.final_start_ms,
        candidate.final_end_ms,
    )
    if remedy.status is BoundaryRemedyStatus.NOT_NEEDED:
        if (
            remedy.suggestion
            or remedy.requested_start_ms is not None
            or remedy.requested_end_ms is not None
            or final_range != initial_range
        ):
            raise ValueError("无需边界补救时不得改变最终范围")
    elif remedy.status is BoundaryRemedyStatus.APPLIED:
        if (
            remedy.requested_start_ms is None
            or remedy.requested_end_ms is None
            or final_range
            != (
                remedy.requested_start_ms,
                remedy.requested_end_ms,
            )
            or final_range == initial_range
            or candidate.final_start_ms
            > candidate.initial_start_ms
            or candidate.final_end_ms < candidate.initial_end_ms
        ):
            raise ValueError("已应用边界补救必须形成新的最终范围")
    elif remedy.status is BoundaryRemedyStatus.INVALID:
        if final_range != initial_range:
            raise ValueError("无效边界补救不得改变最终范围")
    else:
        raise TypeError("边界补救状态不合法")


def _validate_topic_review_snapshot(
    review: TopicReviewSnapshot,
) -> None:
    if not isinstance(review.candidate_id, CandidateId):
        raise TypeError("主题评审快照必须使用 CandidateId")
    for text_value, field_name in (
        (review.topic_name, "topic_name"),
        (review.title, "title"),
        (review.summary, "summary"),
    ):
        if not isinstance(text_value, str) or not text_value.strip():
            raise ValueError(f"主题评审快照 {field_name} 不能为空")
    for score_value, field_name, maximum in (
        (review.learning_value, "learning_value", 10),
        (review.share_value, "share_value", 10),
        (review.publish_ready_score, "publish_ready_score", 100),
    ):
        if (
            not isinstance(score_value, int)
            or isinstance(score_value, bool)
            or not 0 <= score_value <= maximum
        ):
            raise ValueError(f"主题评审快照 {field_name} 超出允许范围")
    if not isinstance(review.topic_complete, bool):
        raise TypeError("主题评审快照 topic_complete 必须是布尔值")
    if not isinstance(review.export_decision, ReviewRecommendation):
        raise TypeError("主题评审快照导出建议不合法")
    if (
        not isinstance(review.keywords, tuple)
        or any(
            not isinstance(keyword, str) or not keyword.strip()
            for keyword in review.keywords
        )
    ):
        raise TypeError("主题评审快照关键词必须是字符串集合")
    if not isinstance(review.needs_human_review, bool):
        raise TypeError("主题评审快照人工复核标记必须是布尔值")
    for text_value, field_name in (
        (review.reject_reason, "reject_reason"),
        (review.boundary_fix_suggestion, "boundary_fix_suggestion"),
    ):
        if not isinstance(text_value, str):
            raise TypeError(f"主题评审快照 {field_name} 必须是字符串")
    for boundary_value, field_name in (
        (review.boundary_fix_start_ms, "boundary_fix_start_ms"),
        (review.boundary_fix_end_ms, "boundary_fix_end_ms"),
    ):
        if boundary_value is not None and (
            not isinstance(boundary_value, int)
            or isinstance(boundary_value, bool)
        ):
            raise TypeError(f"主题评审快照 {field_name} 必须是整数毫秒")


def _validate_short_video(
    short_video: ShortVideo,
    source_duration_ms: int,
) -> None:
    if not isinstance(short_video.short_video_id, ShortVideoId):
        raise TypeError("短视频必须使用 ShortVideoId")
    if not isinstance(short_video.source_candidate_id, CandidateId):
        raise TypeError("短视频必须引用 CandidateId")
    for value, field_name in (
        (short_video.topic_name, "topic_name"),
        (short_video.title, "title"),
        (short_video.summary, "summary"),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"短视频 {field_name} 不能为空")
    if (
        not isinstance(short_video.keywords, tuple)
        or any(
            not isinstance(keyword, str) or not keyword.strip()
            for keyword in short_video.keywords
        )
    ):
        raise TypeError("短视频关键词必须是字符串集合")
    _validate_time_range(
        short_video.final_start_ms,
        short_video.final_end_ms,
        source_duration_ms,
        "短视频最终范围",
    )


def _validate_series(
    series: tuple[SameTopicSeries, ...],
    short_video_ids: set[ShortVideoId],
    candidate_by_short_video_id: dict[
        ShortVideoId,
        FinalCandidate,
    ],
    source_position_by_short_video_id: dict[ShortVideoId, int],
) -> None:
    series_ids: set[SeriesId] = set()
    assigned_short_video_ids: set[ShortVideoId] = set()
    for item in series:
        if not isinstance(item.series_id, SeriesId):
            raise TypeError("同主题系列必须使用 SeriesId")
        if item.series_id in series_ids:
            raise ValueError("同主题系列标识必须唯一")
        series_ids.add(item.series_id)
        if not isinstance(item.topic, str) or not item.topic.strip():
            raise ValueError("同主题系列必须包含主题")
        if (
            not isinstance(item.short_video_ids, tuple)
            or len(item.short_video_ids) < 2
            or any(
                not isinstance(identifier, ShortVideoId)
                for identifier in item.short_video_ids
            )
            or len(item.short_video_ids)
            != len(set(item.short_video_ids))
        ):
            raise ValueError("同主题系列必须引用至少两条唯一短视频")
        if not set(item.short_video_ids) <= short_video_ids:
            raise ValueError("同主题系列引用了未知短视频")
        if assigned_short_video_ids.intersection(item.short_video_ids):
            raise ValueError("每条短视频最多属于一个同主题系列")
        member_topics = {
            _canonical_text(
                candidate_by_short_video_id[
                    identifier
                ].review.topic_name
            )
            for identifier in item.short_video_ids
        }
        if member_topics != {_canonical_text(item.topic)}:
            raise ValueError("同主题系列成员必须拥有同一主题")
        source_positions = tuple(
            source_position_by_short_video_id[identifier]
            for identifier in item.short_video_ids
        )
        if source_positions != tuple(
            range(
                source_positions[0],
                source_positions[0] + len(source_positions),
            )
        ):
            raise ValueError("同主题系列成员必须按素材时间连续有序")
        assigned_short_video_ids.update(item.short_video_ids)


def _validate_time_range(
    start_ms: object,
    end_ms: object,
    source_duration_ms: int,
    field_name: str,
) -> None:
    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
    ):
        raise TypeError(f"{field_name}必须使用整数毫秒")
    if not 0 <= start_ms < end_ms <= source_duration_ms:
        raise ValueError(f"{field_name}必须位于素材时间范围内")


def _canonical_text(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFC", value).casefold().split()
    )
