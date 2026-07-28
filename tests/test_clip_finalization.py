import json
from dataclasses import FrozenInstanceError, asdict, dataclass, replace

import pytest

from video_auto_editor.clip_planning import (
    BoundaryRemedyStatus,
    CandidatePlan,
    ClipPlanning,
    DeliveryPlan,
    PublishedSelection,
    RejectedSelection,
    RejectionReason,
)
from video_auto_editor.configuration import Configuration
from video_auto_editor.runtime import ResultKind
from video_auto_editor.runtime.identity import (
    CandidateId,
    SeriesId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.workspace import Workspace


@dataclass(frozen=True, slots=True)
class _CompleteTranscript:
    transcript_id: TranscriptId
    chunks: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _TranscriptChunk:
    transcript_chunk_id: TranscriptChunkId
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class _CompleteTopicReview:
    reviews: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _CandidateReview:
    candidate_id: CandidateId
    topic_name: str
    topic_complete: bool = True
    learning_value: int = 8
    share_value: int = 8
    publish_ready_score: int = 90
    export_decision: str = "publish_ready"
    title: str = "可发布标题"
    summary: str = "可发布摘要"
    keywords: object = ("关键词",)
    needs_human_review: bool = False
    reject_reason: str = ""
    boundary_fix_suggestion: str = ""
    boundary_fix_start_ms: int | None = None
    boundary_fix_end_ms: int | None = None


def _candidate_plan(
    tmp_path,
    texts: tuple[str, ...],
    *,
    course_context: dict[str, object] | None = None,
    max_clips: int | None = None,
    chunk_duration_ms: int = 180_000,
) -> CandidatePlan:
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"source")
    if course_context is not None:
        source_path.with_suffix(".context.json").write_text(
            json.dumps(
                {
                    "schema_version": "course_context.v1",
                    "course_topic": "课程主题",
                    **course_context,
                }
            ),
            encoding="utf-8",
        )
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    source_duration_ms = max(
        300_000,
        len(texts) * chunk_duration_ms,
    )
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=6,
        duration_ms=source_duration_ms,
    )
    configuration_override = None
    if max_clips is not None:
        configuration_override = {
            "schema_version": "configuration.v1",
            "clip_policy": {"max_clips": max_clips},
        }
    loaded = Configuration.load(
        source.source_file.path,
        configuration_override,
    )
    return ClipPlanning.prepare(
        source,
        _CompleteTranscript(
            TranscriptId.new(),
            tuple(
                _TranscriptChunk(
                    TranscriptChunkId.new(),
                    index * chunk_duration_ms,
                    (index + 1) * chunk_duration_ms,
                    text,
                )
                for index, text in enumerate(texts)
            ),
        ),
        loaded.course_context,
        loaded.effective.clip_policy,
    )


def test_finalize_returns_an_immutable_valid_empty_result(tmp_path):
    candidate_plan = _candidate_plan(tmp_path, ())

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(()),
    )

    assert isinstance(delivery_plan, DeliveryPlan)
    assert delivery_plan.plan_id == candidate_plan.plan_id
    assert delivery_plan.transcript_id == candidate_plan.transcript_id
    assert delivery_plan.source_duration_ms == 300_000
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.candidates == ()
    assert delivery_plan.short_videos == ()
    assert delivery_plan.series == ()
    with pytest.raises(FrozenInstanceError):
        delivery_plan.candidates = ()


def test_finalize_joins_reviews_by_candidate_id_without_mutating_inputs(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("第一段候选正文", "第二段候选正文"),
    )
    first_keywords = ["第一关键词"]
    second_keywords = ["第二关键词"]
    first_review = _CandidateReview(
        candidate_id=candidate_plan.candidates[0].candidate_id,
        topic_name="第一主题",
        export_decision="reject",
        reject_reason="不适合独立发布",
        keywords=first_keywords,
    )
    second_review = _CandidateReview(
        candidate_id=candidate_plan.candidates[1].candidate_id,
        topic_name="第二主题",
        export_decision="reject",
        reject_reason="缺少独立结论",
        keywords=second_keywords,
    )
    review_result = _CompleteTopicReview(
        (second_review, first_review)
    )
    original_candidate_facts = tuple(
        (
            candidate.candidate_id,
            candidate.initial_start_ms,
            candidate.initial_end_ms,
            candidate.transcript_chunk_ids,
        )
        for candidate in candidate_plan.candidates
    )
    original_review_facts = tuple(
        (
            review.candidate_id,
            review.topic_name,
            tuple(review.keywords),
            review.reject_reason,
        )
        for review in review_result.reviews
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        review_result,
    )

    assert [
        candidate.candidate_id
        for candidate in delivery_plan.candidates
    ] == [
        candidate.candidate_id
        for candidate in candidate_plan.candidates
    ]
    assert [
        candidate.review.topic_name
        for candidate in delivery_plan.candidates
    ] == ["第一主题", "第二主题"]
    assert all(
        isinstance(candidate.selection, RejectedSelection)
        for candidate in delivery_plan.candidates
    )
    assert all(
        candidate.selection.outcome == "rejected"
        for candidate in delivery_plan.candidates
    )
    assert all(
        asdict(candidate.selection)["outcome"] == "rejected"
        for candidate in delivery_plan.candidates
    )
    assert all(
        candidate.selection.reason_code
        is RejectionReason.REVIEW_REJECTED
        for candidate in delivery_plan.candidates
    )
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert tuple(
        (
            candidate.candidate_id,
            candidate.initial_start_ms,
            candidate.initial_end_ms,
            candidate.transcript_chunk_ids,
        )
        for candidate in candidate_plan.candidates
    ) == original_candidate_facts
    assert tuple(
        (
            review.candidate_id,
            review.topic_name,
            tuple(review.keywords),
            review.reject_reason,
        )
        for review in review_result.reviews
    ) == original_review_facts
    assert first_keywords == ["第一关键词"]
    assert second_keywords == ["第二关键词"]
    first_keywords.append("事后篡改")
    second_keywords.append("事后篡改")
    assert [
        candidate.review.keywords
        for candidate in delivery_plan.candidates
    ] == [("第一关键词",), ("第二关键词",)]


def test_finalize_publishes_every_eligible_candidate_by_default(tmp_path):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("第一段发布就绪正文", "第二段发布就绪正文"),
    )
    reviews = tuple(
        _CandidateReview(
            candidate_id=candidate.candidate_id,
            topic_name=f"主题 {index}",
            title=f"短视频标题 {index}",
            summary=f"短视频摘要 {index}",
            publish_ready_score=90 + index,
        )
        for index, candidate in enumerate(
            candidate_plan.candidates,
            start=1,
        )
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews[::-1]),
    )

    assert delivery_plan.result_kind is ResultKind.CLIPS
    assert len(delivery_plan.candidates) == 2
    assert all(
        isinstance(candidate.selection, PublishedSelection)
        for candidate in delivery_plan.candidates
    )
    assert all(
        candidate.selection.outcome == "published"
        for candidate in delivery_plan.candidates
    )
    assert all(
        asdict(candidate.selection)["outcome"] == "published"
        for candidate in delivery_plan.candidates
    )
    short_video_ids = tuple(
        candidate.selection.short_video_id
        for candidate in delivery_plan.candidates
    )
    assert all(
        isinstance(short_video_id, ShortVideoId)
        for short_video_id in short_video_ids
    )
    assert len(set(short_video_ids)) == 2
    assert [
        (
            short_video.source_candidate_id,
            short_video.title,
            short_video.summary,
        )
        for short_video in delivery_plan.short_videos
    ] == [
        (
            candidate.candidate_id,
            review.title,
            review.summary,
        )
        for candidate, review in zip(
            candidate_plan.candidates,
            reviews,
            strict=True,
        )
    ]


def test_finalize_applies_quality_gates_then_priority_and_topic_coverage(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "低分重点主题",
            "包含内部闲聊的排除内容",
            "重点主题的第一个合格结论",
            "重点主题的更高质量结论",
            "另一个合格主题的独立结论",
        ),
        course_context={
            "priority_topics": ["重点主题"],
            "excluded_content": ["内部闲聊"],
        },
        max_clips=2,
    )
    scores_and_topics = (
        (70, "重点主题"),
        (99, "重点主题"),
        (85, "重点主题"),
        (98, "重点主题"),
        (90, "其他主题"),
    )
    reviews = tuple(
        _CandidateReview(
            candidate_id=candidate.candidate_id,
            topic_name=topic,
            publish_ready_score=score,
            title=f"{topic}标题 {index}",
            summary=f"{topic}摘要 {index}",
        )
        for index, (
            candidate,
            (score, topic),
        ) in enumerate(
            zip(
                candidate_plan.candidates,
                scores_and_topics,
                strict=True,
            ),
            start=1,
        )
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    published_candidate_ids = {
        candidate.candidate_id
        for candidate in delivery_plan.candidates
        if isinstance(candidate.selection, PublishedSelection)
    }
    assert published_candidate_ids == {
        candidate_plan.candidates[3].candidate_id,
        candidate_plan.candidates[4].candidate_id,
    }
    rejection_reasons = {
        candidate.candidate_id: candidate.selection.reason_code
        for candidate in delivery_plan.candidates
        if isinstance(candidate.selection, RejectedSelection)
    }
    assert rejection_reasons == {
        candidate_plan.candidates[0].candidate_id: (
            RejectionReason.PUBLISH_READY_SCORE_BELOW_THRESHOLD
        ),
        candidate_plan.candidates[1].candidate_id: (
            RejectionReason.EXCLUDED_CONTENT
        ),
        candidate_plan.candidates[2].candidate_id: (
            RejectionReason.MAX_CLIPS_LIMIT
        ),
    }
    published_selection_ids = {
        candidate.selection.short_video_id
        for candidate in delivery_plan.candidates
        if isinstance(candidate.selection, PublishedSelection)
    }
    short_video_ids = {
        short_video.short_video_id
        for short_video in delivery_plan.short_videos
    }
    assert published_selection_ids == short_video_ids
    assert len(short_video_ids) == len(delivery_plan.short_videos)
    assert published_candidate_ids == {
        short_video.source_candidate_id
        for short_video in delivery_plan.short_videos
    }


def test_finalize_applies_a_legal_boundary_remedy_without_mutating_candidate(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("需要补齐结尾的发布就绪正文",),
    )
    candidate = candidate_plan.candidates[0]
    review = _CandidateReview(
        candidate_id=candidate.candidate_id,
        topic_name="边界补救",
        boundary_fix_suggestion="向后扩展以补齐结论",
        boundary_fix_start_ms=0,
        boundary_fix_end_ms=210_000,
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview((review,)),
    )

    finalized = delivery_plan.candidates[0]
    assert (
        candidate.initial_start_ms,
        candidate.initial_end_ms,
    ) == (0, 180_000)
    assert (
        finalized.initial_start_ms,
        finalized.initial_end_ms,
        finalized.final_start_ms,
        finalized.final_end_ms,
    ) == (0, 180_000, 0, 210_000)
    assert (
        finalized.boundary_remedy.status
        is BoundaryRemedyStatus.APPLIED
    )
    assert finalized.boundary_remedy.suggestion == (
        "向后扩展以补齐结论"
    )
    assert delivery_plan.short_videos[0].final_end_ms == 210_000
    assert delivery_plan.short_videos[0].duration_ms == 210_000


def test_finalize_builds_unique_ordered_series_for_contiguous_same_topic(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "主题甲第一步",
            "主题甲第二步",
            "主题乙第一步",
            "主题乙第二步",
            "主题甲独立回顾",
        ),
    )
    topics = ("主题甲", "主题甲", "主题乙", "主题乙", "主题甲")
    reviews = tuple(
        _CandidateReview(
            candidate_id=candidate.candidate_id,
            topic_name=topic,
            title=f"{topic}标题 {index}",
            summary=f"{topic}摘要 {index}",
        )
        for index, (candidate, topic) in enumerate(
            zip(
                candidate_plan.candidates,
                topics,
                strict=True,
            ),
            start=1,
        )
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews[::-1]),
    )

    assert [series.topic for series in delivery_plan.series] == [
        "主题甲",
        "主题乙",
    ]
    assert all(
        isinstance(series.series_id, SeriesId)
        for series in delivery_plan.series
    )
    assert len(
        {series.series_id for series in delivery_plan.series}
    ) == 2
    short_video_id_by_candidate = {
        short_video.source_candidate_id: short_video.short_video_id
        for short_video in delivery_plan.short_videos
    }
    assert [
        series.short_video_ids
        for series in delivery_plan.series
    ] == [
        tuple(
            short_video_id_by_candidate[
                candidate_plan.candidates[index].candidate_id
            ]
            for index in (0, 1)
        ),
        tuple(
            short_video_id_by_candidate[
                candidate_plan.candidates[index].candidate_id
            ]
            for index in (2, 3)
        ),
    ]
    all_series_members = tuple(
        short_video_id
        for series in delivery_plan.series
        for short_video_id in series.short_video_ids
    )
    assert len(all_series_members) == len(set(all_series_members))
    assert (
        short_video_id_by_candidate[
            candidate_plan.candidates[4].candidate_id
        ]
        not in all_series_members
    )


def test_finalize_does_not_spend_multiple_priority_slots_on_one_topic(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "重点主题发布的第一条",
            "重点主题发布的第二条",
            "不同主题的独立结论",
        ),
        course_context={
            "priority_topics": ["重点主题", "发布"],
        },
        max_clips=2,
    )
    review_facts = (
        ("重点主题发布", 99),
        ("重点主题发布", 98),
        ("不同主题", 90),
    )
    reviews = tuple(
        _CandidateReview(
            candidate_id=candidate.candidate_id,
            topic_name=topic,
            publish_ready_score=score,
            title=f"{topic}标题",
            summary=f"{topic}摘要",
        )
        for candidate, (topic, score) in zip(
            candidate_plan.candidates,
            review_facts,
            strict=True,
        )
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    assert {
        candidate.candidate_id
        for candidate in delivery_plan.candidates
        if isinstance(candidate.selection, PublishedSelection)
    } == {
        candidate_plan.candidates[0].candidate_id,
        candidate_plan.candidates[2].candidate_id,
    }


@pytest.mark.parametrize(
    "case",
    ["missing", "duplicate", "unknown"],
)
def test_finalize_strictly_rejects_incomplete_or_invalid_candidate_ids(
    tmp_path,
    case,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("第一段候选正文", "第二段候选正文"),
    )
    first = _CandidateReview(
        candidate_id=candidate_plan.candidates[0].candidate_id,
        topic_name="第一主题",
    )
    second = _CandidateReview(
        candidate_id=candidate_plan.candidates[1].candidate_id,
        topic_name="第二主题",
    )
    if case == "missing":
        reviews = (first,)
    elif case == "duplicate":
        reviews = (first, first, second)
    else:
        reviews = (
            first,
            replace(second, candidate_id=CandidateId.new()),
        )

    with pytest.raises(ValueError):
        ClipPlanning.finalize(
            candidate_plan,
            _CompleteTopicReview(reviews),
        )


def test_finalize_rejects_candidates_that_need_review_or_are_incomplete(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "主题尚未完整",
            "需要人工复核",
            "评审明确淘汰",
        ),
    )
    reviews = (
        _CandidateReview(
            candidate_id=candidate_plan.candidates[0].candidate_id,
            topic_name="未完整主题",
            topic_complete=False,
        ),
        _CandidateReview(
            candidate_id=candidate_plan.candidates[1].candidate_id,
            topic_name="人工复核主题",
            needs_human_review=True,
            reject_reason="开头仍然突兀",
        ),
        _CandidateReview(
            candidate_id=candidate_plan.candidates[2].candidate_id,
            topic_name="淘汰主题",
            export_decision="reject",
            reject_reason="没有独立传播价值",
        ),
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    assert [
        candidate.selection.reason_code
        for candidate in delivery_plan.candidates
        if isinstance(candidate.selection, RejectedSelection)
    ] == [
        RejectionReason.TOPIC_INCOMPLETE,
        RejectionReason.NEEDS_HUMAN_REVIEW,
        RejectionReason.REVIEW_REJECTED,
    ]
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.short_videos == ()
    assert delivery_plan.series == ()


@pytest.mark.parametrize(
    ("start_ms", "end_ms"),
    [
        (None, None),
        (1, 200_000),
        (0, 180_000),
        (0, 301_000),
    ],
)
def test_finalize_does_not_publish_an_invalid_required_boundary_remedy(
    tmp_path,
    start_ms,
    end_ms,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("边界仍不满足发布标准",),
    )
    candidate = candidate_plan.candidates[0]
    review = _CandidateReview(
        candidate_id=candidate.candidate_id,
        topic_name="无效边界补救",
        boundary_fix_suggestion="必须扩展边界",
        boundary_fix_start_ms=start_ms,
        boundary_fix_end_ms=end_ms,
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview((review,)),
    )

    finalized = delivery_plan.candidates[0]
    assert isinstance(finalized.selection, RejectedSelection)
    assert (
        finalized.selection.reason_code
        is RejectionReason.BOUNDARY_REMEDY_INVALID
    )
    assert finalized.selection.needs_human_review is True
    assert finalized.selection.human_review_reason == "必须扩展边界"
    assert (
        finalized.boundary_remedy.status
        is BoundaryRemedyStatus.INVALID
    )
    assert (
        finalized.final_start_ms,
        finalized.final_end_ms,
    ) == (0, 180_000)
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.short_videos == ()


def test_finalize_keeps_sensitive_review_facts_out_of_repr_and_frozen(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("敏感候选正文",),
    )
    review = _CandidateReview(
        candidate_id=candidate_plan.candidates[0].candidate_id,
        topic_name="敏感评审主题",
        title="敏感短视频标题",
        summary="敏感短视频摘要",
        keywords=("敏感关键词",),
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview((review,)),
    )

    representation = repr(delivery_plan)
    assert "敏感候选正文" not in representation
    assert "敏感评审主题" not in representation
    assert "敏感短视频标题" not in representation
    assert "敏感短视频摘要" not in representation
    assert "敏感关键词" not in representation
    with pytest.raises(FrozenInstanceError):
        delivery_plan.candidates[0].final_end_ms = 1
    with pytest.raises(FrozenInstanceError):
        delivery_plan.short_videos[0].title = "事后篡改"


def test_finalize_rechecks_excluded_content_inside_remedied_boundary(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "安全主题的主体内容",
            "补救范围会触及内部闲聊",
        ),
        course_context={"excluded_content": ["内部闲聊"]},
    )
    first, second = candidate_plan.candidates
    reviews = (
        _CandidateReview(
            candidate_id=first.candidate_id,
            topic_name="安全主题",
            boundary_fix_suggestion="向后扩展补齐结论",
            boundary_fix_start_ms=first.initial_start_ms,
            boundary_fix_end_ms=210_000,
        ),
        _CandidateReview(
            candidate_id=second.candidate_id,
            topic_name="排除内容",
            export_decision="reject",
            reject_reason="课程上下文要求排除",
        ),
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    first_selection = delivery_plan.candidates[0].selection
    assert isinstance(first_selection, RejectedSelection)
    assert (
        first_selection.reason_code
        is RejectionReason.EXCLUDED_CONTENT
    )
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.short_videos == ()


def test_finalize_rechecks_all_transcript_inside_remedied_boundary(
    tmp_path,
):
    texts = tuple(
        "内部闲聊" if index == 20 else f"安全正文 {index}"
        for index in range(30)
    )
    candidate_plan = _candidate_plan(
        tmp_path,
        texts,
        course_context={"excluded_content": ["内部闲聊"]},
        chunk_duration_ms=10_000,
    )
    first, second = candidate_plan.candidates
    reviews = (
        _CandidateReview(
            candidate_id=first.candidate_id,
            topic_name="安全主题",
            boundary_fix_suggestion="向后扩展补齐结论",
            boundary_fix_start_ms=first.initial_start_ms,
            boundary_fix_end_ms=300_000,
        ),
        _CandidateReview(
            candidate_id=second.candidate_id,
            topic_name="排除内容",
            export_decision="reject",
            reject_reason="课程上下文要求排除",
        ),
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    first_selection = delivery_plan.candidates[0].selection
    assert isinstance(first_selection, RejectedSelection)
    assert (
        first_selection.reason_code
        is RejectionReason.EXCLUDED_CONTENT
    )
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.short_videos == ()


def test_finalize_does_not_join_same_topic_across_a_rejected_candidate(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        (
            "同主题第一条",
            "中间候选不合格",
            "同主题第二条",
        ),
    )
    reviews = (
        _CandidateReview(
            candidate_id=candidate_plan.candidates[0].candidate_id,
            topic_name="连续主题",
        ),
        _CandidateReview(
            candidate_id=candidate_plan.candidates[1].candidate_id,
            topic_name="连续主题",
            topic_complete=False,
        ),
        _CandidateReview(
            candidate_id=candidate_plan.candidates[2].candidate_id,
            topic_name="连续主题",
        ),
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(reviews),
    )

    assert len(delivery_plan.short_videos) == 2
    assert delivery_plan.series == ()


def test_finalize_preserves_word_boundaries_when_matching_context_terms(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("highlight technique",),
        course_context={"excluded_content": ["high light"]},
    )
    review = _CandidateReview(
        candidate_id=candidate_plan.candidates[0].candidate_id,
        topic_name="highlight",
        title="highlight title",
        summary="highlight summary",
    )

    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview((review,)),
    )

    assert delivery_plan.result_kind is ResultKind.CLIPS
    assert isinstance(
        delivery_plan.candidates[0].selection,
        PublishedSelection,
    )
