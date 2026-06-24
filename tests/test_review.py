import json
import urllib.error

import pytest

from video_auto_editor import transcript
from video_auto_editor.config import CONFIG
from video_auto_editor.context import CourseContext
from video_auto_editor.models import ClipCandidate, TopicReviewResult
from video_auto_editor.review import StepFunChatReviewer, build_topic_review_batches, create_topic_reviewer


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


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload.encode("utf-8")


def chat_response(content):
    return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)


def review_content(candidate_id="candidate_0", **overrides):
    item = {
        "candidate_id": candidate_id,
        "topic_name": "直播拆条",
        "topic_complete": True,
        "learning_value": 9,
        "share_value": 8,
        "publish_ready_score": 91,
        "export_decision": "publish_ready",
        "title": "直播拆条的判断标准",
        "summary": "完整说明一个可发布短视频应具备的结构。",
        "keywords": ["直播拆条", "短视频"],
        "needs_human_review": False,
        "reject_reason": "",
        "boundary_fix_suggestion": "",
    }
    item.update(overrides)
    return json.dumps({"reviews": [item]}, ensure_ascii=False)


def review_content_missing(field_name):
    payload = json.loads(review_content())
    payload["reviews"][0].pop(field_name)
    return json.dumps(payload, ensure_ascii=False)


def http_error(status_code):
    return urllib.error.HTTPError(
        "https://api.example/v1/chat/completions",
        status_code,
        "error",
        {},
        None,
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
    assert CONFIG["topic_review_timeout"] == 180
    assert CONFIG["topic_review_batch_size"] == 3
    assert CONFIG["topic_review_retry_attempts"] == 3
    assert CONFIG["topic_review_retry_backoff_seconds"] == 2.0
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

    batches = build_topic_review_batches([make_candidate(0, 0)], None, {"topic_review_batch_size": 1})

    assert len(batches) == 1
    assert batches[0].course_context_summary == {}
    assert batches[0].candidates[0].text == "候选0正文"


def test_build_topic_review_batches_rejects_invalid_batch_size():
    with pytest.raises(ValueError, match="Invalid topic_review_batch_size: 0, must be >= 1"):
        build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 0})


def test_stepfun_chat_reviewer_maps_success_response_to_review_result(monkeypatch):
    calls = []
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})

    def fake_request(request, timeout):
        calls.append((request, timeout, json.loads(request.data.decode("utf-8"))))
        return FakeResponse(chat_response(review_content()))

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_base_url": "https://api.example/v1",
            "topic_review_provider": "stepfun_chat",
            "topic_review_model": "review-model",
            "topic_review_timeout": 12,
            "topic_review_temperature": 0.1,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(batches)

    assert result.success is True
    assert result.provider_info == {
        "provider": "stepfun_chat",
        "model": "review-model",
        "base_url": "https://api.example/v1",
    }
    assert result.reviews[0].topic_name == "直播拆条"
    assert result.reviews[0].keywords == ["直播拆条", "短视频"]
    assert calls[0][1] == 12
    assert calls[0][0].full_url == "https://api.example/v1/chat/completions"
    assert calls[0][2]["messages"][1]["content"]


def test_stepfun_chat_reviewer_injects_reasoning_effort_when_configured():
    calls = []
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})

    def fake_request(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(chat_response(review_content()))

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_base_url": "https://api.example/v1",
            "topic_review_reasoning_effort": "high",
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(batches)

    assert result.success is True
    assert calls[0]["reasoning_effort"] == "high"


def test_stepfun_chat_reviewer_omits_reasoning_effort_when_blank():
    calls = []
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})

    def fake_request(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(chat_response(review_content()))

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_base_url": "https://api.example/v1",
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(batches)

    assert result.success is True
    assert "reasoning_effort" not in calls[0]


def test_stepfun_chat_reviewer_retries_timeout_then_succeeds():
    calls = []
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})

    def fake_request(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return FakeResponse(chat_response(review_content()))

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 2,
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(batches)

    assert result.success is True
    assert len(calls) == 2
    assert result.reviews[0].topic_name == "直播拆条"


def test_stepfun_chat_reviewer_reports_failure_after_retry_attempts_exhausted():
    calls = []

    def fake_request(request, timeout):
        calls.append(request)
        raise TimeoutError("timed out")

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 3,
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert len(calls) == 3
    assert "Topic review request failed: timed out" in result.error
    assert "batch_index=0" in result.error
    assert "candidate_range=candidate_0" in result.error
    assert "attempt=3/3" in result.error
    assert "failure_type=timeout" in result.error


def test_stepfun_chat_reviewer_retries_http_500_but_not_http_400():
    retryable_calls = []

    def retryable_request(request, timeout):
        retryable_calls.append(request)
        if len(retryable_calls) == 1:
            raise http_error(500)
        return FakeResponse(chat_response(review_content()))

    retryable_reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 2,
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=retryable_request,
    )

    retryable_result = retryable_reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert retryable_result.success is True
    assert len(retryable_calls) == 2

    non_retryable_calls = []

    def non_retryable_request(request, timeout):
        non_retryable_calls.append(request)
        raise http_error(400)

    non_retryable_reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 3,
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=non_retryable_request,
    )

    non_retryable_result = non_retryable_reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert non_retryable_result.success is False
    assert len(non_retryable_calls) == 1
    assert "Topic review HTTP error: 400" in non_retryable_result.error
    assert "attempt=1/3" in non_retryable_result.error
    assert "failure_type=http_400" in non_retryable_result.error


def test_stepfun_chat_reviewer_failure_includes_batch_index_and_candidate_range():
    candidates = [make_candidate(12, 0), make_candidate(13, 30)]

    def fake_request(request, timeout):
        raise TimeoutError("timed out")

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 2,
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(build_topic_review_batches(candidates, config={"topic_review_batch_size": 2}))

    assert result.success is False
    assert "batch_index=0" in result.error
    assert "candidate_range=candidate_12-candidate_13" in result.error
    assert "attempt=2/2" in result.error
    assert "failure_type=timeout" in result.error


def test_stepfun_chat_reviewer_uses_successful_batch_cache(tmp_path):
    cache_dir = tmp_path / "topic_review_cache"
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})
    calls = []

    def first_request(request, timeout):
        calls.append(request)
        return FakeResponse(chat_response(review_content()))

    config = {
        "topic_review_api_key": "sk-test",
        "topic_review_base_url": "https://api.example/v1",
        "topic_review_model": "review-model",
        "topic_review_cache_dir": str(cache_dir),
    }
    first = StepFunChatReviewer(config, request_func=first_request)

    first_result = first.review_batches(batches)

    assert first_result.success is True
    assert len(calls) == 1
    assert len(list(cache_dir.glob("*.json"))) == 1

    def fail_if_called(request, timeout):
        raise AssertionError("cached batch should not make HTTP request")

    second = StepFunChatReviewer(config, request_func=fail_if_called)

    second_result = second.review_batches(batches)

    assert second_result.success is True
    assert second_result.reviews[0].title == "直播拆条的判断标准"


def test_stepfun_chat_reviewer_cache_signature_changes_with_model(tmp_path):
    cache_dir = tmp_path / "topic_review_cache"
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})
    calls = []

    def fake_request(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8"))["model"])
        return FakeResponse(chat_response(review_content(title=f"标题-{len(calls)}")))

    base_config = {
        "topic_review_api_key": "sk-test",
        "topic_review_base_url": "https://api.example/v1",
        "topic_review_model": "review-model-a",
        "topic_review_cache_dir": str(cache_dir),
    }

    first = StepFunChatReviewer(base_config, request_func=fake_request)
    second = StepFunChatReviewer({**base_config, "topic_review_model": "review-model-b"}, request_func=fake_request)

    first_result = first.review_batches(batches)
    second_result = second.review_batches(batches)

    assert first_result.success is True
    assert second_result.success is True
    assert calls == ["review-model-a", "review-model-b"]
    assert second_result.reviews[0].title == "标题-2"
    assert len(list(cache_dir.glob("*.json"))) == 2


def test_stepfun_chat_reviewer_does_not_cache_failed_batch(tmp_path):
    cache_dir = tmp_path / "topic_review_cache"
    batches = build_topic_review_batches([make_candidate(0, 0)], config={"topic_review_batch_size": 1})

    failing = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_cache_dir": str(cache_dir),
        },
        request_func=lambda request, timeout: FakeResponse(chat_response("{bad json")),
    )

    failed_result = failing.review_batches(batches)

    assert failed_result.success is False
    assert list(cache_dir.glob("*.json")) == []

    calls = []

    def success_request(request, timeout):
        calls.append(request)
        return FakeResponse(chat_response(review_content()))

    succeeding = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_cache_dir": str(cache_dir),
        },
        request_func=success_request,
    )

    success_result = succeeding.review_batches(batches)

    assert success_result.success is True
    assert len(calls) == 1


def test_stepfun_chat_reviewer_preserves_successful_reviews_when_later_batch_fails():
    candidates = [make_candidate(0, 0), make_candidate(1, 30)]
    batches = build_topic_review_batches(candidates, config={"topic_review_batch_size": 1})
    calls = []

    def fake_request(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        candidate_id = json.loads(body["messages"][1]["content"])["candidates"][0]["candidate_id"]
        calls.append(candidate_id)
        if candidate_id == "candidate_1":
            raise TimeoutError("timed out")
        return FakeResponse(chat_response(review_content(candidate_id=candidate_id)))

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_attempts": 1,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(batches)

    assert result.success is False
    assert calls == ["candidate_0", "candidate_1"]
    assert result.reviews[0].topic_name == "直播拆条"
    assert 1 not in result.reviews
    assert result.failed_batches == [
        {
            "batch_index": 1,
            "candidate_range": "candidate_1",
            "attempt": 1,
            "max_attempts": 1,
            "failure_type": "timeout",
            "error": "Topic review request failed: timed out",
        }
    ]


def test_stepfun_chat_reviewer_rejects_non_https_base_url_without_request():
    def fail_request(*args, **kwargs):
        raise AssertionError("unsafe base_url should not make HTTP request")

    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test", "topic_review_base_url": "http://api.example/v1"},
        request_func=fail_request,
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert "Topic review base_url must use HTTPS for credential safety" in result.error
    assert "batch_index=0" in result.error
    assert "failure_type=invalid_config" in result.error


def test_stepfun_chat_reviewer_clamps_review_scores_to_expected_ranges():
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse(
            chat_response(review_content(learning_value=-3, share_value=99, publish_ready_score=120))
        ),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is True
    assert result.reviews[0].learning_value == 0
    assert result.reviews[0].share_value == 10
    assert result.reviews[0].publish_ready_score == 100


def test_stepfun_chat_reviewer_missing_api_key_is_unavailable_and_does_not_request(monkeypatch):
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)

    def fail_request(*args, **kwargs):
        raise AssertionError("missing API key should not make HTTP request")

    reviewer = StepFunChatReviewer({"topic_review_api_key": ""}, request_func=fail_request)

    assert reviewer.is_available() is False
    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))
    assert result.success is False
    assert result.error == "Topic review API key missing"


def test_stepfun_chat_reviewer_reports_http_failure():
    def fake_request(request, timeout):
        raise urllib.error.URLError("network down")

    reviewer = StepFunChatReviewer(
        {
            "topic_review_api_key": "sk-test",
            "topic_review_retry_backoff_seconds": 0,
        },
        request_func=fake_request,
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert "Topic review request failed" in result.error
    assert "failure_type=url_error" in result.error


def test_stepfun_chat_reviewer_reports_invalid_chat_json():
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse("{bad json"),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert result.error.startswith("Invalid Chat Completions JSON")
    assert "failure_type=invalid_chat_json" in result.error


def test_stepfun_chat_reviewer_reports_invalid_model_json():
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse(chat_response("{bad json")),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert result.error.startswith("Invalid topic review JSON")
    assert "failure_type=invalid_topic_json" in result.error


def test_stepfun_chat_reviewer_reports_missing_required_review_field():
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse(chat_response(review_content_missing("boundary_fix_suggestion"))),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert "missing fields" in result.error
    assert "boundary_fix_suggestion" in result.error


def test_stepfun_chat_reviewer_reports_missing_candidate_id():
    payload = json.loads(review_content())
    payload["reviews"][0].pop("candidate_id")
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse(chat_response(json.dumps(payload, ensure_ascii=False))),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert "Topic review item missing candidate_id" in result.error
    assert "failure_type=invalid_schema" in result.error


def test_stepfun_chat_reviewer_reports_unknown_candidate_id():
    reviewer = StepFunChatReviewer(
        {"topic_review_api_key": "sk-test"},
        request_func=lambda request, timeout: FakeResponse(chat_response(review_content(candidate_id="candidate_99"))),
    )

    result = reviewer.review_batches(build_topic_review_batches([make_candidate(0, 0)]))

    assert result.success is False
    assert "Topic review returned unknown candidate_id: candidate_99" in result.error
    assert "failure_type=unknown_candidate" in result.error


def test_create_topic_reviewer_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown topic review provider: unknown"):
        create_topic_reviewer({"topic_review_provider": "unknown"})
