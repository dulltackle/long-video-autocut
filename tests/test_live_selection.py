from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate
from video_auto_editor.selection import resolve_live_max_clips, select_live_clips


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


def test_resolve_live_max_clips_uses_temporary_protective_limit_by_default():
    assert resolve_live_max_clips(None, live_config(max_clips=100, temporary_protective_max_clips=5)) == 5


def test_resolve_live_max_clips_uses_explicit_user_limit():
    config = live_config(max_clips=2, max_clips_user_provided=True, temporary_protective_max_clips=5)

    assert resolve_live_max_clips(None, config) == 2
    assert resolve_live_max_clips(3, config) == 3
