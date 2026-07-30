"""TopicReview 深模块拥有的不可变输入与结果。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from video_auto_editor.clip_planning import CandidatePlan
from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.runtime.identity import CandidateId, RunId
from video_auto_editor.text_model import GenerationSettings


@dataclass(frozen=True, slots=True)
class TopicReviewSettings:
    """组合根交给主题评审的稳定模型与 Adapter 设置。"""

    adapter_id: str
    generation: GenerationSettings
    candidate_batch_size: int = 3
    semantic_attempt_limit: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not self.adapter_id:
            raise ValueError("主题评审 Adapter 标识不能为空")
        if not isinstance(self.generation, GenerationSettings):
            raise TypeError("主题评审模型设置必须使用 GenerationSettings")
        if (
            not isinstance(self.candidate_batch_size, int)
            or isinstance(self.candidate_batch_size, bool)
            or not 1 <= self.candidate_batch_size <= 32
        ):
            raise ValueError("相邻候选批次大小必须位于 1 到 32")
        if (
            not isinstance(self.semantic_attempt_limit, int)
            or isinstance(self.semantic_attempt_limit, bool)
            or not 1 <= self.semantic_attempt_limit <= 8
        ):
            raise ValueError("主题评审语义尝试次数必须位于 1 到 8")


@dataclass(frozen=True, slots=True)
class TopicReviewRequest:
    """一次主题评审所需的完整候选方案和运行机制。"""

    candidate_plan: CandidatePlan
    run_id: RunId
    cancellation: CancellationToken = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_plan, CandidatePlan):
            raise TypeError("主题评审只接受 ClipPlanning 签发的候选方案")
        if not isinstance(self.run_id, RunId):
            raise TypeError("主题评审必须绑定 RunId")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("主题评审必须绑定根取消令牌")


@dataclass(frozen=True, slots=True)
class TopicReviewExecutionFacts:
    """编排层可观察的主题评审聚合执行事实。"""

    batch_count: int
    model_request_count: int
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    semantic_retry_count: int = 0
    transport_attempt_count: int = 0
    transport_retry_count: int = 0

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.batch_count, "相邻候选批次数"),
            (self.model_request_count, "模型请求数"),
            (self.cache_hit_count, "缓存命中数"),
            (self.cache_miss_count, "缓存未命中数"),
            (self.semantic_retry_count, "语义重试数"),
            (self.transport_attempt_count, "传输尝试数"),
            (self.transport_retry_count, "传输重试数"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if value < 0:
                raise ValueError(f"{field_name}不能为负数")
        if self.semantic_retry_count > self.model_request_count:
            raise ValueError("语义重试数不能超过模型请求数")
        if self.transport_retry_count > self.transport_attempt_count:
            raise ValueError("传输重试数不能超过传输尝试数")
        if self.cache_hit_count + self.cache_miss_count > self.batch_count:
            raise ValueError("缓存解析批次数不能超过计划批次数")


@dataclass(frozen=True, slots=True)
class CandidateTopicReview:
    """单个候选通过结构和业务校验后的主题评审。"""

    candidate_id: CandidateId
    topic_name: str = field(repr=False)
    topic_complete: bool
    learning_value: int
    share_value: int
    publish_ready_score: int
    export_decision: str
    title: str = field(repr=False)
    summary: str = field(repr=False)
    keywords: tuple[str, ...] = field(repr=False)
    needs_human_review: bool
    reject_reason: str = field(repr=False)
    boundary_fix_suggestion: str = field(repr=False)
    boundary_fix_start_ms: int | None
    boundary_fix_end_ms: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, CandidateId):
            raise TypeError("候选评审必须使用 CandidateId")
        for required_text, field_name in (
            (self.topic_name, "主题名"),
            (self.title, "短视频标题"),
            (self.summary, "内容摘要"),
        ):
            if not isinstance(required_text, str) or not required_text.strip():
                raise ValueError(f"{field_name}不能为空")
            _require_valid_unicode(required_text, field_name)
        for boolean_value, field_name in (
            (self.topic_complete, "主题完整性"),
            (self.needs_human_review, "人工复核标记"),
        ):
            if not isinstance(boolean_value, bool):
                raise TypeError(f"{field_name}必须是布尔值")
        for score_value, field_name, maximum in (
            (self.learning_value, "学习价值", 10),
            (self.share_value, "传播价值", 10),
            (self.publish_ready_score, "发布就绪分", 100),
        ):
            if not isinstance(score_value, int) or isinstance(score_value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if not 0 <= score_value <= maximum:
                raise ValueError(f"{field_name}超出允许范围")
        if self.export_decision not in {
            "publish_ready",
            "needs_review",
            "reject",
        }:
            raise ValueError("导出建议不属于允许值")
        if not isinstance(self.keywords, tuple) or any(
            not isinstance(keyword, str) or not keyword.strip()
            for keyword in self.keywords
        ):
            raise ValueError("主题评审关键词必须是字符串元组")
        for keyword in self.keywords:
            _require_valid_unicode(keyword, "主题评审关键词")
        for optional_text, field_name in (
            (self.reject_reason, "淘汰原因"),
            (self.boundary_fix_suggestion, "边界补救建议"),
        ):
            if not isinstance(optional_text, str):
                raise TypeError(f"{field_name}必须是字符串")
            _require_valid_unicode(optional_text, field_name)
        for boundary_value, field_name in (
            (self.boundary_fix_start_ms, "边界补救开始时间"),
            (self.boundary_fix_end_ms, "边界补救结束时间"),
        ):
            if boundary_value is not None and (
                not isinstance(boundary_value, int) or isinstance(boundary_value, bool)
            ):
                raise TypeError(f"{field_name}必须是整数毫秒或 None")


def _require_valid_unicode(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError(f"{field_name}必须是有效 Unicode 文本") from None


@dataclass(frozen=True, slots=True)
class TopicReviewResult:
    """完整成功的主题评审，不允许携带部分结果。"""

    reviews: tuple[CandidateTopicReview, ...] = field(repr=False)
    execution_facts: TopicReviewExecutionFacts

    def __post_init__(self) -> None:
        if not isinstance(self.reviews, tuple) or any(
            not isinstance(review, CandidateTopicReview) for review in self.reviews
        ):
            raise TypeError("完整主题评审只能包含不可变候选评审")
        if not isinstance(self.execution_facts, TopicReviewExecutionFacts):
            raise TypeError("主题评审结果必须包含执行事实")


_TOPIC_REVIEW_ERROR_CODES = frozenset(
    code for code in ErrorCode if code.value.startswith("topic_review.")
)


class TopicReviewFailure(RuntimeError):
    """不携带部分评审、提示或模型正文的稳定主题评审失败。"""

    __slots__ = (
        "category",
        "diagnostics",
        "error_code",
        "execution_facts",
        "retryable_in_new_run",
        "safe_message",
    )

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        execution_facts: TopicReviewExecutionFacts,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("主题评审失败必须使用稳定 ErrorCode")
        if error_code not in _TOPIC_REVIEW_ERROR_CODES:
            raise ValueError("错误码不属于主题评审模块允许的稳定失败")
        if not isinstance(execution_facts, TopicReviewExecutionFacts):
            raise TypeError("主题评审失败必须包含执行事实")
        definition = get_error_definition(error_code)
        self.error_code = error_code
        self.category: ErrorCategory = definition.category
        self.safe_message = definition.safe_message
        self.retryable_in_new_run = definition.retryable_in_new_run
        self.execution_facts = execution_facts
        self.diagnostics = freeze_error_diagnostics(
            error_code,
            diagnostics,
        )
        super().__init__(self.safe_message)


__all__ = [
    "CandidateTopicReview",
    "TopicReviewExecutionFacts",
    "TopicReviewFailure",
    "TopicReviewRequest",
    "TopicReviewResult",
    "TopicReviewSettings",
]
