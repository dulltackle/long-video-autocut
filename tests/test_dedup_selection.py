from video_auto_editor.dedup import _find_duplicate_groups, check_duplicate_content, cross_video_dedup
from video_auto_editor.models import ClipInfo, Segment
from video_auto_editor.selection import select_best_segment


def make_segment(index, text="", adjusted=0, natural=False, duration=30, repeat=0, stutter=0, duplicate=False):
    segment = Segment(index=index, start_time=index * 10, end_time=index * 10 + duration, duration=duration)
    segment.transcript = text
    segment.adjusted_score = adjusted
    segment.is_natural_end = natural
    segment.repeat_count = repeat
    segment.stutter_count = stutter
    segment.is_duplicate = duplicate
    return segment


def test_find_duplicate_groups_uses_similarity_threshold():
    items = [make_segment(0, "今天讲视频剪辑"), make_segment(1, "今天讲视频剪辑。"), make_segment(2, "完全不同")]

    assert _find_duplicate_groups(items, lambda item: item.transcript, {"duplicate_threshold": 0.7}) == [{0, 1}]


def test_check_duplicate_content_keeps_natural_high_score_later_index():
    candidates = [
        make_segment(0, "重复内容", adjusted=99, natural=False),
        make_segment(1, "重复内容", adjusted=90, natural=True),
        make_segment(2, "重复内容", adjusted=90, natural=True),
    ]

    check_duplicate_content(candidates)

    assert candidates[2].is_duplicate is False
    assert candidates[0].is_duplicate is True
    assert candidates[1].is_duplicate is True
    assert candidates[0].duplicate_with == [2]
    assert candidates[1].duplicate_with == [2]


def test_cross_video_dedup_keeps_natural_high_score_later_name():
    clips = [
        ClipInfo("a", "a.mp4", "相同内容", adjusted_score=95, is_natural_end=False, duration=30),
        ClipInfo("b", "b.mp4", "相同内容", adjusted_score=90, is_natural_end=True, duration=30),
        ClipInfo("c", "c.mp4", "相同内容", adjusted_score=90, is_natural_end=True, duration=30),
    ]

    cross_video_dedup(clips)

    assert clips[2].is_cross_duplicate is False
    assert clips[0].is_cross_duplicate is True
    assert clips[1].is_cross_duplicate is True
    assert clips[0].duplicate_of == "c"
    assert clips[1].duplicate_of == "c"


def test_select_best_segment_skips_duplicates_and_prefers_natural_end():
    candidates = [
        make_segment(0, adjusted=100, natural=False),
        make_segment(1, adjusted=80, natural=True),
        make_segment(2, adjusted=95, natural=True, duplicate=True),
    ]

    assert select_best_segment(candidates).index == 1


def test_select_best_segment_uses_fluency_tolerance_then_score():
    candidates = [
        make_segment(0, adjusted=90, natural=True, repeat=0, stutter=0),
        make_segment(1, adjusted=95, natural=True, repeat=1, stutter=0),
        make_segment(2, adjusted=100, natural=True, repeat=3, stutter=0),
    ]

    assert select_best_segment(candidates).index == 1


def test_select_best_segment_tie_breaks_by_duration_or_later_index():
    natural_candidates = [
        make_segment(0, adjusted=90, natural=True, duration=20),
        make_segment(1, adjusted=90, natural=True, duration=40),
    ]
    assert select_best_segment(natural_candidates).index == 1

    unnatural_candidates = [
        make_segment(0, adjusted=90, natural=False),
        make_segment(1, adjusted=90, natural=False),
    ]
    assert select_best_segment(unnatural_candidates).index == 1
