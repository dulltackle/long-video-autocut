"""SubtitleOptimization 深模块拥有的不可变输入与结果。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from video_auto_editor.clip_planning import DeliveryPlan
from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.runtime.identity import RunId, ShortVideoId
from video_auto_editor.text_model import GenerationSettings
from video_auto_editor.transcription import CompleteTranscript

_FORBIDDEN_LINE_BREAKS = frozenset(
    {
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    }
)


@dataclass(frozen=True, slots=True)
class SubtitleOptimizationSettings:
    """组合根交给字幕优化的稳定模型与业务约束。"""

    adapter_id: str
    generation: GenerationSettings
    window_max_chars: int
    max_chars_per_line: int
    max_lines: int
    semantic_attempt_limit: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not self.adapter_id:
            raise ValueError("字幕优化 Adapter 标识不能为空")
        if not isinstance(self.generation, GenerationSettings):
            raise TypeError("字幕优化模型设置必须使用 GenerationSettings")
        for value, field_name, minimum, maximum in (
            (self.window_max_chars, "字幕子窗口字符预算", 1, 100_000),
            (self.max_chars_per_line, "字幕单行字符上限", 1, 100),
            (self.max_lines, "字幕显示块行数上限", 1, 2),
            (self.semantic_attempt_limit, "字幕优化语义尝试次数", 1, 8),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if not minimum <= value <= maximum:
                raise ValueError(f"{field_name}必须位于 {minimum} 到 {maximum}")


@dataclass(frozen=True, slots=True)
class SubtitleOptimizationRequest:
    """一次强制字幕优化所需的完整交付方案、转写与运行机制。"""

    delivery_plan: DeliveryPlan
    transcript: CompleteTranscript = field(repr=False)
    run_id: RunId
    cancellation: CancellationToken = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_plan, DeliveryPlan):
            raise TypeError("字幕优化只接受 ClipPlanning 签发的交付方案")
        if not isinstance(self.transcript, CompleteTranscript):
            raise TypeError("字幕优化只接受 LiveApplication 签发的完整转写")
        if self.delivery_plan.transcript_id != self.transcript.transcript_id:
            raise ValueError("字幕优化的交付方案必须引用当前完整转写")
        if not isinstance(self.run_id, RunId):
            raise TypeError("字幕优化必须绑定 RunId")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("字幕优化必须绑定根取消令牌")


@dataclass(frozen=True, slots=True)
class SubtitleDisplayBlock:
    """已经通过子序列校验并对齐到素材时间轴的字幕显示块。"""

    start_ms: int
    end_ms: int
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.start_ms, "字幕显示块开始时间"),
            (self.end_ms, "字幕显示块结束时间"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数毫秒")
            if value < 0:
                raise ValueError(f"{field_name}不能为负数")
        if self.end_ms <= self.start_ms:
            raise ValueError("字幕显示块结束时间必须晚于开始时间")
        if not isinstance(self.text, str):
            raise TypeError("字幕显示块正文必须是字符串")
        _require_valid_unicode(self.text, "字幕显示块正文")
        lines = self.text.split("\n")
        if (
            not self.text
            or len(lines) > 2
            or any(not line or "\r" in line for line in lines)
            or any(character in _FORBIDDEN_LINE_BREAKS for character in self.text)
        ):
            raise ValueError("字幕显示块正文不能为空、不得包含非法换行且最多两行")


@dataclass(frozen=True, slots=True)
class OptimizedShortVideoSubtitles:
    """单条待发布短视频的完整优化字幕。"""

    short_video_id: ShortVideoId
    display_blocks: tuple[SubtitleDisplayBlock, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.short_video_id, ShortVideoId):
            raise TypeError("优化字幕必须使用 ShortVideoId")
        if (
            not isinstance(self.display_blocks, tuple)
            or not self.display_blocks
            or any(
                not isinstance(block, SubtitleDisplayBlock)
                for block in self.display_blocks
            )
        ):
            raise ValueError("每条待发布短视频必须包含至少一个字幕显示块")
        previous_end = self.display_blocks[0].start_ms
        for block in self.display_blocks:
            if block.start_ms < previous_end:
                raise ValueError("字幕显示块必须有序且不能重叠")
            previous_end = block.end_ms


@dataclass(frozen=True, slots=True)
class SubtitleOptimizationExecutionFacts:
    """编排层可观察的字幕优化聚合执行事实。"""

    short_video_count: int
    window_count: int
    model_request_count: int
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    semantic_retry_count: int = 0
    transport_attempt_count: int = 0
    transport_retry_count: int = 0

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.short_video_count, "待发布短视频数"),
            (self.window_count, "字幕子窗口数"),
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
        if self.cache_hit_count + self.cache_miss_count > self.window_count:
            raise ValueError("缓存解析子窗口数不能超过计划子窗口数")


@dataclass(frozen=True, slots=True)
class SubtitleOptimizationResult:
    """全部待发布短视频的完整优化字幕，不允许携带部分结果。"""

    short_videos: tuple[OptimizedShortVideoSubtitles, ...] = field(repr=False)
    execution_facts: SubtitleOptimizationExecutionFacts

    def __post_init__(self) -> None:
        if not isinstance(self.short_videos, tuple) or any(
            not isinstance(item, OptimizedShortVideoSubtitles)
            for item in self.short_videos
        ):
            raise TypeError("字幕优化结果只能包含不可变短视频字幕")
        if not isinstance(
            self.execution_facts,
            SubtitleOptimizationExecutionFacts,
        ):
            raise TypeError("字幕优化结果必须包含执行事实")
        if len(self.short_videos) != self.execution_facts.short_video_count:
            raise ValueError("字幕优化结果必须覆盖全部待发布短视频")
        identifiers = tuple(item.short_video_id for item in self.short_videos)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("字幕优化结果不能包含重复短视频标识")
        facts = self.execution_facts
        if facts.window_count < facts.short_video_count:
            raise ValueError("每条待发布短视频必须完成至少一个字幕子窗口")
        if facts.cache_hit_count + facts.cache_miss_count != facts.window_count:
            raise ValueError("字幕优化成功结果必须解析全部子窗口缓存")
        if facts.model_request_count < facts.cache_miss_count:
            raise ValueError("每个缓存未命中子窗口必须完成模型请求")


_SUBTITLE_OPTIMIZATION_ERROR_CODES = frozenset(
    code for code in ErrorCode if code.value.startswith("subtitle_optimization.")
)


class SubtitleOptimizationFailure(RuntimeError):
    """不携带部分字幕、提示或模型正文的稳定字幕优化失败。"""

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
        execution_facts: SubtitleOptimizationExecutionFacts,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("字幕优化失败必须使用稳定 ErrorCode")
        if error_code not in _SUBTITLE_OPTIMIZATION_ERROR_CODES:
            raise ValueError("错误码不属于字幕优化模块允许的稳定失败")
        if not isinstance(
            execution_facts,
            SubtitleOptimizationExecutionFacts,
        ):
            raise TypeError("字幕优化失败必须包含执行事实")
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


def _require_valid_unicode(value: str, field_name: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ValueError(f"{field_name}必须是有效 Unicode 文本") from None
