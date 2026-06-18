import json
from pathlib import Path

from video_auto_editor import export
from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate, LiveClipInfo, TranscriptChunk


def live_config(**overrides):
    config = CONFIG.copy()
    config.update({"buffer_start": 1, "buffer_end": 3, "export_subtitles": True})
    config.update(overrides)
    return config


def make_candidate():
    return ClipCandidate(
        index=0,
        start_time=10,
        end_time=20,
        duration=10,
        text="这是一段直播文本。",
        base_score=88,
        adjusted_score=93,
        title='坏/标题|A',
        summary="直播摘要",
        keywords=["直播", "文本"],
    )


def make_named_candidate(index, title):
    candidate = make_candidate()
    candidate.index = index
    candidate.title = title
    return candidate


def test_export_live_clips_writes_safe_paths_metadata_and_shifted_srt(monkeypatch, tmp_path):
    calls = []

    def fake_clip(video_path, candidate, output_path, config=None):
        calls.append((video_path, candidate.index, output_path))
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips(
        "live.mp4",
        [make_candidate()],
        [
            TranscriptChunk(9, 12, "片头"),
            TranscriptChunk(19, 24, "片尾"),
        ],
        str(tmp_path),
        live_config(),
    )

    assert len(result) == 1
    assert isinstance(result[0], LiveClipInfo)
    assert Path(result[0].output_path).name == "001_坏_标题_A.mp4"
    assert Path(result[0].subtitle_path).name == "001_坏_标题_A.srt"
    assert calls == [("live.mp4", 0, str(tmp_path / "clips" / "001_坏_标题_A.mp4"))]

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source_video"] == "live.mp4"
    assert metadata["clips"][0]["output_path"] == "clips/001_坏_标题_A.mp4"
    assert metadata["clips"][0]["subtitle_path"] == "subtitles/001_坏_标题_A.srt"
    assert metadata["clips"][0]["score"] == 93
    assert metadata["clips"][0]["keywords"] == ["直播", "文本"]

    assert (tmp_path / "subtitles" / "001_坏_标题_A.srt").read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:03,000\n"
        "片头\n\n"
        "2\n"
        "00:00:10,000 --> 00:00:14,000\n"
        "片尾\n\n"
    )


def test_export_live_clips_returns_none_and_skips_metadata_on_clip_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(export, "clip_segment", lambda *args, **kwargs: False)

    result = export.export_live_clips("live.mp4", [make_candidate()], [], str(tmp_path), live_config())

    assert result is None
    assert not (tmp_path / "metadata.json").exists()


def test_export_live_clips_cleans_previous_outputs_on_later_clip_failure(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return candidate.index == 0

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips(
        "live.mp4",
        [make_named_candidate(0, "第一条"), make_named_candidate(1, "第二条")],
        [TranscriptChunk(10, 20, "字幕")],
        str(tmp_path),
        live_config(),
    )

    assert result is None
    assert not list((tmp_path / "clips").glob("*.mp4"))
    assert not list((tmp_path / "subtitles").glob("*.srt"))
    assert not (tmp_path / "metadata.json").exists()


def test_export_live_clips_cleans_outputs_on_srt_failure(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    def fail_export_srt(chunks, output_path):
        Path(output_path).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(export, "clip_segment", fake_clip)
    monkeypatch.setattr(export, "export_srt", fail_export_srt)

    result = export.export_live_clips("live.mp4", [make_candidate()], [], str(tmp_path), live_config())

    assert result is None
    assert not list((tmp_path / "clips").glob("*.mp4"))
    assert not list((tmp_path / "subtitles").glob("*.srt"))
