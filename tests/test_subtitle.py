from video_auto_editor.models import TranscriptChunk
from video_auto_editor.subtitle import filter_filler_words, resegment_chunk, resegment_chunks

FILLERS = ["嗯", "啊", "呃", "哦", "唉", "呐", "嘛", "咯", "呀", "哎", "欸", "噢", "唔"]


def test_filter_drops_standalone_filler_block():
    assert filter_filler_words("嗯。", FILLERS) == ""


def test_filter_drops_repeated_filler():
    assert filter_filler_words("嗯嗯", FILLERS) == ""
    assert filter_filler_words("啊啊啊", FILLERS) == ""


def test_filter_removes_leading_filler_and_dangling_punct():
    assert filter_filler_words("嗯，今天我们讲愉悦技术", FILLERS) == "今天我们讲愉悦技术"


def test_filter_removes_midsentence_filler_keeps_colloquial():
    # 呃 删除；那个 是口头禅但不在词表，保留。
    assert filter_filler_words("那个，呃，重点是", FILLERS) == "那个，重点是"


def test_filter_does_not_touch_glued_tail():
    assert filter_filler_words("好啊", FILLERS) == "好啊"
    assert filter_filler_words("是啊，没错", FILLERS) == "是啊，没错"


def test_filter_handles_multiple_and_residual_punctuation():
    assert filter_filler_words("啊，对，嗯，就这样", FILLERS) == "对，就这样"


def test_filter_empty_word_list_returns_original():
    assert filter_filler_words("啊，对，嗯，就这样", []) == "啊，对，嗯，就这样"


def test_resegment_short_chunk_single_line_no_wrap():
    blocks = resegment_chunk(TranscriptChunk(0, 5, "今天讲技术"), 15, 2)

    assert len(blocks) == 1
    assert blocks[0].text == "今天讲技术"
    assert "\n" not in blocks[0].text
    assert blocks[0].start == 0
    assert blocks[0].end == 5


def test_resegment_medium_chunk_wraps_two_lines():
    text = "甲" * 20
    blocks = resegment_chunk(TranscriptChunk(0, 10, text), 15, 2)

    assert len(blocks) == 1
    lines = blocks[0].text.split("\n")
    assert len(lines) == 2
    assert all(len(line) <= 15 for line in lines)
    assert "".join(lines) == text


def test_resegment_long_chunk_splits_into_capped_blocks():
    text = "甲" * 40
    blocks = resegment_chunk(TranscriptChunk(0, 40, text), 15, 2)

    assert len(blocks) >= 2
    # 每块字符数（不含换行）不超过 block_cap=30。
    assert all(len(block.text.replace("\n", "")) <= 30 for block in blocks)
    # 首块 start、末块 end 对齐原 chunk。
    assert blocks[0].start == 0
    assert blocks[-1].end == 40
    # 块间无缝。
    for prev, nxt in zip(blocks, blocks[1:]):
        assert prev.end == nxt.start
    # 文本无损（拼回去等于原文）。
    assert "".join(block.text.replace("\n", "") for block in blocks) == text


def test_resegment_allocates_time_proportionally_at_punctuation():
    text = "甲" * 15 + "。" + "乙" * 15  # 31 字，> block_cap=30
    blocks = resegment_chunk(TranscriptChunk(0, 31, text), 15, 2)

    assert len(blocks) == 2
    # 句末标点处断块：第一块含 16 字（含句号）。
    assert len(blocks[0].text.replace("\n", "")) == 16
    # 时间按字符比例分配：边界 = 31 * 16 / 31 = 16，落在文本时间中点附近。
    assert abs(blocks[0].end - 16.0) < 0.5
    assert blocks[1].start == blocks[0].end


def test_resegment_chunks_preserves_order():
    chunks = [TranscriptChunk(0, 2, "第一块"), TranscriptChunk(2, 4, "第二块")]
    blocks = resegment_chunks(chunks, 15, 2)

    assert [block.text for block in blocks] == ["第一块", "第二块"]
