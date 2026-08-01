"""形成候选方案，并归并完整主题评审与最终选择。"""

from dataclasses import dataclass
from typing import Protocol

from video_auto_editor.configuration import ClipPolicy, CourseContext
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
    SeriesId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription

from ._model import (
    BoundaryRemedy,
    BoundaryRemedyStatus,
    CandidatePlan,
    CandidateReviewContext,
    ClipCandidate,
    CompleteTopicReview,
    DeliveryPlan,
    FinalCandidate,
    PublishedSelection,
    RejectedSelection,
    RejectionReason,
    ResultKind,
    ReviewRecommendation,
    SameTopicSeries,
    ShortVideo,
    TopicReviewItem,
    TopicReviewSnapshot,
    TranscriptExcerpt,
    _canonical_text,
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


@dataclass(frozen=True, slots=True)
class _ResolvedBoundary:
    final_start_ms: int
    final_end_ms: int
    remedy: BoundaryRemedy


class ClipPlanning:
    """以两阶段接口隐藏候选生成与最终选择算法。"""

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
            transcript_chunks=tuple(
                _to_excerpt(chunk) for chunk in chunks
            ),
        )

    @classmethod
    def finalize(
        cls,
        candidate_plan: CandidatePlan,
        review_result: CompleteTopicReview,
    ) -> DeliveryPlan:
        """把完整主题评审归并为不可变交付方案。"""
        if not isinstance(candidate_plan, CandidatePlan):
            raise TypeError("最终选择只接受 ClipPlanning 签发的候选方案")
        reviews = getattr(review_result, "reviews", None)
        if not isinstance(reviews, tuple):
            raise TypeError("完整主题评审必须使用不可变评审集合")
        review_by_candidate_id = _snapshot_complete_reviews(
            candidate_plan,
            reviews,
        )
        finalized_candidates, short_videos = _select_candidates(
            candidate_plan,
            review_by_candidate_id,
        )
        series = _build_same_topic_series(finalized_candidates)
        return DeliveryPlan._from_finalization(
            plan_id=candidate_plan.plan_id,
            transcript_id=candidate_plan.transcript_id,
            source_duration_ms=candidate_plan.source_duration_ms,
            result_kind=(
                ResultKind.CLIPS
                if short_videos
                else ResultKind.EMPTY
            ),
            candidates=finalized_candidates,
            short_videos=short_videos,
            series=series,
        )


def _snapshot_complete_reviews(
    candidate_plan: CandidatePlan,
    reviews: tuple[TopicReviewItem, ...],
) -> dict[CandidateId, TopicReviewSnapshot]:
    snapshots: dict[CandidateId, TopicReviewSnapshot] = {}
    for review in reviews:
        candidate_id = getattr(review, "candidate_id", None)
        if not isinstance(candidate_id, CandidateId):
            raise TypeError("主题评审必须使用 CandidateId 归并候选")
        if candidate_id in snapshots:
            raise ValueError("完整主题评审包含重复候选标识")
        snapshots[candidate_id] = _snapshot_review(review, candidate_id)

    expected_ids = {
        candidate.candidate_id for candidate in candidate_plan.candidates
    }
    actual_ids = set(snapshots)
    if actual_ids - expected_ids:
        raise ValueError("完整主题评审包含未知候选标识")
    if expected_ids - actual_ids:
        raise ValueError("完整主题评审缺少候选标识")
    return snapshots


def _snapshot_review(
    review: TopicReviewItem,
    candidate_id: CandidateId,
) -> TopicReviewSnapshot:
    topic_name = _required_text(review, "topic_name")
    title = _required_text(review, "title")
    summary = _required_text(review, "summary")
    topic_complete = _required_bool(review, "topic_complete")
    learning_value = _bounded_integer(
        review,
        "learning_value",
        maximum=10,
    )
    share_value = _bounded_integer(
        review,
        "share_value",
        maximum=10,
    )
    publish_ready_score = _bounded_integer(
        review,
        "publish_ready_score",
        maximum=100,
    )
    try:
        export_decision = ReviewRecommendation(
            getattr(review, "export_decision", None)
        )
    except ValueError as exc:
        raise ValueError("主题评审导出建议不合法") from exc
    keywords = getattr(review, "keywords", None)
    if not isinstance(keywords, (list, tuple)) or any(
        not isinstance(keyword, str) or not keyword.strip()
        for keyword in keywords
    ):
        raise TypeError("主题评审关键词必须是字符串集合")
    needs_human_review = _required_bool(
        review,
        "needs_human_review",
    )
    reject_reason = _empty_allowed_text(review, "reject_reason")
    boundary_fix_suggestion = _empty_allowed_text(
        review,
        "boundary_fix_suggestion",
    )
    boundary_fix_start_ms = _optional_integer(
        review,
        "boundary_fix_start_ms",
    )
    boundary_fix_end_ms = _optional_integer(
        review,
        "boundary_fix_end_ms",
    )
    return TopicReviewSnapshot(
        candidate_id=candidate_id,
        topic_name=topic_name,
        topic_complete=topic_complete,
        learning_value=learning_value,
        share_value=share_value,
        publish_ready_score=publish_ready_score,
        export_decision=export_decision,
        title=title,
        summary=summary,
        keywords=tuple(keyword.strip() for keyword in keywords),
        needs_human_review=needs_human_review,
        reject_reason=reject_reason,
        boundary_fix_suggestion=boundary_fix_suggestion,
        boundary_fix_start_ms=boundary_fix_start_ms,
        boundary_fix_end_ms=boundary_fix_end_ms,
    )


def _required_text(value: object, field_name: str) -> str:
    field_value = getattr(value, field_name, None)
    if not isinstance(field_value, str):
        raise TypeError(f"主题评审 {field_name} 必须是字符串")
    normalized = field_value.strip()
    if not normalized:
        raise ValueError(f"主题评审 {field_name} 不能为空")
    return normalized


def _empty_allowed_text(value: object, field_name: str) -> str:
    field_value = getattr(value, field_name, None)
    if not isinstance(field_value, str):
        raise TypeError(f"主题评审 {field_name} 必须是字符串")
    return field_value.strip()


def _required_bool(value: object, field_name: str) -> bool:
    field_value = getattr(value, field_name, None)
    if not isinstance(field_value, bool):
        raise TypeError(f"主题评审 {field_name} 必须是布尔值")
    return field_value


def _bounded_integer(
    value: object,
    field_name: str,
    *,
    maximum: int,
) -> int:
    field_value = getattr(value, field_name, None)
    if (
        not isinstance(field_value, int)
        or isinstance(field_value, bool)
    ):
        raise TypeError(f"主题评审 {field_name} 必须是整数")
    if not 0 <= field_value <= maximum:
        raise ValueError(f"主题评审 {field_name} 超出允许范围")
    return field_value


def _optional_integer(
    value: object,
    field_name: str,
) -> int | None:
    field_value = getattr(value, field_name, None)
    if field_value is None:
        return None
    if (
        not isinstance(field_value, int)
        or isinstance(field_value, bool)
    ):
        raise TypeError(f"主题评审 {field_name} 必须是整数毫秒")
    return field_value


def _select_candidates(
    candidate_plan: CandidatePlan,
    review_by_candidate_id: dict[CandidateId, TopicReviewSnapshot],
) -> tuple[tuple[FinalCandidate, ...], tuple[ShortVideo, ...]]:
    boundary_by_candidate_id = {
        candidate.candidate_id: _resolve_boundary(
            candidate_plan,
            candidate,
            review_by_candidate_id[candidate.candidate_id],
        )
        for candidate in candidate_plan.candidates
    }
    rejection_by_candidate_id = {
        candidate.candidate_id: _publish_rejection_reason(
            candidate_plan,
            review_by_candidate_id[candidate.candidate_id],
            boundary_by_candidate_id[candidate.candidate_id],
        )
        for candidate in candidate_plan.candidates
    }
    eligible = tuple(
        (
            position,
            candidate,
            review_by_candidate_id[candidate.candidate_id],
        )
        for position, candidate in enumerate(candidate_plan.candidates)
        if rejection_by_candidate_id[candidate.candidate_id] is None
    )
    selected_ids = _select_eligible_candidate_ids(
        candidate_plan,
        eligible,
    )
    finalized_candidates = []
    short_videos = []
    for candidate in candidate_plan.candidates:
        review = review_by_candidate_id[candidate.candidate_id]
        boundary = boundary_by_candidate_id[candidate.candidate_id]
        rejection_reason = rejection_by_candidate_id[
            candidate.candidate_id
        ]
        if (
            rejection_reason is None
            and candidate.candidate_id not in selected_ids
        ):
            rejection_reason = RejectionReason.MAX_CLIPS_LIMIT
        if rejection_reason is None:
            short_video_id = ShortVideoId.new()
            selection: PublishedSelection | RejectedSelection = (
                PublishedSelection(short_video_id)
            )
            short_videos.append(
                ShortVideo(
                    short_video_id=short_video_id,
                    source_candidate_id=candidate.candidate_id,
                    topic_name=review.topic_name,
                    title=review.title,
                    summary=review.summary,
                    keywords=review.keywords,
                    final_start_ms=boundary.final_start_ms,
                    final_end_ms=boundary.final_end_ms,
                )
            )
        else:
            needs_human_review = (
                review.needs_human_review
                or review.export_decision
                is ReviewRecommendation.NEEDS_REVIEW
                or rejection_reason
                is RejectionReason.BOUNDARY_REMEDY_INVALID
            )
            human_review_reason = review.reject_reason
            if (
                not human_review_reason
                and rejection_reason
                is RejectionReason.BOUNDARY_REMEDY_INVALID
            ):
                human_review_reason = (
                    review.boundary_fix_suggestion
                )
            selection = RejectedSelection(
                reason_code=rejection_reason,
                needs_human_review=needs_human_review,
                human_review_reason=human_review_reason,
            )
        finalized_candidates.append(
            FinalCandidate(
                candidate_id=candidate.candidate_id,
                initial_start_ms=candidate.initial_start_ms,
                initial_end_ms=candidate.initial_end_ms,
                final_start_ms=boundary.final_start_ms,
                final_end_ms=boundary.final_end_ms,
                transcript_chunk_ids=candidate.transcript_chunk_ids,
                boundary_remedy=boundary.remedy,
                review=review,
                selection=selection,
            )
        )
    return tuple(finalized_candidates), tuple(short_videos)


def _publish_rejection_reason(
    candidate_plan: CandidatePlan,
    review: TopicReviewSnapshot,
    boundary: _ResolvedBoundary,
) -> RejectionReason | None:
    if _matches_excluded_content(
        candidate_plan,
        review,
        boundary,
    ):
        return RejectionReason.EXCLUDED_CONTENT
    if review.needs_human_review:
        return RejectionReason.NEEDS_HUMAN_REVIEW
    if not review.topic_complete:
        return RejectionReason.TOPIC_INCOMPLETE
    if (
        review.publish_ready_score
        < candidate_plan.clip_policy.publish_ready_threshold
    ):
        return RejectionReason.PUBLISH_READY_SCORE_BELOW_THRESHOLD
    if (
        review.export_decision
        is not ReviewRecommendation.PUBLISH_READY
        or review.reject_reason
    ):
        return RejectionReason.REVIEW_REJECTED
    if boundary.remedy.status is BoundaryRemedyStatus.INVALID:
        return RejectionReason.BOUNDARY_REMEDY_INVALID
    return None


def _resolve_boundary(
    candidate_plan: CandidatePlan,
    candidate: ClipCandidate,
    review: TopicReviewSnapshot,
) -> _ResolvedBoundary:
    requested_start_ms = review.boundary_fix_start_ms
    requested_end_ms = review.boundary_fix_end_ms
    requested = bool(review.boundary_fix_suggestion) or any(
        value is not None
        for value in (requested_start_ms, requested_end_ms)
    )
    if not requested:
        return _ResolvedBoundary(
            final_start_ms=candidate.initial_start_ms,
            final_end_ms=candidate.initial_end_ms,
            remedy=BoundaryRemedy(
                status=BoundaryRemedyStatus.NOT_NEEDED,
                suggestion="",
                requested_start_ms=None,
                requested_end_ms=None,
            ),
        )

    legal = (
        requested_start_ms is not None
        and requested_end_ms is not None
        and 0 <= requested_start_ms <= candidate.initial_start_ms
        and candidate.initial_end_ms
        <= requested_end_ms
        <= candidate_plan.source_duration_ms
        and (
            requested_start_ms != candidate.initial_start_ms
            or requested_end_ms != candidate.initial_end_ms
        )
        and (
            candidate_plan.clip_policy.min_duration_seconds
            * 1_000
            <= requested_end_ms - requested_start_ms
            <= candidate_plan.clip_policy.max_duration_seconds
            * 1_000
        )
    )
    if not legal:
        return _ResolvedBoundary(
            final_start_ms=candidate.initial_start_ms,
            final_end_ms=candidate.initial_end_ms,
            remedy=BoundaryRemedy(
                status=BoundaryRemedyStatus.INVALID,
                suggestion=review.boundary_fix_suggestion,
                requested_start_ms=requested_start_ms,
                requested_end_ms=requested_end_ms,
            ),
        )
    assert requested_start_ms is not None
    assert requested_end_ms is not None
    return _ResolvedBoundary(
        final_start_ms=requested_start_ms,
        final_end_ms=requested_end_ms,
        remedy=BoundaryRemedy(
            status=BoundaryRemedyStatus.APPLIED,
            suggestion=review.boundary_fix_suggestion,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
        ),
    )


def _matches_excluded_content(
    candidate_plan: CandidatePlan,
    review: TopicReviewSnapshot,
    boundary: _ResolvedBoundary,
) -> bool:
    course_context = candidate_plan.course_context
    if course_context is None or not course_context.excluded_content:
        return False
    searchable_values = (
        review.topic_name,
        review.title,
        review.summary,
        *review.keywords,
        *(
            excerpt.text
            for excerpt in candidate_plan._transcript_chunks
            if (
                excerpt.start_ms < boundary.final_end_ms
                and excerpt.end_ms > boundary.final_start_ms
            )
        ),
    )
    return any(
        _phrase_matches(excluded, searchable_values)
        for excluded in course_context.excluded_content
    )


def _select_eligible_candidate_ids(
    candidate_plan: CandidatePlan,
    eligible: tuple[
        tuple[int, ClipCandidate, TopicReviewSnapshot],
        ...,
    ],
) -> set[CandidateId]:
    max_clips = candidate_plan.clip_policy.max_clips
    if max_clips is None or len(eligible) <= max_clips:
        return {
            candidate.candidate_id
            for _, candidate, _ in eligible
        }

    priority_topics = (
        candidate_plan.course_context.priority_topics
        if candidate_plan.course_context is not None
        else ()
    )
    ranked = sorted(
        eligible,
        key=lambda item: _selection_rank(
            item[0],
            item[2],
            priority_topics,
        ),
    )
    selected: list[
        tuple[int, ClipCandidate, TopicReviewSnapshot]
    ] = []
    selected_ids: set[CandidateId] = set()
    covered_topics: set[str] = set()

    for priority_topic in priority_topics:
        matching = tuple(
            item
            for item in ranked
            if item[1].candidate_id not in selected_ids
            and _canonical_text(item[2].topic_name)
            not in covered_topics
            and _phrase_matches(
                priority_topic,
                _review_semantic_values(item[2]),
            )
        )
        if not matching:
            continue
        _keep_candidate(
            matching[0],
            selected,
            selected_ids,
            covered_topics,
        )
        if len(selected) == max_clips:
            return selected_ids

    for item in ranked:
        if item[1].candidate_id in selected_ids:
            continue
        canonical_topic = _canonical_text(item[2].topic_name)
        if canonical_topic in covered_topics:
            continue
        _keep_candidate(
            item,
            selected,
            selected_ids,
            covered_topics,
        )
        if len(selected) == max_clips:
            return selected_ids

    for item in ranked:
        if item[1].candidate_id in selected_ids:
            continue
        _keep_candidate(
            item,
            selected,
            selected_ids,
            covered_topics,
        )
        if len(selected) == max_clips:
            break
    return selected_ids


def _selection_rank(
    source_position: int,
    review: TopicReviewSnapshot,
    priority_topics: tuple[str, ...],
) -> tuple[int, int, int, int, int]:
    priority_rank = len(priority_topics)
    semantic_values = _review_semantic_values(review)
    for index, priority_topic in enumerate(priority_topics):
        if _phrase_matches(priority_topic, semantic_values):
            priority_rank = index
            break
    return (
        priority_rank,
        -review.publish_ready_score,
        -review.learning_value,
        -review.share_value,
        source_position,
    )


def _keep_candidate(
    item: tuple[int, ClipCandidate, TopicReviewSnapshot],
    selected: list[
        tuple[int, ClipCandidate, TopicReviewSnapshot]
    ],
    selected_ids: set[CandidateId],
    covered_topics: set[str],
) -> None:
    selected.append(item)
    selected_ids.add(item[1].candidate_id)
    covered_topics.add(_canonical_text(item[2].topic_name))


def _review_semantic_values(
    review: TopicReviewSnapshot,
) -> tuple[str, ...]:
    return (
        review.topic_name,
        review.title,
        review.summary,
        *review.keywords,
    )


def _phrase_matches(
    phrase: str,
    values: tuple[str, ...],
) -> bool:
    canonical_phrase = _canonical_text(phrase)
    return bool(canonical_phrase) and any(
        canonical_phrase in _canonical_text(value)
        for value in values
    )


def _build_same_topic_series(
    candidates: tuple[FinalCandidate, ...],
) -> tuple[SameTopicSeries, ...]:
    series = []
    run_topic = ""
    run_canonical_topic = ""
    run_short_video_ids: list[ShortVideoId] = []

    def flush_run() -> None:
        if len(run_short_video_ids) >= 2:
            series.append(
                SameTopicSeries(
                    series_id=SeriesId.new(),
                    topic=run_topic,
                    short_video_ids=tuple(run_short_video_ids),
                )
            )

    for candidate in candidates:
        if not isinstance(candidate.selection, PublishedSelection):
            flush_run()
            run_topic = ""
            run_canonical_topic = ""
            run_short_video_ids = []
            continue
        canonical_topic = _canonical_text(
            candidate.review.topic_name
        )
        if (
            run_short_video_ids
            and canonical_topic != run_canonical_topic
        ):
            flush_run()
            run_short_video_ids = []
        if not run_short_video_ids:
            run_topic = candidate.review.topic_name
            run_canonical_topic = canonical_topic
        run_short_video_ids.append(
            candidate.selection.short_video_id
        )
    flush_run()
    return tuple(series)


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
