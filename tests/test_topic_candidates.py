from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate, TranscriptChunk
from video_auto_editor.topic import generate_clip_candidates


def live_config(**overrides):
    config = CONFIG.copy()
    config.update(
        {
            "min_clip_duration": 20,
            "max_clip_duration": 80,
            "target_clip_duration": 60,
            "topic_overlap_seconds": 10,
            "context_expand_before": 12,
            "context_expand_after": 8,
        }
    )
    config.update(overrides)
    return config


def test_generate_clip_candidates_returns_empty_for_empty_or_blank_chunks():
    assert generate_clip_candidates([], [], 120, live_config()) == []
    assert generate_clip_candidates([TranscriptChunk(0, 10, "  \n ")], [], 120, live_config()) == []


def test_generate_clip_candidates_creates_multiple_windows_with_overlap():
    chunks = [
        TranscriptChunk(0, 20, "第一段"),
        TranscriptChunk(20, 40, "第二段"),
        TranscriptChunk(40, 60, "第三段"),
        TranscriptChunk(60, 80, "第四段"),
        TranscriptChunk(80, 100, "第五段"),
    ]

    candidates = generate_clip_candidates(chunks, [], 100, live_config())

    assert [(c.index, c.start_time, c.end_time, c.duration) for c in candidates] == [
        (0, 0, 60, 60),
        (1, 40, 100, 60),
    ]
    assert candidates[0].chunk_start_index == 0
    assert candidates[0].chunk_end_index == 2
    assert candidates[1].chunk_start_index == 2
    assert candidates[1].chunk_end_index == 4


def test_generate_clip_candidates_normalizes_text():
    chunks = [
        TranscriptChunk(0, 20, "  第一段\n文本  "),
        TranscriptChunk(20, 40, "第二段\t文本"),
    ]

    candidates = generate_clip_candidates(chunks, [], 40, live_config(target_clip_duration=40))

    assert candidates[0].text == "第一段 文本 第二段 文本"


def test_generate_clip_candidates_adjusts_to_nearby_silence_boundaries():
    chunks = [TranscriptChunk(10, 50, "边界校准片段")]
    silences = [(0, 8), (52, 60)]

    candidates = generate_clip_candidates(
        chunks,
        silences,
        total_duration=70,
        config=live_config(target_clip_duration=40),
    )

    assert candidates == [
        ClipCandidate(
            index=0,
            start_time=8,
            end_time=52,
            duration=44,
            text="边界校准片段",
            base_score=96.0,
            chunk_start_index=0,
            chunk_end_index=0,
        )
    ]


def test_generate_clip_candidates_ignores_invalid_silence_spans():
    chunks = [TranscriptChunk(10, 50, "非法静音区间")]
    silences = [(12, 8), (52, 60)]

    candidates = generate_clip_candidates(
        chunks,
        silences,
        total_duration=70,
        config=live_config(target_clip_duration=40),
    )

    assert len(candidates) == 1
    assert candidates[0].start_time == 10
    assert candidates[0].end_time == 52


def test_generate_clip_candidates_does_not_adjust_end_beyond_total_duration():
    chunks = [TranscriptChunk(10, 48, "视频尾部片段")]
    silences = [(55, 60)]

    candidates = generate_clip_candidates(
        chunks,
        silences,
        total_duration=50,
        config=live_config(target_clip_duration=40),
    )

    assert len(candidates) == 1
    assert candidates[0].end_time == 48


def test_generate_clip_candidates_skips_non_positive_duration_after_clamp():
    chunks = [TranscriptChunk(80, 90, "越界转写片段")]

    candidates = generate_clip_candidates(
        chunks,
        [],
        total_duration=50,
        config=live_config(min_clip_duration=1, target_clip_duration=10),
    )

    assert candidates == []


def test_generate_clip_candidates_filters_too_short_and_too_long_windows():
    config = live_config(min_clip_duration=30, max_clip_duration=50, target_clip_duration=40)

    short = generate_clip_candidates([TranscriptChunk(0, 20, "太短")], [], 100, config)
    long = generate_clip_candidates([TranscriptChunk(0, 70, "太长")], [], 100, config)

    assert short == []
    assert long == []


def test_generate_clip_candidates_uses_transcript_times_without_silence():
    chunks = [TranscriptChunk(5, 45, "无静音边界")]

    candidates = generate_clip_candidates(chunks, [], 60, live_config(target_clip_duration=40))

    assert candidates[0].start_time == 5
    assert candidates[0].end_time == 45
    assert candidates[0].duration == 40
