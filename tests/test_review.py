from video_auto_editor import transcript
from video_auto_editor.config import CONFIG
from video_auto_editor.context import CourseContext
from video_auto_editor.models import ClipCandidate, TopicReviewResult
from video_auto_editor.review import build_topic_review_batches


def make_candidate(index, start, text=None):
    return ClipCandidate(
        index=index,
        start_time=start,
        end_time=start + 30,
        duration=30,
        text=text or f"候选{index}正文",
        base_score=80 + index,
        title=f"候选{index}标题",
        summary=f"候选{index}摘要",
        keywords=[f"关键词{index}"],
    )


def test_topic_review_result_captures_required_contract_fields():
    review = TopicReviewResult(
        topic_name="直播拆条",
        topic_complete=True,
        learning_value=9,
        share_value=8,
        publish_ready_score=91,
        export_decision="publish_ready",
        title="直播拆条的判断标准",
        summary="完整说明一个可发布短视频应具备的结构。",
        keywords=["直播拆条", "短视频"],
        needs_human_review=False,
        reject_reason="",
        boundary_fix_suggestion="",
    )

    assert review.topic_name == "直播拆条"
    assert review.keywords == ["直播拆条", "短视频"]
    assert review.publish_ready_score == 91


def test_default_topic_review_config_uses_stepfun_chat():
    assert CONFIG["topic_review_enabled"] is True
    assert CONFIG["topic_review_provider"] == "stepfun_chat"
    assert CONFIG["topic_review_model"] == "step-2-mini"
    assert CONFIG["topic_review_timeout"] == 60
    assert CONFIG["topic_review_batch_size"] == 3
    assert CONFIG["topic_review_temperature"] == 0.2
    assert CONFIG["topic_review_api_key_env"] == "STEPFUN_API_KEY"
    assert CONFIG["topic_review_base_url_env"] == "STEPFUN_BASE_URL"
    assert CONFIG["topic_review_base_url"] == "https://api.stepfun.com/v1"
    assert CONFIG["topic_review_publish_ready_threshold"] == 80


def test_topic_review_config_does_not_affect_asr_cache_signature():
    base_config = {"asr_provider": "stepaudio", "asr_model": "stepaudio-2.5-asr"}
    topic_config = {
        **base_config,
        "topic_review_enabled": False,
        "topic_review_provider": "openai_compatible",
        "topic_review_model": "custom-reviewer",
        "topic_review_base_url": "https://review.example/v1",
        "topic_review_publish_ready_threshold": 95,
    }

    assert transcript._asr_cache_signature(base_config) == transcript._asr_cache_signature(topic_config)


def test_build_topic_review_batches_sorts_candidates_by_time_and_batch_size():
    candidates = [make_candidate(2, 60), make_candidate(0, 0), make_candidate(1, 30)]

    batches = build_topic_review_batches(candidates, config={"topic_review_batch_size": 2})

    assert [batch.batch_index for batch in batches] == [0, 1]
    assert [[item.candidate_index for item in batch.candidates] for batch in batches] == [[0, 1], [2]]
    assert batches[0].to_payload()["candidates"][0]["candidate_id"] == "candidate_0"


def test_build_topic_review_batches_includes_neighbor_context():
    candidates = [make_candidate(0, 0), make_candidate(1, 30), make_candidate(2, 60)]

    batches = build_topic_review_batches(candidates, config={"topic_review_batch_size": 3})
    payload = batches[0].to_payload()["candidates"]

    assert "previous_candidate" not in payload[0]
    assert payload[0]["next_candidate"]["candidate_id"] == "candidate_1"
    assert payload[1]["previous_candidate"]["candidate_id"] == "candidate_0"
    assert payload[1]["next_candidate"]["candidate_id"] == "candidate_2"
    assert payload[2]["previous_candidate"]["candidate_id"] == "candidate_1"
    assert "next_candidate" not in payload[2]


def test_build_topic_review_batches_includes_course_context_summary():
    context = CourseContext({"course_title": "直播课", "priority_topics": ["剪辑"]})

    batches = build_topic_review_batches([make_candidate(0, 0)], context, {"topic_review_batch_size": 1})

    assert batches[0].to_payload()["course_context_summary"] == {
        "known_fields": ["course_title", "priority_topics"],
        "string_fields": ["course_title"],
        "list_counts": {"priority_topics": 1},
        "unknown_fields": [],
    }


def test_build_topic_review_batches_allows_missing_course_context_and_empty_candidates():
    assert build_topic_review_batches([], config={"topic_review_batch_size": 2}) == []

    batches = build_topic_review_batches([make_candidate(0, 0)], None, {"topic_review_batch_size": 0})

    assert len(batches) == 1
    assert batches[0].course_context_summary == {}
    assert batches[0].candidates[0].text == "候选0正文"
