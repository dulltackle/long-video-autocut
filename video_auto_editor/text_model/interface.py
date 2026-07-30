"""供应商无感知的同步纯文本生成端口。"""

from __future__ import annotations

import math
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import RemoteRequestId, RunStage
from video_auto_editor.runtime.identity import OperationId, RunId

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,7}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_FINISH_REASONS = frozenset({"stop", "length", "refusal"})


class PromptRole(str, Enum):
    """首期纯文本消息允许的供应商无关角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """不包含工具、多模态或会话状态的单条纯文本提示消息。"""

    role: PromptRole
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.role, PromptRole):
            raise TypeError("提示消息角色必须使用 PromptRole")
        if not isinstance(self.content, str):
            raise TypeError("提示消息正文必须是字符串")
        invalid_unicode = False
        try:
            self.content.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            invalid_unicode = True
        if invalid_unicode:
            raise ValueError("提示消息正文必须是有效 Unicode 文本") from None


class ReasoningEffort(str, Enum):
    """供应商无关的推理强度。"""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class GenerationSettings:
    """由业务模块逐次选择的生成参数。"""

    model: str
    temperature: float
    reasoning_effort: ReasoningEffort
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or _MODEL_ID.fullmatch(self.model) is None:
            raise ValueError("文本模型标识格式不合法")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
            or not 0.0 <= self.temperature <= 2.0
        ):
            raise ValueError("文本生成温度必须位于 0.0 到 2.0")
        object.__setattr__(self, "temperature", float(self.temperature))
        if not isinstance(self.reasoning_effort, ReasoningEffort):
            raise TypeError("推理强度必须使用 ReasoningEffort")
        if self.max_output_tokens is not None and (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or not 1 <= self.max_output_tokens <= 65_536
        ):
            raise ValueError("文本生成输出上限必须位于 1 到 65536")


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """只关联本地诊断、绝不进入供应商请求的观测上下文。"""

    run_id: RunId
    stage: RunStage
    operation_id: OperationId

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("文本生成观测上下文必须使用 RunId")
        if not isinstance(self.stage, RunStage):
            raise TypeError("文本生成观测上下文必须使用 RunStage")
        if not isinstance(self.operation_id, OperationId):
            raise TypeError("文本生成观测上下文必须使用 OperationId")


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    """一次同步、非流式、无会话状态的纯文本生成请求。"""

    messages: tuple[PromptMessage, ...]
    settings: GenerationSettings
    observation: ObservationContext
    cancellation: CancellationToken = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.messages, tuple)
            or not self.messages
            or any(not isinstance(message, PromptMessage) for message in self.messages)
        ):
            raise TypeError("文本生成提示必须是非空 PromptMessage 元组")
        if not isinstance(self.settings, GenerationSettings):
            raise TypeError("文本生成请求必须包含 GenerationSettings")
        if not isinstance(self.observation, ObservationContext):
            raise TypeError("文本生成请求必须包含 ObservationContext")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("文本生成请求必须绑定 CancellationToken")


@dataclass(frozen=True, slots=True)
class TextModelExecutionFacts:
    """一次逻辑生成调用产生的供应商无关执行事实。"""

    transport_attempt_count: int
    elapsed_ms: int
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    remote_request_id: RemoteRequestId | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _bounded_integer(
            self.transport_attempt_count,
            field_name="传输尝试次数",
            minimum=0,
            maximum=32,
        )
        _bounded_integer(
            self.elapsed_ms,
            field_name="文本生成耗时毫秒",
            minimum=0,
            maximum=2**53 - 1,
        )
        if self.finish_reason is not None and self.finish_reason not in _FINISH_REASONS:
            raise ValueError("文本生成结束原因不属于中性闭集")
        for value, field_name in (
            (self.input_tokens, "输入 token 数"),
            (self.output_tokens, "输出 token 数"),
        ):
            if value is not None:
                _bounded_integer(
                    value,
                    field_name=field_name,
                    minimum=0,
                    maximum=2**53 - 1,
                )
        if self.remote_request_id is not None and not isinstance(
            self.remote_request_id, RemoteRequestId
        ):
            raise TypeError("远端请求标识必须先由 Adapter 脱敏")
        if self.transport_attempt_count == 0 and any(
            value is not None
            for value in (
                self.finish_reason,
                self.input_tokens,
                self.output_tokens,
                self.remote_request_id,
            )
        ):
            raise ValueError("零传输尝试不得携带供应商响应事实")

    @property
    def transport_retry_count(self) -> int:
        """同一次逻辑生成中额外发生的传输尝试数。"""
        return max(0, self.transport_attempt_count - 1)


@dataclass(frozen=True, slots=True)
class TextGenerationResponse:
    """只包含模型原始文本与中性执行事实的完整成功响应。"""

    text: str = field(repr=False)
    execution_facts: TextModelExecutionFacts

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("文本模型响应正文必须是字符串")
        invalid_unicode = False
        try:
            self.text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            invalid_unicode = True
        if invalid_unicode:
            raise ValueError("文本模型响应正文必须是有效 Unicode 文本") from None
        if not isinstance(self.execution_facts, TextModelExecutionFacts):
            raise TypeError("文本模型响应必须包含执行事实")


class TextModelReadinessCode(str, Enum):
    """文本模型端口本地准备检查的稳定阻塞原因。"""

    CREDENTIAL_MISSING = "credential_missing"
    HTTPS_REQUIRED = "https_required"
    TIMEOUT_INVALID = "timeout_invalid"
    RETRY_POLICY_INVALID = "retry_policy_invalid"
    CONCURRENCY_INVALID = "concurrency_invalid"
    HEADERS_INVALID = "headers_invalid"
    TLS_VERIFICATION_UNAVAILABLE = "tls_verification_unavailable"
    TRANSPORT_CONFIGURATION_INVALID = "transport_configuration_invalid"


_READINESS_MESSAGES: Mapping[TextModelReadinessCode, str] = MappingProxyType(
    {
        TextModelReadinessCode.CREDENTIAL_MISSING: "缺少文本模型供应商凭据。",
        TextModelReadinessCode.HTTPS_REQUIRED: "文本模型供应商地址必须使用 HTTPS。",
        TextModelReadinessCode.TIMEOUT_INVALID: "文本模型传输超时配置不合法。",
        TextModelReadinessCode.RETRY_POLICY_INVALID: "文本模型传输重试配置不合法。",
        TextModelReadinessCode.CONCURRENCY_INVALID: "文本模型供应商并发配置不合法。",
        TextModelReadinessCode.HEADERS_INVALID: "文本模型请求头配置不合法。",
        TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE: (
            "文本模型 TLS 证书或主机名校验不可用。"
        ),
        TextModelReadinessCode.TRANSPORT_CONFIGURATION_INVALID: (
            "文本模型传输配置不合法。"
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    """准备检查发现的稳定、脱敏阻塞问题。"""

    code: TextModelReadinessCode
    diagnostics: Mapping[str, object] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.code, TextModelReadinessCode):
            raise TypeError("文本模型准备问题必须使用稳定 code")
        object.__setattr__(
            self,
            "diagnostics",
            _freeze_readiness_diagnostics(self.diagnostics),
        )

    @property
    def safe_message(self) -> str:
        return _READINESS_MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """本地、只读、可重复的文本模型准备快照。"""

    ready: bool
    configuration_fingerprint: str
    issues: tuple[ReadinessIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise TypeError("准备状态必须是布尔值")
        if (
            not isinstance(self.configuration_fingerprint, str)
            or _FINGERPRINT.fullmatch(self.configuration_fingerprint) is None
        ):
            raise ValueError("文本模型配置指纹必须是 SHA-256 十六进制摘要")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, ReadinessIssue) for issue in self.issues
        ):
            raise TypeError("准备问题必须是 ReadinessIssue 不可变元组")
        if self.ready == bool(self.issues):
            raise ValueError("准备状态必须与阻塞问题保持一致")


class TextModelFailureKind(str, Enum):
    """共享端口稳定表达的供应商无感知失败闭集。"""

    INVALID_CONFIGURATION = "invalid_configuration"
    AUTHENTICATION_FAILED = "authentication_failed"
    REQUEST_REJECTED = "request_rejected"
    RATE_LIMITED = "rate_limited"
    REQUEST_TIMEOUT = "request_timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    RESPONSE_PROTOCOL_INVALID = "response_protocol_invalid"
    GENERATION_REFUSED = "generation_refused"
    OUTPUT_TRUNCATED = "output_truncated"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


class TextModelEventKind(str, Enum):
    """Adapter 在一次逻辑生成内发出的中性结构化事件。"""

    CALL_STARTED = "call_started"
    TRANSPORT_FAILED = "transport_failed"
    RETRY_PLANNED = "retry_planned"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TextModelEvent:
    """只含关联标识与安全执行事实，不携带提示、输出或供应商配置。"""

    kind: TextModelEventKind
    observation: ObservationContext
    transport_attempt_count: int
    elapsed_ms: int
    failure_kind: TextModelFailureKind | None = None
    reason_code: str | None = None
    retry_delay_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TextModelEventKind):
            raise TypeError("文本模型事件必须使用稳定 kind")
        if not isinstance(self.observation, ObservationContext):
            raise TypeError("文本模型事件必须包含观测上下文")
        _bounded_integer(
            self.transport_attempt_count,
            field_name="事件传输尝试次数",
            minimum=0,
            maximum=32,
        )
        _bounded_integer(
            self.elapsed_ms,
            field_name="事件耗时毫秒",
            minimum=0,
            maximum=2**53 - 1,
        )
        failure_events = frozenset(
            {
                TextModelEventKind.TRANSPORT_FAILED,
                TextModelEventKind.RETRY_PLANNED,
                TextModelEventKind.CANCELLED,
                TextModelEventKind.FAILED,
            }
        )
        if self.kind in failure_events:
            if not isinstance(self.failure_kind, TextModelFailureKind):
                raise TypeError("失败类文本模型事件必须包含稳定失败类型")
        elif self.failure_kind is not None:
            raise ValueError("成功类文本模型事件不得包含失败类型")
        if (
            self.kind is TextModelEventKind.CANCELLED
            and self.failure_kind is not TextModelFailureKind.CANCELLED
        ):
            raise ValueError("取消事件必须携带取消失败类型")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str)
            or _STABLE_CODE.fullmatch(self.reason_code) is None
        ):
            raise ValueError("文本模型事件原因必须使用稳定代码")
        if self.kind is TextModelEventKind.RETRY_PLANNED:
            _bounded_integer(
                self.retry_delay_ms,
                field_name="事件重试退避毫秒",
                minimum=0,
                maximum=60_000,
            )
        elif self.retry_delay_ms is not None:
            raise ValueError("非重试事件不得包含重试退避")


class TextModelEventSink(Protocol):
    """由组合根可选注入、可被并发调用的结构化事件接收端。

    隔离测试可以省略；正式运行必须注入。一旦注入，每条事件均为必需事实：
    继承 ``PreservedApplicationFailure`` 的已分类基础设施失败（当前为
    ``DiagnosticsFailure``）会原样传播，以保留 ``diagnostics.write_failed``；
    其他 ``record`` 异常由 Adapter 统一映射为
    ``INTERNAL / observation.sink_failed``，并替代尚未成功记录的原终态，
    包括成功、失败与取消。
    """

    def record(self, event: TextModelEvent) -> None:
        """持久化或收集一条已脱敏事件。"""
        ...


@dataclass(frozen=True, slots=True)
class _FailureDefinition:
    safe_message: str
    retryable_in_new_run: bool


_FAILURE_DEFINITIONS: Mapping[TextModelFailureKind, _FailureDefinition] = (
    MappingProxyType(
        {
            TextModelFailureKind.INVALID_CONFIGURATION: _FailureDefinition(
                "文本模型 Adapter 配置不合法。",
                False,
            ),
            TextModelFailureKind.AUTHENTICATION_FAILED: _FailureDefinition(
                "文本模型服务拒绝了已配置的凭据。",
                True,
            ),
            TextModelFailureKind.REQUEST_REJECTED: _FailureDefinition(
                "文本模型服务拒绝了请求。",
                False,
            ),
            TextModelFailureKind.RATE_LIMITED: _FailureDefinition(
                "文本模型服务触发了请求限流。",
                True,
            ),
            TextModelFailureKind.REQUEST_TIMEOUT: _FailureDefinition(
                "文本模型服务请求超时。",
                True,
            ),
            TextModelFailureKind.SERVICE_UNAVAILABLE: _FailureDefinition(
                "文本模型服务暂时不可用。",
                True,
            ),
            TextModelFailureKind.RESPONSE_PROTOCOL_INVALID: _FailureDefinition(
                "文本模型服务响应不符合协议。",
                True,
            ),
            TextModelFailureKind.GENERATION_REFUSED: _FailureDefinition(
                "文本模型服务拒绝生成结果。",
                False,
            ),
            TextModelFailureKind.OUTPUT_TRUNCATED: _FailureDefinition(
                "文本模型服务输出被截断。",
                True,
            ),
            TextModelFailureKind.CANCELLED: _FailureDefinition(
                "文本生成已按中断请求停止。",
                False,
            ),
            TextModelFailureKind.INTERNAL: _FailureDefinition(
                "文本模型 Adapter 发生内部错误。",
                False,
            ),
        }
    )
)


_FAILURE_DIAGNOSTIC_FIELDS: Mapping[TextModelFailureKind, frozenset[str]] = (
    MappingProxyType(
        {
            TextModelFailureKind.INVALID_CONFIGURATION: frozenset(
                {"field", "reason_code"}
            ),
            TextModelFailureKind.AUTHENTICATION_FAILED: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.REQUEST_REJECTED: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.RATE_LIMITED: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.REQUEST_TIMEOUT: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.SERVICE_UNAVAILABLE: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.RESPONSE_PROTOCOL_INVALID: frozenset(
                {"attempt", "http_status", "reason_code", "remote_request_id"}
            ),
            TextModelFailureKind.GENERATION_REFUSED: frozenset(
                {
                    "attempt",
                    "finish_reason",
                    "http_status",
                    "reason_code",
                    "remote_request_id",
                }
            ),
            TextModelFailureKind.OUTPUT_TRUNCATED: frozenset(
                {
                    "attempt",
                    "finish_reason",
                    "http_status",
                    "reason_code",
                    "remote_request_id",
                }
            ),
            TextModelFailureKind.CANCELLED: frozenset({"attempt", "signal_number"}),
            TextModelFailureKind.INTERNAL: frozenset({"attempt", "reason_code"}),
        }
    )
)


class TextModelFailure(RuntimeError):
    """不携带部分文本、提示或供应商原始响应的稳定端口失败。"""

    __slots__ = (
        "diagnostics",
        "execution_facts",
        "kind",
        "retryable_in_new_run",
        "safe_message",
    )

    def __init__(
        self,
        kind: TextModelFailureKind,
        *,
        execution_facts: TextModelExecutionFacts,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(kind, TextModelFailureKind):
            raise TypeError("文本模型失败必须使用稳定 failure kind")
        if not isinstance(execution_facts, TextModelExecutionFacts):
            raise TypeError("文本模型失败必须包含执行事实")
        definition = _FAILURE_DEFINITIONS[kind]
        self.kind = kind
        self.safe_message = definition.safe_message
        self.retryable_in_new_run = definition.retryable_in_new_run
        self.execution_facts = execution_facts
        self.diagnostics = _freeze_failure_diagnostics(kind, diagnostics)
        super().__init__(self.safe_message)

    def _fresh(self) -> TextModelFailure:
        """为确定性脚本重放创建不携带旧 traceback 的新失败。"""
        return type(self)(
            self.kind,
            execution_facts=self.execution_facts,
            diagnostics=self.diagnostics,
        )


class TextModelPort(Protocol):
    """StepFun 与确定性 Adapter 共同满足的最小共享接口。"""

    def check_readiness(self) -> ReadinessReport:
        """执行本地、只读、无远程请求且可重复的准备检查。"""
        ...

    def generate(
        self,
        request: TextGenerationRequest,
    ) -> TextGenerationResponse:
        """同步返回原始文本；失败或取消不返回部分结果。"""
        ...


def _bounded_integer(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name}必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field_name}超出允许范围")
    return value


def _freeze_readiness_diagnostics(
    diagnostics: Mapping[str, object] | None,
) -> Mapping[str, object]:
    if diagnostics is None:
        return MappingProxyType({})
    if not isinstance(diagnostics, Mapping):
        raise TypeError("准备问题诊断必须是映射")
    if any(not isinstance(key, str) for key in diagnostics):
        raise TypeError("准备问题诊断字段名必须是字符串")
    allowed = frozenset({"component", "field", "operation", "reason_code"})
    unknown = set(diagnostics).difference(allowed)
    if unknown:
        raise ValueError("准备问题诊断包含未批准字段")
    frozen: dict[str, object] = {}
    for key, value in sorted(diagnostics.items()):
        pattern = _FIELD_NAME if key == "field" else _STABLE_CODE
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"准备问题诊断字段 {key} 不合法")
        frozen[key] = value
    return MappingProxyType(frozen)


def _freeze_failure_diagnostics(
    kind: TextModelFailureKind,
    diagnostics: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    source: Mapping[str, Any] = {} if diagnostics is None else diagnostics
    if not isinstance(source, Mapping):
        raise TypeError("文本模型失败诊断必须是映射")
    if any(not isinstance(key, str) for key in source):
        raise TypeError("文本模型失败诊断字段名必须是字符串")
    unknown = set(source).difference(_FAILURE_DIAGNOSTIC_FIELDS[kind])
    if unknown:
        raise ValueError("文本模型失败诊断包含未批准字段")
    frozen: dict[str, Any] = {}
    for key, value in sorted(source.items()):
        if key == "attempt":
            frozen[key] = _bounded_integer(
                value,
                field_name="诊断尝试次数",
                minimum=1,
                maximum=32,
            )
        elif key == "http_status":
            frozen[key] = _bounded_integer(
                value,
                field_name="HTTP 状态码",
                minimum=100,
                maximum=599,
            )
        elif key == "signal_number":
            if value not in {signal.SIGINT, signal.SIGTERM}:
                raise ValueError("取消诊断只允许 SIGINT 或 SIGTERM")
            frozen[key] = value
        elif key == "remote_request_id":
            if not isinstance(value, RemoteRequestId):
                raise TypeError("远端请求标识必须先由 Adapter 脱敏")
            frozen[key] = value
        elif key == "finish_reason":
            if value not in _FINISH_REASONS:
                raise ValueError("失败结束原因不属于中性闭集")
            frozen[key] = value
        elif key == "field":
            if not isinstance(value, str) or _FIELD_NAME.fullmatch(value) is None:
                raise ValueError("配置字段诊断不合法")
            frozen[key] = value
        else:
            if not isinstance(value, str) or _STABLE_CODE.fullmatch(value) is None:
                raise ValueError("失败原因诊断不合法")
            frozen[key] = value
    return MappingProxyType(frozen)


__all__ = [
    "GenerationSettings",
    "ObservationContext",
    "PromptMessage",
    "PromptRole",
    "ReadinessIssue",
    "ReadinessReport",
    "ReasoningEffort",
    "TextGenerationRequest",
    "TextGenerationResponse",
    "TextModelEvent",
    "TextModelEventKind",
    "TextModelEventSink",
    "TextModelExecutionFacts",
    "TextModelFailure",
    "TextModelFailureKind",
    "TextModelPort",
    "TextModelReadinessCode",
]
