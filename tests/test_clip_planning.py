import json
from dataclasses import FrozenInstanceError, dataclass, replace

import pytest

from video_auto_editor.clip_planning import CandidatePlan, ClipPlanning
from video_auto_editor.configuration import Configuration
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
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


def _source_description(tmp_path, *, duration_ms: int) -> SourceDescription:
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"source")
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    return SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=6,
        duration_ms=duration_ms,
    )


def test_prepare_returns_an_immutable_empty_candidate_plan(tmp_path):
    source = _source_description(tmp_path, duration_ms=300_000)
    transcript_id = TranscriptId.new()
    transcript = _CompleteTranscript(transcript_id, ())
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )

    assert isinstance(plan, CandidatePlan)
    assert isinstance(plan.plan_id, PlanId)
    assert plan.transcript_id == transcript_id
    assert plan.source_duration_ms == 300_000
    assert plan.candidates == ()
    assert not hasattr(plan, "result_kind")
    with pytest.raises(FrozenInstanceError):
        plan.candidates = ()


def test_candidate_plan_can_only_be_issued_by_clip_planning(tmp_path):
    source = _source_description(tmp_path, duration_ms=300_000)
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    with pytest.raises(
        TypeError,
        match="只能由 ClipPlanning 创建",
    ):
        CandidatePlan(
            plan_id=PlanId.new(),
            transcript_id=TranscriptId.new(),
            source_duration_ms=source.duration_ms,
            course_context=None,
            clip_policy=clip_policy,
            candidates=[],
        )


def test_prepare_forms_typed_candidates_with_legal_ranges_and_chunk_references(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=420_000)
    chunks = tuple(
        _TranscriptChunk(
            TranscriptChunkId.new(),
            start_ms,
            start_ms + 60_000,
            f"第 {index} 段忠实转写",
        )
        for index, start_ms in enumerate(
            range(0, 360_000, 60_000),
            start=1,
        )
    )
    transcript = _CompleteTranscript(TranscriptId.new(), chunks)
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )

    assert [
        (
            candidate.initial_start_ms,
            candidate.initial_end_ms,
            candidate.transcript_chunk_ids,
        )
        for candidate in plan.candidates
    ] == [
        (0, 180_000, tuple(chunk.transcript_chunk_id for chunk in chunks[:3])),
        (
            180_000,
            360_000,
            tuple(chunk.transcript_chunk_id for chunk in chunks[3:]),
        ),
    ]
    assert all(
        isinstance(candidate.candidate_id, CandidateId)
        for candidate in plan.candidates
    )
    assert len({candidate.candidate_id for candidate in plan.candidates}) == 2
    with pytest.raises(FrozenInstanceError):
        plan.candidates[0].initial_end_ms = 1


def test_prepare_forms_neighboring_review_context_in_source_time_order(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=420_000)
    chunks = tuple(
        _TranscriptChunk(
            TranscriptChunkId.new(),
            start_ms,
            start_ms + 60_000,
            f"敏感忠实转写 {index}",
        )
        for index, start_ms in enumerate(
            range(0, 360_000, 60_000),
            start=1,
        )
    )
    transcript = _CompleteTranscript(TranscriptId.new(), chunks[::-1])
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )

    first_context = plan.candidates[0].review_context
    second_context = plan.candidates[1].review_context
    assert first_context.preceding_chunks == ()
    assert tuple(
        excerpt.transcript_chunk_id
        for excerpt in first_context.candidate_chunks
    ) == tuple(chunk.transcript_chunk_id for chunk in chunks[:3])
    assert [
        (excerpt.start_ms, excerpt.end_ms, excerpt.text)
        for excerpt in first_context.candidate_chunks
    ] == [
        (chunk.start_ms, chunk.end_ms, chunk.text)
        for chunk in chunks[:3]
    ]
    assert tuple(
        excerpt.transcript_chunk_id
        for excerpt in first_context.following_chunks
    ) == (chunks[3].transcript_chunk_id,)
    assert tuple(
        excerpt.transcript_chunk_id
        for excerpt in second_context.preceding_chunks
    ) == (chunks[2].transcript_chunk_id,)
    assert tuple(
        excerpt.transcript_chunk_id
        for excerpt in second_context.candidate_chunks
    ) == tuple(chunk.transcript_chunk_id for chunk in chunks[3:])
    assert second_context.following_chunks == ()
    assert [
        (
            candidate.initial_start_ms,
            candidate.initial_end_ms,
        )
        for candidate in plan.candidates
    ] == [(0, 180_000), (180_000, 360_000)]
    assert "敏感忠实转写" not in repr(plan)
    assert "敏感忠实转写" not in repr(first_context)
    with pytest.raises(FrozenInstanceError):
        first_context.candidate_chunks = ()
    with pytest.raises(FrozenInstanceError):
        first_context.candidate_chunks[0].text = "被篡改"


def test_prepare_carries_deferred_review_and_selection_inputs_without_applying_them(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=420_000)
    source.source_file.path.with_suffix(".context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": "不可变架构课",
                "priority_topics": ["重点主题"],
                "excluded_content": ["忠实转写"],
            }
        ),
        encoding="utf-8",
    )
    loaded = Configuration.load(
        source.source_file.path,
        {
            "schema_version": "configuration.v1",
            "clip_policy": {"max_clips": 1},
        },
    )
    chunks = tuple(
        _TranscriptChunk(
            TranscriptChunkId.new(),
            start_ms,
            start_ms + 60_000,
            f"第 {index} 段忠实转写",
        )
        for index, start_ms in enumerate(
            range(0, 360_000, 60_000),
            start=1,
        )
    )

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(TranscriptId.new(), chunks),
        loaded.course_context,
        loaded.effective.clip_policy,
    )

    assert plan.course_context == loaded.course_context
    assert plan.clip_policy == loaded.effective.clip_policy
    assert len(plan.candidates) == 2
    assert "不可变架构课" not in repr(plan)
    assert "忠实转写" not in repr(plan)
    forbidden_final_facts = {
        "final_end_ms",
        "final_start_ms",
        "result_kind",
        "selection",
        "series_id",
        "short_video_id",
        "topic_review",
    }
    assert all(
        not hasattr(plan, field_name)
        for field_name in forbidden_final_facts
    )
    assert all(
        all(
            not hasattr(candidate, field_name)
            for field_name in forbidden_final_facts
        )
        for candidate in plan.candidates
    )


def test_prepare_snapshots_course_context_as_deeply_immutable_data(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=180_000)
    source.source_file.path.with_suffix(".context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": "不可变输入课",
                "priority_topics": ["初始重点"],
            }
        ),
        encoding="utf-8",
    )
    loaded = Configuration.load(source.source_file.path)
    assert loaded.course_context is not None
    mutable_topics = ["初始重点"]
    forged_context = replace(
        loaded.course_context,
        priority_topics=mutable_topics,
    )

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(TranscriptId.new(), ()),
        forged_context,
        loaded.effective.clip_policy,
    )
    mutable_topics.append("事后篡改")

    assert plan.course_context is not forged_context
    assert plan.course_context is not None
    assert plan.course_context.priority_topics == ("初始重点",)


def test_prepare_rejects_a_clip_policy_with_an_invalid_duration_order(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=420_000)
    loaded = Configuration.load(source.source_file.path)
    invalid_policy = replace(
        loaded.effective.clip_policy,
        min_duration_seconds=240,
        target_duration_seconds=180,
    )

    with pytest.raises(
        ValueError,
        match="最短、目标、最长时长顺序",
    ):
        ClipPlanning.prepare(
            source,
            _CompleteTranscript(TranscriptId.new(), ()),
            None,
            invalid_policy,
        )


@pytest.mark.parametrize(
    "policy_override",
    [
        {"max_duration_seconds": 3_601},
        {"max_clips": 1_001},
    ],
)
def test_prepare_rejects_a_clip_policy_outside_certified_bounds(
    tmp_path,
    policy_override,
):
    source = _source_description(tmp_path, duration_ms=420_000)
    loaded = Configuration.load(source.source_file.path)
    invalid_policy = replace(
        loaded.effective.clip_policy,
        **policy_override,
    )

    with pytest.raises(ValueError, match="认证范围"):
        ClipPlanning.prepare(
            source,
            _CompleteTranscript(TranscriptId.new(), ()),
            None,
            invalid_policy,
        )


def test_prepare_does_not_invent_text_boundaries_inside_an_oversized_chunk(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=360_000)
    transcript_chunk_id = TranscriptChunkId.new()
    transcript = _CompleteTranscript(
        TranscriptId.new(),
        (
            _TranscriptChunk(
                transcript_chunk_id,
                0,
                360_000,
                "一个供应商返回的长转写文本块",
            ),
        ),
    )
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )

    assert plan.candidates == ()


def test_prepare_absorbs_nested_chunks_without_shrinking_a_legal_candidate(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=180_000)
    outer = _TranscriptChunk(
        TranscriptChunkId.new(),
        0,
        180_000,
        "完整候选正文",
    )
    nested = _TranscriptChunk(
        TranscriptChunkId.new(),
        60_000,
        100_000,
        "同一时间范围内的并行转写",
    )
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(
            TranscriptId.new(),
            (nested, outer),
        ),
        None,
        clip_policy,
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert (
        candidate.initial_start_ms,
        candidate.initial_end_ms,
    ) == (0, 180_000)
    assert candidate.transcript_chunk_ids == (
        outer.transcript_chunk_id,
        nested.transcript_chunk_id,
    )
    assert candidate.review_context.preceding_chunks == ()
    assert candidate.review_context.following_chunks == ()

    same_facts_different_order = ClipPlanning.prepare(
        source,
        _CompleteTranscript(
            transcript_id=TranscriptId.new(),
            chunks=(outer, nested),
        ),
        None,
        clip_policy,
    )
    assert [
        (
            item.initial_start_ms,
            item.initial_end_ms,
            item.transcript_chunk_ids,
            item.review_context.candidate_chunks,
        )
        for item in plan.candidates
    ] == [
        (
            item.initial_start_ms,
            item.initial_end_ms,
            item.transcript_chunk_ids,
            item.review_context.candidate_chunks,
        )
        for item in same_facts_different_order.candidates
    ]


def test_prepare_uses_content_before_run_local_ids_to_break_time_ties(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=180_000)
    chunks = (
        _TranscriptChunk(
            TranscriptChunkId(
                "transcript_chunk_00000000-0000-4000-8000-000000000001"
            ),
            0,
            180_000,
            "乙内容",
        ),
        _TranscriptChunk(
            TranscriptChunkId(
                "transcript_chunk_00000000-0000-4000-8000-000000000002"
            ),
            0,
            180_000,
            "一内容",
        ),
    )
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(TranscriptId.new(), chunks),
        None,
        clip_policy,
    )

    assert [
        excerpt.text
        for excerpt in plan.candidates[0].review_context.candidate_chunks
    ] == ["一内容", "乙内容"]


def test_prepare_creates_fresh_non_content_derived_ids_for_each_plan(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=180_000)
    transcript = _CompleteTranscript(
        TranscriptId.new(),
        (
            _TranscriptChunk(
                TranscriptChunkId.new(),
                0,
                180_000,
                "相同业务输入",
            ),
        ),
    )
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    first = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )
    second = ClipPlanning.prepare(
        source,
        transcript,
        None,
        clip_policy,
    )

    assert first.plan_id != second.plan_id
    assert first.candidates[0].candidate_id == (
        first.candidates[0].candidate_id
    )
    assert first.candidates[0].candidate_id != (
        second.candidates[0].candidate_id
    )
    assert (
        first.candidates[0].initial_start_ms,
        first.candidates[0].initial_end_ms,
        first.candidates[0].transcript_chunk_ids,
    ) == (
        second.candidates[0].initial_start_ms,
        second.candidates[0].initial_end_ms,
        second.candidates[0].transcript_chunk_ids,
    )


@pytest.mark.parametrize(
    "chunks",
    [
        (
            _TranscriptChunk(
                TranscriptChunkId.new(),
                -1,
                60_000,
                "越界转写",
            ),
        ),
        (
            _TranscriptChunk(
                TranscriptChunkId.new(),
                0,
                60_000,
                " ",
            ),
        ),
    ],
)
def test_prepare_rejects_an_invalid_complete_transcript(tmp_path, chunks):
    source = _source_description(tmp_path, duration_ms=180_000)
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    with pytest.raises(ValueError):
        ClipPlanning.prepare(
            source,
            _CompleteTranscript(TranscriptId.new(), chunks),
            None,
            clip_policy,
        )


def test_prepare_extends_the_previous_candidate_to_cover_a_short_tail(
    tmp_path,
):
    source = _source_description(tmp_path, duration_ms=200_000)
    chunks = (
        _TranscriptChunk(
            TranscriptChunkId.new(),
            0,
            60_000,
            "第一段",
        ),
        _TranscriptChunk(
            TranscriptChunkId.new(),
            60_000,
            120_000,
            "第二段",
        ),
        _TranscriptChunk(
            TranscriptChunkId.new(),
            120_000,
            180_000,
            "第三段",
        ),
        _TranscriptChunk(
            TranscriptChunkId.new(),
            180_000,
            200_000,
            "不足最短时长的尾段",
        ),
    )
    clip_policy = Configuration.load(
        source.source_file.path
    ).effective.clip_policy

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(TranscriptId.new(), chunks),
        None,
        clip_policy,
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert (
        candidate.initial_start_ms,
        candidate.initial_end_ms,
    ) == (0, 200_000)
    assert candidate.transcript_chunk_ids == tuple(
        chunk.transcript_chunk_id for chunk in chunks
    )


def test_prepare_uses_a_valid_custom_clip_duration_policy(tmp_path):
    source = _source_description(tmp_path, duration_ms=180_000)
    loaded = Configuration.load(
        source.source_file.path,
        {
            "schema_version": "configuration.v1",
            "clip_policy": {
                "min_duration_seconds": 30,
                "target_duration_seconds": 90,
                "max_duration_seconds": 120,
            },
        },
    )
    chunks = tuple(
        _TranscriptChunk(
            TranscriptChunkId.new(),
            start_ms,
            start_ms + 30_000,
            f"策略转写 {index}",
        )
        for index, start_ms in enumerate(
            range(0, 180_000, 30_000),
            start=1,
        )
    )

    plan = ClipPlanning.prepare(
        source,
        _CompleteTranscript(TranscriptId.new(), chunks),
        None,
        loaded.effective.clip_policy,
    )

    assert [
        (candidate.initial_start_ms, candidate.initial_end_ms)
        for candidate in plan.candidates
    ] == [(0, 90_000), (90_000, 180_000)]
