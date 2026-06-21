from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate, LiveExportDecision, TopicReviewResult
from video_auto_editor.selection import select_live_exports


def make_candidate(index=0):
    return ClipCandidate(
        index=index,
        start_time=10 + index * 70,
        end_time=70 + index * 70,
        duration=60,
        text=f"候选 {index}",
        base_score=88,
        adjusted_score=93,
    )


def make_review(topic="时间管理", score=86):
    return TopicReviewResult(
        topic_name=topic,
        topic_complete=True,
        learning_value=8,
        share_value=7,
        publish_ready_score=score,
        export_decision="publish_ready",
        title=f"{topic}标题",
        summary=f"{topic}摘要",
        keywords=[topic],
        needs_human_review=False,
        reject_reason="",
        boundary_fix_suggestion="",
    )


def test_export_decision_is_attached_to_candidate():
    config = CONFIG.copy()
    candidate = make_candidate()
    candidate.review = make_review()

    selected, decisions = select_live_exports([candidate], None, config, review_status="reviewed")

    assert selected == [candidate]
    assert len(decisions) == 1
    assert isinstance(decisions[0], LiveExportDecision)
    assert candidate.export_selection == decisions[0]
    assert decisions[0].candidate_index == 0
    assert decisions[0].decision == "export"
    assert decisions[0].reason == "publish_ready"
    assert decisions[0].review_status == "reviewed"
    assert decisions[0].topic_name == "时间管理"
