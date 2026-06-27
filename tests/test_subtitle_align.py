from video_auto_editor.models import TranscriptChunk
from video_auto_editor.subtitle_align import build_window, validate_and_align


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


def _seq_char_times(n):
    return [(float(i), float(i + 1)) for i in range(n)]


def test_validate_and_align_deletes_words_and_splits_blocks():
    text = "今天天气真好啊我们出门吧"
    char_times = _seq_char_times(len(text))
    # 删掉句末「啊」语气词，并按语义切成两块。
    blocks = validate_and_align(text, char_times, ["今天天气真好", "我们出门吧"], 15, 1)

    assert blocks is not None
    assert [block.text for block in blocks] == ["今天天气真好", "我们出门吧"]
    assert blocks[0].start == 0.0
    assert blocks[0].end == 6.0  # "好" 在索引 5，end=6
    assert blocks[1].start == 7.0  # "我" 在索引 7（跳过索引 6 的「啊」）
    assert blocks[1].end == 12.0


def test_validate_and_align_rejects_inserted_char():
    text = "今天好"
    char_times = _seq_char_times(len(text))

    assert validate_and_align(text, char_times, ["今天很好"], 15, 1) is None


def test_validate_and_align_rejects_modified_char():
    text = "今天好"
    char_times = _seq_char_times(len(text))

    assert validate_and_align(text, char_times, ["今日好"], 15, 1) is None


def test_validate_and_align_rejects_reordered_chars():
    text = "今天好"
    char_times = _seq_char_times(len(text))

    assert validate_and_align(text, char_times, ["天今好"], 15, 1) is None


def test_validate_and_align_greedy_earliest_match_on_repeats():
    text = "好好学习"
    char_times = _seq_char_times(len(text))
    # 两个「好」分块，贪心最早匹配应让指针单调推进：第一块取索引 0，第二块取索引 1。
    blocks = validate_and_align(text, char_times, ["好", "好学习"], 15, 1)

    assert blocks is not None
    assert blocks[0].start == 0.0
    assert blocks[0].end == 1.0
    assert blocks[1].start == 1.0
    assert blocks[1].end == 4.0


def test_validate_and_align_wraps_long_block_into_lines():
    text = "一二三四五六七八九十"
    char_times = _seq_char_times(len(text))
    blocks = validate_and_align(text, char_times, ["一二三四五六七八九十"], 5, 2)

    assert blocks is not None
    assert "\n" in blocks[0].text
    assert blocks[0].text.replace("\n", "") == "一二三四五六七八九十"
    # 整块时间仍取首末匹配字。
    assert blocks[0].start == 0.0
    assert blocks[0].end == 10.0


def test_validate_and_align_skips_blank_lines():
    text = "今天好"
    char_times = _seq_char_times(len(text))
    blocks = validate_and_align(text, char_times, ["今天好", "  "], 15, 1)

    assert blocks is not None
    assert [block.text for block in blocks] == ["今天好"]
