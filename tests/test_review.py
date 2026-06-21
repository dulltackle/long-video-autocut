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
