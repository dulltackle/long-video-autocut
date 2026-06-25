"""任务1：评审部分批次失败时保留成功评审而非整次降级的 cli 层行为。"""

from video_auto_editor import cli
from video_auto_editor.models import ClipCandidate, TopicReviewResult
from video_auto_editor.review import TopicReviewProviderResult


def make_candidate(index, start=0):
    return ClipCandidate(
        index=index,
        start_time=start,
        end_time=start + 30,
        duration=30,
        text=f"候选{index}正文",
        base_score=80 + index,
        title=f"候选{index}标题",
        summary=f"候选{index}摘要",
        keywords=[f"关键词{index}"],
    )


def make_review(title):
    return TopicReviewResult(
        topic_name="直播拆条",
        topic_complete=True,
        learning_value=9,
        share_value=8,
        publish_ready_score=91,
        export_decision="publish_ready",
        title=title,
        summary="完整说明。",
        keywords=["直播拆条"],
        needs_human_review=False,
        reject_reason="",
        boundary_fix_suggestion="",
    )


class FakeReviewer:
    provider_name = "fake"
    model = "fake-model"
    base_url = "https://api.example/v1"

    def __init__(self, result):
        self._result = result

    def is_available(self):
        return True

    def review_batches(self, batches):
        return self._result


def _patch_reviewer(monkeypatch, result):
    monkeypatch.setattr(cli, "create_topic_reviewer", lambda config: FakeReviewer(result))


def test_partial_batch_failure_keeps_reviewed_status_and_warns(monkeypatch):
    candidates = [make_candidate(0, 0), make_candidate(1, 30)]
    result = TopicReviewProviderResult(
        success=False,
        reviews={0: make_review("第一条")},
        error="batch 1 failed",
        failed_batches=[{"batch_index": 1, "candidate_range": "candidate_1", "failure_type": "invalid_schema"}],
    )
    _patch_reviewer(monkeypatch, result)

    status, provider_info, warnings = cli._review_live_candidates(
        candidates,
        None,
        {"topic_review_batch_size": 1},
    )

    assert status == "reviewed"
    assert candidates[0].review is not None
    assert candidates[0].title == "第一条"
    assert candidates[1].review is None
    assert any("部分批次评审失败（1/2）" in warning for warning in warnings)
    diagnostics = provider_info["review_diagnostics"]
    assert diagnostics["failed_review_batch_count"] == 1
    assert diagnostics["reviewed_candidate_count"] == 1


def test_full_batch_success_has_no_warning_or_diagnostics(monkeypatch):
    candidates = [make_candidate(0, 0), make_candidate(1, 30)]
    result = TopicReviewProviderResult(
        success=True,
        reviews={0: make_review("第一条"), 1: make_review("第二条")},
    )
    _patch_reviewer(monkeypatch, result)

    status, provider_info, warnings = cli._review_live_candidates(
        candidates,
        None,
        {"topic_review_batch_size": 1},
    )

    assert status == "reviewed"
    assert warnings == []
    assert "review_diagnostics" not in provider_info
    assert candidates[0].review is not None
    assert candidates[1].review is not None


def test_all_batches_failed_degrades_to_unreviewed(monkeypatch):
    candidates = [make_candidate(0, 0), make_candidate(1, 30)]
    result = TopicReviewProviderResult(
        success=False,
        reviews={},
        error="all batches failed",
        failed_batches=[
            {"batch_index": 0, "candidate_range": "candidate_0", "failure_type": "invalid_schema"},
            {"batch_index": 1, "candidate_range": "candidate_1", "failure_type": "invalid_schema"},
        ],
    )
    _patch_reviewer(monkeypatch, result)

    status, provider_info, warnings = cli._review_live_candidates(
        candidates,
        None,
        {"topic_review_batch_size": 1},
    )

    assert status == "unreviewed"
    assert candidates[0].review is None
    assert candidates[1].review is None
    assert any("主题评审失败" in warning for warning in warnings)
    assert provider_info["review_diagnostics"]["failed_review_batch_count"] == 2
