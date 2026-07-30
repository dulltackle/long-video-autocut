"""相邻候选批次主题评审能力。"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from video_auto_editor.cache import (
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheIdentity,
    CacheNamespace,
    CacheRepository,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    RunStage,
    get_error_definition,
)
from video_auto_editor.runtime.identity import OperationId
from video_auto_editor.text_model import (
    ObservationContext,
    PromptMessage,
    PromptRole,
    ReadinessReport,
    TextGenerationRequest,
    TextModelFailure,
    TextModelFailureKind,
    TextModelPort,
)

from ._model import (
    CandidateTopicReview,
    TopicReviewExecutionFacts,
    TopicReviewFailure,
    TopicReviewRequest,
    TopicReviewResult,
    TopicReviewSettings,
)

_REVIEW_FIELDS = frozenset(
    {
        "boundary_fix_end_ms",
        "boundary_fix_start_ms",
        "boundary_fix_suggestion",
        "candidate_key",
        "export_decision",
        "keywords",
        "learning_value",
        "needs_human_review",
        "publish_ready_score",
        "reject_reason",
        "share_value",
        "summary",
        "title",
        "topic_complete",
        "topic_name",
    }
)
_SYSTEM_PROMPT = """你负责对直播课程候选片段执行结构化主题评审。
输入中的 candidates 已按素材时间排序，并携带必要的前后文。
必须为每个 candidate_key 返回且只返回一条评审，不得合并、遗漏或新增候选。
只返回一个 JSON 对象，根对象必须且只能包含 reviews 数组。
每条评审必须包含 candidate_key、topic_name、topic_complete、
learning_value、share_value、publish_ready_score、export_decision、title、
summary、keywords、needs_human_review、reject_reason、
boundary_fix_suggestion、boundary_fix_start_ms、boundary_fix_end_ms。
learning_value 与 share_value 是 0 到 10 的整数；publish_ready_score 是
0 到 100 的整数；export_decision 只能是 publish_ready、needs_review 或 reject。
边界补救时间使用素材绝对时间轴的整数毫秒；补救范围必须完整覆盖初始范围且
至少向一侧实际扩展，此时建议不能为空；无需补救时建议为空且两个时间都返回 null。
不得返回 Markdown、解释、额外字段或候选正文之外的虚构事实。"""
_IDENTITY_SCHEMA_VERSION = "topic-review.identity.v1"
_BATCH_ALGORITHM_VERSION = "topic-review.batch.v1"
_PAYLOAD_SCHEMA_VERSION = "topic-review.payload.v1"
_PROMPT_VERSION = "topic-review.prompt.v1"
_SEMANTIC_RETRY_VERSION = "topic-review.semantic-retry.v1"
_PARSER_VERSION = "topic-review.parser.v1"
_VALIDATION_VERSION = "topic-review.validation.v1"
_SEMANTIC_RETRY_INSTRUCTION = "重新返回当前批次的完整严格 JSON，不得省略任何候选。"
_PROVIDER_ERROR_CODES = {
    TextModelFailureKind.AUTHENTICATION_FAILED: (
        ErrorCode.TOPIC_REVIEW_AUTHENTICATION_FAILED
    ),
    TextModelFailureKind.REQUEST_REJECTED: ErrorCode.TOPIC_REVIEW_REQUEST_REJECTED,
    TextModelFailureKind.RATE_LIMITED: ErrorCode.TOPIC_REVIEW_RATE_LIMITED,
    TextModelFailureKind.REQUEST_TIMEOUT: ErrorCode.TOPIC_REVIEW_REQUEST_TIMEOUT,
    TextModelFailureKind.SERVICE_UNAVAILABLE: (
        ErrorCode.TOPIC_REVIEW_SERVICE_UNAVAILABLE
    ),
    TextModelFailureKind.RESPONSE_PROTOCOL_INVALID: (
        ErrorCode.TOPIC_REVIEW_RESPONSE_PROTOCOL_INVALID
    ),
    TextModelFailureKind.GENERATION_REFUSED: (
        ErrorCode.TOPIC_REVIEW_GENERATION_REFUSED
    ),
    TextModelFailureKind.OUTPUT_TRUNCATED: ErrorCode.TOPIC_REVIEW_OUTPUT_TRUNCATED,
}


class _InvalidOutput(ValueError):
    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("主题评审模型输出未通过业务校验")


class _DuplicateJsonField(ValueError):
    pass


@dataclass(slots=True)
class _ExecutionLedger:
    batch_count: int
    model_request_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    semantic_retry_count: int = 0
    transport_attempt_count: int = 0
    transport_retry_count: int = 0

    def snapshot(self) -> TopicReviewExecutionFacts:
        return TopicReviewExecutionFacts(
            batch_count=self.batch_count,
            model_request_count=self.model_request_count,
            cache_hit_count=self.cache_hit_count,
            cache_miss_count=self.cache_miss_count,
            semantic_retry_count=self.semantic_retry_count,
            transport_attempt_count=self.transport_attempt_count,
            transport_retry_count=self.transport_retry_count,
        )


class TopicReview:
    """隐藏批次、提示、校验、语义重试和处理缓存。"""

    __slots__ = ("_cache", "_model", "_settings")

    def __init__(
        self,
        text_model: TextModelPort,
        cache_repository: CacheRepository,
        settings: TopicReviewSettings,
    ) -> None:
        if not callable(getattr(text_model, "check_readiness", None)) or not callable(
            getattr(text_model, "generate", None)
        ):
            raise TypeError("主题评审必须使用 TextModelPort")
        if not isinstance(cache_repository, CacheRepository):
            raise TypeError("主题评审必须使用处理缓存仓库")
        if not isinstance(settings, TopicReviewSettings):
            raise TypeError("主题评审必须使用 TopicReviewSettings")
        self._model = text_model
        self._cache = cache_repository
        self._settings = settings

    def check_readiness(self) -> ReadinessReport:
        """执行只读、本地且不发起模型生成的准备检查。"""
        report = self._model.check_readiness()
        if not isinstance(report, ReadinessReport):
            raise TypeError("文本模型准备检查必须返回 ReadinessReport")
        return report

    def review(self, request: TopicReviewRequest) -> TopicReviewResult:
        """同步返回全部候选的结构化评审。"""
        if not isinstance(request, TopicReviewRequest):
            raise TypeError("主题评审只接受 TopicReviewRequest")
        request.cancellation.raise_if_cancelled()
        candidates = request.candidate_plan.candidates
        if not candidates:
            return TopicReviewResult(
                reviews=(),
                execution_facts=TopicReviewExecutionFacts(
                    batch_count=0,
                    model_request_count=0,
                ),
            )

        batch_size = self._settings.candidate_batch_size
        batches = tuple(
            candidates[index : index + batch_size]
            for index in range(0, len(candidates), batch_size)
        )
        ledger = _ExecutionLedger(batch_count=len(batches))
        configuration_fingerprint = self.check_readiness().configuration_fingerprint
        reviews: list[CandidateTopicReview] = []
        for batch in batches:
            candidates_by_key = {
                f"candidate_{index}": candidate
                for index, candidate in enumerate(batch, start=1)
            }
            payload = _batch_payload(request.candidate_plan, batch)
            spec = _cache_spec(
                payload,
                candidates_by_key,
                request.candidate_plan,
                self._settings,
                configuration_fingerprint,
            )

            def compute_batch(
                batch_candidates=candidates_by_key,
                batch_payload=payload,
            ) -> tuple[CandidateTopicReview, ...]:
                ledger.cache_miss_count += 1
                return self._generate_batch(
                    request,
                    batch_candidates,
                    batch_payload,
                    ledger,
                )

            resolution = self._cache.resolve(
                spec,
                cancellation=request.cancellation,
                compute=compute_batch,
            )
            if resolution.from_cache:
                ledger.cache_hit_count += 1
            reviews.extend(resolution.value)
        return TopicReviewResult(
            reviews=tuple(reviews),
            execution_facts=ledger.snapshot(),
        )

    def _generate_batch(
        self,
        request: TopicReviewRequest,
        candidates_by_key,
        payload: dict[str, object],
        ledger: _ExecutionLedger,
    ) -> tuple[CandidateTopicReview, ...]:
        base_messages = (
            PromptMessage(
                role=PromptRole.SYSTEM,
                content=_SYSTEM_PROMPT,
            ),
            PromptMessage(
                role=PromptRole.USER,
                content=_canonical_json(payload),
            ),
        )
        invalid: _InvalidOutput | None = None
        for attempt in range(
            1,
            self._settings.semantic_attempt_limit + 1,
        ):
            request.cancellation.raise_if_cancelled()
            messages: tuple[PromptMessage, ...] = base_messages
            if invalid is not None:
                messages += (
                    PromptMessage(
                        role=PromptRole.USER,
                        content=_canonical_json(
                            _semantic_retry_payload(
                                attempt,
                                invalid.reason_code,
                            )
                        ),
                    ),
                )
            try:
                response = self._model.generate(
                    TextGenerationRequest(
                        messages=messages,
                        settings=self._settings.generation,
                        observation=ObservationContext(
                            run_id=request.run_id,
                            stage=RunStage.TOPIC_REVIEW,
                            operation_id=OperationId.new(),
                        ),
                        cancellation=request.cancellation,
                    )
                )
            except TextModelFailure as failure:
                ledger.model_request_count += 1
                ledger.transport_attempt_count += (
                    failure.execution_facts.transport_attempt_count
                )
                ledger.transport_retry_count += (
                    failure.execution_facts.transport_retry_count
                )
                if failure.kind is TextModelFailureKind.CANCELLED:
                    request.cancellation.raise_if_cancelled()
                    raise
                error_code = _PROVIDER_ERROR_CODES.get(failure.kind)
                if error_code is None:
                    raise
                raise TopicReviewFailure(
                    error_code,
                    execution_facts=ledger.snapshot(),
                    diagnostics=_compatible_failure_diagnostics(
                        error_code,
                        failure.diagnostics,
                    ),
                ) from None
            ledger.model_request_count += 1
            ledger.transport_attempt_count += (
                response.execution_facts.transport_attempt_count
            )
            ledger.transport_retry_count += (
                response.execution_facts.transport_retry_count
            )
            try:
                return _parse_response(
                    response.text,
                    candidates_by_key,
                    request.candidate_plan,
                )
            except _InvalidOutput as failure:
                invalid = failure
                if attempt < self._settings.semantic_attempt_limit:
                    ledger.semantic_retry_count += 1
                    continue
                raise TopicReviewFailure(
                    ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID,
                    execution_facts=ledger.snapshot(),
                    diagnostics={
                        "attempt": attempt,
                        "reason_code": failure.reason_code,
                    },
                ) from None
        raise AssertionError("主题评审语义尝试循环必须产生终态")


def _compatible_failure_diagnostics(
    error_code: ErrorCode,
    diagnostics: Mapping[str, object],
) -> Mapping[str, object]:
    definition = get_error_definition(error_code)
    value_choices = definition.diagnostic_value_choices
    return {
        key: value
        for key, value in diagnostics.items()
        if key in definition.allowed_diagnostics
        and (key not in value_choices or value in value_choices[key])
    }


def _semantic_retry_payload(
    attempt: int,
    reason_code: str,
) -> dict[str, object]:
    return {
        "failed_attempt": attempt - 1,
        "instruction": _SEMANTIC_RETRY_INSTRUCTION,
        "reason_code": reason_code,
        "semantic_attempt": attempt,
    }


def _cache_spec(
    payload,
    candidates_by_key,
    candidate_plan,
    settings: TopicReviewSettings,
    configuration_fingerprint: str,
) -> CacheEntrySpec[tuple[CandidateTopicReview, ...]]:
    generation = settings.generation
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TOPIC_REVIEW,
        identity_schema_version=_IDENTITY_SCHEMA_VERSION,
        algorithm_version=_BATCH_ALGORITHM_VERSION,
        payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
        adapter_id=settings.adapter_id,
        model_id=generation.model,
        configuration_fingerprint=configuration_fingerprint,
        result_inputs={
            "candidate_batch": payload["candidates"],
            "course_context": payload["course_context"],
            "review_constraints": payload["review_constraints"],
            "batch_algorithm_version": _BATCH_ALGORITHM_VERSION,
            "candidate_batch_size": settings.candidate_batch_size,
            "prompt_sha256": _prompt_digest(payload),
            "prompt_version": _PROMPT_VERSION,
            "request_schema_version": payload["schema_version"],
            "semantic_attempt_limit": settings.semantic_attempt_limit,
            "semantic_retry_version": _SEMANTIC_RETRY_VERSION,
            "model_settings": {
                "max_output_tokens": generation.max_output_tokens,
                "reasoning_effort": generation.reasoning_effort.value,
                "temperature": generation.temperature,
            },
            "parser_version": _PARSER_VERSION,
            "validation_version": _VALIDATION_VERSION,
        },
    )
    return CacheEntrySpec(
        identity=identity,
        encode=_encode_cached_reviews,
        decode=lambda cached: _decode_cached_reviews(
            cached,
            candidates_by_key,
            candidate_plan,
        ),
    )


def _prompt_digest(payload) -> str:
    prompt_contract = _canonical_json(
        {
            "semantic_retry_example": _semantic_retry_payload(
                2,
                "output.structure_invalid",
            ),
            "system": _SYSTEM_PROMPT,
            "user": _canonical_json(payload),
        }
    ).encode("utf-8")
    return hashlib.sha256(prompt_contract).hexdigest()


def _encode_cached_reviews(
    reviews: tuple[CandidateTopicReview, ...],
) -> object:
    return {
        "reviews": [
            {
                "boundary_fix_end_ms": review.boundary_fix_end_ms,
                "boundary_fix_start_ms": review.boundary_fix_start_ms,
                "boundary_fix_suggestion": review.boundary_fix_suggestion,
                "candidate_key": f"candidate_{index}",
                "export_decision": review.export_decision,
                "keywords": list(review.keywords),
                "learning_value": review.learning_value,
                "needs_human_review": review.needs_human_review,
                "publish_ready_score": review.publish_ready_score,
                "reject_reason": review.reject_reason,
                "share_value": review.share_value,
                "summary": review.summary,
                "title": review.title,
                "topic_complete": review.topic_complete,
                "topic_name": review.topic_name,
            }
            for index, review in enumerate(reviews, start=1)
        ]
    }


def _decode_cached_reviews(
    payload,
    candidates_by_key,
    candidate_plan,
) -> tuple[CandidateTopicReview, ...]:
    try:
        return _parse_payload(
            payload,
            candidates_by_key,
            candidate_plan,
        )
    except _InvalidOutput:
        raise CachedPayloadInvalid() from None


def _batch_payload(candidate_plan, batch) -> dict[str, object]:
    context = candidate_plan.course_context
    return {
        "schema_version": "topic-review-request.v1",
        "course_context": (
            {}
            if context is None
            else {
                "attribution": context.attribution,
                "course_topic": context.course_topic,
                "excluded_content": list(context.excluded_content),
            }
        ),
        "review_constraints": {
            "max_duration_ms": (candidate_plan.clip_policy.max_duration_seconds * 1000),
            "min_duration_ms": (candidate_plan.clip_policy.min_duration_seconds * 1000),
            "publish_ready_threshold": (
                candidate_plan.clip_policy.publish_ready_threshold
            ),
            "source_duration_ms": candidate_plan.source_duration_ms,
            "target_duration_ms": (
                candidate_plan.clip_policy.target_duration_seconds * 1000
            ),
        },
        "candidates": [
            {
                "candidate_key": f"candidate_{index}",
                "initial_start_ms": candidate.initial_start_ms,
                "initial_end_ms": candidate.initial_end_ms,
                "preceding": [
                    _excerpt_payload(excerpt)
                    for excerpt in candidate.review_context.preceding_chunks
                ],
                "content": [
                    _excerpt_payload(excerpt)
                    for excerpt in candidate.review_context.candidate_chunks
                ],
                "following": [
                    _excerpt_payload(excerpt)
                    for excerpt in candidate.review_context.following_chunks
                ],
            }
            for index, candidate in enumerate(batch, start=1)
        ],
    }


def _excerpt_payload(excerpt) -> dict[str, object]:
    return {
        "start_ms": excerpt.start_ms,
        "end_ms": excerpt.end_ms,
        "text": excerpt.text,
    }


def _parse_response(
    text,
    candidates_by_key,
    candidate_plan,
) -> tuple[CandidateTopicReview, ...]:
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_non_finite_json,
        )
    except (
        _DuplicateJsonField,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise _InvalidOutput("output.structure_invalid") from None
    return _parse_payload(
        payload,
        candidates_by_key,
        candidate_plan,
    )


def _parse_payload(
    payload,
    candidates_by_key,
    candidate_plan,
) -> tuple[CandidateTopicReview, ...]:
    if not isinstance(payload, dict) or set(payload) != {"reviews"}:
        raise _InvalidOutput("output.structure_invalid")
    items = payload["reviews"]
    if not isinstance(items, list):
        raise _InvalidOutput("output.structure_invalid")
    parsed = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != _REVIEW_FIELDS:
            raise _InvalidOutput("output.structure_invalid")
        key = item["candidate_key"]
        if not isinstance(key, str):
            raise _InvalidOutput("output.structure_invalid")
        if key not in candidates_by_key:
            raise _InvalidOutput("output.candidate_unknown")
        if key in parsed:
            raise _InvalidOutput("output.candidate_duplicate")
        keywords = item["keywords"]
        if not isinstance(keywords, list):
            raise _InvalidOutput("output.structure_invalid")
        for field_name, maximum in (
            ("learning_value", 10),
            ("share_value", 10),
            ("publish_ready_score", 100),
        ):
            score = item[field_name]
            if (
                not isinstance(score, int)
                or isinstance(score, bool)
                or not 0 <= score <= maximum
            ):
                raise _InvalidOutput("output.score_invalid")
        _validate_boundary(
            item,
            candidates_by_key[key],
            candidate_plan,
        )
        _validate_review_constraints(item, candidate_plan)
        try:
            parsed[key] = CandidateTopicReview(
                candidate_id=candidates_by_key[key].candidate_id,
                topic_name=item["topic_name"],
                topic_complete=item["topic_complete"],
                learning_value=item["learning_value"],
                share_value=item["share_value"],
                publish_ready_score=item["publish_ready_score"],
                export_decision=item["export_decision"],
                title=item["title"],
                summary=item["summary"],
                keywords=tuple(keywords),
                needs_human_review=item["needs_human_review"],
                reject_reason=item["reject_reason"],
                boundary_fix_suggestion=item["boundary_fix_suggestion"],
                boundary_fix_start_ms=item["boundary_fix_start_ms"],
                boundary_fix_end_ms=item["boundary_fix_end_ms"],
            )
        except (TypeError, ValueError):
            raise _InvalidOutput("output.constraint_failed") from None
    if set(parsed) != set(candidates_by_key):
        raise _InvalidOutput("output.candidate_missing")
    return tuple(parsed[key] for key in candidates_by_key)


def _strict_json_object(pairs) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonField(key)
        value[key] = item
    return value


def _reject_non_finite_json(_value: str) -> None:
    raise ValueError("非有限数值不属于 JSON")


def _validate_boundary(item, candidate, candidate_plan) -> None:
    start = item["boundary_fix_start_ms"]
    end = item["boundary_fix_end_ms"]
    suggestion = item["boundary_fix_suggestion"]
    if (start is None) != (end is None):
        raise _InvalidOutput("output.boundary_invalid")
    if start is None:
        if not isinstance(suggestion, str) or suggestion.strip():
            raise _InvalidOutput("output.boundary_invalid")
        return
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
    ):
        raise _InvalidOutput("output.boundary_invalid")
    duration = end - start
    minimum = candidate_plan.clip_policy.min_duration_seconds * 1000
    maximum = candidate_plan.clip_policy.max_duration_seconds * 1000
    if (
        start < 0
        or end > candidate_plan.source_duration_ms
        or start >= end
        or start > candidate.initial_start_ms
        or end < candidate.initial_end_ms
        or (start == candidate.initial_start_ms and end == candidate.initial_end_ms)
        or not minimum <= duration <= maximum
    ):
        raise _InvalidOutput("output.boundary_invalid")
    if not isinstance(suggestion, str) or not suggestion.strip():
        raise _InvalidOutput("output.boundary_invalid")


def _validate_review_constraints(item, candidate_plan) -> None:
    decision = item["export_decision"]
    if decision == "publish_ready" and (
        item["topic_complete"] is not True
        or item["needs_human_review"] is not False
        or item["publish_ready_score"]
        < candidate_plan.clip_policy.publish_ready_threshold
        or not isinstance(item["reject_reason"], str)
        or bool(item["reject_reason"].strip())
    ):
        raise _InvalidOutput("output.constraint_failed")
    if decision == "needs_review" and item["needs_human_review"] is not True:
        raise _InvalidOutput("output.constraint_failed")
    if decision == "reject" and (
        not isinstance(item["reject_reason"], str) or not item["reject_reason"].strip()
    ):
        raise _InvalidOutput("output.constraint_failed")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["TopicReview"]
