import json
import threading
from pathlib import Path

from video_auto_editor import export
from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate, LiveClipInfo, LiveExportDecision, TranscriptChunk


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

    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
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
    assert metadata["generated_at"]
    assert metadata["status"] == "reviewed"
    assert metadata["export_count"] == 1
    assert metadata["clips"][0]["output_path"] == "clips/001_坏_标题_A.mp4"
    assert metadata["clips"][0]["subtitle_path"] == "subtitles/001_坏_标题_A.srt"
    assert metadata["clips"][0]["score"] == 93
    assert metadata["clips"][0]["keywords"] == ["直播", "文本"]
    assert metadata["exports"] == metadata["clips"]

    assert (tmp_path / "subtitles" / "001_坏_标题_A.srt").read_text(encoding="utf-8") == (
        "1\n"
        "00:00:00,000 --> 00:00:03,000\n"
        "片头\n\n"
        "2\n"
        "00:00:10,000 --> 00:00:14,000\n"
        "片尾\n\n"
    )


def test_export_live_clips_filters_fillers_and_caps_line_length(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips(
        "live.mp4",
        [make_candidate()],  # start 10 end 20 -> clip [9, 23]
        [
            TranscriptChunk(10, 13, "嗯，今天我们讲愉悦技术"),
            TranscriptChunk(14, 19, "甲" * 20),
        ],
        str(tmp_path),
        live_config(),
    )

    srt = Path(result[0].subtitle_path).read_text(encoding="utf-8")
    assert "嗯" not in srt
    assert "今天我们讲愉悦技术" in srt
    for line in srt.splitlines():
        if "-->" in line or line.strip().isdigit() or not line.strip():
            continue
        assert len(line) <= 15


def test_export_live_clips_burns_subtitles_by_default(monkeypatch, tmp_path):
    calls = []

    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        calls.append(subtitle_path)
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips(
        "live.mp4",
        [make_candidate()],
        [TranscriptChunk(10, 13, "今天讲技术")],
        str(tmp_path),
        live_config(burn_subtitles=True),
    )

    assert len(calls) == 1
    assert calls[0] == result[0].subtitle_path
    assert Path(result[0].subtitle_path).exists()


def test_export_live_clips_skips_burn_when_disabled_but_keeps_sidecar_srt(monkeypatch, tmp_path):
    calls = []

    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        calls.append(subtitle_path)
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips(
        "live.mp4",
        [make_candidate()],
        [TranscriptChunk(10, 13, "今天讲技术")],
        str(tmp_path),
        live_config(burn_subtitles=False),
    )

    assert calls == [None]
    assert Path(result[0].subtitle_path).exists()


def test_export_live_clips_carries_selection_boundary_and_series(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    candidate = make_candidate()
    candidate.start_time = 12
    candidate.end_time = 22
    candidate.duration = 10
    candidate.export_selection = LiveExportDecision(
        candidate_index=0,
        selected_for_export=True,
        decision="export",
        reason="publish_ready",
        review_status="reviewed",
        publish_ready_score=91,
        export_rank=1,
        original_start=10,
        original_end=20,
        final_start=12,
        final_end=22,
        topic_name="主题 A",
        boundary_fix_applied=True,
        boundary_fix_suggestion="使用明确字段修正边界。",
        series_key="topic-abc",
    )

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    result = export.export_live_clips("live.mp4", [candidate], [], str(tmp_path), live_config())

    assert result[0].topic_name == "主题 A"
    assert result[0].publish_ready_score == 91
    assert result[0].export_decision == "export"
    assert result[0].decision_reason == "publish_ready"
    assert result[0].original_start == 10
    assert result[0].original_end == 20
    assert result[0].final_start == 12
    assert result[0].final_end == 22
    assert result[0].boundary_fix_applied is True
    assert result[0].series_key == "topic-abc"

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["clips"][0]["topic_name"] == "主题 A"
    assert metadata["clips"][0]["original_start"] == 10
    assert metadata["clips"][0]["final_start"] == 12
    assert metadata["clips"][0]["series_key"] == "topic-abc"


def test_export_live_clips_writes_not_exported_and_human_review_summary(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    selected = make_named_candidate(0, "第一条")
    selected.export_selection = LiveExportDecision(
        candidate_index=0,
        selected_for_export=True,
        decision="export",
        reason="publish_ready",
        review_status="reviewed",
        topic_name="主题 A",
        series_key="topic-a",
    )
    rejected = make_named_candidate(1, "第二条")
    rejected.export_selection = LiveExportDecision(
        candidate_index=1,
        selected_for_export=False,
        decision="skip",
        reason="boundary_fix_needs_human_review",
        review_status="reviewed",
        publish_ready_score=88,
        topic_name="主题 A",
        boundary_fix_suggestion="建议向后补足结束句。",
        series_key="topic-a",
    )

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    export.export_live_clips(
        "live.mp4",
        [selected],
        [],
        str(tmp_path),
        live_config(topic_review_publish_ready_threshold=85),
        candidates=[selected, rejected],
        review_status="reviewed",
        review_provider={"provider": "fake"},
    )

    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["review_provider"] == {"provider": "fake"}
    assert metadata["publish_ready_threshold"] == 85
    assert metadata["not_exported_count"] == 1
    assert metadata["not_exported"][0]["reason"] == "boundary_fix_needs_human_review"
    assert metadata["not_exported"][0]["boundary_fix_suggestion"] == "建议向后补足结束句。"
    assert metadata["human_review"] == metadata["not_exported"]


def test_export_live_clips_returns_none_and_skips_metadata_on_clip_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(export, "clip_segment", lambda *args, **kwargs: False)

    result = export.export_live_clips("live.mp4", [make_candidate()], [], str(tmp_path), live_config())

    assert result is None
    assert not (tmp_path / "metadata.json").exists()


def test_export_live_clips_cleans_previous_outputs_on_later_clip_failure(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
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


def test_export_live_clips_runs_clips_concurrently(monkeypatch, tmp_path):
    # 屏障要求全部 3 条 clip 同时进入裁剪才能放行；若实现仍串行，屏障会超时抛
    # BrokenBarrierError 使测试失败，精确证明并发确实发生。
    barrier = threading.Barrier(3, timeout=5)

    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        barrier.wait()
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    candidates = [make_named_candidate(index, f"第{index}条") for index in range(3)]
    result = export.export_live_clips(
        "live.mp4",
        candidates,
        [],
        str(tmp_path),
        live_config(export_concurrency=3, export_subtitles=False),
    )

    assert len(result) == 3
    assert [clip.index for clip in result] == [1, 2, 3]


def test_export_live_clips_concurrency_one_preserves_order_and_numbering(monkeypatch, tmp_path):
    calls = []

    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        calls.append(candidate.index)
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    candidates = [make_named_candidate(index, f"第{index}条") for index in range(3)]
    result = export.export_live_clips(
        "live.mp4",
        candidates,
        [],
        str(tmp_path),
        live_config(export_concurrency=1, export_subtitles=False),
    )

    assert calls == [0, 1, 2]
    assert [clip.index for clip in result] == [1, 2, 3]
    assert [clip.title for clip in result] == ["第0条", "第1条", "第2条"]
    assert Path(result[0].output_path).name == "001_第0条.mp4"
    assert Path(result[2].output_path).name == "003_第2条.mp4"


def test_export_live_clips_concurrent_failure_cleans_all_outputs(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
        Path(output_path).write_text("clip", encoding="utf-8")
        return candidate.index != 1

    monkeypatch.setattr(export, "clip_segment", fake_clip)

    candidates = [make_named_candidate(index, f"第{index}条") for index in range(3)]
    result = export.export_live_clips(
        "live.mp4",
        candidates,
        [TranscriptChunk(10, 20, "字幕")],
        str(tmp_path),
        live_config(export_concurrency=3),
    )

    assert result is None
    assert not list((tmp_path / "clips").glob("*.mp4"))
    assert not list((tmp_path / "subtitles").glob("*.srt"))
    assert not (tmp_path / "metadata.json").exists()


def test_export_live_clips_cleans_outputs_on_srt_failure(monkeypatch, tmp_path):
    def fake_clip(video_path, candidate, output_path, config=None, subtitle_path=None):
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
