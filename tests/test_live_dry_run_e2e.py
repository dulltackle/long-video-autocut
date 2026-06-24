import json

from video_auto_editor import cli, transcript


def completed(returncode=0, stderr="", stdout=""):
    return type("Completed", (), {"returncode": returncode, "stderr": stderr, "stdout": stdout})()


def sse(*deltas):
    lines = []
    for start, end, text in deltas:
        event = {
            "type": "transcript.text.delta",
            "delta": text,
            "start_time": int(round(start * 1000)),
            "end_time": int(round(end * 1000)),
        }
        lines.append("data: " + json.dumps(event, ensure_ascii=False))
    lines.append('data: {"type": "transcript.text.done", "text": ""}')
    return "\n\n".join(lines) + "\n\n"


def test_live_dry_run_generates_plan_report_and_transcript_without_exports(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("fake video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    asr_calls = []
    review_calls = []
    asr_responses = [
        sse(
            (0, 30, "第一段内容，介绍核心概念。"),
            (30, 60, "第二段内容，展开方法步骤。"),
        ),
        sse(
            (0, 30, "第三段内容，展开案例。"),
            (30, 60, "第四段内容，需要人工确认边界。"),
        ),
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
        if request.full_url.endswith("/audio/asr/sse"):
            asr_calls.append(request)
            return FakeResponse(asr_responses[len(asr_calls) - 1])
        if request.full_url.endswith("/chat/completions"):
            body = json.loads(request.data.decode("utf-8"))
            review_calls.append(body)
            review_payload = {
                "reviews": [
                    {
                        "candidate_id": "candidate_0",
                        "topic_name": "核心概念",
                        "topic_complete": True,
                        "learning_value": 9,
                        "share_value": 8,
                        "publish_ready_score": 92,
                        "export_decision": "publish_ready",
                        "title": "核心概念的三个步骤",
                        "summary": "介绍核心概念并展开方法步骤。",
                        "keywords": ["核心概念", "方法"],
                        "needs_human_review": False,
                        "reject_reason": "",
                        "boundary_fix_suggestion": "",
                    },
                    {
                        "candidate_id": "candidate_1",
                        "topic_name": "案例边界",
                        "topic_complete": False,
                        "learning_value": 7,
                        "share_value": 6,
                        "publish_ready_score": 64,
                        "export_decision": "needs_review",
                        "title": "案例边界需要复核",
                        "summary": "案例内容可用，但结束边界需要人工确认。",
                        "keywords": ["案例", "边界"],
                        "needs_human_review": True,
                        "reject_reason": "",
                        "boundary_fix_suggestion": "建议向后补足结束句。",
                    },
                ]
            }
            return FakeResponse(
                json.dumps(
                    {"choices": [{"message": {"content": json.dumps(review_payload, ensure_ascii=False)}}]},
                    ensure_ascii=False,
                )
            )
        raise AssertionError(f"unexpected request: {request.full_url}")

    monkeypatch.setattr(cli, "get_video_duration", lambda video_path_arg: 120.0)
    monkeypatch.setattr(cli, "detect_silence", lambda video_path_arg, config=None: [])
    monkeypatch.setattr(transcript.subprocess, "run", fake_run)
    monkeypatch.setattr(transcript.urllib.request, "urlopen", fake_request)
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setitem(cli.CONFIG, "asr_shard_seconds", 60)
    monkeypatch.setitem(cli.CONFIG, "asr_retry_backoff_seconds", 0)
    monkeypatch.setitem(cli.CONFIG, "min_clip_duration", 30)
    monkeypatch.setitem(cli.CONFIG, "max_clip_duration", 80)
    monkeypatch.setitem(cli.CONFIG, "target_clip_duration", 60)
    monkeypatch.setitem(cli.CONFIG, "topic_overlap_seconds", 0)
    monkeypatch.setitem(cli.CONFIG, "topic_review_enabled", True)

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
    assert "第四段内容，需要人工确认边界。" in srt
    assert "00:01:30,000 --> 00:02:00,000" in srt
    assert len(asr_calls) == 2
    assert len(review_calls) == 1
    assert len(list((work_dir / "live" / "topic_review_cache").glob("*.json"))) == 1

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["status"] == "reviewed"
    assert plan["candidates"][0]["review"]["topic_name"] == "核心概念"
    assert plan["candidates"][1]["review"]["needs_human_review"] is True
    assert plan["selected"]

    report = report_path.read_text(encoding="utf-8")
    assert "Dry-run：本报告包含主题评审结果，但未导出短视频。" in report
    assert "| candidate_0 | 核心概念 | yes | 9 | 8 | 92 | publish_ready | no |  |" in report
    assert "| candidate_1 | 案例边界 | no | 7 | 6 | 64 | needs_review | yes | 建议向后补足结束句。 |" in report
    assert "- Exported clips: 0 (dry-run)" in report

    assert not (output_dir / "metadata.json").exists()
    assert not list((output_dir / "clips").glob("*.mp4")) if (output_dir / "clips").exists() else True
    assert not list((output_dir / "subtitles").glob("*.srt")) if (output_dir / "subtitles").exists() else True
