"""运行诊断的封闭不可变值。"""

from dataclasses import dataclass
from enum import Enum

from video_auto_editor.cache import CacheNamespace, CacheOutcome
from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.delivery.capability import PublishedDelivery
from video_auto_editor.runtime.errors import RunError


class StageOutcome(str, Enum):
    """一个已进入阶段允许的终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class OperationKind(str, Enum):
    """需要独立计时、并发或失败关系的封闭工作类型。"""

    PREFLIGHT_CHECK = "preflight_check"
    TRANSCRIPTION_SHARD = "transcription_shard"
    COVERAGE_RECOVERY = "coverage_recovery"
    CANDIDATE_BATCH = "candidate_batch"
    SUBTITLE_WINDOW = "subtitle_window"
    SHORT_VIDEO_EXPORT = "short_video_export"
    MEDIA_SUBPROCESS = "media_subprocess"
    EXTERNAL_REQUEST = "external_request"
    CACHE_READ = "cache_read"
    CACHE_WRITE = "cache_write"
    CACHE_QUARANTINE = "cache_quarantine"
    PUBLICATION_COMMIT = "publication_commit"
    BACKUP_RESTORE = "backup_restore"


class OperationOutcome(str, Enum):
    """单项诊断操作允许的终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED_DUE_TO_PRIMARY = "cancelled_due_to_primary"


class RetryKind(str, Enum):
    """必须分别统计的额外工作类别。"""

    TRANSPORT_RETRY = "transport_retry"
    SEMANTIC_RETRY = "semantic_retry"
    COVERAGE_RECOVERY = "coverage_recovery"


class InterruptionSignal(str, Enum):
    """应用受控处理的两类 POSIX 终止信号。"""

    SIGINT = "sigint"
    SIGTERM = "sigterm"

    @property
    def exit_code(self) -> int:
        """返回信号对应的稳定 CLI 退出码。"""
        return 130 if self is InterruptionSignal.SIGINT else 143


class CertifiedPlatform(str, Enum):
    """首个生产版本封闭认证的平台标识。"""

    UBUNTU_24_04_AMD64 = "ubuntu_24_04_amd64"


class PreflightOutcome(str, Enum):
    """认证环境聚合预检的封闭结果。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProviderCapability(str, Enum):
    """可以发生供应商外发的三个业务能力。"""

    TRANSCRIPTION = "transcription"
    TOPIC_REVIEW = "topic_review"
    SUBTITLE_OPTIMIZATION = "subtitle_optimization"


class ProviderTransport(str, Enum):
    """Adapter 是否会把敏感内容传出本机。"""

    REMOTE = "remote"
    LOCAL = "local"


class ExternalDataCategory(str, Enum):
    """供应商请求中允许披露的敏感数据类别。"""

    AUDIO_SHARD = "audio_shard"
    CANDIDATE_TRANSCRIPT = "candidate_transcript"
    COURSE_CONTEXT = "course_context"
    BUSINESS_CONSTRAINTS = "business_constraints"
    SUBTITLE_WINDOW = "subtitle_window"
    FIXED_INSTRUCTIONS = "fixed_instructions"


class ExternalRequestOutcome(str, Enum):
    """一次供应商请求的封闭观测结果。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ZeroRequestReason(str, Enum):
    """选中 Adapter 却没有远程请求的稳定原因。"""

    CACHE_HIT = "cache_hit"
    NO_WORK = "no_work"
    DETERMINISTIC_ADAPTER = "deterministic_adapter"
    PRECONDITION_FAILED = "precondition_failed"


class DeliveryBuildState(str, Enum):
    """交付构建可观察的状态。"""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class DeliveryVerificationState(str, Enum):
    """独立完整性验证可观察的状态。"""

    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class PublicationState(str, Enum):
    """原子发布可观察的状态。"""

    IN_PROGRESS = "in_progress"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class ArtifactRole(str, Enum):
    """诊断可以披露的标准交付文件角色。"""

    MANIFEST = "manifest"
    TRANSCRIPT_JSON = "transcript_json"
    TRANSCRIPT_SRT = "transcript_srt"
    PLAN = "plan"
    METADATA = "metadata"
    REPORT = "report"
    SHORT_VIDEO = "short_video"


class RecoveredNoticeKind(str, Enum):
    """不属于终态错误的已恢复事项。"""

    TRANSPORT_RETRY_SUCCEEDED = "transport_retry_succeeded"
    SEMANTIC_RETRY_SUCCEEDED = "semantic_retry_succeeded"
    COVERAGE_RECOVERY_SUCCEEDED = "coverage_recovery_succeeded"
    CACHE_CORRUPTION_RECOVERED = "cache_corruption_recovered"


class _DiagnosticTerminalState(str, Enum):
    """运行终态写入诊断 schema 时使用的内部投影。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True, init=False)
class DiagnosticCompletion:
    """RunDiagnostics 收尾所需的类型化诊断投影。"""

    state: _DiagnosticTerminalState
    primary_error: RunError | None
    associated_errors: tuple[RunError, ...]
    recovery_incomplete: bool
    published_delivery: PublishedDelivery | None
    result_kind: ResultKind | None
    interruption_signal: InterruptionSignal | None
    cleanup_duration_ms: int | None

    def __new__(cls) -> "DiagnosticCompletion":
        raise TypeError("DiagnosticCompletion 只能通过诊断收尾工厂创建")

    @classmethod
    def failed(
        cls,
        primary_error: RunError,
        *,
        associated_errors: tuple[RunError, ...] = (),
        recovery_incomplete: bool = False,
    ) -> "DiagnosticCompletion":
        """形成带一个主错误的失败终态。"""
        if not isinstance(primary_error, RunError):
            raise TypeError("失败终态必须包含 RunError 主错误")
        if not isinstance(associated_errors, tuple) or any(
            not isinstance(error, RunError) for error in associated_errors
        ):
            raise TypeError("关联错误必须是 RunError 元组")
        if not isinstance(recovery_incomplete, bool):
            raise TypeError("恢复完整性必须是布尔值")
        instance = object.__new__(cls)
        object.__setattr__(instance, "state", _DiagnosticTerminalState.FAILED)
        object.__setattr__(instance, "primary_error", primary_error)
        object.__setattr__(
            instance,
            "associated_errors",
            associated_errors,
        )
        object.__setattr__(
            instance,
            "recovery_incomplete",
            recovery_incomplete,
        )
        object.__setattr__(instance, "published_delivery", None)
        object.__setattr__(instance, "result_kind", None)
        object.__setattr__(instance, "interruption_signal", None)
        object.__setattr__(instance, "cleanup_duration_ms", None)
        return instance

    @classmethod
    def succeeded(
        cls,
        published_delivery: PublishedDelivery,
        *,
        result_kind: ResultKind,
    ) -> "DiagnosticCompletion":
        """形成只能由真实发布提交证明支撑的成功终态。"""
        if not isinstance(published_delivery, PublishedDelivery):
            raise TypeError("成功终态必须包含 PublishedDelivery 提交证明")
        # 属性访问同时验证 PublishedDelivery 的私有签发 authority。
        published_delivery.run_id
        if not isinstance(result_kind, ResultKind):
            raise TypeError("成功结果必须使用 ResultKind")
        instance = object.__new__(cls)
        object.__setattr__(instance, "state", _DiagnosticTerminalState.SUCCEEDED)
        object.__setattr__(instance, "primary_error", None)
        object.__setattr__(instance, "associated_errors", ())
        object.__setattr__(instance, "recovery_incomplete", False)
        object.__setattr__(
            instance,
            "published_delivery",
            published_delivery,
        )
        object.__setattr__(instance, "result_kind", result_kind)
        object.__setattr__(instance, "interruption_signal", None)
        object.__setattr__(instance, "cleanup_duration_ms", None)
        return instance

    @classmethod
    def interrupted(
        cls,
        signal: InterruptionSignal,
        *,
        cleanup_duration_ms: int,
        associated_errors: tuple[RunError, ...] = (),
        recovery_incomplete: bool = False,
    ) -> "DiagnosticCompletion":
        """形成无主错误、可包含清理错误的受控中断终态。"""
        if not isinstance(signal, InterruptionSignal):
            raise TypeError("中断终态必须使用 InterruptionSignal")
        if (
            not isinstance(cleanup_duration_ms, int)
            or isinstance(cleanup_duration_ms, bool)
        ):
            raise TypeError("中断清理耗时必须是整数")
        if cleanup_duration_ms < 0:
            raise ValueError("中断清理耗时不能为负数")
        if not isinstance(associated_errors, tuple) or any(
            not isinstance(error, RunError) for error in associated_errors
        ):
            raise TypeError("关联错误必须是 RunError 元组")
        if not isinstance(recovery_incomplete, bool):
            raise TypeError("恢复完整性必须是布尔值")
        if bool(associated_errors) != recovery_incomplete:
            raise ValueError("中断清理错误与恢复完整性必须一致")
        instance = object.__new__(cls)
        object.__setattr__(instance, "state", _DiagnosticTerminalState.INTERRUPTED)
        object.__setattr__(instance, "primary_error", None)
        object.__setattr__(
            instance,
            "associated_errors",
            associated_errors,
        )
        object.__setattr__(
            instance,
            "recovery_incomplete",
            recovery_incomplete,
        )
        object.__setattr__(instance, "published_delivery", None)
        object.__setattr__(instance, "result_kind", None)
        object.__setattr__(instance, "interruption_signal", signal)
        object.__setattr__(
            instance,
            "cleanup_duration_ms",
            cleanup_duration_ms,
        )
        return instance


@dataclass(frozen=True, slots=True)
class DiagnosticPackageSnapshot:
    """诊断接收 Adapter 当前已接受的不可变字节快照。"""

    events: bytes
    manifest: bytes | None


@dataclass(frozen=True, slots=True, init=False)
class DiagnosticFinalization:
    """提交点前后均能安全表达诊断收尾结果的最小证据。"""

    terminal_sequence: int | None
    manifest_persisted: bool
    diagnostics_incomplete: bool

    def __new__(cls) -> "DiagnosticFinalization":
        raise TypeError("DiagnosticFinalization 只能由 RunDiagnostics 创建")

    @classmethod
    def _persisted(cls, terminal_sequence: int) -> "DiagnosticFinalization":
        instance = object.__new__(cls)
        object.__setattr__(instance, "terminal_sequence", terminal_sequence)
        object.__setattr__(instance, "manifest_persisted", True)
        object.__setattr__(instance, "diagnostics_incomplete", False)
        return instance

    @classmethod
    def _incomplete(
        cls,
        terminal_sequence: int | None,
    ) -> "DiagnosticFinalization":
        instance = object.__new__(cls)
        object.__setattr__(instance, "terminal_sequence", terminal_sequence)
        object.__setattr__(instance, "manifest_persisted", False)
        object.__setattr__(instance, "diagnostics_incomplete", True)
        return instance
