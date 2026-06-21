"""直播主题评审输入构造与 provider 封装。"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Protocol

from video_auto_editor.config import CONFIG
from video_auto_editor.models import TopicReviewResult


REQUIRED_REVIEW_FIELDS = {
    "topic_name",
    "topic_complete",
    "learning_value",
    "share_value",
    "publish_ready_score",
    "export_decision",
    "title",
    "summary",
    "keywords",
    "needs_human_review",
    "reject_reason",
    "boundary_fix_suggestion",
}


@dataclass
class TopicReviewCandidateInput:
    """单个候选提交给主题评审模型的稳定输入。"""

    candidate_id: str
    candidate_index: int
    start_time: float
    end_time: float
    duration: float
    title: str
    summary: str
    keywords: List[str]
    text: str
    previous_candidate: dict | None = None
    next_candidate: dict | None = None

    def to_payload(self):
        payload = {
            "candidate_id": self.candidate_id,
            "candidate_index": self.candidate_index,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "title": self.title,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "text": self.text,
        }
        if self.previous_candidate is not None:
            payload["previous_candidate"] = dict(self.previous_candidate)
        if self.next_candidate is not None:
            payload["next_candidate"] = dict(self.next_candidate)
        return payload


@dataclass
class TopicReviewBatch:
    """一次主题评审请求中的候选批次。"""

    batch_index: int
    course_context_summary: dict = field(default_factory=dict)
    candidates: List[TopicReviewCandidateInput] = field(default_factory=list)

    def to_payload(self):
        return {
            "batch_index": self.batch_index,
            "course_context_summary": dict(self.course_context_summary),
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }


@dataclass
class TopicReviewProviderResult:
    """主题评审 provider 执行结果。"""

    success: bool
    reviews: dict[int, TopicReviewResult] = field(default_factory=dict)
    error: str = ""
    provider_info: dict = field(default_factory=dict)


class TopicReviewer(Protocol):
    """主题评审 provider 最小契约。"""

    def is_available(self):
        """检查 provider 当前是否具备请求条件。"""
        ...

    def review_batches(self, batches):
        """评审多个候选批次。"""
        ...


class StepFunChatReviewer:
    """使用 OpenAI-compatible Chat Completions 形态调用 StepFun Chat。"""

    def __init__(self, config=None, request_func=None):
        self.config = config or CONFIG
        self.api_key = _resolve_topic_review_api_key(self.config)
        self.base_url = _resolve_topic_review_base_url(self.config)
        self.model = str(_config_get(self.config, "topic_review_model", "step-2-mini"))
        self.timeout = int(_config_get(self.config, "topic_review_timeout", 60))
        self.temperature = float(_config_get(self.config, "topic_review_temperature", 0.2))
        self.provider_name = str(_config_get(self.config, "topic_review_provider", "stepfun_chat"))
        self.request_func = request_func or urllib.request.urlopen

    def is_available(self):
        """只检查 API Key，不发起网络请求。"""
        return bool(self.api_key)

    def review_batches(self, batches):
        """调用 Chat Completions 并解析结构化评审结果。"""
        provider_info = {"provider": self.provider_name, "model": self.model, "base_url": self.base_url}
        if not self.api_key:
            return TopicReviewProviderResult(False, error="Topic review API key missing", provider_info=provider_info)
        if not batches:
            return TopicReviewProviderResult(True, provider_info=provider_info)

        reviews = {}
        for batch in batches:
            result = self._review_batch(batch)
            if not result.success:
                result.provider_info = provider_info
                return result
            reviews.update(result.reviews)
        return TopicReviewProviderResult(True, reviews=reviews, provider_info=provider_info)

    def _review_batch(self, batch):
        requested_ids = {
            candidate.candidate_id: candidate.candidate_index
            for candidate in batch.candidates
        }
        try:
            request = self._build_request(batch)
            with self.request_func(request, timeout=self.timeout) as response:
                raw_response = response.read().decode("utf-8")
        except ValueError as exc:
            return TopicReviewProviderResult(False, error=str(exc))
        except urllib.error.HTTPError as exc:
            return TopicReviewProviderResult(False, error=f"Topic review HTTP error: {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return TopicReviewProviderResult(False, error=f"Topic review request failed: {exc}")
        except Exception as exc:
            return TopicReviewProviderResult(False, error=f"Topic review request failed: {exc}")

        try:
            chat_payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            return TopicReviewProviderResult(False, error=f"Invalid Chat Completions JSON: {exc.msg}")

        content = _extract_chat_content(chat_payload)
        if content is None:
            return TopicReviewProviderResult(False, error="Chat Completions response missing message content")

        try:
            review_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            return TopicReviewProviderResult(False, error=f"Invalid topic review JSON: {exc.msg}")

        try:
            reviews = _parse_review_payload(review_payload, requested_ids)
        except ValueError as exc:
            return TopicReviewProviderResult(False, error=str(exc))
        return TopicReviewProviderResult(True, reviews=reviews)

    def _build_request(self, batch):
        _validate_https_base_url(self.base_url)
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是直播课程短视频主题评审器。"
                        "请只返回 JSON object，格式为 {\"reviews\": [...]}。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(batch.to_payload(), ensure_ascii=False, sort_keys=True),
                },
            ],
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )


def build_topic_review_batches(candidates, course_context=None, config=None):
    """按候选时间顺序构造相邻上下文评审批次。"""
    if not candidates:
        return []

    config = config or CONFIG
    batch_size = _topic_review_batch_size(config)
    ordered = sorted(candidates, key=lambda candidate: (candidate.start_time, candidate.end_time, candidate.index))
    context_summary = course_context.summary() if course_context is not None else {}

    review_inputs = [
        _candidate_input(candidate, ordered[index - 1] if index > 0 else None, ordered[index + 1] if index + 1 < len(ordered) else None)
        for index, candidate in enumerate(ordered)
    ]

    return [
        TopicReviewBatch(
            batch_index=batch_index,
            course_context_summary=context_summary,
            candidates=review_inputs[start:start + batch_size],
        )
        for batch_index, start in enumerate(range(0, len(review_inputs), batch_size))
    ]


def _candidate_input(candidate, previous_candidate, next_candidate):
    return TopicReviewCandidateInput(
        candidate_id=_candidate_id(candidate),
        candidate_index=candidate.index,
        start_time=candidate.start_time,
        end_time=candidate.end_time,
        duration=candidate.duration,
        title=candidate.title,
        summary=candidate.summary,
        keywords=list(candidate.keywords),
        text=candidate.text,
        previous_candidate=_neighbor_summary(previous_candidate),
        next_candidate=_neighbor_summary(next_candidate),
    )


def _neighbor_summary(candidate):
    if candidate is None:
        return None
    return {
        "candidate_id": _candidate_id(candidate),
        "candidate_index": candidate.index,
        "start_time": candidate.start_time,
        "end_time": candidate.end_time,
        "duration": candidate.duration,
        "title": candidate.title,
        "summary": candidate.summary,
        "keywords": list(candidate.keywords),
    }


def _candidate_id(candidate):
    return f"candidate_{candidate.index}"


def create_topic_reviewer(config=None, request_func=None):
    """按配置创建主题评审 provider。"""
    config = config or CONFIG
    provider = str(_config_get(config, "topic_review_provider", "stepfun_chat")).strip().lower()
    if provider in {"stepfun_chat", "openai_compatible"}:
        return StepFunChatReviewer(config, request_func=request_func)
    raise ValueError(f"Unknown topic review provider: {provider}")


def _parse_review_payload(payload, requested_ids):
    if not isinstance(payload, dict) or not isinstance(payload.get("reviews"), list):
        raise ValueError("Topic review response must contain a reviews list")

    reviews = {}
    for item in payload["reviews"]:
        if not isinstance(item, dict):
            raise ValueError("Topic review item must be an object")
        candidate_id = item.get("candidate_id")
        if not candidate_id:
            raise ValueError("Topic review item missing candidate_id")
        if candidate_id not in requested_ids:
            raise ValueError(f"Topic review returned unknown candidate_id: {candidate_id}")

        missing = sorted(field for field in REQUIRED_REVIEW_FIELDS if field not in item)
        if missing:
            raise ValueError(f"Topic review item {candidate_id} missing fields: {', '.join(missing)}")

        reviews[requested_ids[candidate_id]] = TopicReviewResult(
            topic_name=str(item["topic_name"]),
            topic_complete=bool(item["topic_complete"]),
            learning_value=_bounded_int(item["learning_value"], "learning_value", 0, 10),
            share_value=_bounded_int(item["share_value"], "share_value", 0, 10),
            publish_ready_score=_bounded_int(item["publish_ready_score"], "publish_ready_score", 0, 100),
            export_decision=str(item["export_decision"]),
            title=str(item["title"]),
            summary=str(item["summary"]),
            keywords=[str(keyword) for keyword in item["keywords"]],
            needs_human_review=bool(item["needs_human_review"]),
            reject_reason=str(item["reject_reason"]),
            boundary_fix_suggestion=str(item["boundary_fix_suggestion"]),
            boundary_fix_start=_optional_float(item.get("boundary_fix_start"), "boundary_fix_start"),
            boundary_fix_end=_optional_float(item.get("boundary_fix_end"), "boundary_fix_end"),
        )

    missing_ids = [candidate_id for candidate_id in requested_ids if requested_ids[candidate_id] not in reviews]
    if missing_ids:
        raise ValueError(f"Topic review response missing candidate_id: {', '.join(missing_ids)}")
    return reviews


def _extract_chat_content(payload):
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def _resolve_topic_review_api_key(config):
    api_key = _config_get(config, "topic_review_api_key", None)
    if api_key:
        return str(api_key)
    env_name = _config_get(config, "topic_review_api_key_env", "STEPFUN_API_KEY")
    return os.environ.get(str(env_name), "")


def _resolve_topic_review_base_url(config):
    base_url = _config_get(config, "topic_review_base_url", None)
    if base_url:
        return str(base_url)
    env_name = _config_get(config, "topic_review_base_url_env", "STEPFUN_BASE_URL")
    return os.environ.get(str(env_name), "https://api.stepfun.com/v1")


def _topic_review_batch_size(config):
    raw_value = config.get("topic_review_batch_size", 1)
    try:
        batch_size = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid topic_review_batch_size: {raw_value}, must be >= 1") from exc
    if batch_size < 1:
        raise ValueError(f"Invalid topic_review_batch_size: {batch_size}, must be >= 1")
    return batch_size


def _bounded_int(value, field_name, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Topic review field {field_name} must be an integer") from exc
    return max(minimum, min(maximum, parsed))


def _optional_float(value, field_name):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Topic review field {field_name} must be a number") from exc


def _validate_https_base_url(base_url):
    parsed = urllib.parse.urlparse(str(base_url))
    if parsed.scheme.lower() != "https":
        raise ValueError("Topic review base_url must use HTTPS for credential safety")


def _config_get(config, key, default=None):
    if config is not None and key in config:
        return config[key]
    if key in CONFIG:
        return CONFIG[key]
    return default
