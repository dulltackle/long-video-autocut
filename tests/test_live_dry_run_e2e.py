import json
from pathlib import Path

from video_auto_editor import cli, transcript
from video_auto_editor.models import TranscriptChunk
from video_auto_editor.transcript import VideoTranscriptionResult


def test_live_dry_run_generates_plan_report_and_transcript_without_exports(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("fake video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    chunks = [
        TranscriptChunk(0, 40, "第一段内容，介绍核心概念。"),
        TranscriptChunk(40, 90, "第二段内容，展开案例并自然结束。"),
        TranscriptChunk(90, 118, "第三段内容，补充注意事项。"),
    ]

    class FakeASRProvider:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir_arg):
            assert video_path_arg == str(video_path)
            assert work_dir_arg == str(work_dir / "live")
            return VideoTranscriptionResult(success=True, chunks=chunks, transcript_path=str(work_dir / "live" / "fake.json"))

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(transcript, "create_transcriber", lambda config=None: FakeASRProvider())

    cli.main(
        [
            "live",
            str(video_path),
            "--output-dir",
            str(output_dir),
            "--work-dir",
            str(work_dir),
            "--dry-run",
        ]
    )

    srt_path = output_dir / "transcript.srt"
    plan_path = output_dir / "plan.json"
    report_path = output_dir / "拆条报告.md"

    assert srt_path.exists()
    assert "第一段内容，介绍核心概念。" in srt_path.read_text(encoding="utf-8")
    assert "00:00:40,000 --> 00:01:30,000" in srt_path.read_text(encoding="utf-8")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["status"] == "unreviewed"
    assert plan["candidates"]
    assert plan["selected"]

    report = report_path.read_text(encoding="utf-8")
    assert "Dry-run：本报告是未评审拆条方案，不代表发布就绪短视频。" in report
    assert "- Exported clips: 0 (dry-run)" in report

    assert not (output_dir / "metadata.json").exists()
    assert not list((output_dir / "clips").glob("*.mp4")) if (output_dir / "clips").exists() else True
    assert not list((output_dir / "subtitles").glob("*.srt")) if (output_dir / "subtitles").exists() else True
