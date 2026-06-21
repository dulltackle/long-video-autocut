import json

from video_auto_editor.context import CourseContext
from video_auto_editor.models import ClipCandidate, TopicReviewResult
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
    assert payload["review_provider"] == {}
    assert "text" not in payload["candidates"][0]
    assert "review" not in payload["candidates"][0]


def test_write_plan_writes_valid_json(tmp_path):
    plan_path = write_plan("live.mp4", str(tmp_path), None, [make_candidate(1)], [], ["未评审"])

    payload = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan_path == str(tmp_path / "plan.json")
    assert payload["context"] == {"loaded": False, "summary": {}}
    assert payload["candidates"][0]["index"] == 1
    assert payload["selected"] == []


def test_build_plan_writes_structured_review_payload():
    candidate = make_candidate()
    candidate.review = TopicReviewResult(
        topic_name="时间管理",
        topic_complete=True,
        learning_value=8,
        share_value=7,
        publish_ready_score=86,
        export_decision="publish_ready",
        title="高效管理时间的三个动作",
        summary="候选片段完整讲解时间管理动作。",
        keywords=["时间管理", "效率"],
        needs_human_review=False,
        reject_reason="",
        boundary_fix_suggestion="",
    )

    payload = build_plan(
        "live.mp4",
        None,
        [candidate],
        [candidate],
        status="reviewed",
        review_provider={"provider": "stepfun_chat", "model": "step-2-mini"},
    )

    review = payload["candidates"][0]["review"]
    assert payload["status"] == "reviewed"
    assert payload["review_provider"] == {"provider": "stepfun_chat", "model": "step-2-mini"}
    assert review == {
        "topic_name": "时间管理",
        "topic_complete": True,
        "learning_value": 8,
        "share_value": 7,
        "publish_ready_score": 86,
        "export_decision": "publish_ready",
        "title": "高效管理时间的三个动作",
        "summary": "候选片段完整讲解时间管理动作。",
        "keywords": ["时间管理", "效率"],
        "needs_human_review": False,
        "reject_reason": "",
        "boundary_fix_suggestion": "",
    }
    assert payload["selected"][0]["review"] == review
