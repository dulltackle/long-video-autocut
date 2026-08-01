from video_auto_editor.scoring import analyze_fluency


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
