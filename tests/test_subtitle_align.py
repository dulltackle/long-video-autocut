from video_auto_editor.models import TranscriptChunk
from video_auto_editor.subtitle_align import build_window


def test_build_window_concatenates_text_and_char_times():
    chunks = [
        TranscriptChunk(0.0, 2.0, "你好", char_spans=[(0.0, 1.0), (1.0, 2.0)]),
        TranscriptChunk(2.0, 4.0, "再见", char_spans=[(2.0, 3.0), (3.0, 4.0)]),
    ]

    text, char_times = build_window(chunks)

    assert text == "你好再见"
    assert char_times == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]
    assert len(char_times) == len(text)


def test_build_window_proportional_fallback_when_char_spans_missing():
    chunks = [TranscriptChunk(0.0, 4.0, "今天好。", char_spans=None)]

    text, char_times = build_window(chunks)

    assert text == "今天好。"
    assert char_times == [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0)]


def test_build_window_falls_back_when_char_spans_length_mismatch():
    # char_spans 与文本长度不一致时不可信，回退按比例兜底。
    chunks = [TranscriptChunk(0.0, 2.0, "你好", char_spans=[(0.0, 1.0)])]

    text, char_times = build_window(chunks)

    assert text == "你好"
    assert char_times == [(0.0, 1.0), (1.0, 2.0)]


def test_build_window_mixes_precise_and_fallback_segments():
    chunks = [
        TranscriptChunk(0.0, 2.0, "你好", char_spans=[(0.0, 0.5), (0.5, 2.0)]),
        TranscriptChunk(2.0, 4.0, "再见", char_spans=None),
    ]

    text, char_times = build_window(chunks)

    assert text == "你好再见"
    assert char_times == [(0.0, 0.5), (0.5, 2.0), (2.0, 3.0), (3.0, 4.0)]


def test_build_window_skips_empty_chunks():
    chunks = [
        TranscriptChunk(0.0, 1.0, "", char_spans=None),
        TranscriptChunk(1.0, 2.0, "好", char_spans=[(1.0, 2.0)]),
    ]

    text, char_times = build_window(chunks)

    assert text == "好"
    assert char_times == [(1.0, 2.0)]
