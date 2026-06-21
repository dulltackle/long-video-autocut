from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate, TopicReviewResult
from video_auto_editor.selection import resolve_live_max_clips, select_live_clips, select_live_exports


def live_config(**overrides):
    config = CONFIG.copy()
    config.update({"min_clip_gap_seconds": 0})
    config.update(overrides)
    return config


def make_candidate(index, start, end, base=80, adjusted=0, duplicate=False):
    return ClipCandidate(
        index=index,
        start_time=start,
        end_time=end,
        duration=end - start,
        text=f"候选 {index}",
        base_score=base,
        adjusted_score=adjusted,
        is_duplicate=duplicate,
    )


def test_select_live_clips_returns_empty_for_empty_candidates():
    assert select_live_clips([], 3, live_config()) == []
    assert select_live_clips([make_candidate(1, 0, 10)], 0, live_config()) == []


def test_select_live_clips_limits_by_score_and_max_clips():
    candidates = [
        make_candidate(0, 0, 10, adjusted=70),
        make_candidate(1, 20, 30, adjusted=95),
        make_candidate(2, 40, 50, adjusted=90),
    ]

    selected = select_live_clips(candidates, 2, live_config())

    assert [candidate.index for candidate in selected] == [1, 2]


def test_select_live_clips_keeps_highest_score_for_overlapping_candidates():
    candidates = [
        make_candidate(0, 0, 60, adjusted=80),
        make_candidate(1, 10, 70, adjusted=95),
        make_candidate(2, 90, 130, adjusted=70),
    ]

    selected = select_live_clips(candidates, 3, live_config())

    assert [candidate.index for candidate in selected] == [1, 2]


def test_select_live_clips_skips_duplicates_and_returns_time_order():
    candidates = [
        make_candidate(0, 90, 120, adjusted=99),
        make_candidate(1, 0, 30, adjusted=80),
        make_candidate(2, 40, 70, adjusted=95, duplicate=True),
    ]

    selected = select_live_clips(candidates, 3, live_config())

    assert [candidate.index for candidate in selected] == [1, 0]


def test_resolve_live_max_clips_has_no_default_limit():
    assert resolve_live_max_clips(None, live_config(max_clips=100, temporary_protective_max_clips=5)) is None


def test_resolve_live_max_clips_uses_explicit_user_limit():
    config = live_config(max_clips=2, max_clips_user_provided=True, temporary_protective_max_clips=5)

    assert resolve_live_max_clips(None, config) == 2
    assert resolve_live_max_clips(3, config) == 3


def review(
    decision="publish_ready",
    score=90,
    complete=True,
    human=False,
    topic="主题",
    suggestion="",
    boundary_start=None,
    boundary_end=None,
):
    return TopicReviewResult(
        topic_name=topic,
        topic_complete=complete,
        learning_value=8,
        share_value=8,
        publish_ready_score=score,
        export_decision=decision,
        title=f"{topic}标题",
        summary=f"{topic}摘要",
        keywords=[topic],
        needs_human_review=human,
        reject_reason="",
        boundary_fix_suggestion=suggestion,
        boundary_fix_start=boundary_start,
        boundary_fix_end=boundary_end,
    )


def test_select_live_exports_keeps_only_publish_ready_reviewed_candidates():
    candidates = [
        make_candidate(0, 0, 10, adjusted=70),
        make_candidate(1, 20, 30, adjusted=95),
        make_candidate(2, 40, 50, adjusted=90),
        make_candidate(3, 60, 70, adjusted=85),
        make_candidate(4, 80, 90, adjusted=80),
        make_candidate(5, 100, 110, adjusted=75, duplicate=True),
    ]
    candidates[0].review = review(score=92)
    candidates[1].review = review(score=79)
    candidates[2].review = review(complete=False)
    candidates[3].review = review(human=True)
    candidates[4].review = review(decision="reject")
    candidates[5].review = review(score=96)

    selected, decisions = select_live_exports(candidates, None, live_config(), review_status="reviewed")

    assert [candidate.index for candidate in selected] == [0]
    reasons = {decision.candidate_index: decision.reason for decision in decisions}
    assert reasons == {
        0: "publish_ready",
        1: "publish_ready_score_below_threshold",
        2: "topic_incomplete",
        3: "needs_human_review",
        4: "review_decision_reject",
        5: "duplicate",
    }
    assert decisions[0].selected_for_export is True
    assert decisions[0].publish_ready_score == 92
    assert decisions[0].final_start == 0
    assert decisions[0].final_end == 10


def test_select_live_exports_does_not_truncate_reviewed_candidates_without_explicit_limit():
    candidates = [make_candidate(index, index * 20, index * 20 + 10, adjusted=100 - index) for index in range(7)]
    for candidate in candidates:
        candidate.review = review(score=90 - candidate.index)

    selected, decisions = select_live_exports(
        candidates,
        None,
        live_config(temporary_protective_max_clips=2),
        review_status="reviewed",
    )

    assert [candidate.index for candidate in selected] == list(range(7))
    assert all(decision.selected_for_export for decision in decisions)


def test_select_live_exports_applies_explicit_limit_by_quality_then_returns_time_order():
    candidates = [
        make_candidate(0, 40, 50, adjusted=70),
        make_candidate(1, 0, 10, adjusted=99),
        make_candidate(2, 20, 30, adjusted=80),
    ]
    candidates[0].review = review(score=90)
    candidates[1].review = review(score=95)
    candidates[2].review = review(score=93)

    selected, decisions = select_live_exports(
        candidates,
        None,
        live_config(max_clips=2, max_clips_user_provided=True),
        review_status="reviewed",
    )

    assert [candidate.index for candidate in selected] == [1, 2]
    assert {decision.candidate_index: decision.reason for decision in decisions} == {
        0: "max_clips_limit",
        1: "publish_ready",
        2: "publish_ready",
    }


def test_select_live_exports_blocks_unreviewed_candidates_by_default():
    selected, decisions = select_live_exports(
        [make_candidate(0, 0, 10), make_candidate(1, 20, 30)],
        None,
        live_config(allow_unreviewed_export=False),
        review_status="unreviewed",
    )

    assert selected == []
    assert [decision.reason for decision in decisions] == [
        "unreviewed_export_not_allowed",
        "unreviewed_export_not_allowed",
    ]


def test_select_live_exports_preserves_unreviewed_compatibility_when_allowed():
    candidates = [
        make_candidate(0, 0, 10, adjusted=70),
        make_candidate(1, 20, 30, adjusted=95),
    ]

    selected, decisions = select_live_exports(
        candidates,
        None,
        live_config(allow_unreviewed_export=True, max_clips=1, max_clips_user_provided=True),
        review_status="unreviewed",
    )

    assert [candidate.index for candidate in selected] == [1]
    assert {decision.candidate_index: decision.reason for decision in decisions} == {
        0: "legacy_score_not_selected",
        1: "legacy_score_selection",
    }
    assert decisions[1].selected_for_export is True


def test_select_live_exports_applies_explicit_boundary_fix():
    candidate = make_candidate(0, 10, 70, adjusted=90)
    candidate.review = review(boundary_start=12.5, boundary_end=68.0)

    selected, decisions = select_live_exports([candidate], None, live_config(), review_status="reviewed")

    assert selected == [candidate]
    assert candidate.start_time == 12.5
    assert candidate.end_time == 68.0
    assert candidate.duration == 55.5
    assert decisions[0].original_start == 10
    assert decisions[0].original_end == 70
    assert decisions[0].final_start == 12.5
    assert decisions[0].final_end == 68.0
    assert decisions[0].boundary_fix_applied is True


def test_select_live_exports_does_not_parse_natural_language_boundary_suggestion():
    candidate = make_candidate(0, 10, 70, adjusted=90)
    candidate.review = review(suggestion="建议向后补足结束句到 75 秒。")

    selected, decisions = select_live_exports([candidate], None, live_config(), review_status="reviewed")

    assert selected == []
    assert candidate.start_time == 10
    assert candidate.end_time == 70
    assert decisions[0].reason == "boundary_fix_needs_human_review"
    assert decisions[0].boundary_fix_suggestion == "建议向后补足结束句到 75 秒。"
    assert decisions[0].boundary_fix_applied is False


def test_select_live_exports_assigns_stable_series_key_by_topic_name():
    candidates = [
        make_candidate(0, 0, 10, adjusted=90),
        make_candidate(1, 20, 30, adjusted=89),
        make_candidate(2, 40, 50, adjusted=88),
    ]
    candidates[0].review = review(topic="同一主题")
    candidates[1].review = review(topic="同一主题")
    candidates[2].review = review(topic="另一主题")

    _, decisions = select_live_exports(candidates, None, live_config(), review_status="reviewed")
    series_by_index = {decision.candidate_index: decision.series_key for decision in decisions}

    assert series_by_index[0]
    assert series_by_index[0] == series_by_index[1]
    assert series_by_index[0] != series_by_index[2]
