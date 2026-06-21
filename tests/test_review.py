from video_auto_editor import transcript
from video_auto_editor.config import CONFIG
from video_auto_editor.models import TopicReviewResult


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
