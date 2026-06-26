from video_auto_editor.subtitle import filter_filler_words

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
