import json

from video_auto_editor import cli, transcript


def completed(returncode=0, stderr="", stdout=""):
    return type("Completed", (), {"returncode": returncode, "stderr": stderr, "stdout": stdout})()


def test_live_dry_run_generates_plan_report_and_transcript_without_exports(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("fake video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    http_calls = []
    http_responses = [
        '{"segments": [{"start": 0, "end": 40, "text": "第一段内容，介绍核心概念。"}]}',
        '{"segments": [{"start": 0, "end": 50, "text": "第二段内容，展开案例并自然结束。"}]}',
    ]

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return completed(0, stdout='{"format": {"duration": "120.0"}}')
        if cmd[0] == "ffmpeg":
            (tmp_path / "commands.log").write_text("ffmpeg called", encoding="utf-8")
            output_path = cmd[-1]
            from pathlib import Path

            Path(output_path).write_bytes(b"audio")
            return completed(0)
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_request(request, timeout):
        http_calls.append(request)
        return FakeResponse(http_responses[len(http_calls) - 1])

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(transcript.subprocess, "run", fake_run)
    monkeypatch.setattr(transcript.urllib.request, "urlopen", fake_request)
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setitem(cli.CONFIG, "asr_shard_seconds", 60)
    monkeypatch.setitem(cli.CONFIG, "asr_retry_backoff_seconds", 0)

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
    srt = srt_path.read_text(encoding="utf-8")
    assert "第一段内容，介绍核心概念。" in srt
    assert "第二段内容，展开案例并自然结束。" in srt
    assert "00:01:00,000 --> 00:01:50,000" in srt
    assert len(http_calls) == 2

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
