"""PROTOTYPE — 语音识别模块契约的纯逻辑草案。

问题：编排层是否只需要“结构化预检 + 整源语音识别”两个动作，以及统一的
转写文本、执行事实和类型化失败，就能驱动生产级直播拆条运行，同时对供应商、
音频准备、识别分片、重试和覆盖补救保持无感知？

这是 throwaway 原型，不是生产实现。交互外壳位于
``prototype_transcription_contract.py``。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Protocol


class CacheUse(str, Enum):
    """编排层需要记录的中性缓存事实。"""

    HIT = "hit"
    MISS = "miss"


class SpeechPresence(str, Enum):
    """完整处理后的语音存在性，而不是供应商原始响应状态。"""

    PRESENT = "present"
    ABSENT = "absent"


class FailureCategory(str, Enum):
    """与已决定的运行退出码类别对齐，但不在这里决定具体错误码。"""

    INVALID_CONFIGURATION = "invalid_configuration"
    INPUT = "input"
    EXTERNAL = "external"
    LOCAL_PROCESSING = "local_processing"
    INTERNAL = "internal"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class ReadinessIssue:
    category: FailureCategory
    safe_message: str


@dataclass(frozen=True)
class ReadinessReport:
    """预检结果；预期中的配置/环境问题以数据返回，便于一次展示完整清单。"""

    ready: bool
    issues: tuple[ReadinessIssue, ...] = ()


@dataclass(frozen=True)
class Cancellation:
    """原型中的取消令牌；生产实现可替换为线程安全令牌。"""

    requested: bool = False
    signal: str | None = None


@dataclass(frozen=True)
class VerifiedSourceMedia:
    """source_analysis 已验证、可供后续阶段消费的源素材描述。"""

    path: str
    duration_seconds: float


@dataclass(frozen=True)
class TranscriptionRequest:
    """编排层传入的全部逐次运行信息。"""

    source: VerifiedSourceMedia
    private_work_dir: str
    cancellation: Cancellation = Cancellation()


@dataclass(frozen=True)
class TranscriptChunk:
    """供应商无感知的全局时间轴转写文本块。"""

    start_seconds: float
    end_seconds: float
    text: str
    character_spans: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True)
class ExecutionFacts:
    """供运行诊断包记录的中性事实，不暴露供应商或识别分片。"""

    cache_use: CacheUse
    retry_count: int = 0
    recovery_count: int = 0


@dataclass(frozen=True)
class TranscriptionResult:
    """成功结果；空 chunks 只有在完整处理确认无语音时才合法。"""

    chunks: tuple[TranscriptChunk, ...]
    speech_presence: SpeechPresence
    facts: ExecutionFacts


class TranscriptionFailure(Exception):
    """预期失败的统一形态；调用方不解析供应商消息或异常类型。"""

    def __init__(
        self,
        category: FailureCategory,
        safe_message: str,
        *,
        retryable: bool,
        facts: ExecutionFacts,
        diagnostics: dict[str, object],
    ) -> None:
        super().__init__(safe_message)
        self.category = category
        self.safe_message = safe_message
        self.retryable = retryable
        self.facts = facts
        self.diagnostics = diagnostics

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category.value,
            "safe_message": self.safe_message,
            "retryable": self.retryable,
            "facts": _jsonable(asdict(self.facts)),
            "diagnostics": _jsonable(self.diagnostics),
        }


class SpeechRecognition(Protocol):
    """外部 seam 上的候选最小接口。

    StepAudio 生产 Adapter 与确定性测试 Adapter 都应满足这个接口。具体供应商
    配置在组合根装配 Adapter，直播拆条编排层只持有此接口。
    """

    def check_readiness(self) -> ReadinessReport:
        """本地、只读、无远程请求地检查当前 Adapter 的全部硬要求。"""
        ...

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        """全有或全无地处理一个源视频；失败不返回部分转写文本。"""
        ...


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    intent: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("fresh_success", "首次识别成功", "内部完成音频准备、分片、重试与覆盖验证"),
    Scenario("cache_hit", "整场转写缓存命中", "不创建生产 Adapter 的远程请求"),
    Scenario("silent_source", "完整确认素材无语音", "空转写文本仍是合法的语音识别结果"),
    Scenario("missing_secret", "预检发现密钥缺失", "远程请求前返回完整、可展示的预检问题"),
    Scenario("external_exhausted", "供应商请求重试耗尽", "供应商异常被翻译为统一外部失败"),
    Scenario("invalid_response", "供应商结果不满足契约", "不完整覆盖不能伪装成静音或成功"),
    Scenario("local_media_failure", "本地音频准备失败", "媒体错误与供应商错误保持不同类别"),
    Scenario("interrupted", "识别过程中收到中断", "停止新工作并向运行状态机传播中断"),
)


@dataclass(frozen=True)
class PrototypeState:
    scenario_index: int = 0
    phase: str = "not_run"
    readiness: dict[str, object] | None = None
    result: dict[str, object] | None = None
    failure: dict[str, object] | None = None
    orchestration_decision: str = "尚未运行场景"

    @property
    def scenario(self) -> Scenario:
        return SCENARIOS[self.scenario_index]


class DeterministicSpeechRecognitionAdapter:
    """满足同一接口的确定性测试 Adapter。

    场景脚本只存在于本 Adapter 内；编排层始终只调用 ``check_readiness`` 和
    ``transcribe``，不按供应商或场景分支。
    """

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def check_readiness(self) -> ReadinessReport:
        if self._scenario.key == "missing_secret":
            return ReadinessReport(
                ready=False,
                issues=(
                    ReadinessIssue(
                        FailureCategory.INVALID_CONFIGURATION,
                        "语音识别凭据未配置",
                    ),
                ),
            )
        return ReadinessReport(ready=True)

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        facts = ExecutionFacts(cache_use=CacheUse.MISS)

        if request.cancellation.requested or self._scenario.key == "interrupted":
            raise TranscriptionFailure(
                FailureCategory.INTERRUPTED,
                "语音识别已按中断请求停止",
                retryable=False,
                facts=facts,
                diagnostics={
                    "operation": "transcription",
                    "signal": request.cancellation.signal or "unknown",
                },
            )
        if self._scenario.key == "missing_secret":
            raise TranscriptionFailure(
                FailureCategory.INVALID_CONFIGURATION,
                "预检失败后不得进入语音识别",
                retryable=False,
                facts=facts,
                diagnostics={"operation": "credential_check"},
            )
        if self._scenario.key == "external_exhausted":
            raise TranscriptionFailure(
                FailureCategory.EXTERNAL,
                "语音识别供应商在内部重试后仍不可用",
                retryable=True,
                facts=replace(facts, retry_count=3),
                diagnostics={
                    "operation": "recognize_audio",
                    "http_status": 503,
                    "attempt_count": 3,
                    "request_id": "redacted-demo-request-id",
                },
            )
        if self._scenario.key == "invalid_response":
            raise TranscriptionFailure(
                FailureCategory.EXTERNAL,
                "语音识别返回结果未通过完整性验证",
                retryable=True,
                facts=replace(facts, retry_count=1, recovery_count=2),
                diagnostics={
                    "operation": "validate_coverage",
                    "remaining_gap_seconds": 42.5,
                },
            )
        if self._scenario.key == "local_media_failure":
            raise TranscriptionFailure(
                FailureCategory.LOCAL_PROCESSING,
                "无法准备语音识别所需音频",
                retryable=False,
                facts=facts,
                diagnostics={
                    "operation": "prepare_audio",
                    "tool": "ffmpeg",
                    "exit_code": 1,
                    "stderr_excerpt": "脱敏后的媒体处理错误摘要",
                },
            )
        if self._scenario.key == "cache_hit":
            return _validate_result(
                TranscriptionResult(
                    chunks=(
                        TranscriptChunk(4.0, 6.5, "缓存中的转写文本"),
                    ),
                    speech_presence=SpeechPresence.PRESENT,
                    facts=ExecutionFacts(cache_use=CacheUse.HIT),
                ),
                request.source,
            )
        if self._scenario.key == "silent_source":
            return _validate_result(
                TranscriptionResult(
                    chunks=(),
                    speech_presence=SpeechPresence.ABSENT,
                    facts=facts,
                ),
                request.source,
            )
        return _validate_result(
            TranscriptionResult(
                chunks=(
                    TranscriptChunk(
                        0.5,
                        2.0,
                        "今天好",
                        ((0.5, 0.8), (0.8, 1.1), (1.1, 1.4)),
                    ),
                    TranscriptChunk(2.1, 4.2, "编排层只依赖统一结果和失败语义。"),
                ),
                speech_presence=SpeechPresence.PRESENT,
                facts=replace(facts, retry_count=1, recovery_count=1),
            ),
            request.source,
        )


def select_scenario(state: PrototypeState, offset: int) -> PrototypeState:
    """纯状态转换：切换场景并清空上次运行观察。"""

    index = (state.scenario_index + offset) % len(SCENARIOS)
    return PrototypeState(scenario_index=index)


def run_selected_scenario(state: PrototypeState) -> PrototypeState:
    """以编排层视角执行所选 Adapter，并捕获它能观察到的全部状态。"""

    adapter: SpeechRecognition = DeterministicSpeechRecognitionAdapter(state.scenario)
    readiness = adapter.check_readiness()
    readiness_payload = _jsonable(asdict(readiness))

    if not readiness.ready:
        return replace(
            state,
            phase="preflight_failed",
            readiness=readiness_payload,
            result=None,
            failure=None,
            orchestration_decision="终止于 preflight；不进入 source_analysis 或 transcription",
        )

    request = TranscriptionRequest(
        source=VerifiedSourceMedia(
            path="/private/input/live.mp4",
            duration_seconds=3_600.0,
        ),
        private_work_dir="/private/runs/demo/transcription",
        cancellation=Cancellation(
            requested=state.scenario.key == "interrupted",
            signal="SIGINT" if state.scenario.key == "interrupted" else None,
        ),
    )
    try:
        result = adapter.transcribe(request)
    except TranscriptionFailure as exc:
        decision = {
            FailureCategory.EXTERNAL: "运行进入 failed；映射退出码 30",
            FailureCategory.LOCAL_PROCESSING: "运行进入 failed；映射退出码 40",
            FailureCategory.INPUT: "运行进入 failed；映射退出码 20",
            FailureCategory.INVALID_CONFIGURATION: "运行进入 failed；映射退出码 2",
            FailureCategory.INTERNAL: "运行进入 failed；映射退出码 70",
            FailureCategory.INTERRUPTED: "运行进入 interrupted；信号决定退出码 130/143",
        }[exc.category]
        return replace(
            state,
            phase="transcription_failed",
            readiness=readiness_payload,
            result=None,
            failure=exc.as_dict(),
            orchestration_decision=decision,
        )

    return replace(
        state,
        phase="transcription_succeeded",
        readiness=readiness_payload,
        result=_jsonable(asdict(result)),
        failure=None,
        orchestration_decision="进入 candidate_planning；编排层不读取供应商、分片或重试算法",
    )


def interface_summary() -> dict[str, object]:
    """展示当前候选契约，供 TUI 每帧渲染。"""

    return {
        "external_interface": [
            "check_readiness() -> ReadinessReport",
            "transcribe(TranscriptionRequest) -> TranscriptionResult",
        ],
        "request": [
            "verified_source(path/duration_seconds)",
            "private_work_dir",
            "cancellation",
        ],
        "injected_at_composition": [
            "supplier configuration",
            "cache repository",
            "structured diagnostics recorder",
        ],
        "success": ["chunks", "speech_presence", "facts(cache_use/retry_count/recovery_count)"],
        "failure": [
            "category",
            "safe_message",
            "retryable(for a new run only)",
            "facts",
            "sanitized diagnostics",
        ],
        "success_invariants": [
            "global ordered timestamps within source duration",
            "non-empty text and valid intervals",
            "optional character spans validated when present",
            "no internal artifact paths",
        ],
        "stage_semantics": "all-or-nothing; progress is emitted as diagnostics, never partial results",
        "hidden_inside_module": [
            "supplier/model/endpoint/credentials",
            "audio preparation and temporary paths",
            "shard plan and overlap merge",
            "retry and coverage-recovery algorithms",
            "whole-transcript and shard caches",
            "raw supplier errors and responses",
        ],
    }


def _validate_result(
    result: TranscriptionResult,
    source: VerifiedSourceMedia,
) -> TranscriptionResult:
    """原型中的统一成功不变量；实际实现应由模块共享验证。"""

    if result.speech_presence is SpeechPresence.ABSENT and result.chunks:
        raise AssertionError("无语音结果不得包含转写文本块")
    if result.speech_presence is SpeechPresence.PRESENT and not result.chunks:
        raise AssertionError("有语音结果必须包含转写文本块")

    previous_key: tuple[float, float] | None = None
    for chunk in result.chunks:
        if not chunk.text.strip():
            raise AssertionError("转写文本块不得为空")
        if chunk.start_seconds < 0 or chunk.end_seconds <= chunk.start_seconds:
            raise AssertionError("转写文本块时间区间无效")
        if chunk.end_seconds > source.duration_seconds:
            raise AssertionError("转写文本块不得超出素材时长")
        key = (chunk.start_seconds, chunk.end_seconds)
        if previous_key is not None and key < previous_key:
            raise AssertionError("转写文本块必须按全局时间轴有序")
        previous_key = key

        spans = chunk.character_spans
        if spans is None:
            continue
        if len(spans) != len(chunk.text):
            raise AssertionError("逐字时间存在时必须与文本一一对应")
        previous_end = chunk.start_seconds
        for start, end in spans:
            if start < previous_end or end <= start or end > chunk.end_seconds:
                raise AssertionError("逐字时间必须有序且位于文本块区间内")
            previous_end = end
    return result


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
