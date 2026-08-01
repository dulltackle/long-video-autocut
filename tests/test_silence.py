from types import SimpleNamespace

from video_auto_editor import silence


def test_detect_silence_parses_multiple_spans(monkeypatch):
    stderr = """
    [silencedetect @ 0x1] silence_start: 1.25
    [silencedetect @ 0x1] silence_end: 2.50 | silence_duration: 1.25
    [silencedetect @ 0x1] silence_start: 8
    [silencedetect @ 0x1] silence_end: 9.75 | silence_duration: 1.75
    """

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["ffmpeg", "-i"]
        assert "silencedetect=noise=-30dB:d=0.8" in cmd
        return SimpleNamespace(returncode=0, stderr=stderr)

    monkeypatch.setattr(silence.subprocess, "run", fake_run)

    assert silence.detect_silence("input.mov") == [(1.25, 2.5), (8.0, 9.75)]


def test_detect_silence_returns_empty_when_no_spans(monkeypatch):
    monkeypatch.setattr(
        silence.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="no silence"),
    )

    assert silence.detect_silence("input.mov") == []


def test_detect_silence_raises_on_ffmpeg_failure(monkeypatch):
    monkeypatch.setattr(
        silence.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="bad input"),
    )

    try:
        silence.detect_silence("input.mov")
    except RuntimeError as exc:
        assert "FFmpeg silencedetect failed: bad input" in str(exc)
    else:
        raise AssertionError("detect_silence should raise on ffmpeg failure")
