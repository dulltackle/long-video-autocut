"""供应商无感知的语音识别阶段契约。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    RemoteRequestId,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.runtime.identity import (
    OperationId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
)

_EVENT_REASON_CODE = re.compile(
    r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,7}"
)


class CacheUse(str, Enum):
    """整场转写处理缓存的中性使用事实。"""

    HIT = "hit"
    MISS = "miss"


class SpeechPresence(str, Enum):
    """完整语音识别后的语音存在性。"""

    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class CharacterSpan:
    """单个 Unicode 码点在素材时间轴上的真实区间。"""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        _integer_milliseconds(self.start_ms, field_name="逐字开始时间")
        _integer_milliseconds(self.end_ms, field_name="逐字结束时间")
        if self.end_ms <= self.start_ms:
            raise ValueError("逐字结束时间必须晚于开始时间")


@dataclass(frozen=True, slots=True)
class TranscriptionChunk:
    """不携带运行标识的中性转写文本块。"""

    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    character_spans: tuple[CharacterSpan, ...] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _integer_milliseconds(self.start_ms, field_name="转写文本块开始时间")
        _integer_milliseconds(self.end_ms, field_name="转写文本块结束时间")
        if self.end_ms <= self.start_ms:
            raise ValueError("转写文本块结束时间必须晚于开始时间")
        if not isinstance(self.text, str):
            raise TypeError("转写文本块正文必须是字符串")
        if not self.text.strip():
            raise ValueError("转写文本块正文不能为空")
        if self.character_spans is None:
            return
        if not isinstance(self.character_spans, tuple) or any(
            not isinstance(span, CharacterSpan)
            for span in self.character_spans
        ):
            raise TypeError("逐字时间必须是 CharacterSpan 元组")
        if len(self.character_spans) != len(self.text):
            raise ValueError("逐字时间必须与 Unicode 码点一一对应")
        previous_end = self.start_ms
        for span in self.character_spans:
            if (
                span.start_ms < previous_end
                or span.end_ms > self.end_ms
            ):
                raise ValueError("逐字时间必须有序且位于转写文本块内")
            previous_end = span.end_ms


@dataclass(frozen=True, slots=True)
class ExecutionFacts:
    """编排层可记录但不据此解释内部算法的中性执行事实。"""

    cache_use: CacheUse
    retry_count: int = 0
    recovery_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.cache_use, CacheUse):
            raise TypeError("处理缓存使用事实必须使用 CacheUse")
        for value, field_name in (
            (self.retry_count, "内部重试次数"),
            (self.recovery_count, "覆盖补救次数"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if value < 0:
                raise ValueError(f"{field_name}不能为负数")
        if self.cache_use is CacheUse.HIT and (
            self.retry_count != 0 or self.recovery_count != 0
        ):
            raise ValueError("整场转写缓存命中时不得发生内部重试或覆盖补救")


class TranscriptionRemoteRequestEventKind(str, Enum):
    """一个逻辑识别分片请求的供应商无感知状态。"""

    STARTED = "started"
    ATTEMPT_FAILED = "attempt_failed"
    RETRY_PLANNED = "retry_planned"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED_DUE_TO_PRIMARY = "cancelled_due_to_primary"


@dataclass(frozen=True, slots=True)
class TranscriptionRemoteRequestEvent:
    """只携带可并发关联的安全远程请求事实。"""

    kind: TranscriptionRemoteRequestEventKind
    correlation_id: OperationId
    transport_attempt_count: int
    remote_request_id: RemoteRequestId | None = field(
        default=None,
        repr=False,
    )
    reason_code: str | None = None
    retry_delay_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TranscriptionRemoteRequestEventKind):
            raise TypeError("语音识别远程请求事件必须使用稳定 kind")
        if not isinstance(self.correlation_id, OperationId):
            raise TypeError("语音识别远程请求事件必须使用 OperationId 关联")
        if (
            not isinstance(self.transport_attempt_count, int)
            or isinstance(self.transport_attempt_count, bool)
        ):
            raise TypeError("语音识别远程请求传输次数必须是整数")
        if not 0 <= self.transport_attempt_count <= 3:
            raise ValueError("语音识别远程请求累计传输次数必须位于 0 到 3")
        if self.kind is TranscriptionRemoteRequestEventKind.STARTED:
            if self.transport_attempt_count != 0:
                raise ValueError("逻辑识别分片请求开始时尚未发生传输")
        elif self.transport_attempt_count < 1:
            raise ValueError("逻辑识别分片请求后续事件必须包含累计传输次数")
        if self.remote_request_id is not None and not isinstance(
            self.remote_request_id,
            RemoteRequestId,
        ):
            raise TypeError("远端请求标识必须先由 Adapter 脱敏")
        if (
            self.kind
            in {
                TranscriptionRemoteRequestEventKind.STARTED,
                TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            }
            and self.remote_request_id is not None
        ):
            raise ValueError("尚未完成的传输事件不得携带远端请求标识")
        failure_events = {
            TranscriptionRemoteRequestEventKind.ATTEMPT_FAILED,
            TranscriptionRemoteRequestEventKind.RETRY_PLANNED,
            TranscriptionRemoteRequestEventKind.FAILED,
            TranscriptionRemoteRequestEventKind.INTERRUPTED,
            TranscriptionRemoteRequestEventKind.CANCELLED_DUE_TO_PRIMARY,
        }
        if self.kind in failure_events:
            if (
                not isinstance(self.reason_code, str)
                or _EVENT_REASON_CODE.fullmatch(self.reason_code) is None
            ):
                raise ValueError("失败类语音识别请求事件必须携带稳定原因码")
        elif self.reason_code is not None:
            raise ValueError("成功类语音识别请求事件不得携带失败原因")
        if self.kind is TranscriptionRemoteRequestEventKind.RETRY_PLANNED:
            if (
                not isinstance(self.retry_delay_ms, int)
                or isinstance(self.retry_delay_ms, bool)
                or not 0 <= self.retry_delay_ms <= 60_000
            ):
                raise ValueError("重试计划必须携带安全的毫秒退避时长")
        elif self.retry_delay_ms is not None:
            raise ValueError("非重试计划事件不得携带退避时长")


class TranscriptionRemoteRequestEventSink(Protocol):
    """由组合根可选注入、可被并发调用的必需事件接收端。

    一旦注入，事件写入即为 fail-closed：已分类的应用失败保持原样，其他
    ``record`` 异常由 Adapter 映射为不含异常文本的稳定内部失败。
    """

    def record(self, event: TranscriptionRemoteRequestEvent) -> None:
        """记录一次实际远程传输请求的状态。"""
        ...


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """一次全有或全无语音识别的成功结果。"""

    chunks: tuple[TranscriptionChunk, ...]
    speech_presence: SpeechPresence
    execution_facts: ExecutionFacts

    def __post_init__(self) -> None:
        if not isinstance(self.chunks, tuple) or any(
            not isinstance(chunk, TranscriptionChunk)
            for chunk in self.chunks
        ):
            raise TypeError("语音识别结果只能包含不可变转写文本块")
        if not isinstance(self.speech_presence, SpeechPresence):
            raise TypeError("语音存在性必须使用 SpeechPresence")
        if not isinstance(self.execution_facts, ExecutionFacts):
            raise TypeError("语音识别结果必须包含 ExecutionFacts")
        if (
            self.speech_presence is SpeechPresence.PRESENT
            and not self.chunks
        ):
            raise ValueError("确认存在语音时必须包含转写文本块")
        if (
            self.speech_presence is SpeechPresence.ABSENT
            and self.chunks
        ):
            raise ValueError("确认无语音时不得包含转写文本块")


@dataclass(frozen=True, slots=True, init=False)
class TranscriptChunk:
    """应用为本次运行签发标识后的转写文本块。"""

    transcript_chunk_id: TranscriptChunkId
    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    character_spans: tuple[CharacterSpan, ...] | None = field(
        default=None,
        repr=False,
    )

    def __new__(  # noqa: PYI034 - Python 3.10 不提供 typing.Self
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "TranscriptChunk":
        raise TypeError("TranscriptChunk 只能由 LiveApplication 创建")

    @classmethod
    def _from_application(
        cls,
        transcript_chunk_id: TranscriptChunkId,
        chunk: TranscriptionChunk,
    ) -> "TranscriptChunk":
        if not isinstance(transcript_chunk_id, TranscriptChunkId):
            raise TypeError("转写文本块必须使用 TranscriptChunkId")
        if not isinstance(chunk, TranscriptionChunk):
            raise TypeError("应用只能标识已验证的中性转写文本块")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "transcript_chunk_id",
            transcript_chunk_id,
        )
        object.__setattr__(instance, "start_ms", chunk.start_ms)
        object.__setattr__(instance, "end_ms", chunk.end_ms)
        object.__setattr__(instance, "text", chunk.text)
        object.__setattr__(
            instance,
            "character_spans",
            chunk.character_spans,
        )
        return instance


@dataclass(frozen=True, slots=True, init=False)
class CompleteTranscript:
    """一次运行内带全新业务标识的完整转写文本。"""

    transcript_id: TranscriptId
    speech_presence: SpeechPresence
    chunks: tuple[TranscriptChunk, ...]

    def __new__(  # noqa: PYI034 - Python 3.10 不提供 typing.Self
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "CompleteTranscript":
        raise TypeError("CompleteTranscript 只能由 LiveApplication 创建")

    @classmethod
    def _from_application(
        cls,
        *,
        transcript_id: TranscriptId,
        speech_presence: SpeechPresence,
        chunks: tuple[TranscriptChunk, ...],
    ) -> "CompleteTranscript":
        if not isinstance(transcript_id, TranscriptId):
            raise TypeError("完整转写文本必须使用 TranscriptId")
        if not isinstance(speech_presence, SpeechPresence):
            raise TypeError("完整转写文本必须包含 SpeechPresence")
        if not isinstance(chunks, tuple) or any(
            not isinstance(chunk, TranscriptChunk) for chunk in chunks
        ):
            raise TypeError("完整转写文本只能包含已标识文本块")
        if speech_presence is SpeechPresence.PRESENT and not chunks:
            raise ValueError("确认存在语音时必须包含已标识文本块")
        if speech_presence is SpeechPresence.ABSENT and chunks:
            raise ValueError("确认无语音时不得包含已标识文本块")
        identifiers = tuple(chunk.transcript_chunk_id for chunk in chunks)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("完整转写文本块标识必须唯一")
        instance = object.__new__(cls)
        object.__setattr__(instance, "transcript_id", transcript_id)
        object.__setattr__(instance, "speech_presence", speech_presence)
        object.__setattr__(instance, "chunks", chunks)
        return instance


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    """准备检查发现的稳定、脱敏阻塞问题。"""

    error_code: ErrorCode
    diagnostics: Mapping[str, Any] | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.error_code, ErrorCode):
            raise TypeError("准备问题必须使用稳定 ErrorCode")
        if self.diagnostics is not None and not isinstance(
            self.diagnostics,
            Mapping,
        ):
            raise TypeError("准备问题诊断必须是映射")
        object.__setattr__(
            self,
            "diagnostics",
            freeze_error_diagnostics(
                self.error_code,
                self.diagnostics,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """本地只读准备检查的完整快照。"""

    ready: bool
    issues: tuple[ReadinessIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise TypeError("准备状态必须是布尔值")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, ReadinessIssue) for issue in self.issues
        ):
            raise TypeError("准备问题必须是 ReadinessIssue 不可变元组")
        if self.ready == bool(self.issues):
            raise ValueError("准备状态必须与阻塞问题保持一致")


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    """语音识别阶段逐次运行所需的最小不可变输入。"""

    source: SourceDescription
    temporary_workspace: ManagedDirectoryCapability = field(repr=False)
    cancellation: CancellationToken = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceDescription):
            raise TypeError("语音识别只接受已验证素材描述")
        if not isinstance(
            self.temporary_workspace,
            ManagedDirectoryCapability,
        ):
            raise TypeError("语音识别必须使用受管临时 workspace capability")
        if (
            self.temporary_workspace.role
            is not ManagedDirectoryRole.RUN_TEMPORARY
        ):
            raise ValueError("语音识别只能使用本次运行的临时 workspace")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("语音识别必须绑定根取消令牌")


class SpeechRecognition(Protocol):
    """生产与确定性 Adapter 共同满足的最小接口。"""

    def check_readiness(self) -> ReadinessReport:
        """执行本地、只读且可重复的准备检查。"""
        ...

    def transcribe(
        self,
        request: TranscriptionRequest,
    ) -> TranscriptionResult:
        """同步返回完整结果；失败或取消不得返回部分转写文本。"""
        ...


_TRANSCRIPTION_ERROR_CODES = frozenset(
    code
    for code in ErrorCode
    if code.value.startswith(("config.", "input.", "transcription."))
    or code
    in {
        ErrorCode.CACHE_INFRASTRUCTURE_FAILED,
        ErrorCode.INTERNAL_UNEXPECTED,
        ErrorCode.MEDIA_PROCESSING_FAILED,
    }
)


class TranscriptionFailure(RuntimeError):
    """不携带部分转写文本的稳定类型化语音识别失败。"""

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
        execution_facts: ExecutionFacts,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("语音识别失败必须使用稳定 ErrorCode")
        if error_code not in _TRANSCRIPTION_ERROR_CODES:
            raise ValueError("错误码不属于语音识别模块允许的稳定失败")
        if not isinstance(execution_facts, ExecutionFacts):
            raise TypeError("语音识别失败必须包含 ExecutionFacts")
        if execution_facts.cache_use is CacheUse.HIT:
            raise ValueError("语音识别失败不得声明整场转写缓存命中")
        if diagnostics is not None and not isinstance(diagnostics, Mapping):
            raise TypeError("语音识别失败诊断必须是映射")
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

    def _fresh(self) -> "TranscriptionFailure":
        """为一次脚本重放创建不携带旧 traceback 的新异常。"""
        return type(self)(
            self.error_code,
            execution_facts=self.execution_facts,
            diagnostics=self.diagnostics,
        )


def validate_result_for_source(
    result: TranscriptionResult,
    source: SourceDescription,
) -> None:
    """验证成功结果位于已验证素材的全局时间轴内。"""
    if not isinstance(result, TranscriptionResult):
        raise TypeError("语音识别 Adapter 必须返回 TranscriptionResult")
    if not isinstance(source, SourceDescription):
        raise TypeError("语音识别结果验证需要已验证素材描述")
    previous_key: tuple[int, int] | None = None
    for chunk in result.chunks:
        if chunk.end_ms > source.duration_ms:
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                execution_facts=result.execution_facts,
                diagnostics={"reason_code": "output.out_of_bounds"},
            )
        key = (chunk.start_ms, chunk.end_ms)
        if previous_key is not None and key < previous_key:
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                execution_facts=result.execution_facts,
                diagnostics={"reason_code": "output.time_invalid"},
            )
        previous_key = key


def _integer_milliseconds(value: object, *, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name}必须是整数毫秒")
    if value < 0:
        raise ValueError(f"{field_name}不能为负数")
