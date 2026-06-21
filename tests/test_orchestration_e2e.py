"""skill 编排最小协作闭环测试。

不依赖真实 video-auto-editor 子进程、真实 ffmpeg/ffprobe、真实 StepAudio 或真实
评审模型。用 fake 环境探测、样例课程信息与样例产物，串联预检→上下文→（fake 调用
产出 fake 产物）→解释→诊断。
"""

import json

from video_auto_editor.context import load_course_context, write_course_context
from video_auto_editor.orchestration import (
    diagnose_run,
    interpret_output_dir,
)
from video_auto_editor.preflight import EnvironmentProbe, run_preflight


def ready_probe():
    return EnvironmentProbe(
        commands={"video-auto-editor": True, "ffmpeg": True, "ffprobe": True},
        env={"STEPFUN_API_KEY": "sk-test"},
    )


def not_ready_probe():
    return EnvironmentProbe(
        commands={"video-auto-editor": True, "ffmpeg": True, "ffprobe": True},
        env={},
    )


def write_plan(output_dir, payload):
    (output_dir / "plan.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_metadata(output_dir, payload):
    (output_dir / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def reviewed_export_artifacts():
    plan = {
        "source_video": "live.mp4",
        "status": "reviewed",
        "export_mode": "reviewed_publish_ready",
        "dry_run": False,
        "publish_ready_threshold": 80,
        "context": {"loaded": True},
        "export_count": 1,
        "warnings": [],
        "exports": [{"export_index": 1, "title": "片段一", "export_selection": {}}],
        "candidates": [],
    }
    metadata = {
        "status": "reviewed",
        "export_count": 1,
        "clips": [
            {
                "index": 1,
                "title": "片段一",
                "topic_name": "剪辑",
                "publish_ready_score": 90,
                "final_start": 1.0,
                "final_end": 80.0,
                "output_path": "clips/001_片段一.mp4",
                "subtitle_path": "subtitles/001_片段一.srt",
                "series_key": "剪辑",
                "needs_human_review": False,
            }
        ],
        "not_exported": [],
        "human_review": [],
    }
    return plan, metadata


def reviewed_dry_run_plan():
    return {
        "source_video": "live.mp4",
        "status": "reviewed",
        "export_mode": "reviewed_publish_ready",
        "dry_run": True,
        "publish_ready_threshold": 80,
        "context": {"loaded": True},
        "export_count": 1,
        "warnings": [],
        "exports": [
            {
                "export_index": 1,
                "title": "片段一",
                "video_path": "clips/001_片段一.mp4",
                "generated": False,
                "dry_run": True,
                "export_selection": {"topic_name": "剪辑"},
            }
        ],
        "candidates": [],
    }


def unreviewed_plan():
    return {
        "source_video": "live.mp4",
        "status": "unreviewed",
        "export_mode": "unreviewed_no_export",
        "dry_run": False,
        "context": {"loaded": True},
        "export_count": 0,
        "warnings": ["主题评审不可用：缺少 API Key，未发起评审请求。"],
        "exports": [],
        "candidates": [],
    }


def test_preflight_blocks_before_cli_when_not_ready():
    result = run_preflight(not_ready_probe())

    assert result.ready is False
    # not-ready 时应在调用 CLI 前停下，并能给出可执行修复提示。
    assert any("STEPFUN_API_KEY" in check.hint for check in result.errors)


def test_preflight_ready_path():
    assert run_preflight(ready_probe()).ready is True


def test_context_generation_is_loadable(tmp_path):
    path = tmp_path / "course-context.json"
    build = write_course_context(
        {"course_title": "直播课", "priority_topics": ["剪辑", "发布"], "platform": "抖音"},
        path,
    )

    context = load_course_context(path)
    assert context.summary()["string_fields"] == ["course_title"]
    assert build.unknown_fields == ["platform"]


def test_interpret_three_artifact_scenarios(tmp_path):
    reviewed_dir = tmp_path / "reviewed"
    reviewed_dir.mkdir()
    plan, metadata = reviewed_export_artifacts()
    write_plan(reviewed_dir, plan)
    write_metadata(reviewed_dir, metadata)
    reviewed = interpret_output_dir(str(reviewed_dir))
    assert reviewed["run_mode"] == "reviewed_export"
    assert reviewed["exports"][0]["video_path"] == "clips/001_片段一.mp4"

    dry_dir = tmp_path / "dry"
    dry_dir.mkdir()
    write_plan(dry_dir, reviewed_dry_run_plan())
    dry = interpret_output_dir(str(dry_dir))
    assert dry["run_mode"] == "reviewed_dry_run"
    assert dry["exports"][0]["generated"] is False

    unreviewed_dir = tmp_path / "unreviewed"
    unreviewed_dir.mkdir()
    write_plan(unreviewed_dir, unreviewed_plan())
    unreviewed = interpret_output_dir(str(unreviewed_dir))
    assert unreviewed["run_mode"] == "unreviewed_no_export"
    assert unreviewed["export_count"] == 0


def test_diagnose_distinguishes_failure_types():
    asr = diagnose_run(exit_code=1, has_transcript=False)
    assert asr[0]["category"] == "asr_failed"

    degraded = diagnose_run(exit_code=0, has_transcript=True, plan=unreviewed_plan())
    assert any(d["category"] == "review_degraded" for d in degraded)

    plan, _ = reviewed_export_artifacts()
    plan["export_count"] = 0
    empty = diagnose_run(exit_code=0, has_transcript=True, plan=plan)
    assert any(d["category"] == "no_publish_ready" for d in empty)


def test_full_min_orchestration_loop(tmp_path):
    # 预检
    assert run_preflight(ready_probe()).ready is True

    # 上下文
    context_path = tmp_path / "course-context.json"
    write_course_context({"course_title": "直播课", "priority_topics": ["剪辑"]}, context_path)
    assert load_course_context(context_path).data["course_title"] == "直播课"

    # fake 调用产出 fake 产物
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    plan, metadata = reviewed_export_artifacts()
    write_plan(output_dir, plan)
    write_metadata(output_dir, metadata)

    # 解释
    report = interpret_output_dir(str(output_dir))
    assert report["run_mode"] == "reviewed_export"
    assert report["export_count"] == 1

    # 诊断（成功导出时无中止类失败）
    diagnoses = diagnose_run(exit_code=0, has_transcript=True, plan=plan)
    assert all(d["severity"] != "abort" for d in diagnoses)
