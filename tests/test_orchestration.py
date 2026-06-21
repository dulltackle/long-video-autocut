import json

from video_auto_editor.orchestration import (
    interpret_artifacts,
    interpret_output_dir,
)


def reviewed_plan(dry_run=False):
    return {
        "source_video": "live.mp4",
        "status": "reviewed",
        "export_mode": "reviewed_publish_ready",
        "dry_run": dry_run,
        "publish_ready_threshold": 80,
        "warnings": [],
        "exports": [
            {
                "export_index": 1,
                "title": "如何剪辑直播",
                "video_path": "clips/001_如何剪辑直播.mp4",
                "subtitle_path": "subtitles/001_如何剪辑直播.srt",
                "generated": False,
                "dry_run": dry_run,
                "export_selection": {
                    "topic_name": "剪辑技巧",
                    "publish_ready_score": 88,
                    "final_start": 10.0,
                    "final_end": 95.0,
                    "series_key": "剪辑",
                    "needs_human_review": False,
                },
            }
        ],
        "candidates": [
            {
                "index": 2,
                "export_selection": {
                    "candidate_index": 2,
                    "selected_for_export": False,
                    "decision": "skip",
                    "reason": "needs_human_review",
                    "topic_name": "发布策略",
                    "publish_ready_score": 60,
                    "needs_human_review": True,
                    "boundary_fix_suggestion": "向后延长结尾",
                    "series_key": "剪辑",
                },
            }
        ],
    }


def reviewed_metadata():
    return {
        "source_video": "live.mp4",
        "status": "reviewed",
        "publish_ready_threshold": 80,
        "export_count": 1,
        "clips": [
            {
                "index": 1,
                "title": "如何剪辑直播",
                "topic_name": "剪辑技巧",
                "publish_ready_score": 88,
                "final_start": 10.0,
                "final_end": 95.0,
                "output_path": "clips/001_如何剪辑直播.mp4",
                "subtitle_path": "subtitles/001_如何剪辑直播.srt",
                "series_key": "剪辑",
                "needs_human_review": False,
            }
        ],
        "not_exported": [
            {
                "candidate_index": 2,
                "decision": "skip",
                "reason": "needs_human_review",
                "topic_name": "发布策略",
                "publish_ready_score": 60,
                "needs_human_review": True,
                "boundary_fix_suggestion": "向后延长结尾",
                "series_key": "剪辑",
            }
        ],
        "human_review": [],
    }


def unreviewed_plan():
    return {
        "source_video": "live.mp4",
        "status": "unreviewed",
        "export_mode": "unreviewed_no_export",
        "dry_run": False,
        "publish_ready_threshold": 80,
        "warnings": ["主题评审不可用：缺少 API Key，未发起评审请求。"],
        "exports": [],
        "candidates": [
            {
                "index": 0,
                "export_selection": {
                    "candidate_index": 0,
                    "selected_for_export": False,
                    "decision": "skip",
                    "reason": "unreviewed_export_not_allowed",
                    "topic_name": "",
                    "publish_ready_score": None,
                    "needs_human_review": False,
                    "series_key": "",
                },
            }
        ],
    }


def test_reviewed_export_interpretation():
    result = interpret_artifacts(reviewed_plan(), reviewed_metadata())

    assert result["run_mode"] == "reviewed_export"
    assert result["exports_generated"] is True
    assert result["export_count"] == 1
    export = result["exports"][0]
    assert export["title"] == "如何剪辑直播"
    assert export["video_path"] == "clips/001_如何剪辑直播.mp4"
    assert export["final_end"] == 95.0
    assert export["generated"] is True


def test_reviewed_dry_run_interpretation():
    result = interpret_artifacts(reviewed_plan(dry_run=True), metadata=None)

    assert result["run_mode"] == "reviewed_dry_run"
    assert result["exports_generated"] is False
    export = result["exports"][0]
    assert export["generated"] is False
    assert export["video_path"] == "clips/001_如何剪辑直播.mp4"


def test_unreviewed_interpretation_keeps_warning():
    result = interpret_artifacts(unreviewed_plan(), metadata=None)

    assert result["run_mode"] == "unreviewed_no_export"
    assert result["export_count"] == 0
    assert result["warnings"][0]["message"].startswith("主题评审不可用")
    assert result["not_exported"][0]["reason_label"] == "未评审且未允许兼容导出"


def test_not_exported_and_human_review_preserved():
    result = interpret_artifacts(reviewed_plan(), reviewed_metadata())

    not_exported = result["not_exported"][0]
    assert not_exported["candidate_index"] == 2
    assert not_exported["reason_label"] == "评审标记需人工复核"
    assert not_exported["needs_human_review"] is True
    assert not_exported["boundary_fix_suggestion"] == "向后延长结尾"

    human_review = result["human_review"]
    assert any(item["index"] == 2 for item in human_review)


def test_series_groups_same_topic():
    result = interpret_artifacts(reviewed_plan(), reviewed_metadata())

    series_keys = {group["series_key"] for group in result["series"]}
    assert "剪辑" in series_keys


def test_interpret_missing_metadata_does_not_raise(tmp_path):
    (tmp_path / "plan.json").write_text(
        json.dumps(reviewed_plan(dry_run=True), ensure_ascii=False), encoding="utf-8"
    )

    result = interpret_output_dir(str(tmp_path))
    assert result["run_mode"] == "reviewed_dry_run"


def test_no_none_rendered_for_missing_fields():
    plan = {
        "source_video": "live.mp4",
        "status": "reviewed",
        "export_mode": "reviewed_publish_ready",
        "dry_run": True,
        "exports": [{"export_index": 1, "export_selection": {}}],
        "candidates": [],
        "warnings": [],
    }
    result = interpret_artifacts(plan, metadata=None)
    export = result["exports"][0]
    assert export["title"] == ""
    assert export["topic_name"] == ""
    assert export["series_key"] == ""


from video_auto_editor.orchestration import diagnose_run


def test_diagnose_asr_failure_is_abort():
    diagnoses = diagnose_run(exit_code=1, has_transcript=False)

    assert len(diagnoses) == 1
    diag = diagnoses[0]
    assert diag["category"] == "asr_failed"
    assert diag["severity"] == "abort"
    assert "STEPFUN_API_KEY" in diag["rerun_command"]


def test_diagnose_review_missing_key_is_degraded():
    plan = {
        "status": "unreviewed",
        "export_count": 0,
        "context": {"loaded": True},
        "warnings": ["主题评审不可用：缺少 API Key，未发起评审请求。"],
    }
    diagnoses = diagnose_run(exit_code=0, has_transcript=True, plan=plan)

    review = next(d for d in diagnoses if d["category"] == "review_degraded")
    assert review["severity"] == "degraded"
    assert "STEPFUN_API_KEY" in review["rerun_command"]
    assert "--allow-unreviewed-export" in review["compatibility_command"]


def test_diagnose_no_publish_ready_is_info_not_failure():
    plan = {
        "status": "reviewed",
        "export_count": 0,
        "context": {"loaded": True},
        "warnings": [],
    }
    diagnoses = diagnose_run(exit_code=0, has_transcript=True, plan=plan)

    categories = {d["category"] for d in diagnoses}
    assert "no_publish_ready" in categories
    no_ready = next(d for d in diagnoses if d["category"] == "no_publish_ready")
    assert no_ready["severity"] == "info"
    assert "--dry-run" in no_ready["rerun_command"]


def test_diagnose_missing_context_hint():
    plan = {
        "status": "reviewed",
        "export_count": 1,
        "exports": [{}],
        "context": {"loaded": False},
        "warnings": [],
    }
    diagnoses = diagnose_run(exit_code=0, has_transcript=True, plan=plan)

    assert any(d["category"] == "missing_context" for d in diagnoses)


def test_diagnose_review_disabled_suggests_compatibility():
    plan = {
        "status": "unreviewed",
        "export_count": 0,
        "context": {"loaded": True},
        "warnings": ["主题评审已关闭，plan.json status 为 unreviewed。"],
    }
    diagnoses = diagnose_run(exit_code=0, has_transcript=True, plan=plan)

    review = next(d for d in diagnoses if d["category"] == "review_degraded")
    assert "--allow-unreviewed-export" in review["rerun_command"]
