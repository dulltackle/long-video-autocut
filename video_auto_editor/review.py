"""直播主题评审输入构造与 provider 封装。"""

import hashlib
import json
import os
import time
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

TOPIC_REVIEW_CACHE_SCHEMA_VERSION = "topic_review_cache_v1"
TOPIC_REVIEW_PROMPT_VERSION = "stepfun_chat_topic_review_v1"


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
    min_clip_duration: float = 0.0
    max_clip_duration: float = 0.0

    def to_payload(self):
        return {
            "batch_index": self.batch_index,
            "course_context_summary": dict(self.course_context_summary),
            "min_clip_duration": self.min_clip_duration,
            "max_clip_duration": self.max_clip_duration,
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }


@dataclass
class TopicReviewProviderResult:
    """主题评审 provider 执行结果。"""

    success: bool
    reviews: dict[int, TopicReviewResult] = field(default_factory=dict)
    error: str = ""
    provider_info: dict = field(default_factory=dict)
    failed_batches: list = field(default_factory=list)


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
        self.retry_attempts = _positive_int_config(self.config, "topic_review_retry_attempts", 1)
        self.concurrency = _positive_int_config(self.config, "topic_review_concurrency", 1)
        self.retry_backoff_seconds = _non_negative_float_config(
            self.config,
            "topic_review_retry_backoff_seconds",
            0.0,
        )
        self.temperature = float(_config_get(self.config, "topic_review_temperature", 0.2))
        self.reasoning_effort = str(_config_get(self.config, "topic_review_reasoning_effort", "")).strip()
        self.provider_name = str(_config_get(self.config, "topic_review_provider", "stepfun_chat"))
        self.cache_dir = str(_config_get(self.config, "topic_review_cache_dir", "") or "")
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

        if self.concurrency > 1 and len(batches) > 1:
            return self._review_batches_concurrent(batches, provider_info)
        return self._review_batches_sequential(batches, provider_info)

    def _review_batches_sequential(self, batches, provider_info):
        """串行评审：遇到首个失败批次即返回，保留此前成功结果。"""
        reviews = {}
        for batch in batches:
            result = self._review_batch(batch)
            if not result.success:
                result.provider_info = provider_info
                result.reviews = dict(reviews)
                return result
            reviews.update(result.reviews)
        return TopicReviewProviderResult(True, reviews=reviews, provider_info=provider_info)

    def _review_batches_concurrent(self, batches, provider_info):
        """并发评审：各批次互相独立，I/O 并行后聚合结果与全部失败诊断。

        批次之间无共享状态（缓存按 signature 写入各自文件、请求各自独立），
        因此可安全并行。与串行路径相比，失败时不会短路，而是跑完全部批次，
        保留所有成功评审并汇总全部失败批次，便于一次性定位问题。
        """
        from concurrent.futures import ThreadPoolExecutor

        workers = min(self.concurrency, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(self._review_batch, batches))

        reviews = {}
        failed_batches = []
        errors = []
        for result in results:
            reviews.update(result.reviews)
            if not result.success:
                failed_batches.extend(result.failed_batches)
                errors.append(result.error)

        if failed_batches:
            return TopicReviewProviderResult(
                False,
                reviews=reviews,
                error="; ".join(errors),
                provider_info=provider_info,
                failed_batches=failed_batches,
            )
        return TopicReviewProviderResult(True, reviews=reviews, provider_info=provider_info)

    def _review_batch(self, batch):
        requested_ids = {
            candidate.candidate_id: candidate.candidate_index
            for candidate in batch.candidates
        }
        cached = self._read_cached_batch(batch, requested_ids)
        if cached is not None:
            return cached

        # 把"请求 + 解析"作为一个可重试单元：网络/可重试 HTTP 失败按退避重试；
        # 200 响应但结构不合规（invalid_topic_json / invalid_schema）立即带强约束
        # 重发（服务端健康，无需退避）。其余失败（invalid_config / invalid_chat_* /
        # unknown_candidate）不重试，立即落败，交由上层部分降级兜底。
        last_failure = None
        for attempt in range(1, self.retry_attempts + 1):
            outcome = self._attempt_review(batch, requested_ids, attempt)
            if outcome.success:
                self._write_cached_batch(batch, requested_ids, outcome.reviews)
                return outcome

            last_failure = outcome
            if attempt >= self.retry_attempts:
                break
            failure_type = _failed_batch_type(outcome)
            if _is_network_retryable_failure(failure_type):
                self._sleep_before_retry(attempt)
                continue
            if failure_type in _SCHEMA_RETRYABLE_FAILURES:
                continue
            break

        return last_failure

    def _attempt_review(self, batch, requested_ids, attempt):
        request_result = self._request_batch_once(batch, attempt)
        if not request_result.success:
            return request_result
        raw_response = request_result.provider_info["raw_response"]
        return self._parse_batch_response(batch, raw_response, requested_ids, attempt)

    def _request_batch_once(self, batch, attempt):
        try:
            request = self._build_request(batch, attempt)
            with self.request_func(request, timeout=self.timeout) as response:
                raw_response = response.read().decode("utf-8")
            return TopicReviewProviderResult(True, provider_info={"raw_response": raw_response, "attempt": attempt})
        except ValueError as exc:
            return self._batch_failure(batch, attempt, "invalid_config", str(exc))
        except urllib.error.HTTPError as exc:
            return self._batch_failure(batch, attempt, f"http_{exc.code}", f"Topic review HTTP error: {exc.code}")
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            return self._batch_failure(batch, attempt, _request_failure_type(exc), f"Topic review request failed: {exc}")
        except Exception as exc:
            return self._batch_failure(batch, attempt, "request_error", f"Topic review request failed: {exc}")

    def _parse_batch_response(self, batch, raw_response, requested_ids, attempt):
        try:
            chat_payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            return self._batch_failure(batch, attempt, "invalid_chat_json", f"Invalid Chat Completions JSON: {exc.msg}")

        content = _extract_chat_content(chat_payload)
        if content is None:
            return self._batch_failure(
                batch,
                attempt,
                "invalid_chat_response",
                "Chat Completions response missing message content",
            )

        try:
            review_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            return self._batch_failure(batch, attempt, "invalid_topic_json", f"Invalid topic review JSON: {exc.msg}")

        try:
            reviews = _parse_review_payload(review_payload, requested_ids)
        except ValueError as exc:
            return self._batch_failure(batch, attempt, _schema_failure_type(str(exc)), str(exc))
        return TopicReviewProviderResult(True, reviews=reviews)

    def _sleep_before_retry(self, failed_attempt):
        delay = self.retry_backoff_seconds * failed_attempt
        if delay > 0:
            time.sleep(delay)

    def _batch_failure(self, batch, attempt, failure_type, message):
        failed_batch = {
            "batch_index": batch.batch_index,
            "candidate_range": _batch_candidate_range(batch),
            "attempt": attempt,
            "max_attempts": self.retry_attempts,
            "failure_type": failure_type,
            "error": message,
        }
        return TopicReviewProviderResult(
            False,
            error=(
                f"{message} "
                f"[batch_index={batch.batch_index} "
                f"candidate_range={_batch_candidate_range(batch)} "
                f"attempt={attempt}/{self.retry_attempts} "
                f"failure_type={failure_type}]"
            ),
            failed_batches=[failed_batch],
        )

    def _read_cached_batch(self, batch, requested_ids):
        cache_path = self._cache_path(batch)
        if not cache_path or not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if payload.get("schema_version") != TOPIC_REVIEW_CACHE_SCHEMA_VERSION:
            return None
        if payload.get("signature") != self._cache_signature(batch):
            return None
        try:
            reviews = _parse_review_payload({"reviews": payload.get("reviews")}, requested_ids)
        except ValueError:
            return None
        return TopicReviewProviderResult(True, reviews=reviews, provider_info={"cache_hit": True})

    def _write_cached_batch(self, batch, requested_ids, reviews):
        cache_path = self._cache_path(batch)
        if not cache_path:
            return
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            payload = {
                "schema_version": TOPIC_REVIEW_CACHE_SCHEMA_VERSION,
                "signature": self._cache_signature(batch),
                "reviews": [
                    _review_result_cache_item(candidate_id, reviews[candidate_index])
                    for candidate_id, candidate_index in requested_ids.items()
                    if candidate_index in reviews
                ],
            }
            if len(payload["reviews"]) != len(requested_ids):
                return
            with open(cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(payload, cache_file, ensure_ascii=False, indent=2, sort_keys=True)
                cache_file.write("\n")
        except OSError:
            return

    def _cache_path(self, batch):
        if not self.cache_dir:
            return ""
        return os.path.join(self.cache_dir, f"{self._cache_signature(batch)}.json")

    def _cache_signature(self, batch):
        payload = {
            "schema_version": TOPIC_REVIEW_CACHE_SCHEMA_VERSION,
            "prompt_version": TOPIC_REVIEW_PROMPT_VERSION,
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
            "required_review_fields": sorted(REQUIRED_REVIEW_FIELDS),
            "batch": batch.to_payload(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _build_request(self, batch, attempt=1):
        _validate_https_base_url(self.base_url)
        endpoint = f"{self.base_url.rstrip('/')}/chat/completions"
        messages = [
                {
                    "role": "system",
                    "content": (
                        "你是直播课程短视频主题评审器。输入 candidates 是按时间排序的候选片段，"
                        "你要为输入里的每一个候选各产出一条评审，并只返回一个 JSON object，"
                        "结构为 {\"reviews\": [<逐个候选的评审对象>]}。\n"
                        "reviews 必须是数组，数组里的每个元素都必须是对象（不能是字符串），"
                        "且与输入候选一一对应。每个评审对象必须且只能包含以下字段：\n"
                        "- candidate_id: 字符串，原样回填对应候选的 candidate_id；\n"
                        "- topic_name: 字符串，该片段的主题名；\n"
                        "- topic_complete: 布尔，主题是否自成完整一段；\n"
                        "- learning_value: 整数 0-10，学习价值；\n"
                        "- share_value: 整数 0-10，传播价值；\n"
                        "- publish_ready_score: 整数 0-100，发布就绪综合分；\n"
                        "- export_decision: 字符串，取值 publish_ready / needs_review / reject 之一；\n"
                        "- title: 字符串，建议的短视频标题；\n"
                        "- summary: 字符串，一句话内容摘要；\n"
                        "- keywords: 字符串数组，关键词；\n"
                        "- needs_human_review: 布尔，是否需要人工复核；\n"
                        "- reject_reason: 字符串，若不建议发布则给出原因，否则空字符串；\n"
                        "- boundary_fix_suggestion: 字符串，边界修正建议，无则空字符串；\n"
                        "可选字段 boundary_fix_start / boundary_fix_end 为数字，表示建议裁剪边界，"
                        "必须是与输入候选一致的绝对时间轴秒，且补救窗口需与原候选片段 [start_time, end_time] 重叠"
                        "（可向相邻内容或静音边界小幅扩展），"
                        "补救后时长须落在输入 batch 的 min_clip_duration~max_clip_duration 之间；"
                        "不确定或无需修正时请省略这两个字段。\n"
                        "不要输出除该 JSON object 以外的任何文字。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(batch.to_payload(), ensure_ascii=False, sort_keys=True),
                },
        ]
        if attempt > 1:
            messages.append({"role": "user", "content": _SCHEMA_RETRY_REMINDER})
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
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
    min_clip_duration = float(config.get("min_clip_duration", CONFIG["min_clip_duration"]))
    max_clip_duration = float(config.get("max_clip_duration", CONFIG["max_clip_duration"]))
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
            min_clip_duration=min_clip_duration,
            max_clip_duration=max_clip_duration,
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


_REVIEW_LIST_ALIASES = ("reviews", "candidates", "results", "data")


def _coerce_review_items(payload):
    """兼容评审模型的多种返回形态，统一成评审对象列表。

    既接受规范的 {"reviews": [...]}，也接受 candidates/results/data 等常见别名键
    包裹的数组（仅当其值为 list 时），以及裸数组 [...] 和裸单个评审对象
    {"candidate_id": ...}，以适配未严格遵循 schema 的 Chat 模型。别名仅放宽
    "数组放在哪个键下"，每个评审对象仍走 _parse_review_payload 的完整字段与
    candidate_id 校验，不掩盖契约违例。

    部分模型偶发把首个键名写坏（candidate_id 键被破坏），但其余必填评审字段
    完整。只要对象包含全部 REQUIRED_REVIEW_FIELDS，仍按裸单个评审对象处理，
    candidate_id 留待 _parse_review_payload 在单候选批次中按上下文回填。
    """
    if isinstance(payload, dict):
        for alias in _REVIEW_LIST_ALIASES:
            if isinstance(payload.get(alias), list):
                return payload[alias]
        if "candidate_id" in payload:
            return [payload]
        if REQUIRED_REVIEW_FIELDS.issubset(payload.keys()):
            return [payload]
    elif isinstance(payload, list):
        return payload
    raise ValueError("Topic review response must contain a reviews list")


def _parse_review_payload(payload, requested_ids):
    items = _coerce_review_items(payload)

    # 仅当模型把单候选评审的 candidate_id 键写坏（裸对象、无 reviews 包装、缺 candidate_id
    # 键但含全部必填字段）时，按单候选批次上下文回填 candidate_id；其余情况下 candidate_id
    # 缺失或不匹配仍视为错误，避免掩盖真正的契约违例。
    recover_single = (
        len(requested_ids) == 1
        and len(items) == 1
        and isinstance(payload, dict)
        and "reviews" not in payload
        and "candidate_id" not in payload
        and REQUIRED_REVIEW_FIELDS.issubset(payload.keys())
    )
    single_requested = next(iter(requested_ids)) if recover_single else None
    reviews = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Topic review item must be an object")
        candidate_id = item.get("candidate_id")
        if single_requested is not None and (not candidate_id or candidate_id not in requested_ids):
            # 单候选批次只可能对应这一个候选，回填被模型写坏的 candidate_id。
            candidate_id = single_requested
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


def _is_retryable_http_status(status_code):
    return int(status_code) in {429, 500, 502, 503, 504}


# 200 响应但模型输出结构不合规的失败类型：可在重试余额内带强约束重发（无需退避）。
_SCHEMA_RETRYABLE_FAILURES = ("invalid_topic_json", "invalid_schema")

# 重试时追加的强约束提示，仅在 attempt>1 时附加为额外 user 消息，不影响首次请求体与缓存签名。
_SCHEMA_RETRY_REMINDER = (
    "上一次返回的结构不合规，已被拒绝。本次必须严格只返回一个 JSON object，"
    "结构为 {\"reviews\": [<逐个候选的评审对象>]}：reviews 是数组，每个候选一条且与输入"
    "candidates 一一对应，candidate_id 原样回填。禁止输出该 JSON object 以外的任何文字、"
    "解释或 Markdown 代码块。"
)


def _failed_batch_type(result):
    failed_batches = getattr(result, "failed_batches", None) or []
    if failed_batches:
        return failed_batches[0].get("failure_type", "")
    return ""


def _is_network_retryable_failure(failure_type):
    if failure_type in {"timeout", "url_error", "os_error"}:
        return True
    if failure_type.startswith("http_"):
        try:
            return _is_retryable_http_status(int(failure_type[len("http_"):]))
        except ValueError:
            return False
    return False


def _batch_candidate_range(batch):
    candidate_ids = [candidate.candidate_id for candidate in batch.candidates]
    if not candidate_ids:
        return "none"
    if len(candidate_ids) == 1:
        return candidate_ids[0]
    return f"{candidate_ids[0]}-{candidate_ids[-1]}"


def _request_failure_type(exc):
    text = str(exc).lower()
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in text:
        return "timeout"
    if isinstance(exc, urllib.error.URLError):
        return "url_error"
    if isinstance(exc, OSError):
        return "os_error"
    return "request_error"


def _schema_failure_type(message):
    if "unknown candidate_id" in message:
        return "unknown_candidate"
    return "invalid_schema"


def _review_result_cache_item(candidate_id, review):
    return {
        "candidate_id": candidate_id,
        "topic_name": review.topic_name,
        "topic_complete": review.topic_complete,
        "learning_value": review.learning_value,
        "share_value": review.share_value,
        "publish_ready_score": review.publish_ready_score,
        "export_decision": review.export_decision,
        "title": review.title,
        "summary": review.summary,
        "keywords": list(review.keywords),
        "needs_human_review": review.needs_human_review,
        "reject_reason": review.reject_reason,
        "boundary_fix_suggestion": review.boundary_fix_suggestion,
        "boundary_fix_start": review.boundary_fix_start,
        "boundary_fix_end": review.boundary_fix_end,
    }


def _positive_int_config(config, key, default):
    raw_value = _config_get(config, key, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return int(default)
    return max(1, value)


def _non_negative_float_config(config, key, default):
    raw_value = _config_get(config, key, default)
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, value)


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
