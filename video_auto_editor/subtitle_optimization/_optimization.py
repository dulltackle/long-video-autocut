"""强制字幕优化能力。"""

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
from video_auto_editor.transcription import TranscriptChunk

from ._model import (
    _FORBIDDEN_LINE_BREAKS,
    OptimizedShortVideoSubtitles,
    SubtitleDisplayBlock,
    SubtitleOptimizationExecutionFacts,
    SubtitleOptimizationFailure,
    SubtitleOptimizationRequest,
    SubtitleOptimizationResult,
    SubtitleOptimizationSettings,
)

_SYSTEM_PROMPT = (
    "你负责把直播短视频的忠实转写整理成烧录字幕显示块。\n"
    "把任务看作用橡皮擦处理原文：只删除字符，并用换行划分显示块。\n"
    "不得新增字符，不得改写或同义替换，不得换序，不得增加空格。\n"
    "保留下来的每个字符必须保持原样和原有顺序。\n"
    "每个逻辑显示块在输出中占一行；不得返回 Markdown、编号或解释。\n"
    "示例输入：嗯，那个我们今天讲这个流程啊。\n"
    "示例输出：我们今天讲这个流程"
)
_SEMANTIC_RETRY_INSTRUCTION = (
    "上一结果不合法。请重新处理同一原文，只返回完整字幕显示块。"
)
_IDENTITY_SCHEMA_VERSION = "subtitle-optimization.identity.v1"
_WINDOW_ALGORITHM_VERSION = "subtitle-optimization.window.v1"
_PAYLOAD_SCHEMA_VERSION = "subtitle-optimization.payload.v1"
_PROMPT_VERSION = "subtitle-optimization.prompt.v1"
_SEMANTIC_RETRY_VERSION = "subtitle-optimization.semantic-retry.v1"
_PARSER_VERSION = "subtitle-optimization.parser.v1"
_VALIDATION_VERSION = "subtitle-optimization.validation.v1"
_PROVIDER_ERROR_CODES = {
    TextModelFailureKind.AUTHENTICATION_FAILED: (
        ErrorCode.SUBTITLE_OPTIMIZATION_AUTHENTICATION_FAILED
    ),
    TextModelFailureKind.REQUEST_REJECTED: (
        ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_REJECTED
    ),
    TextModelFailureKind.RATE_LIMITED: (ErrorCode.SUBTITLE_OPTIMIZATION_RATE_LIMITED),
    TextModelFailureKind.REQUEST_TIMEOUT: (
        ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_TIMEOUT
    ),
    TextModelFailureKind.SERVICE_UNAVAILABLE: (
        ErrorCode.SUBTITLE_OPTIMIZATION_SERVICE_UNAVAILABLE
    ),
    TextModelFailureKind.RESPONSE_PROTOCOL_INVALID: (
        ErrorCode.SUBTITLE_OPTIMIZATION_RESPONSE_PROTOCOL_INVALID
    ),
    TextModelFailureKind.GENERATION_REFUSED: (
        ErrorCode.SUBTITLE_OPTIMIZATION_GENERATION_REFUSED
    ),
    TextModelFailureKind.OUTPUT_TRUNCATED: (
        ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_TRUNCATED
    ),
}


class _InvalidOutput(ValueError):
    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("字幕优化模型输出未通过业务校验")


@dataclass(frozen=True, slots=True)
class _CharacterTime:
    start_ms: int
    end_ms: int
    is_precise: bool
    limit_ms: int


@dataclass(frozen=True, slots=True)
class _WindowChunk:
    text: str
    character_times: tuple[_CharacterTime, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedWindow:
    block_texts: tuple[str, ...]
    display_blocks: tuple[SubtitleDisplayBlock, ...]


@dataclass(slots=True)
class _ExecutionLedger:
    short_video_count: int
    window_count: int = 0
    model_request_count: int = 0
    transport_attempt_count: int = 0
    transport_retry_count: int = 0
    semantic_retry_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0

    def snapshot(self) -> SubtitleOptimizationExecutionFacts:
        return SubtitleOptimizationExecutionFacts(
            short_video_count=self.short_video_count,
            window_count=self.window_count,
            model_request_count=self.model_request_count,
            transport_attempt_count=self.transport_attempt_count,
            transport_retry_count=self.transport_retry_count,
            semantic_retry_count=self.semantic_retry_count,
            cache_hit_count=self.cache_hit_count,
            cache_miss_count=self.cache_miss_count,
        )


class SubtitleOptimization:
    """隐藏子窗口、提示、校验、时间对齐、语义重试和处理缓存。"""

    __slots__ = ("_cache", "_model", "_settings")

    def __init__(
        self,
        text_model: TextModelPort,
        cache_repository: CacheRepository,
        settings: SubtitleOptimizationSettings,
    ) -> None:
        if not callable(getattr(text_model, "check_readiness", None)) or not callable(
            getattr(text_model, "generate", None)
        ):
            raise TypeError("字幕优化必须使用 TextModelPort")
        if not isinstance(cache_repository, CacheRepository):
            raise TypeError("字幕优化必须使用处理缓存仓库")
        if not isinstance(settings, SubtitleOptimizationSettings):
            raise TypeError("字幕优化必须使用 SubtitleOptimizationSettings")
        self._model = text_model
        self._cache = cache_repository
        self._settings = settings

    def check_readiness(self) -> ReadinessReport:
        """执行只读、本地且不发起模型生成的准备检查。"""
        report = self._model.check_readiness()
        if not isinstance(report, ReadinessReport):
            raise TypeError("文本模型准备检查必须返回 ReadinessReport")
        return report

    def optimize(
        self,
        request: SubtitleOptimizationRequest,
    ) -> SubtitleOptimizationResult:
        """同步返回全部待发布短视频的优化字幕。"""
        if not isinstance(request, SubtitleOptimizationRequest):
            raise TypeError("字幕优化只接受 SubtitleOptimizationRequest")
        request.cancellation.raise_if_cancelled()
        short_videos = request.delivery_plan.short_videos
        if not short_videos:
            return SubtitleOptimizationResult(
                short_videos=(),
                execution_facts=SubtitleOptimizationExecutionFacts(
                    short_video_count=0,
                    window_count=0,
                    model_request_count=0,
                ),
            )
        ledger = _ExecutionLedger(short_video_count=len(short_videos))
        configuration_fingerprint = self.check_readiness().configuration_fingerprint
        optimized = []
        for short_video in short_videos:
            try:
                chunks = _short_video_chunks(
                    request,
                    short_video.final_start_ms,
                    short_video.final_end_ms,
                )
            except _InvalidOutput as failure:
                raise _output_invalid_failure(
                    ledger,
                    failure.reason_code,
                ) from None
            windows = _group_chunks(
                chunks,
                self._settings.window_max_chars,
            )
            ledger.window_count += len(windows)
            display_blocks: list[SubtitleDisplayBlock] = []
            for window in windows:
                request.cancellation.raise_if_cancelled()
                text, character_times = _join_window(window)
                spec = _cache_spec(
                    text,
                    character_times,
                    self._settings,
                    configuration_fingerprint,
                )

                def compute_window(
                    window_text=text,
                    window_character_times=character_times,
                ) -> _ResolvedWindow:
                    ledger.cache_miss_count += 1
                    return self._generate_window(
                        request,
                        window_text,
                        window_character_times,
                        ledger,
                    )

                resolution = self._cache.resolve(
                    spec,
                    cancellation=request.cancellation,
                    compute=compute_window,
                )
                if resolution.from_cache:
                    ledger.cache_hit_count += 1
                display_blocks.extend(resolution.value.display_blocks)
            if not _display_blocks_are_ordered(display_blocks):
                raise _output_invalid_failure(
                    ledger,
                    "output.alignment_failed",
                )
            optimized.append(
                OptimizedShortVideoSubtitles(
                    short_video_id=short_video.short_video_id,
                    display_blocks=tuple(display_blocks),
                )
            )
        return SubtitleOptimizationResult(
            short_videos=tuple(optimized),
            execution_facts=ledger.snapshot(),
        )

    def _generate_window(
        self,
        request: SubtitleOptimizationRequest,
        text: str,
        character_times: tuple[_CharacterTime, ...],
        ledger: _ExecutionLedger,
    ) -> _ResolvedWindow:
        messages: tuple[PromptMessage, ...] = (
            PromptMessage(
                role=PromptRole.SYSTEM,
                content=_system_prompt(self._settings),
            ),
            PromptMessage(
                role=PromptRole.USER,
                content=text,
            ),
        )
        invalid: _InvalidOutput | None = None
        for attempt in range(
            1,
            self._settings.semantic_attempt_limit + 1,
        ):
            request.cancellation.raise_if_cancelled()
            attempt_messages: tuple[PromptMessage, ...] = messages
            if invalid is not None:
                attempt_messages += (
                    PromptMessage(
                        role=PromptRole.USER,
                        content=(
                            f"{_SEMANTIC_RETRY_INSTRUCTION}\n"
                            f"reason_code={invalid.reason_code}"
                        ),
                    ),
                )
            try:
                response = self._model.generate(
                    TextGenerationRequest(
                        messages=attempt_messages,
                        settings=self._settings.generation,
                        observation=ObservationContext(
                            run_id=request.run_id,
                            stage=RunStage.DELIVERY_BUILD,
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
                raise SubtitleOptimizationFailure(
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
                return _parse_and_align(
                    text,
                    character_times,
                    response.text,
                    self._settings,
                )
            except _InvalidOutput as failure:
                invalid = failure
                if attempt < self._settings.semantic_attempt_limit:
                    ledger.semantic_retry_count += 1
                    continue
                raise _output_invalid_failure(
                    ledger,
                    failure.reason_code,
                    attempt=attempt,
                ) from None
        raise AssertionError("字幕优化语义尝试循环必须产生终态")


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


def _output_invalid_failure(
    ledger: _ExecutionLedger,
    reason_code: str,
    *,
    attempt: int | None = None,
) -> SubtitleOptimizationFailure:
    diagnostics: dict[str, object] = {"reason_code": reason_code}
    if attempt is not None:
        diagnostics["attempt"] = attempt
    return SubtitleOptimizationFailure(
        ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID,
        execution_facts=ledger.snapshot(),
        diagnostics=diagnostics,
    )


def _system_prompt(settings: SubtitleOptimizationSettings) -> str:
    display_capacity = settings.max_chars_per_line * settings.max_lines
    return (
        f"{_SYSTEM_PROMPT}\n"
        f"每个输出显示块最多 {display_capacity} 个字符；渲染时会按每行最多 "
        f"{settings.max_chars_per_line} 个字符自动折为最多 "
        f"{settings.max_lines} 行，不要自行添加块内换行。"
    )


def _cache_spec(
    source_text: str,
    character_times: tuple[_CharacterTime, ...],
    settings: SubtitleOptimizationSettings,
    configuration_fingerprint: str,
) -> CacheEntrySpec[_ResolvedWindow]:
    generation = settings.generation
    identity = CacheIdentity.create(
        namespace=CacheNamespace.SUBTITLE_OPTIMIZATION,
        identity_schema_version=_IDENTITY_SCHEMA_VERSION,
        algorithm_version=_WINDOW_ALGORITHM_VERSION,
        payload_schema_version=_PAYLOAD_SCHEMA_VERSION,
        adapter_id=settings.adapter_id,
        model_id=generation.model,
        configuration_fingerprint=configuration_fingerprint,
        result_inputs={
            "source_text": source_text,
            "display_constraints": {
                "max_chars_per_line": settings.max_chars_per_line,
                "max_lines": settings.max_lines,
            },
            "prompt_sha256": _prompt_digest(source_text, settings),
            "prompt_version": _PROMPT_VERSION,
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
        encode=_encode_cached_window,
        decode=lambda payload: _decode_cached_window(
            payload,
            source_text,
            character_times,
            settings,
        ),
    )


def _prompt_digest(
    source_text: str,
    settings: SubtitleOptimizationSettings,
) -> str:
    prompt_contract = json.dumps(
        {
            "semantic_retry_instruction": _SEMANTIC_RETRY_INSTRUCTION,
            "system": _system_prompt(settings),
            "user": source_text,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(prompt_contract).hexdigest()


def _encode_cached_window(value: _ResolvedWindow) -> object:
    return {"blocks": list(value.block_texts)}


def _decode_cached_window(
    payload: object,
    source_text: str,
    character_times: tuple[_CharacterTime, ...],
    settings: SubtitleOptimizationSettings,
) -> _ResolvedWindow:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"blocks"}
        or not isinstance(payload["blocks"], list)
        or any(not isinstance(block, str) for block in payload["blocks"])
    ):
        raise CachedPayloadInvalid()
    block_texts = tuple(payload["blocks"])
    try:
        _validate_block_texts(block_texts)
        return _align_block_texts(
            source_text,
            character_times,
            block_texts,
            settings,
        )
    except _InvalidOutput:
        raise CachedPayloadInvalid() from None


def _short_video_chunks(
    request: SubtitleOptimizationRequest,
    start_ms: int,
    end_ms: int,
) -> tuple[_WindowChunk, ...]:
    chunks = []
    for chunk in sorted(
        request.transcript.chunks,
        key=lambda item: (
            item.start_ms,
            item.end_ms,
        ),
    ):
        if chunk.start_ms >= end_ms or chunk.end_ms <= start_ms:
            continue
        times = _character_times(chunk)
        selected_text = []
        selected_times = []
        for character, character_time in zip(
            chunk.text,
            times,
        ):
            clipped_start = max(start_ms, character_time.start_ms)
            clipped_end = min(end_ms, character_time.end_ms)
            if clipped_end <= clipped_start:
                continue
            selected_text.append(character)
            selected_times.append(
                _CharacterTime(
                    start_ms=clipped_start,
                    end_ms=clipped_end,
                    is_precise=character_time.is_precise,
                    limit_ms=min(end_ms, character_time.limit_ms),
                )
            )
        if selected_text:
            chunks.append(
                _WindowChunk(
                    text="".join(selected_text),
                    character_times=tuple(selected_times),
                )
            )
    if not chunks:
        raise _InvalidOutput("output.alignment_failed")
    return tuple(chunks)


def _character_times(chunk: TranscriptChunk) -> tuple[_CharacterTime, ...]:
    if chunk.character_spans is not None:
        return tuple(
            _CharacterTime(
                start_ms=span.start_ms,
                end_ms=span.end_ms,
                is_precise=True,
                limit_ms=span.end_ms,
            )
            for span in chunk.character_spans
        )
    count = len(chunk.text)
    duration = chunk.end_ms - chunk.start_ms
    return tuple(
        _CharacterTime(
            start_ms=chunk.start_ms + (duration * index // count),
            end_ms=chunk.start_ms + _fallback_end_offset(duration, count, index),
            is_precise=False,
            limit_ms=chunk.end_ms,
        )
        for index in range(count)
    )


def _fallback_end_offset(duration: int, count: int, index: int) -> int:
    numerator = duration * (index + 1)
    if duration < count:
        numerator += count - 1
    return numerator // count


def _group_chunks(
    chunks: tuple[_WindowChunk, ...],
    character_budget: int,
) -> tuple[tuple[_WindowChunk, ...], ...]:
    groups = []
    current: list[_WindowChunk] = []
    current_size = 0
    for chunk in chunks:
        chunk_size = len(chunk.text)
        if current and current_size + chunk_size > character_budget:
            groups.append(tuple(current))
            current = []
            current_size = 0
        current.append(chunk)
        current_size += chunk_size
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _join_window(
    chunks: tuple[_WindowChunk, ...],
) -> tuple[str, tuple[_CharacterTime, ...]]:
    return (
        "".join(chunk.text for chunk in chunks),
        tuple(
            character_time
            for chunk in chunks
            for character_time in chunk.character_times
        ),
    )


def _parse_and_align(
    source_text: str,
    character_times: tuple[_CharacterTime, ...],
    response_text: str,
    settings: SubtitleOptimizationSettings,
) -> _ResolvedWindow:
    block_texts = _strict_block_texts(response_text)
    return _align_block_texts(
        source_text,
        character_times,
        block_texts,
        settings,
    )


def _align_block_texts(
    source_text: str,
    character_times: tuple[_CharacterTime, ...],
    block_texts: tuple[str, ...],
    settings: SubtitleOptimizationSettings,
) -> _ResolvedWindow:
    _validate_block_texts(block_texts)
    pointer = 0
    blocks: list[SubtitleDisplayBlock] = []
    capacity = settings.max_chars_per_line * settings.max_lines
    previous_end: int | None = None
    for block_text in block_texts:
        if len(block_text) > capacity:
            raise _InvalidOutput("output.display_constraint_failed")
        matched_times = []
        for character in block_text:
            while pointer < len(source_text) and source_text[pointer] != character:
                pointer += 1
            if pointer >= len(source_text):
                raise _InvalidOutput(_subsequence_reason(source_text, block_texts))
            matched_times.append(character_times[pointer])
            pointer += 1
        previous_precise_end: int | None = None
        for character_time in matched_times:
            if not character_time.is_precise:
                continue
            if (
                previous_precise_end is not None
                and character_time.start_ms < previous_precise_end
            ):
                raise _InvalidOutput("output.alignment_failed")
            previous_precise_end = character_time.end_ms
        start_ms = min(character_time.start_ms for character_time in matched_times)
        end_ms = max(character_time.end_ms for character_time in matched_times)
        block_limit_ms = max(
            character_time.limit_ms for character_time in matched_times
        )
        has_precise_time = previous_precise_end is not None
        if previous_end is not None and start_ms < previous_end:
            precise_time_would_be_clipped = any(
                character_time.is_precise and character_time.start_ms < previous_end
                for character_time in matched_times
            )
            if precise_time_would_be_clipped:
                raise _InvalidOutput("output.alignment_failed")
            start_ms = previous_end
        if end_ms <= start_ms:
            if has_precise_time or start_ms >= block_limit_ms:
                raise _InvalidOutput("output.alignment_failed")
            end_ms = start_ms + 1
        if end_ms > block_limit_ms:
            raise _InvalidOutput("output.alignment_failed")
        try:
            blocks.append(
                SubtitleDisplayBlock(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=_wrap_display_block(block_text, settings),
                )
            )
        except (TypeError, ValueError):
            raise _InvalidOutput("output.alignment_failed") from None
        previous_end = end_ms
    return _ResolvedWindow(
        block_texts=block_texts,
        display_blocks=tuple(blocks),
    )


def _display_blocks_are_ordered(
    blocks: list[SubtitleDisplayBlock],
) -> bool:
    if not blocks:
        return False
    previous_end = blocks[0].start_ms
    for block in blocks:
        if block.start_ms < previous_end:
            return False
        previous_end = block.end_ms
    return True


def _strict_block_texts(response_text: str) -> tuple[str, ...]:
    if not isinstance(response_text, str) or not response_text:
        raise _InvalidOutput("output.structure_invalid")
    if "\r" in response_text:
        raise _InvalidOutput("output.line_break_invalid")
    blocks = tuple(response_text.split("\n"))
    _validate_block_texts(blocks)
    return blocks


def _validate_block_texts(blocks: tuple[str, ...]) -> None:
    if not blocks or any(
        not block
        or "\n" in block
        or "\r" in block
        or any(character in _FORBIDDEN_LINE_BREAKS for character in block)
        for block in blocks
    ):
        raise _InvalidOutput("output.line_break_invalid")


def _subsequence_reason(
    source_text: str,
    block_texts: tuple[str, ...],
) -> str:
    available: dict[str, int] = {}
    for character in source_text:
        available[character] = available.get(character, 0) + 1
    for character in "".join(block_texts):
        remaining = available.get(character, 0)
        if remaining == 0:
            return "output.character_added"
        available[character] = remaining - 1
    return "output.character_reordered"


def _wrap_display_block(
    text: str,
    settings: SubtitleOptimizationSettings,
) -> str:
    if len(text) <= settings.max_chars_per_line:
        return text
    return "\n".join(
        text[index : index + settings.max_chars_per_line]
        for index in range(0, len(text), settings.max_chars_per_line)
    )


__all__ = ["SubtitleOptimization"]
