import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_auto_editor import cli, media
from video_auto_editor.models import (
    ClipCandidate,
    ClipInfo,
    LiveClipInfo,
    LiveExportDecision,
    Segment,
    TopicReviewResult,
    TranscriptChunk,
)
from video_auto_editor.report import generate_batch_report, generate_live_report, generate_single_report
from video_auto_editor.review import TopicReviewProviderResult
from video_auto_editor.transcript import VideoTranscriptionResult


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_get_video_duration_parses_ffprobe_json(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(stdout='{"format": {"duration": "12.5"}}')

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    assert media.get_video_duration("input.mov") == 12.5
    assert calls[0] == ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "input.mov"]


def test_get_video_duration_returns_none_on_invalid_json(monkeypatch):
    monkeypatch.setattr(media.subprocess, "run", lambda *args, **kwargs: completed(stdout="bad"))

    assert media.get_video_duration("input.mov") is None


def test_clip_segment_builds_existing_ffmpeg_command(monkeypatch):
    calls = []
    segment = Segment(index=1, start_time=2.0, end_time=8.0, duration=6.0)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(0)

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    assert media.clip_segment("input.mov", segment, "out.mp4") is True
    assert calls[0] == [
        "ffmpeg", "-y", "-i", "input.mov",
        "-ss", "1.0", "-to", "11.0",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        "out.mp4",
    ]


def test_clip_segment_burns_subtitles_with_input_side_seek(monkeypatch):
    calls = []
    segment = Segment(index=1, start_time=2.0, end_time=8.0, duration=6.0)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(0)

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    assert media.clip_segment("input.mov", segment, "out.mp4", subtitle_path="subs/001.srt") is True
    cmd = calls[0]
    # 输入侧 seek：-ss 在 -i 之前，并以 -t 限定时长。
    assert cmd[:7] == ["ffmpeg", "-y", "-ss", "1.0", "-i", "input.mov", "-t"]
    assert cmd[7] == "10.0"  # duration = (8+3) - (2-1) = 11 - 1 = 10
    # 烧录滤镜含 subtitles= 与 force_style。
    vf_index = cmd.index("-vf")
    vf_value = cmd[vf_index + 1]
    assert vf_value.startswith("subtitles=subs/001.srt")
    assert "force_style=" in vf_value
    assert "Alignment=2" in vf_value
    assert cmd[-1] == "out.mp4"


def test_clip_segment_escapes_subtitle_path_special_chars():
    escaped = media._escape_subtitles_path("a:b'c\\d")
    assert escaped == "a\\:b\\'c\\\\d"


def test_clip_segment_rejects_invalid_time_range(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(media.subprocess, "run", fail_if_called)

    invalid = Segment(index=1, start_time=8.0, end_time=2.0, duration=-6.0)
    assert media.clip_segment("input.mov", invalid, "out.mp4") is False

    negative = Segment(index=2, start_time=-1.0, end_time=2.0, duration=3.0)
    assert media.clip_segment("input.mov", negative, "out.mp4") is False


def test_concat_videos_writes_absolute_paths_and_cleans_list(monkeypatch, tmp_path):
    calls = []
    captured_list = {}
    output_path = tmp_path / "final.mp4"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        list_file = Path(cmd[cmd.index("-i") + 1])
        captured_list["content"] = list_file.read_text(encoding="utf-8")
        return completed(0)

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    assert media.concat_videos(["a.mp4", "b.mp4"], str(output_path)) is True
    assert calls[0][:6] == ["ffmpeg", "-y", "-f", "concat", "-safe", "0"]
    assert f"file '{os.path.abspath('a.mp4')}'" in captured_list["content"]
    assert f"file '{os.path.abspath('b.mp4')}'" in captured_list["content"]
    assert not list(tmp_path.glob("concat_*.list.txt"))


def test_generate_single_report_contains_candidates_and_transcript(tmp_path):
    best = Segment(index=1, start_time=10, end_time=40, duration=30, total_score=95)
    best.adjusted_score = 98
    best.is_natural_end = True
    best.transcript = "转写|内容\n第二行"

    report_path = generate_single_report(
        "video",
        str(tmp_path),
        total_duration=120,
        silences=[(1, 2)],
        segments=[best],
        candidates=[best],
        best=best,
    )

    content = Path(report_path).read_text(encoding="utf-8")
    assert "# video Clip Report" in content
    assert "| seg_1 | 10.0-40.0s | 30.0s | 95 | 98.0 | yes |  | ✅ |" in content
    assert "- **Transcript**: 转写\\|内容 第二行" in content


def test_generate_batch_report_contains_dedup_and_total_duration(tmp_path):
    kept = ClipInfo("b|name", "b.mp4", "保留|文本\n第二行", 91, True, 30)
    removed = ClipInfo("a", "a.mp4", "保留文本", 80, False, 20, True, "b|name")

    report_path = generate_batch_report(str(tmp_path), [removed, kept], [kept], [removed], "final.mp4")

    content = Path(report_path).read_text(encoding="utf-8")
    assert "## Cross-Video Dedup" in content
    assert "| a | 80.0 | no | ❌ Remove | duplicate of b\\|name |" in content
    assert "| 1 | b\\|name | 30.0s | 91.0 | yes | 保留\\|文本 第二行 |" in content
    assert "**Total duration**: 30.0s (0.5min)" in content


def test_generate_live_report_rejects_selected_export_mismatch(tmp_path):
    candidate = ClipCandidate(0, 0, 10, 10, "文本", title="标题", base_score=80)

    with pytest.raises(ValueError, match="selected \\(1\\) and exports \\(0\\)"):
        generate_live_report(
            "live",
            str(tmp_path),
            total_duration=10,
            silences=[],
            candidates=[candidate],
            selected=[candidate],
            exports=[],
        )


def test_generate_live_report_does_not_render_none_review_reason(tmp_path):
    candidate = ClipCandidate(0, 0, 10, 10, "文本", title="标题", base_score=80)
    candidate.review = TopicReviewResult(
        topic_name="主题",
        topic_complete=True,
        learning_value=8,
        share_value=7,
        publish_ready_score=90,
        export_decision="publish_ready",
        title="标题",
        summary="摘要",
        keywords=["主题"],
        needs_human_review=False,
        reject_reason=None,
        boundary_fix_suggestion=None,
    )

    report_path = generate_live_report(
        "live",
        str(tmp_path),
        total_duration=10,
        silences=[],
        candidates=[candidate],
        selected=[candidate],
        exports=[],
        dry_run=True,
    )

    content = Path(report_path).read_text(encoding="utf-8")
    assert "None" not in content
    assert "| candidate_0 | 主题 | yes | 8 | 7 | 90 | publish_ready | no |  |" in content


def test_generate_live_report_lists_exports_rejections_human_review_series_and_deliverables(tmp_path):
    (tmp_path / "plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / "transcript.srt").write_text("", encoding="utf-8")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "clips").mkdir()
    (tmp_path / "subtitles").mkdir()
    selected = ClipCandidate(0, 12, 68, 56, "入选文本", title="入选标题", base_score=90, adjusted_score=95)
    selected.export_selection = LiveExportDecision(
        candidate_index=0,
        selected_for_export=True,
        decision="export",
        reason="publish_ready",
        review_status="reviewed",
        publish_ready_score=92,
        export_rank=1,
        original_start=10,
        original_end=70,
        final_start=12,
        final_end=68,
        topic_name="主题 A",
        series_key="topic-a",
    )
    rejected = ClipCandidate(1, 80, 140, 60, "未导出文本", title="未导出标题", base_score=88, adjusted_score=90)
    rejected.export_selection = LiveExportDecision(
        candidate_index=1,
        selected_for_export=False,
        decision="skip",
        reason="boundary_fix_needs_human_review",
        review_status="reviewed",
        publish_ready_score=89,
        topic_name="主题 A",
        needs_human_review=True,
        boundary_fix_suggestion="建议补足收尾。",
        series_key="topic-a",
    )
    export_info = LiveClipInfo(
        1,
        "入选标题",
        12,
        68,
        56,
        95,
        "入选文本",
        str(tmp_path / "clips" / "001_入选标题.mp4"),
        str(tmp_path / "subtitles" / "001_入选标题.srt"),
    )

    report_path = generate_live_report(
        "live",
        str(tmp_path),
        total_duration=200,
        silences=[],
        candidates=[selected, rejected],
        selected=[selected],
        exports=[export_info],
        config={"export_subtitles": True},
    )

    content = Path(report_path).read_text(encoding="utf-8")
    assert "Reviewed 非 dry-run 交付包" in content
    assert "- 字幕烧录: 开（白字黑描边·底部居中）" in content
    assert "## 导出清单" in content
    assert "| 0 | 入选标题 | 主题 A | 92 | 12.0-68.0s | `clips/001_入选标题.mp4` | `subtitles/001_入选标题.srt` |" in content
    assert "## 未导出候选" in content
    assert "boundary_fix_needs_human_review" in content
    assert "建议补足收尾。" in content
    assert "## 人工复核" in content
    assert "| candidate_1 | 主题 A | boundary_fix_needs_human_review | 建议补足收尾。 |" in content
    assert "## 同主题系列" in content
    assert "| topic-a | 主题 A | candidate_0 |" in content
    assert "## 标准交付物" in content
    assert "| `metadata.json` | yes |" in content


def test_generate_live_report_marks_burn_disabled(tmp_path):
    candidate = ClipCandidate(0, 12, 68, 56, "入选文本", title="入选标题", base_score=90, adjusted_score=95)
    export_info = LiveClipInfo(
        1, "入选标题", 12, 68, 56, 95, "入选文本",
        str(tmp_path / "clips" / "001_入选标题.mp4"),
        str(tmp_path / "subtitles" / "001_入选标题.srt"),
    )

    report_path = generate_live_report(
        "live",
        str(tmp_path),
        total_duration=200,
        silences=[],
        candidates=[candidate],
        selected=[candidate],
        exports=[export_info],
        config={"export_subtitles": True, "burn_subtitles": False},
    )

    content = Path(report_path).read_text(encoding="utf-8")
    assert "- 字幕烧录: 关（仅旁挂 SRT）" in content


def test_main_dispatches_batch_subcommand(monkeypatch, tmp_path):
    calls = []

    def fake_process_batch(input_dir, output_dir, work_dir, config=None):
        calls.append((input_dir, output_dir, work_dir, config["asr_provider"]))

    monkeypatch.setattr(cli, "process_batch", fake_process_batch)

    cli.main(["batch", str(tmp_path), "--output-dir", "out", "--work-dir", "work"])

    assert calls == [(str(tmp_path), "out", "work", "stepaudio")]


def test_main_dispatches_single_subcommand(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "input.mp4"
    video_path.write_text("not real video", encoding="utf-8")

    def fake_process_single(video_path_arg, output_dir, work_dir, config=None):
        calls.append((video_path_arg, output_dir, work_dir, config["asr_provider"]))
        return ClipInfo("input", "out/input_clip.mp4", "", 0, False, 0)

    monkeypatch.setattr(cli, "process_single_video", fake_process_single)

    cli.main(["single", str(video_path), "--output-dir", "out", "--work-dir", "work"])

    assert calls == [(str(video_path), "out", "work", "stepaudio")]


def test_main_passes_config_file_to_single_subcommand(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "input.mp4"
    video_path.write_text("not real video", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"asr_provider": "whisper"}', encoding="utf-8")

    def fake_process_single(video_path_arg, output_dir, work_dir, config=None):
        calls.append(config["asr_provider"])
        return ClipInfo("input", "out/input_clip.mp4", "", 0, False, 0)

    monkeypatch.setattr(cli, "process_single_video", fake_process_single)

    cli.main(["single", str(video_path), "--config-file", str(config_path)])

    assert calls == ["whisper"]


def test_main_passes_config_file_to_batch_subcommand(monkeypatch, tmp_path):
    calls = []
    config_path = tmp_path / "config.json"
    config_path.write_text('{"min_score": 75}', encoding="utf-8")

    def fake_process_batch(input_dir, output_dir, work_dir, config=None):
        calls.append(config["min_score"])

    monkeypatch.setattr(cli, "process_batch", fake_process_batch)

    cli.main(["batch", str(tmp_path), "--config-file", str(config_path)])

    assert calls == [75]


def test_main_dispatches_live_subcommand(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")

    def fake_process_live(video_path_arg, output_dir, work_dir, config=None, course_context=None, dry_run=False):
        calls.append(
            (
                video_path_arg,
                output_dir,
                work_dir,
                config["max_clips"],
                config.get("max_clips_user_provided", False),
                config["allow_unreviewed_export"],
                course_context.data if course_context else None,
                dry_run,
            )
        )
        return []

    monkeypatch.setattr(cli, "process_live_video", fake_process_live)

    context_path = tmp_path / "context.json"
    context_path.write_text('{"course_title": "直播课"}', encoding="utf-8")
    cli.main(
        [
            "live",
            str(video_path),
            "--output-dir",
            "out",
            "--work-dir",
            "work",
            "--max-clips",
            "2",
            "--context-file",
            str(context_path),
            "--dry-run",
            "--allow-unreviewed-export",
        ]
    )

    assert calls == [(str(video_path), "out", "work", 2, True, True, {"course_title": "直播课"}, True)]


def test_main_passes_config_file_to_live_subcommand(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"asr_provider": "whisper", "allow_unreviewed_export": true, "max_clips": 4}',
        encoding="utf-8",
    )

    def fake_process_live(video_path_arg, output_dir, work_dir, config=None, course_context=None, dry_run=False):
        calls.append(
            (
                config["asr_provider"],
                config["allow_unreviewed_export"],
                config["max_clips"],
                config.get("max_clips_user_provided", False),
            )
        )
        return []

    monkeypatch.setattr(cli, "process_live_video", fake_process_live)

    cli.main(["live", str(video_path), "--config-file", str(config_path)])

    assert calls == [("whisper", True, 4, False)]


def test_main_live_cli_max_clips_overrides_config_file(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_clips": 4}', encoding="utf-8")

    def fake_process_live(video_path_arg, output_dir, work_dir, config=None, course_context=None, dry_run=False):
        calls.append((config["max_clips"], config.get("max_clips_user_provided", False)))
        return []

    monkeypatch.setattr(cli, "process_live_video", fake_process_live)

    cli.main(["live", str(video_path), "--config-file", str(config_path), "--max-clips", "2"])

    assert calls == [(2, True)]


def test_main_rejects_invalid_config_file(tmp_path, capsys):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"unknown": true}', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["live", str(video_path), "--config-file", str(config_path)])

    assert exc_info.value.code == 2
    assert "未知配置项：unknown" in capsys.readouterr().err


def test_main_uses_protective_live_max_clips_when_unspecified(monkeypatch, tmp_path):
    calls = []
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")

    def fake_process_live(video_path_arg, output_dir, work_dir, config=None, course_context=None, dry_run=False):
        calls.append((config["max_clips"], "max_clips_user_provided" in config))
        return []

    monkeypatch.setattr(cli, "process_live_video", fake_process_live)

    cli.main(["live", str(video_path)])

    assert calls == [(5, False)]


def test_main_rejects_invalid_context_file(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["live", str(video_path), "--context-file", str(context_path)])

    assert exc_info.value.code == 2


def test_main_rejects_non_positive_live_max_clips(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("not real video", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["live", str(video_path), "--max-clips", "0"])

    assert exc_info.value.code == 2


def test_main_requires_explicit_subcommand():
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    assert exc_info.value.code == 2


def test_process_single_video_success_and_batch_mode(monkeypatch, tmp_path):
    reports = []
    clip_calls = []
    segments = [
        Segment(index=0, start_time=0, end_time=10, duration=10),
        Segment(index=1, start_time=10, end_time=30, duration=20),
    ]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path: 30.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path, config=None: [(10, 10.5)])
    monkeypatch.setattr(cli, "identify_segments", lambda silences, total_duration: segments)

    def fake_score(segment, silences, total_duration):
        segment.total_score = 80 if segment.index == 0 else 95
        return segment

    def fake_transcribe(video_path, candidates, work_dir, transcriber=None, config=None):
        assert [candidate.index for candidate in candidates] == [1]
        candidates[0].transcript = "完整结束。"
        return candidates

    def fake_analyze(transcript):
        return 0, 0, True, False

    def fake_clip(video_path, segment, output_path, config=None):
        clip_calls.append((segment.index, output_path))
        Path(output_path).write_text("clip", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "score_segment", fake_score)
    monkeypatch.setattr(cli, "transcribe_candidates", fake_transcribe)
    monkeypatch.setattr(cli, "analyze_fluency", fake_analyze)
    monkeypatch.setattr(cli, "clip_segment", fake_clip)
    monkeypatch.setattr(cli, "generate_single_report", lambda *args, **kwargs: reports.append(args) or "report.md")

    result = cli.process_single_video("sample.mp4", str(tmp_path), str(tmp_path / "work"), batch_mode=True)

    assert result == ClipInfo("sample", str(tmp_path / "sample_clip.mp4"), "完整结束。", 100, True, 20)
    assert clip_calls == [(1, str(tmp_path / "sample_clip.mp4"))]
    assert reports == []


def test_process_single_video_falls_back_to_top_five_and_returns_none_on_clip_failure(monkeypatch, tmp_path):
    segments = [Segment(index=i, start_time=i, end_time=i + 1, duration=1) for i in range(6)]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path: 10.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path, config=None: [])
    monkeypatch.setattr(cli, "identify_segments", lambda silences, total_duration: segments)

    def fake_score(segment, silences, total_duration):
        segment.total_score = 100 - segment.index
        return segment

    def fake_transcribe(video_path, candidates, work_dir, transcriber=None, config=None):
        assert [candidate.index for candidate in candidates] == [0, 1, 2, 3, 4]
        return candidates

    monkeypatch.setattr(cli, "score_segment", fake_score)
    monkeypatch.setattr(cli, "transcribe_candidates", fake_transcribe)
    monkeypatch.setattr(cli, "clip_segment", lambda *args, **kwargs: False)

    assert cli.process_single_video("sample.mp4", str(tmp_path), str(tmp_path / "work")) is None


def test_process_live_video_exports_transcript_srt(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(1, 2.5, "直播文本")]
    candidates = [ClipCandidate(0, 1, 2.5, 1.5, "直播文本", base_score=80)]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [(0.5, 1.0)])

    def fake_transcribe(video_path_arg, video_work, config=None):
        assert video_path_arg == str(video_path)
        assert video_work == str(work_dir / "live")
        return VideoTranscriptionResult(
            success=True,
            chunks=chunks,
            cache_path=str(work_dir / "live" / "transcript.json"),
        )

    monkeypatch.setattr(cli, "transcribe_video", fake_transcribe)
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )
    monkeypatch.setattr(
        cli,
        "export_live_clips",
        lambda *args, **kwargs: [
            LiveClipInfo(1, "直播文本", 1, 2.5, 1.5, 80, "直播文本", str(output_dir / "clips" / "001_直播文本.mp4"))
        ],
    )

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config={**cli.CONFIG, "allow_unreviewed_export": True},
    )

    assert len(result) == 1
    assert (output_dir / "transcript.srt").read_text(encoding="utf-8") == (
        "1\n"
        "00:00:01,000 --> 00:00:02,500\n"
        "直播文本\n\n"
    )
    assert (output_dir / "拆条报告.md").exists()
    assert (output_dir / "plan.json").exists()


def test_process_live_video_dry_run_writes_plan_and_skips_exports(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(10, 70, "直播文本。")]
    candidates = [ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90)]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )

    def fail_export(*args, **kwargs):
        raise AssertionError("dry-run should not export clips")

    monkeypatch.setattr(cli, "export_live_clips", fail_export)

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config=cli.CONFIG.copy(),
        dry_run=True,
    )

    assert result == []
    assert (output_dir / "transcript.srt").exists()
    assert (output_dir / "plan.json").exists()
    assert (output_dir / "拆条报告.md").exists()
    assert not (output_dir / "metadata.json").exists()
    assert not list((output_dir / "clips").glob("*.mp4")) if (output_dir / "clips").exists() else True
    assert not list((output_dir / "subtitles").glob("*.srt")) if (output_dir / "subtitles").exists() else True
    report = (output_dir / "拆条报告.md").read_text(encoding="utf-8")
    assert "Dry-run：本报告是未评审拆条方案，不代表发布就绪短视频。" in report
    assert "- Selected clips: 0" in report
    assert "- Exported clips: 0 (dry-run)" in report
    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "unreviewed"
    assert plan["selected"] == []
    assert any("主题评审不可用" in warning for warning in plan["warnings"])


def test_process_live_video_skips_export_when_review_unavailable_by_default(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(10, 70, "直播文本。")]
    candidates = [ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90)]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )

    def fail_export(*args, **kwargs):
        raise AssertionError("unreviewed export must require explicit allow_unreviewed_export")

    monkeypatch.setattr(cli, "export_live_clips", fail_export)

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config=cli.CONFIG.copy(),
        dry_run=False,
    )

    assert result == []
    assert not (output_dir / "metadata.json").exists()
    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["selected"] == []


def test_process_live_video_writes_reviewed_plan_and_report(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(10, 70, "直播文本。")]
    candidates = [ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90)]
    seen_batches = []

    class FakeReviewer:
        provider_name = "stepfun_chat"
        model = "fake-review"
        base_url = "https://api.example/v1"

        def is_available(self):
            return True

        def review_batches(self, batches):
            seen_batches.extend(batches)
            return TopicReviewProviderResult(
                success=True,
                reviews={
                    0: TopicReviewResult(
                        topic_name="直播主题",
                        topic_complete=True,
                        learning_value=9,
                        share_value=8,
                        publish_ready_score=92,
                        export_decision="publish_ready",
                        title="评审标题",
                        summary="评审摘要",
                        keywords=["评审", "直播"],
                        needs_human_review=False,
                        reject_reason="",
                        boundary_fix_suggestion="",
                    )
                },
                provider_info={"provider": "stepfun_chat", "model": "fake-review"},
            )

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )
    monkeypatch.setattr(cli, "create_topic_reviewer", lambda config: FakeReviewer())

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config=cli.CONFIG.copy(),
        dry_run=True,
    )

    assert result == []
    assert seen_batches
    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "reviewed"
    assert plan["review_provider"] == {"provider": "stepfun_chat", "model": "fake-review"}
    assert plan["selected"][0]["title"] == "评审标题"
    assert plan["selected"][0]["review"]["topic_name"] == "直播主题"
    report = (output_dir / "拆条报告.md").read_text(encoding="utf-8")
    assert "Dry-run：本报告包含主题评审结果，但未导出短视频。" in report
    assert "## 主题评审" in report
    assert "| candidate_0 | 直播主题 | yes | 9 | 8 | 92 | publish_ready | no |  |" in report


def test_process_live_video_keeps_unreviewed_plan_on_review_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(10, 70, "直播文本。")]
    candidates = [ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90)]

    class FakeReviewer:
        provider_name = "stepfun_chat"
        model = "fake-review"
        base_url = "https://api.example/v1"

        def is_available(self):
            return True

        def review_batches(self, batches):
            return TopicReviewProviderResult(success=False, error="模型响应缺少字段")

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )
    monkeypatch.setattr(cli, "create_topic_reviewer", lambda config: FakeReviewer())

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config=cli.CONFIG.copy(),
        dry_run=True,
    )

    assert result == []
    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "unreviewed"
    assert plan["selected"] == []
    assert any("主题评审失败：模型响应缺少字段" in warning for warning in plan["warnings"])
    report = (output_dir / "拆条报告.md").read_text(encoding="utf-8")
    assert "主题评审失败：模型响应缺少字段" in report


def test_process_live_video_passes_clean_review_provider_to_export_after_review_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [TranscriptChunk(10, 70, "直播文本。")]
    candidates = [ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90, title="直播候选")]
    captured = {}

    class FakeReviewer:
        provider_name = "stepfun_chat"
        model = "fake-review"
        base_url = "https://api.example/v1"

        def is_available(self):
            return True

        def review_batches(self, batches):
            return TopicReviewProviderResult(
                success=False,
                reviews={
                    0: TopicReviewResult(
                        topic_name="直播主题",
                        topic_complete=True,
                        learning_value=9,
                        share_value=8,
                        publish_ready_score=92,
                        export_decision="publish_ready",
                        title="评审标题",
                        summary="评审摘要",
                        keywords=["评审", "直播"],
                        needs_human_review=False,
                        reject_reason="",
                        boundary_fix_suggestion="",
                    )
                },
                error="第二批失败",
                failed_batches=[
                    {
                        "batch_index": 1,
                        "candidate_range": "candidate_1",
                        "attempt": 1,
                        "max_attempts": 1,
                        "failure_type": "timeout",
                        "error": "timeout",
                    }
                ],
            )

    def fake_export(*args, **kwargs):
        captured["review_provider"] = kwargs["review_provider"]
        captured["review_status"] = kwargs["review_status"]
        return [
            LiveClipInfo(
                1,
                "直播候选",
                10,
                70,
                60,
                90,
                "直播文本。",
                str(output_dir / "clips" / "001.mp4"),
            )
        ]

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )
    monkeypatch.setattr(
        cli,
        "generate_clip_candidates",
        lambda chunks_arg, silences, total_duration, config=None: candidates,
    )
    monkeypatch.setattr(cli, "create_topic_reviewer", lambda config: FakeReviewer())
    monkeypatch.setattr(cli, "export_live_clips", fake_export)

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config={**cli.CONFIG, "allow_unreviewed_export": True},
        dry_run=False,
    )

    assert result
    expected_provider = {
        "provider": "stepfun_chat",
        "model": "fake-review",
        "base_url": "https://api.example/v1",
    }
    # 新契约：只要有任意候选评审成功即进入 reviewed 选择路径，失败批次的候选自然跳过，
    # 但传给导出的 review_provider 仍保持干净（不泄漏 review_diagnostics）。
    assert captured["review_status"] == "reviewed"
    assert captured["review_provider"] == expected_provider
    assert "review_diagnostics" not in captured["review_provider"]
    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["review_provider"] == expected_provider
    assert plan["reviewed_candidate_count"] == 1
    assert plan["failed_review_batch_count"] == 1


def test_review_live_candidates_degrades_on_invalid_batch_size(monkeypatch):
    candidate = ClipCandidate(0, 10, 70, 60, "直播文本。", base_score=90)

    class FakeReviewer:
        provider_name = "stepfun_chat"
        model = "fake-review"
        base_url = "https://api.example/v1"

        def is_available(self):
            return True

        def review_batches(self, batches):
            raise AssertionError("invalid batch size should stop before provider request")

    monkeypatch.setattr(cli, "create_topic_reviewer", lambda config: FakeReviewer())

    status, provider_info, warnings = cli._review_live_candidates(
        [candidate],
        None,
        {**cli.CONFIG, "topic_review_batch_size": 0},
    )

    assert status == "unreviewed"
    assert provider_info == {
        "provider": "stepfun_chat",
        "model": "fake-review",
        "base_url": "https://api.example/v1",
    }
    assert warnings == ["主题评审配置错误：Invalid topic_review_batch_size: 0, must be >= 1"]


def test_process_live_video_logs_generated_candidates(monkeypatch, tmp_path, capsys):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    chunks = [TranscriptChunk(10, 50, "直播候选文本")]
    silences = [(8, 10), (50, 55)]
    candidates = [ClipCandidate(0, 10, 50, 40, "直播候选文本", base_score=95)]
    calls = {}

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: silences)
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=True, chunks=chunks, cache_path="cache.json"),
    )

    def fake_generate(chunks_arg, silences_arg, total_duration, config=None):
        calls["args"] = (chunks_arg, silences_arg, total_duration)
        return candidates

    monkeypatch.setattr(cli, "generate_clip_candidates", fake_generate)
    monkeypatch.setattr(
        cli,
        "export_live_clips",
        lambda *args, **kwargs: [
            LiveClipInfo(1, "直播候选文本", 10, 50, 40, 95, "直播候选文本", str(tmp_path / "out" / "clips" / "001.mp4"))
        ],
    )

    cli.process_live_video(
        str(video_path),
        str(tmp_path / "out"),
        str(tmp_path / "work"),
        config={**cli.CONFIG, "allow_unreviewed_export": True},
    )

    assert calls["args"] == (chunks, silences, 120.0)
    output = capsys.readouterr().out
    assert "Generated 1 clip candidates" in output
    assert "candidate_0: 10.0-50.0s (40.0s) score=95.0 | 直播候选文本" in output


def test_process_live_video_returns_none_on_transcription_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(
        cli,
        "transcribe_video",
        lambda *args, **kwargs: VideoTranscriptionResult(success=False, chunks=[], error="failed"),
    )

    assert cli.process_live_video(str(video_path), str(tmp_path / "out"), str(tmp_path / "work")) is None


def test_process_live_video_returns_none_when_default_asr_unavailable(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])

    result = cli.process_live_video(
        str(video_path),
        str(output_dir),
        str(work_dir),
        config=cli.CONFIG.copy(),
        dry_run=True,
    )

    assert result is None
    assert not (output_dir / "plan.json").exists()
    assert not (output_dir / "transcript.srt").exists()
    assert "ASR provider stepaudio unavailable" in capsys.readouterr().out


def test_process_live_video_returns_none_on_silence_detection_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(
        cli,
        "detect_silence",
        lambda video_path_arg, config=None: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")),
    )

    assert cli.process_live_video(str(video_path), str(tmp_path / "out"), str(tmp_path / "work")) is None


def test_process_single_video_returns_none_on_silence_detection_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "get_video_duration", lambda video_path: 10.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path, config=None: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))

    assert cli.process_single_video("sample.mp4", str(tmp_path), str(tmp_path / "work")) is None


def test_process_batch_empty_directory_returns_without_report(tmp_path, capsys):
    cli.process_batch(str(tmp_path), str(tmp_path / "out"), str(tmp_path / "work"))

    assert "No video files found" in capsys.readouterr().out
    assert not (tmp_path / "out" / "batch_report.md").exists()


def test_process_batch_sorts_supported_files_cleans_and_reports(monkeypatch, tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    input_dir.mkdir()
    output_dir.mkdir()
    work_dir.mkdir()
    for name in ["b.mp4", "a.MTS", "ignored.avi", "c.mov"]:
        (input_dir / name).write_text("video", encoding="utf-8")

    seen = []

    def fake_process_single(video_path, output_dir_arg, work_dir_arg, batch_mode=False, config=None):
        seen.append(Path(video_path).name)
        clip_path = output_dir / f"{Path(video_path).stem}_clip.mp4"
        clip_path.write_text("clip", encoding="utf-8")
        return ClipInfo(Path(video_path).stem, str(clip_path), Path(video_path).stem, 90, True, 10)

    def fake_concat(paths, output_path, config=None):
        Path(output_path).write_text("final", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "process_single_video", fake_process_single)
    monkeypatch.setattr(cli, "cross_video_dedup", lambda clips, config=None: clips)
    monkeypatch.setattr(cli, "concat_videos", fake_concat)

    cli.process_batch(str(input_dir), str(output_dir), str(work_dir))

    assert seen == ["a.MTS", "b.mp4", "c.mov"]
    assert not work_dir.exists()
    assert not list(output_dir.glob("*_clip.mp4"))
    assert (output_dir / "batch_report.md").exists()


def test_process_batch_logs_failed_videos(monkeypatch, tmp_path, capsys):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    input_dir.mkdir()
    output_dir.mkdir()
    work_dir.mkdir()
    (input_dir / "a.mp4").write_text("video", encoding="utf-8")
    (input_dir / "b.mp4").write_text("video", encoding="utf-8")

    def fake_process_single(video_path, output_dir_arg, work_dir_arg, batch_mode=False, config=None):
        if Path(video_path).name == "a.mp4":
            return None
        clip_path = output_dir / "b_clip.mp4"
        clip_path.write_text("clip", encoding="utf-8")
        return ClipInfo("b", str(clip_path), "文本", 90, True, 10)

    monkeypatch.setattr(cli, "process_single_video", fake_process_single)
    monkeypatch.setattr(cli, "cross_video_dedup", lambda clips, config=None: clips)
    def fake_concat(paths, output_path, config=None):
        Path(output_path).write_text("final", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "concat_videos", fake_concat)

    cli.process_batch(str(input_dir), str(output_dir), str(work_dir))

    assert "Failed to process a.mp4" in capsys.readouterr().out


def test_process_batch_skips_work_dir_cleanup_when_paths_overlap(monkeypatch, tmp_path, capsys):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    work_dir = output_dir
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "a.mp4").write_text("video", encoding="utf-8")

    def fake_process_single(video_path, output_dir_arg, work_dir_arg, batch_mode=False, config=None):
        clip_path = output_dir / "a_clip.mp4"
        clip_path.write_text("clip", encoding="utf-8")
        return ClipInfo("a", str(clip_path), "文本", 90, True, 10)

    def fake_concat(paths, output_path, config=None):
        Path(output_path).write_text("final", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "process_single_video", fake_process_single)
    monkeypatch.setattr(cli, "cross_video_dedup", lambda clips, config=None: clips)
    monkeypatch.setattr(cli, "concat_videos", fake_concat)

    cli.process_batch(str(input_dir), str(output_dir), str(work_dir))

    assert output_dir.exists()
    assert list(output_dir.glob("final_concat_*.mp4"))
    assert "Skipping work_dir cleanup" in capsys.readouterr().out


def test_process_batch_concat_failure_does_not_generate_report(monkeypatch, tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    input_dir.mkdir()
    output_dir.mkdir()
    work_dir.mkdir()
    (input_dir / "a.mp4").write_text("video", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "process_single_video",
        lambda *args, **kwargs: ClipInfo("a", str(output_dir / "a_clip.mp4"), "", 90, True, 10),
    )
    monkeypatch.setattr(cli, "cross_video_dedup", lambda clips, config=None: clips)
    monkeypatch.setattr(cli, "concat_videos", lambda *args, **kwargs: False)

    cli.process_batch(str(input_dir), str(output_dir), str(work_dir))

    assert not (output_dir / "batch_report.md").exists()
