import json
from pathlib import Path

from video_auto_editor import cli, transcript


def completed(returncode=0, stderr="", stdout=""):
    return type("Completed", (), {"returncode": returncode, "stderr": stderr, "stdout": stdout})()


def test_live_reviewed_non_dry_run_generates_export_deliverables(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("fake video", encoding="utf-8")
    output_dir = tmp_path / "out"
    work_dir = tmp_path / "work"
    asr_calls = []
    review_calls = []
    ffmpeg_outputs = []
    asr_responses = [
        json.dumps(
            {
                "segments": [
                    {"start": 0, "end": 30, "text": "第一段内容，介绍核心概念。"},
                    {"start": 30, "end": 60, "text": "第二段内容，展开方法步骤。"},
                    {"start": 60, "end": 90, "text": "第三段内容，案例铺垫。"},
                ]
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "segments": [
                    {"start": 0, "end": 30, "text": "第四段内容，案例需要复核。"},
                    {"start": 30, "end": 60, "text": "第五段内容，总结关键动作。"},
                    {"start": 60, "end": 90, "text": "第六段内容，给出行动清单。"},
                ]
            },
            ensure_ascii=False,
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
            return completed(0, stdout='{"format": {"duration": "180.0"}}')
        if cmd[0] == "ffmpeg":
            if cmd[-1] != "-":
                Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[-1]).write_bytes(b"fake-media")
                ffmpeg_outputs.append(cmd[-1])
            return completed(0, stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_request(request, timeout):
        if request.full_url.endswith("/audio/transcriptions"):
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
                        "publish_ready_score": 94,
                        "export_decision": "publish_ready",
                        "title": "核心概念的方法步骤",
                        "summary": "介绍核心概念并展开方法步骤。",
                        "keywords": ["核心概念", "方法"],
                        "needs_human_review": False,
                        "reject_reason": "",
                        "boundary_fix_suggestion": "",
                    },
                    {
                        "candidate_id": "candidate_1",
                        "topic_name": "案例复核",
                        "topic_complete": False,
                        "learning_value": 7,
                        "share_value": 6,
                        "publish_ready_score": 66,
                        "export_decision": "needs_review",
                        "title": "案例边界需要复核",
                        "summary": "案例内容可用，但结束边界需要人工确认。",
                        "keywords": ["案例", "边界"],
                        "needs_human_review": True,
                        "reject_reason": "",
                        "boundary_fix_suggestion": "建议向后补足结束句。",
                    },
                    {
                        "candidate_id": "candidate_2",
                        "topic_name": "行动清单",
                        "topic_complete": True,
                        "learning_value": 9,
                        "share_value": 9,
                        "publish_ready_score": 91,
                        "export_decision": "publish_ready",
                        "title": "行动清单的关键动作",
                        "summary": "总结关键动作并给出行动清单。",
                        "keywords": ["行动清单", "关键动作"],
                        "needs_human_review": False,
                        "reject_reason": "",
                        "boundary_fix_suggestion": "",
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

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)
    monkeypatch.setattr(transcript.urllib.request, "urlopen", fake_request)
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setitem(cli.CONFIG, "asr_shard_seconds", 90)
    monkeypatch.setitem(cli.CONFIG, "asr_retry_backoff_seconds", 0)
    monkeypatch.setitem(cli.CONFIG, "min_clip_duration", 30)
    monkeypatch.setitem(cli.CONFIG, "max_clip_duration", 80)
    monkeypatch.setitem(cli.CONFIG, "target_clip_duration", 60)
    monkeypatch.setitem(cli.CONFIG, "topic_overlap_seconds", 0)
    monkeypatch.setitem(cli.CONFIG, "topic_review_enabled", True)
    monkeypatch.setitem(cli.CONFIG, "topic_review_batch_size", 3)
    monkeypatch.setitem(cli.CONFIG, "temporary_protective_max_clips", 1)

    cli.main(
        [
            "live",
            str(video_path),
            "--output-dir",
            str(output_dir),
            "--work-dir",
            str(work_dir),
        ]
    )

    srt = (output_dir / "transcript.srt").read_text(encoding="utf-8")
    assert "第一段内容，介绍核心概念。" in srt
    assert "第六段内容，给出行动清单。" in srt
    assert "00:02:30,000 --> 00:03:00,000" in srt
    assert len(asr_calls) == 2
    assert len(review_calls) == 1

    plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["status"] == "reviewed"
    assert plan["export_count"] == 2
    assert [item["candidate_index"] for item in plan["exports"]] == [0, 2]
    assert plan["candidates"][1]["export_selection"]["reason"] == "needs_human_review"

    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "reviewed"
    assert [item["title"] for item in metadata["clips"]] == ["核心概念的方法步骤", "行动清单的关键动作"]
    assert metadata["not_exported"][0]["reason"] == "needs_human_review"
    assert metadata["human_review"][0]["boundary_fix_suggestion"] == "建议向后补足结束句。"

    assert len(list((output_dir / "clips").glob("*.mp4"))) == 2
    assert len(list((output_dir / "subtitles").glob("*.srt"))) == 2
    assert any(str(output_dir / "clips") in path for path in ffmpeg_outputs)

    report = (output_dir / "拆条报告.md").read_text(encoding="utf-8")
    assert "Reviewed 非 dry-run 交付包" in report
    assert "核心概念的方法步骤" in report
    assert "needs_human_review" in report
    assert "## 同主题系列" in report
    assert "| `metadata.json` | yes |" in report
