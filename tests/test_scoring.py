from video_auto_editor.models import Segment
from video_auto_editor.scoring import (
    _score_boundary,
    analyze_fluency,
    calculate_adjusted_score,
    score_segment,
)


def test_score_boundary_covers_silence_and_video_edges():
    assert _score_boundary([(1.0, 2.0)], 2.0, 10.0, is_start=True) == 25
    assert _score_boundary([(1.0, 1.5)], 1.0, 10.0, is_start=False) == 20
    assert _score_boundary([(1.0, 1.4)], 1.0, 10.0, is_start=False) == 10
    assert _score_boundary([], 0.0, 10.0, is_start=True) == 15
    assert _score_boundary([], 10.0, 10.0, is_start=False) == 15
    assert _score_boundary([], 5.0, 10.0, is_start=True) == 5


def test_score_segment_counts_interruptions_and_caps_short_rhythm():
    segment = Segment(index=1, start_time=2.0, end_time=9.0, duration=7.0)

    score_segment(segment, [(1.0, 2.0), (3.0, 4.0), (5.0, 5.5), (9.0, 10.0)], 12.0)

    assert segment.score_start == 25
    assert segment.score_end == 25
    assert segment.internal_silences == [(3.0, 4.0), (5.0, 5.5)]
    assert segment.interruption_count == 2
    assert segment.interruption_duration == 1.5
    assert segment.score_fluency == 20
    assert segment.score_rhythm == 11
    assert segment.total_score == 81


def test_score_segment_fluency_decreases_with_many_interruptions():
    segment = Segment(index=1, start_time=0, end_time=30, duration=30)
    internal = [(i, i + 0.2) for i in [2, 4, 6, 8, 10]]

    score_segment(segment, internal, 30)

    assert segment.interruption_count == 5
    assert segment.score_fluency == 10


def test_analyze_fluency_detects_repeat_stutter_interruption_and_natural_end():
    repeat, stutter, natural, interrupted = analyze_fluency("我们今天今天讲这个内容。")
    assert repeat >= 1
    assert stutter == 0
    assert natural is True
    assert interrupted is False

    repeat, stutter, natural, interrupted = analyze_fluency("嗯那个我们继续然后")
    assert repeat == 0
    assert stutter == 2
    assert natural is False
    assert interrupted is True


def test_calculate_adjusted_score_applies_bonus_penalty_and_clamp():
    segment = Segment(index=1, start_time=0, end_time=60, duration=60, total_score=90)
    segment.repeat_count = 2
    segment.stutter_count = 1
    segment.is_natural_end = True

    assert calculate_adjusted_score(segment) == 91.5

    high = Segment(index=2, start_time=0, end_time=60, duration=60, total_score=100)
    high.is_natural_end = True
    assert calculate_adjusted_score(high) == 100

    low = Segment(index=3, start_time=0, end_time=30, duration=30, total_score=5)
    low.repeat_count = 5
    low.stutter_count = 5
    low.is_interrupted = True
    assert calculate_adjusted_score(low) == 0
