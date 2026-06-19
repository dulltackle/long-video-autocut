import json

from video_auto_editor.context import CourseContext
from video_auto_editor.models import ClipCandidate
from video_auto_editor.plan import build_plan, write_plan


def make_candidate(index=0):
    return ClipCandidate(
        index=index,
        start_time=10,
        end_time=70,
        duration=60,
        text="不应写入 plan 的完整转写文本",
        base_score=88,
        adjusted_score=93,
        title="候选标题",
        summary="候选摘要",
        keywords=["剪辑"],
    )


def test_build_plan_contains_unreviewed_status_and_context_summary():
    context = CourseContext({"course_title": "直播课", "priority_topics": ["剪辑"]})
    candidate = make_candidate()

    payload = build_plan("/abs/path/live.mp4", context, [candidate], [candidate], ["warning"])

    assert payload["source_video"] == "live.mp4"
    assert payload["status"] == "unreviewed"
    assert payload["context"]["loaded"] is True
    assert payload["context"]["summary"]["known_fields"] == ["course_title", "priority_topics"]
    assert payload["candidates"][0]["score"] == 93
    assert payload["selected"][0]["index"] == 0
    assert payload["warnings"] == ["warning"]
    assert "text" not in payload["candidates"][0]


def test_write_plan_writes_valid_json(tmp_path):
    plan_path = write_plan("live.mp4", str(tmp_path), None, [make_candidate(1)], [], ["未评审"])

    payload = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan_path == str(tmp_path / "plan.json")
    assert payload["context"] == {"loaded": False, "summary": {}}
    assert payload["candidates"][0]["index"] == 1
    assert payload["selected"] == []
