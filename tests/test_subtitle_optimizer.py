import json
import urllib.error

from video_auto_editor.config import CONFIG
from video_auto_editor.models import TranscriptChunk
from video_auto_editor.subtitle_optimizer import (
    StepFunChatSubtitleOptimizer,
    create_subtitle_optimizer,
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


def optimizer_config(**overrides):
    config = CONFIG.copy()
    config.update(
        {
            "subtitle_optimization_api_key": "test-key",
            "subtitle_max_lines": 1,
            # 单测里关闭退避，避免失败重试路径真的 sleep。
            "subtitle_optimization_retry_backoff_seconds": 0,
        }
    )
    config.update(overrides)
    return config


def window_chunks():
    # "今天天气真好啊我们出门吧"，逐字时间为 0..12。
    text = "今天天气真好啊我们出门吧"
    spans = [(float(i), float(i + 1)) for i in range(len(text))]
    return [TranscriptChunk(0.0, float(len(text)), text, char_spans=spans)]


def test_optimize_window_success_returns_aligned_blocks():
    def fake_request(request, timeout):
        return FakeResponse(chat_response("今天天气真好\n我们出门吧"))

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)

    blocks = optimizer.optimize_window(window_chunks())

    assert blocks is not None
    assert [block.text for block in blocks] == ["今天天气真好", "我们出门吧"]
    assert blocks[0].start == 0.0
    assert blocks[0].end == 6.0
    assert blocks[1].start == 7.0  # 跳过被删的「啊」
    assert blocks[1].end == 12.0


def test_optimize_window_non_subsequence_fails():
    def fake_request(request, timeout):
        # 模型补了「很」字，违反子序列约束。
        return FakeResponse(chat_response("今天天气很好"))

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)

    assert optimizer.optimize_window(window_chunks()) is None


def test_optimize_window_http_error_fails_without_raising():
    def fake_request(request, timeout):
        raise urllib.error.HTTPError("url", 500, "boom", {}, None)

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)

    assert optimizer.optimize_window(window_chunks()) is None


def test_optimize_window_timeout_fails_without_raising():
    def fake_request(request, timeout):
        raise TimeoutError("timed out")

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)

    assert optimizer.optimize_window(window_chunks()) is None


def test_optimize_window_retries_then_succeeds():
    calls = []

    def fake_request(request, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("still down")
        return FakeResponse(chat_response("今天天气真好\n我们出门吧"))

    optimizer = StepFunChatSubtitleOptimizer(
        optimizer_config(subtitle_optimization_retry_attempts=2, subtitle_optimization_retry_backoff_seconds=0),
        request_func=fake_request,
    )

    blocks = optimizer.optimize_window(window_chunks())

    assert blocks is not None
    assert len(calls) == 2


def test_optimize_window_strips_markdown_code_fence():
    def fake_request(request, timeout):
        return FakeResponse(chat_response("```\n今天天气真好\n我们出门吧\n```"))

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)

    blocks = optimizer.optimize_window(window_chunks())

    assert blocks is not None
    assert [block.text for block in blocks] == ["今天天气真好", "我们出门吧"]


def test_is_available_only_checks_key():
    available = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=lambda *a, **k: None)
    assert available.is_available() is True

    config = CONFIG.copy()
    config["subtitle_optimization_api_key"] = ""
    config["subtitle_optimization_api_key_env"] = "DEFINITELY_UNSET_KEY_ENV"
    unavailable = StepFunChatSubtitleOptimizer(config, request_func=lambda *a, **k: None)
    assert unavailable.is_available() is False
    assert unavailable.optimize_window(window_chunks()) is None


def test_empty_model_inherits_topic_review_model():
    config = optimizer_config(subtitle_optimization_model="", topic_review_model="step-2-mini")
    optimizer = StepFunChatSubtitleOptimizer(config, request_func=lambda *a, **k: None)
    assert optimizer.model == "step-2-mini"

    config_explicit = optimizer_config(subtitle_optimization_model="custom-sub-model")
    optimizer_explicit = StepFunChatSubtitleOptimizer(config_explicit, request_func=lambda *a, **k: None)
    assert optimizer_explicit.model == "custom-sub-model"


def test_create_subtitle_optimizer_factory():
    optimizer = create_subtitle_optimizer(optimizer_config(), request_func=lambda *a, **k: None)
    assert isinstance(optimizer, StepFunChatSubtitleOptimizer)


def test_build_request_targets_chat_completions_without_json_format():
    captured = {}

    def fake_request(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(chat_response("今天天气真好\n我们出门吧"))

    optimizer = StepFunChatSubtitleOptimizer(optimizer_config(), request_func=fake_request)
    optimizer.optimize_window(window_chunks())

    assert captured["url"].endswith("/chat/completions")
    # 纯文本分块契约：不应使用 JSON response_format。
    assert "response_format" not in captured["body"]
    assert captured["body"]["messages"][1]["content"] == "今天天气真好啊我们出门吧"
