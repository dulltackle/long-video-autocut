"""稳定公共错误与 CLI 退出码。"""

import hashlib
import re
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, Callable, FrozenSet, Mapping

from .identity import ErrorId, OperationId


class ExitCode(IntEnum):
    """直播拆条运行的稳定进程退出码。"""

    SUCCESS = 0
    INVALID_USAGE = 2
    PREFLIGHT_FAILED = 10
    INPUT_FAILED = 20
    EXTERNAL_SERVICE_FAILED = 30
    LOCAL_PROCESSING_FAILED = 40
    DELIVERY_FAILED = 50
    PUBLICATION_FAILED = 60
    INTERNAL_ERROR = 70
    SIGINT = 130
    SIGTERM = 143


class ErrorCategory(str, Enum):
    """由自动化消费的粗粒度错误类别。"""

    CONFIGURATION = "configuration"
    ENVIRONMENT = "environment"
    INPUT = "input"
    EXTERNAL_SERVICE = "external_service"
    LOCAL_PROCESSING = "local_processing"
    DELIVERY = "delivery"
    PUBLICATION = "publication"
    INTERNAL = "internal"

    @property
    def exit_code(self) -> ExitCode:
        """返回该类别唯一对应的稳定退出码。"""
        return _CATEGORY_EXIT_CODES[self]


_CATEGORY_EXIT_CODES = MappingProxyType(
    {
        ErrorCategory.CONFIGURATION: ExitCode.INVALID_USAGE,
        ErrorCategory.ENVIRONMENT: ExitCode.PREFLIGHT_FAILED,
        ErrorCategory.INPUT: ExitCode.INPUT_FAILED,
        ErrorCategory.EXTERNAL_SERVICE: ExitCode.EXTERNAL_SERVICE_FAILED,
        ErrorCategory.LOCAL_PROCESSING: ExitCode.LOCAL_PROCESSING_FAILED,
        ErrorCategory.DELIVERY: ExitCode.DELIVERY_FAILED,
        ErrorCategory.PUBLICATION: ExitCode.PUBLICATION_FAILED,
        ErrorCategory.INTERNAL: ExitCode.INTERNAL_ERROR,
    }
)


class ErrorCode(str, Enum):
    """封闭公共错误码。"""

    CONFIG_SCHEMA_INVALID = "config.schema_invalid"
    CONFIG_VALUE_INVALID = "config.value_invalid"
    CONFIG_CONFLICT = "config.conflict"
    CONFIG_CREDENTIAL_MISSING = "config.credential_missing"
    CONFIG_HTTPS_REQUIRED = "config.https_required"

    ENVIRONMENT_PLATFORM_UNSUPPORTED = "environment.platform_unsupported"
    ENVIRONMENT_PYTHON_UNSUPPORTED = "environment.python_unsupported"
    ENVIRONMENT_INSTALLATION_MANIFEST_INVALID = (
        "environment.installation_manifest_invalid"
    )
    ENVIRONMENT_FFMPEG_UNAVAILABLE = "environment.ffmpeg_unavailable"
    ENVIRONMENT_FFPROBE_UNAVAILABLE = "environment.ffprobe_unavailable"
    ENVIRONMENT_FONT_UNAVAILABLE = "environment.font_unavailable"
    ENVIRONMENT_TLS_CA_UNAVAILABLE = "environment.tls_ca_unavailable"
    ENVIRONMENT_WORKSPACE_UNWRITABLE = "environment.workspace_unwritable"
    ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED = (
        "environment.atomic_publication_unsupported"
    )
    ENVIRONMENT_DIAGNOSTICS_UNWRITABLE = "environment.diagnostics_unwritable"

    INPUT_MISSING = "input.missing"
    INPUT_UNREADABLE = "input.unreadable"
    INPUT_UNSUPPORTED = "input.unsupported"
    INPUT_MEDIA_INVALID = "input.media_invalid"
    INPUT_REQUIRED_STREAM_MISSING = "input.required_stream_missing"

    TRANSCRIPTION_AUTHENTICATION_FAILED = "transcription.authentication_failed"
    TRANSCRIPTION_REQUEST_REJECTED = "transcription.request_rejected"
    TRANSCRIPTION_RATE_LIMITED = "transcription.rate_limited"
    TRANSCRIPTION_REQUEST_TIMEOUT = "transcription.request_timeout"
    TRANSCRIPTION_SERVICE_UNAVAILABLE = "transcription.service_unavailable"
    TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID = (
        "transcription.response_protocol_invalid"
    )
    TRANSCRIPTION_GENERATION_REFUSED = "transcription.generation_refused"
    TRANSCRIPTION_OUTPUT_TRUNCATED = "transcription.output_truncated"
    TRANSCRIPTION_OUTPUT_INVALID = "transcription.output_invalid"
    TRANSCRIPTION_COVERAGE_INCOMPLETE = "transcription.coverage_incomplete"
    TRANSCRIPTION_AUDIO_PREPARATION_FAILED = (
        "transcription.audio_preparation_failed"
    )

    TOPIC_REVIEW_AUTHENTICATION_FAILED = "topic_review.authentication_failed"
    TOPIC_REVIEW_REQUEST_REJECTED = "topic_review.request_rejected"
    TOPIC_REVIEW_RATE_LIMITED = "topic_review.rate_limited"
    TOPIC_REVIEW_REQUEST_TIMEOUT = "topic_review.request_timeout"
    TOPIC_REVIEW_SERVICE_UNAVAILABLE = "topic_review.service_unavailable"
    TOPIC_REVIEW_RESPONSE_PROTOCOL_INVALID = (
        "topic_review.response_protocol_invalid"
    )
    TOPIC_REVIEW_GENERATION_REFUSED = "topic_review.generation_refused"
    TOPIC_REVIEW_OUTPUT_TRUNCATED = "topic_review.output_truncated"
    TOPIC_REVIEW_OUTPUT_INVALID = "topic_review.output_invalid"

    SUBTITLE_OPTIMIZATION_AUTHENTICATION_FAILED = (
        "subtitle_optimization.authentication_failed"
    )
    SUBTITLE_OPTIMIZATION_REQUEST_REJECTED = (
        "subtitle_optimization.request_rejected"
    )
    SUBTITLE_OPTIMIZATION_RATE_LIMITED = "subtitle_optimization.rate_limited"
    SUBTITLE_OPTIMIZATION_REQUEST_TIMEOUT = (
        "subtitle_optimization.request_timeout"
    )
    SUBTITLE_OPTIMIZATION_SERVICE_UNAVAILABLE = (
        "subtitle_optimization.service_unavailable"
    )
    SUBTITLE_OPTIMIZATION_RESPONSE_PROTOCOL_INVALID = (
        "subtitle_optimization.response_protocol_invalid"
    )
    SUBTITLE_OPTIMIZATION_GENERATION_REFUSED = (
        "subtitle_optimization.generation_refused"
    )
    SUBTITLE_OPTIMIZATION_OUTPUT_TRUNCATED = (
        "subtitle_optimization.output_truncated"
    )
    SUBTITLE_OPTIMIZATION_OUTPUT_INVALID = "subtitle_optimization.output_invalid"

    MEDIA_PROCESSING_FAILED = "media.processing_failed"
    CACHE_INFRASTRUCTURE_FAILED = "cache.infrastructure_failed"
    DIAGNOSTICS_WRITE_FAILED = "diagnostics.write_failed"
    WORKSPACE_CLEANUP_FAILED = "workspace.cleanup_failed"

    DELIVERY_BUILD_FAILED = "delivery.build_failed"
    DELIVERY_EXPORT_FAILED = "delivery.export_failed"
    DELIVERY_VERIFICATION_FAILED = "delivery.verification_failed"
    DELIVERY_CLEANUP_FAILED = "delivery.cleanup_failed"

    PUBLICATION_COMMIT_FAILED = "publication.commit_failed"
    PUBLICATION_BACKUP_FAILED = "publication.backup_failed"
    PUBLICATION_ROLLBACK_FAILED = "publication.rollback_failed"

    INTERNAL_UNEXPECTED = "internal.unexpected"


class OperatorAction(str, Enum):
    """运行诊断提供给操作员的稳定下一步动作。"""

    FIX_CONFIGURATION = "fix_configuration"
    INSTALL_OR_UPGRADE_DEPENDENCY = "install_or_upgrade_dependency"
    CHECK_INPUT_MEDIA = "check_input_media"
    CHECK_CREDENTIALS = "check_credentials"
    RETRY_LATER = "retry_later"
    FREE_DISK_SPACE = "free_disk_space"
    INSPECT_PUBLICATION_BACKUP = "inspect_publication_backup"
    REPORT_INTERNAL_ERROR = "report_internal_error"


class RunStage(str, Enum):
    """错误可归属的封闭运行阶段。"""

    INITIALIZED = "initialized"
    PREFLIGHT = "preflight"
    SOURCE_ANALYSIS = "source_analysis"
    TRANSCRIPTION = "transcription"
    CANDIDATE_PLANNING = "candidate_planning"
    TOPIC_REVIEW = "topic_review"
    DELIVERY_BUILD = "delivery_build"
    DELIVERY_VERIFICATION = "delivery_verification"
    PUBLISHING = "publishing"


class ErrorModule(str, Enum):
    """可以拥有公共终态错误的封闭深模块。"""

    APPLICATION = "application"
    CONFIGURATION = "configuration"
    WORKSPACE = "workspace"
    READINESS = "readiness"
    SOURCE_ANALYSIS = "source_analysis"
    TRANSCRIPTION = "transcription"
    CLIP_PLANNING = "clip_planning"
    TOPIC_REVIEW = "topic_review"
    SUBTITLE_OPTIMIZATION = "subtitle_optimization"
    DELIVERY_BUILD = "delivery_build"
    DELIVERY_VERIFICATION = "delivery_verification"
    PUBLICATION = "publication"
    RUN_DIAGNOSTICS = "run_diagnostics"
    CACHE = "cache"
    RUNTIME = "runtime"


class RemoteRequestId(str):
    """Adapter 原始请求 ID 的不可逆脱敏投影。"""

    __slots__ = ()

    def __new__(cls, _value: str = "") -> "RemoteRequestId":
        raise TypeError("RemoteRequestId 只能由 Adapter 脱敏创建")

    @classmethod
    def from_adapter(cls, value: str) -> "RemoteRequestId":
        """哈希 Adapter 明确披露的远端请求 ID，不保留原值。"""
        if not isinstance(value, str):
            raise TypeError("远端请求 ID 必须是字符串")
        if not 1 <= len(value) <= 256 or not value.isprintable():
            raise ValueError("远端请求 ID 必须是 1 到 256 个可打印字符")
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return str.__new__(cls, f"sha256:{digest}")


class DetectedVersion(str):
    """由 Readiness 解析并规范化的已检测版本事实。"""

    __slots__ = ()

    def __new__(cls, _value: str = "") -> "DetectedVersion":
        raise TypeError("DetectedVersion 只能由 Readiness 签发")

    @classmethod
    def from_readiness(cls, value: str) -> "DetectedVersion":
        """签发不含原始命令输出的规范版本事实。"""
        normalized = _string_matching(
            value,
            pattern=_DETECTED_VERSION,
            maximum_length=64,
        )
        return str.__new__(cls, normalized)


class RequiredVersion(str):
    """由 Readiness 认证基线签发的版本要求。"""

    __slots__ = ()

    def __new__(cls, _value: str = "") -> "RequiredVersion":
        raise TypeError("RequiredVersion 只能由 Readiness 签发")

    @classmethod
    def from_readiness(cls, value: str) -> "RequiredVersion":
        """签发来自认证基线而非动态输入的版本要求。"""
        normalized = _string_matching(
            value,
            pattern=_REQUIRED_VERSION,
            maximum_length=64,
        )
        return str.__new__(cls, normalized)


class _InternalSourceModule(str):
    __slots__ = ()

    def __new__(cls, _value: str = "") -> "_InternalSourceModule":
        raise TypeError("内部模块位置只能由 Runtime 签发")


class _InternalFunction(str):
    __slots__ = ()

    def __new__(cls, _value: str = "") -> "_InternalFunction":
        raise TypeError("内部函数位置只能由 Runtime 签发")


class _InternalLine(int):
    __slots__ = ()

    def __new__(cls, _value: int = 0) -> "_InternalLine":
        raise TypeError("内部源码行只能由 Runtime 签发")


@dataclass(frozen=True, slots=True, init=False)
class InternalLocation:
    """Runtime 从包内异常位置签发的安全源码投影。"""

    source_module: _InternalSourceModule
    function: _InternalFunction
    line: _InternalLine

    def __new__(cls) -> "InternalLocation":
        raise TypeError("InternalLocation 只能由 Runtime 签发")

    @classmethod
    def from_runtime(
        cls,
        *,
        source_module: str,
        function: str,
        line: int,
    ) -> "InternalLocation":
        """签发不含绝对源码路径、异常文本或局部变量的位置。"""
        normalized_module = _string_matching(
            source_module,
            pattern=_SOURCE_MODULE,
            maximum_length=128,
        )
        normalized_function = _safe_function(function)
        normalized_line = _integer_between(line, 1, 2**31 - 1)
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "source_module",
            str.__new__(_InternalSourceModule, normalized_module),
        )
        object.__setattr__(
            instance,
            "function",
            str.__new__(_InternalFunction, normalized_function),
        )
        object.__setattr__(
            instance,
            "line",
            int.__new__(_InternalLine, normalized_line),
        )
        return instance


_EMPTY_DIAGNOSTIC_CHOICES: Mapping[str, FrozenSet[Any]] = MappingProxyType({})
_DIAGNOSTIC_VALUE_CHOICES: Mapping[
    ErrorCode, Mapping[str, FrozenSet[Any]]
] = MappingProxyType({})
_REQUIRED_DIAGNOSTICS: Mapping[ErrorCode, FrozenSet[str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """一个公共错误码的稳定注册项。"""

    code: ErrorCode
    category: ErrorCategory
    safe_message: str
    retryable_in_new_run: bool
    operator_action: OperatorAction
    allowed_diagnostics: FrozenSet[str]

    @property
    def exit_code(self) -> ExitCode:
        """由粗粒度类别投影稳定退出码。"""
        return self.category.exit_code

    @property
    def diagnostic_value_choices(self) -> Mapping[str, FrozenSet[Any]]:
        """返回按当前错误码收窄的字段值闭集。"""
        return _DIAGNOSTIC_VALUE_CHOICES.get(
            self.code,
            _EMPTY_DIAGNOSTIC_CHOICES,
        )

    @property
    def required_diagnostics(self) -> FrozenSet[str]:
        """返回该故障发生时必然已知的安全事实字段。"""
        return _REQUIRED_DIAGNOSTICS.get(self.code, frozenset())


def _definition(
    code: ErrorCode,
    category: ErrorCategory,
    safe_message: str,
    operator_action: OperatorAction,
    allowed_diagnostics: FrozenSet[str] = frozenset(),
    retryable_in_new_run: bool = True,
) -> ErrorDefinition:
    return ErrorDefinition(
        code=code,
        category=category,
        safe_message=safe_message,
        retryable_in_new_run=retryable_in_new_run,
        operator_action=operator_action,
        allowed_diagnostics=allowed_diagnostics,
    )


def _provider_definitions() -> list[ErrorDefinition]:
    definitions = []
    phases = {
        "transcription": "语音识别",
        "topic_review": "主题评审",
        "subtitle_optimization": "字幕优化",
    }
    conditions = {
        "authentication_failed": (
            "服务拒绝了已配置的凭据。",
            OperatorAction.CHECK_CREDENTIALS,
        ),
        "request_rejected": ("服务拒绝了请求。", OperatorAction.RETRY_LATER),
        "rate_limited": ("服务触发了请求限流。", OperatorAction.RETRY_LATER),
        "request_timeout": ("服务请求超时。", OperatorAction.RETRY_LATER),
        "service_unavailable": ("服务暂时不可用。", OperatorAction.RETRY_LATER),
        "response_protocol_invalid": (
            "服务响应不符合协议。",
            OperatorAction.RETRY_LATER,
        ),
        "generation_refused": ("服务拒绝生成结果。", OperatorAction.RETRY_LATER),
        "output_truncated": ("服务输出被截断。", OperatorAction.RETRY_LATER),
        "output_invalid": ("服务输出未通过业务校验。", OperatorAction.RETRY_LATER),
    }
    provider_fields = frozenset(
        {
            "http_status",
            "remote_request_id",
            "attempt",
            "reason_code",
        }
    )
    provider_output_fields = provider_fields | {"finish_reason"}
    for namespace, phase_name in phases.items():
        for condition, (message, action) in conditions.items():
            code = ErrorCode(f"{namespace}.{condition}")
            definitions.append(
                _definition(
                    code=code,
                    category=ErrorCategory.EXTERNAL_SERVICE,
                    safe_message=f"{phase_name}{message}",
                    operator_action=action,
                    allowed_diagnostics=(
                        provider_output_fields
                        if condition
                        in {
                            "generation_refused",
                            "output_truncated",
                            "output_invalid",
                        }
                        else provider_fields
                    ),
                )
            )
    return definitions


def _build_error_registry() -> Mapping[ErrorCode, ErrorDefinition]:
    configuration_schema_fields = frozenset(
        {"field", "fields", "schema_version", "reason_code"}
    )
    environment_version_fields = frozenset(
        {
            "component",
            "detected_version",
            "required_version",
            "operation",
            "reason_code",
        }
    )
    environment_filesystem_fields = frozenset(
        {"component", "operation", "reason_code"}
    )
    media_fields = frozenset(
        {
            "operation",
            "media_exit_code",
            "reason_code",
            "stderr_length",
            "stderr_sha256",
        }
    )
    delivery_fields = frozenset(
        {
            "operation",
            "artifact_role",
            "reason_code",
            "media_exit_code",
            "stderr_length",
            "stderr_sha256",
        }
    )
    definitions = [
        _definition(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            ErrorCategory.CONFIGURATION,
            "配置 schema 不受支持或不合法。",
            OperatorAction.FIX_CONFIGURATION,
            configuration_schema_fields,
        ),
        _definition(
            ErrorCode.CONFIG_VALUE_INVALID,
            ErrorCategory.CONFIGURATION,
            "配置包含不合法的值。",
            OperatorAction.FIX_CONFIGURATION,
            frozenset({"field", "fields", "reason_code"}),
        ),
        _definition(
            ErrorCode.CONFIG_CONFLICT,
            ErrorCategory.CONFIGURATION,
            "配置字段之间存在冲突。",
            OperatorAction.FIX_CONFIGURATION,
            frozenset({"fields", "reason_code"}),
        ),
        _definition(
            ErrorCode.CONFIG_CREDENTIAL_MISSING,
            ErrorCategory.CONFIGURATION,
            "缺少所需的供应商凭据。",
            OperatorAction.CHECK_CREDENTIALS,
            frozenset({"capability"}),
        ),
        _definition(
            ErrorCode.CONFIG_HTTPS_REQUIRED,
            ErrorCategory.CONFIGURATION,
            "供应商地址必须使用 HTTPS。",
            OperatorAction.FIX_CONFIGURATION,
            frozenset({"field"}),
        ),
        _definition(
            ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED,
            ErrorCategory.ENVIRONMENT,
            "当前平台不在认证生产环境范围内。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            ErrorCategory.ENVIRONMENT,
            "当前 Python 版本不受支持。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID,
            ErrorCategory.ENVIRONMENT,
            "安装清单缺失或未通过校验。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            ErrorCategory.ENVIRONMENT,
            "FFmpeg 不可用或缺少所需能力。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
            ErrorCategory.ENVIRONMENT,
            "ffprobe 不可用或缺少所需能力。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
            ErrorCategory.ENVIRONMENT,
            "烧录字幕所需字体不可用。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
            ErrorCategory.ENVIRONMENT,
            "TLS 证书校验能力不可用。",
            OperatorAction.INSTALL_OR_UPGRADE_DEPENDENCY,
            environment_version_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
            ErrorCategory.ENVIRONMENT,
            "受管 workspace 不可写。",
            OperatorAction.FREE_DISK_SPACE,
            environment_filesystem_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
            ErrorCategory.ENVIRONMENT,
            "workspace 不支持所需的原子发布语义。",
            OperatorAction.FIX_CONFIGURATION,
            environment_filesystem_fields,
        ),
        _definition(
            ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
            ErrorCategory.ENVIRONMENT,
            "运行诊断包无法初始化。",
            OperatorAction.FREE_DISK_SPACE,
            environment_filesystem_fields,
        ),
        _definition(
            ErrorCode.INPUT_MISSING,
            ErrorCategory.INPUT,
            "输入素材不存在。",
            OperatorAction.CHECK_INPUT_MEDIA,
            frozenset({"reason_code"}),
        ),
        _definition(
            ErrorCode.INPUT_UNREADABLE,
            ErrorCategory.INPUT,
            "输入素材不可读取。",
            OperatorAction.CHECK_INPUT_MEDIA,
            frozenset({"reason_code"}),
        ),
        _definition(
            ErrorCode.INPUT_UNSUPPORTED,
            ErrorCategory.INPUT,
            "输入素材格式不受支持。",
            OperatorAction.CHECK_INPUT_MEDIA,
            frozenset({"reason_code"}),
        ),
        _definition(
            ErrorCode.INPUT_MEDIA_INVALID,
            ErrorCategory.INPUT,
            "输入素材不是合法可处理的媒体。",
            OperatorAction.CHECK_INPUT_MEDIA,
            frozenset(
                {
                    "reason_code",
                    "media_exit_code",
                    "stderr_length",
                    "stderr_sha256",
                }
            ),
        ),
        _definition(
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            ErrorCategory.INPUT,
            "输入素材缺少所需的音频或视频流。",
            OperatorAction.CHECK_INPUT_MEDIA,
            frozenset({"reason_code", "stream_type"}),
        ),
    ]
    definitions.extend(_provider_definitions())
    definitions.extend(
        [
            _definition(
                ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
                ErrorCategory.EXTERNAL_SERVICE,
                "语音覆盖补救后仍不完整。",
                OperatorAction.RETRY_LATER,
                frozenset({"gap_count", "gap_duration_ms", "reason_code"}),
            ),
            _definition(
                ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
                ErrorCategory.LOCAL_PROCESSING,
                "语音识别音频准备失败。",
                OperatorAction.CHECK_INPUT_MEDIA,
                media_fields,
            ),
            _definition(
                ErrorCode.MEDIA_PROCESSING_FAILED,
                ErrorCategory.LOCAL_PROCESSING,
                "本地媒体处理失败。",
                OperatorAction.CHECK_INPUT_MEDIA,
                media_fields,
            ),
            _definition(
                ErrorCode.CACHE_INFRASTRUCTURE_FAILED,
                ErrorCategory.LOCAL_PROCESSING,
                "处理缓存基础设施失败。",
                OperatorAction.FREE_DISK_SPACE,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.DIAGNOSTICS_WRITE_FAILED,
                ErrorCategory.LOCAL_PROCESSING,
                "运行诊断无法继续持久化。",
                OperatorAction.FREE_DISK_SPACE,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.WORKSPACE_CLEANUP_FAILED,
                ErrorCategory.LOCAL_PROCESSING,
                "受管 workspace 清理不完整。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.DELIVERY_BUILD_FAILED,
                ErrorCategory.DELIVERY,
                "标准交付物构建失败。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                frozenset({"operation", "artifact_role", "reason_code"}),
            ),
            _definition(
                ErrorCode.DELIVERY_EXPORT_FAILED,
                ErrorCategory.DELIVERY,
                "短视频导出失败。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                delivery_fields,
            ),
            _definition(
                ErrorCode.DELIVERY_VERIFICATION_FAILED,
                ErrorCategory.DELIVERY,
                "标准交付物完整性验证失败。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                delivery_fields,
            ),
            _definition(
                ErrorCode.DELIVERY_CLEANUP_FAILED,
                ErrorCategory.DELIVERY,
                "未发布标准交付物清理失败。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.PUBLICATION_COMMIT_FAILED,
                ErrorCategory.PUBLICATION,
                "标准交付物原子发布失败。",
                OperatorAction.INSPECT_PUBLICATION_BACKUP,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.PUBLICATION_BACKUP_FAILED,
                ErrorCategory.PUBLICATION,
                "上一版标准交付物备份失败。",
                OperatorAction.INSPECT_PUBLICATION_BACKUP,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.PUBLICATION_ROLLBACK_FAILED,
                ErrorCategory.PUBLICATION,
                "标准交付物发布回滚失败。",
                OperatorAction.INSPECT_PUBLICATION_BACKUP,
                frozenset({"operation", "reason_code"}),
            ),
            _definition(
                ErrorCode.INTERNAL_UNEXPECTED,
                ErrorCategory.INTERNAL,
                "发生未分类的内部错误。",
                OperatorAction.REPORT_INTERNAL_ERROR,
                frozenset({"source_module", "function", "line"}),
                retryable_in_new_run=False,
            ),
        ]
    )

    registry = {definition.code: definition for definition in definitions}
    if len(registry) != len(definitions) or set(registry) != set(ErrorCode):
        raise RuntimeError("公共错误注册表必须且只能覆盖全部 ErrorCode")
    return MappingProxyType(registry)


ERROR_REGISTRY = _build_error_registry()


def get_error_definition(code: ErrorCode | str) -> ErrorDefinition:
    """从封闭注册表读取公共错误定义。"""
    try:
        normalized = code if isinstance(code, ErrorCode) else ErrorCode(code)
    except ValueError as exc:
        raise ValueError(f"未知公共错误码：{code}") from exc
    return ERROR_REGISTRY[normalized]


_CAPABILITIES = frozenset(
    {"transcription", "topic_review", "subtitle_optimization"}
)
_STREAM_TYPES = frozenset({"audio", "video"})
_ARTIFACT_ROLES = frozenset(
    {
        "delivery_manifest",
        "faithful_transcript",
        "faithful_transcript_rendering",
        "clip_plan",
        "short_video_catalog",
        "human_report",
        "short_video_media",
    }
)
_STABLE_CODE = re.compile(
    r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,7}"
)
_CONFIGURATION_FIELD = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
)
_CONFIGURATION_SCHEMA_VERSION = re.compile(
    r"(?:configuration|course_context)\.v[1-9][0-9]*"
)
_DETECTED_VERSION = re.compile(
    r"v?[0-9]+(?:\.[0-9]+){0,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_REQUIRED_VERSION = re.compile(
    r"(?:[<>=!~^]{0,2})v?[0-9]+(?:\.[0-9]+){0,3}"
    r"(?:,(?:[<>=!~^]{0,2})v?[0-9]+(?:\.[0-9]+){0,3})*"
)
_REMOTE_REQUEST_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_MODULE = re.compile(r"video_auto_editor(?:\.[a-z][a-z0-9_]*)*")
_FUNCTION = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*|<[a-z_][a-z0-9_]*>)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SENSITIVE_DIAGNOSTIC_FRAGMENT = re.compile(
    r"(?:^|_)(?:authorization|credential|password|secret|token|api_key)(?:_|$)",
    re.IGNORECASE,
)


def _build_diagnostic_value_choices() -> Mapping[
    ErrorCode, Mapping[str, FrozenSet[Any]]
]:
    choices: dict[ErrorCode, dict[str, FrozenSet[Any]]] = {}

    def allow(
        code: ErrorCode,
        field: str,
        values: FrozenSet[Any],
    ) -> None:
        choices.setdefault(code, {})[field] = values

    reason_codes: dict[ErrorCode, FrozenSet[str]] = {
        ErrorCode.CONFIG_SCHEMA_INVALID: frozenset(
            """
            schema.malformed_json schema.root_not_object schema.version_missing
            schema.version_unsupported schema.unknown_field schema.null_forbidden
            """.split()
        ),
        ErrorCode.CONFIG_VALUE_INVALID: frozenset(
            """
            value.wrong_type value.empty value.invalid_format value.invalid_enum
            value.out_of_range value.too_many_items value.duplicate_item
            """.split()
        ),
        ErrorCode.CONFIG_CONFLICT: frozenset(
            """
            conflict.incomplete_adapter conflict.duration_order
            conflict.mutually_exclusive
            """.split()
        ),
        ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED: frozenset(
            """
            platform.os_unsupported platform.release_unsupported
            platform.architecture_unsupported
            """.split()
        ),
        ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED: frozenset(
            """
            python.implementation_unsupported python.version_too_old
            python.version_too_new python.venv_required python.prefix_mismatch
            """.split()
        ),
        ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID: frozenset(
            """
            manifest.missing manifest.unreadable manifest.schema_invalid
            manifest.version_mismatch manifest.digest_mismatch
            manifest.prefix_mismatch
            """.split()
        ),
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE: frozenset(
            """
            tool.missing tool.version_unreadable tool.version_unsupported
            tool.version_mismatch ffmpeg.filter_missing ffmpeg.encoder_missing
            ffmpeg.smoke_test_failed
            """.split()
        ),
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE: frozenset(
            """
            tool.missing tool.version_unreadable tool.version_unsupported
            tool.version_mismatch ffprobe.probe_failed
            """.split()
        ),
        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE: frozenset(
            """
            font.fontconfig_missing font.not_found font.family_mismatch
            font.file_unreadable font.burn_test_failed
            """.split()
        ),
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE: frozenset(
            """
            tls.ca_store_unavailable tls.ca_store_empty
            tls.verification_unavailable
            """.split()
        ),
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE: frozenset(
            """
            filesystem.create_failed filesystem.not_directory
            filesystem.marker_invalid filesystem.permission_denied
            filesystem.write_failed filesystem.file_sync_failed
            filesystem.directory_sync_failed filesystem.lock_failed
            workspace.ownership_changed workspace.symlink_encountered
            workspace.permission_denied workspace.io_failed
            """.split()
        ),
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED: frozenset(
            """
            filesystem.cross_device filesystem.atomic_replace_failed
            filesystem.file_sync_failed filesystem.directory_sync_failed
            filesystem.cleanup_failed
            """.split()
        ),
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE: frozenset(
            """
            diagnostics.create_failed diagnostics.open_failed
            diagnostics.append_failed diagnostics.file_sync_failed
            diagnostics.directory_sync_failed
            """.split()
        ),
        ErrorCode.INPUT_MISSING: frozenset({"input.not_found"}),
        ErrorCode.INPUT_UNREADABLE: frozenset(
            {
                "input.not_regular_file",
                "input.permission_denied",
                "input.read_failed",
            }
        ),
        ErrorCode.INPUT_UNSUPPORTED: frozenset(
            {
                "input.extension_unsupported",
                "input.container_unsupported",
                "input.codec_unsupported",
            }
        ),
        ErrorCode.INPUT_MEDIA_INVALID: frozenset(
            {
                "media.empty",
                "media.probe_failed",
                "media.container_invalid",
                "media.duration_invalid",
            }
        ),
        ErrorCode.INPUT_REQUIRED_STREAM_MISSING: frozenset(
            {"media.stream_missing"}
        ),
        ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE: frozenset(
            """
            coverage.gap_remaining coverage.conflict_remaining
            coverage.budget_exhausted coverage.no_progress
            coverage.evidence_inconclusive
            """.split()
        ),
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED: frozenset(
            """
            media.spawn_failed media.process_failed media.output_missing
            media.output_invalid
            """.split()
        ),
        ErrorCode.MEDIA_PROCESSING_FAILED: frozenset(
            """
            media.spawn_failed media.process_failed media.output_missing
            media.output_invalid media.probe_failed
            """.split()
        ),
        ErrorCode.CACHE_INFRASTRUCTURE_FAILED: frozenset(
            """
            cache.permission_denied cache.disk_full cache.lock_failed
            cache.read_failed cache.write_failed cache.file_sync_failed
            cache.directory_sync_failed cache.atomic_replace_failed
            cache.quarantine_failed
            """.split()
        ),
        ErrorCode.DIAGNOSTICS_WRITE_FAILED: frozenset(
            """
            diagnostics.permission_denied diagnostics.disk_full
            diagnostics.append_failed diagnostics.file_sync_failed
            diagnostics.serialization_failed diagnostics.atomic_replace_failed
            diagnostics.directory_sync_failed
            """.split()
        ),
        ErrorCode.WORKSPACE_CLEANUP_FAILED: frozenset(
            """
            workspace.ownership_changed workspace.symlink_encountered
            workspace.permission_denied workspace.remove_failed
            workspace.directory_sync_failed
            """.split()
        ),
        ErrorCode.DELIVERY_BUILD_FAILED: frozenset(
            """
            delivery.invariant_violation delivery.serialization_failed
            delivery.render_failed delivery.write_failed
            delivery.file_sync_failed delivery.directory_sync_failed
            """.split()
        ),
        ErrorCode.DELIVERY_EXPORT_FAILED: frozenset(
            """
            media.spawn_failed media.process_failed media.output_missing
            media.output_invalid media.subtitle_burn_failed media.probe_failed
            """.split()
        ),
        ErrorCode.DELIVERY_VERIFICATION_FAILED: frozenset(
            """
            verification.schema_invalid verification.run_id_mismatch
            verification.identity_invalid verification.identity_duplicate
            verification.reference_dangling verification.result_kind_mismatch
            verification.path_unsafe verification.symlink_present
            verification.file_set_mismatch verification.length_mismatch
            verification.digest_mismatch verification.media_invalid
            verification.stream_missing verification.duration_mismatch
            verification.transcript_mismatch
            verification.sidecar_subtitle_present
            """.split()
        ),
        ErrorCode.DELIVERY_CLEANUP_FAILED: frozenset(
            """
            workspace.ownership_changed workspace.symlink_encountered
            workspace.permission_denied workspace.remove_failed
            workspace.directory_sync_failed
            """.split()
        ),
        ErrorCode.PUBLICATION_COMMIT_FAILED: frozenset(
            """
            publication.binding_changed publication.snapshot_changed
            publication.destination_not_empty publication.atomic_replace_failed
            publication.directory_sync_failed publication.commit_state_uncertain
            """.split()
        ),
        ErrorCode.PUBLICATION_BACKUP_FAILED: frozenset(
            """
            publication.ownership_changed publication.previous_remove_failed
            publication.backup_move_failed publication.directory_sync_failed
            """.split()
        ),
        ErrorCode.PUBLICATION_ROLLBACK_FAILED: frozenset(
            """
            publication.new_delivery_remove_failed
            publication.delivery_restore_failed
            publication.previous_restore_failed publication.directory_sync_failed
            publication.rollback_state_uncertain
            """.split()
        ),
    }

    provider_reasons = {
        "authentication_failed": frozenset(
            {
                "authentication.credential_rejected",
                "authentication.credential_expired",
                "authentication.permission_denied",
            }
        ),
        "request_rejected": frozenset(
            {
                "request.invalid",
                "request.payload_too_large",
                "request.parameter_unsupported",
                "request.content_rejected",
            }
        ),
        "rate_limited": frozenset(
            {
                "rate_limit.requests",
                "rate_limit.tokens",
                "rate_limit.concurrency",
                "rate_limit.quota",
            }
        ),
        "request_timeout": frozenset(
            {
                "timeout.connect",
                "timeout.write",
                "timeout.read",
                "timeout.overall",
            }
        ),
        "service_unavailable": frozenset(
            {
                "transport.dns_failed",
                "transport.connection_failed",
                "transport.tls_failed",
                "service.server_error",
                "service.overloaded",
            }
        ),
        "response_protocol_invalid": frozenset(
            {
                "protocol.status_invalid",
                "protocol.content_type_invalid",
                "protocol.body_invalid",
                "protocol.json_invalid",
                "protocol.field_missing",
                "protocol.field_type_invalid",
            }
        ),
        "generation_refused": frozenset(
            {"generation.provider_refused", "generation.safety_refused"}
        ),
        "output_truncated": frozenset(
            {
                "output.length_limit",
                "output.body_truncated",
                "output.incomplete",
            }
        ),
    }
    provider_output_invalid = {
        "transcription": frozenset(
            {
                "output.empty_with_speech",
                "output.text_invalid",
                "output.time_invalid",
                "output.overlap_text_mismatch",
                "output.char_timing_invalid",
                "output.out_of_bounds",
            }
        ),
        "topic_review": frozenset(
            {
                "output.structure_invalid",
                "output.candidate_missing",
                "output.candidate_duplicate",
                "output.candidate_unknown",
                "output.score_invalid",
                "output.boundary_invalid",
                "output.constraint_failed",
            }
        ),
        "subtitle_optimization": frozenset(
            {
                "output.structure_invalid",
                "output.not_subsequence",
                "output.character_added",
                "output.character_reordered",
                "output.line_break_invalid",
                "output.alignment_failed",
                "output.display_constraint_failed",
            }
        ),
    }
    http_statuses: dict[str, FrozenSet[int]] = {
        "authentication_failed": frozenset({401, 403}),
        "request_rejected": frozenset(
            status
            for status in range(400, 500)
            if status not in {401, 403, 408, 429}
        ),
        "rate_limited": frozenset({429}),
        "request_timeout": frozenset({408, 504}),
        "service_unavailable": frozenset(range(500, 600)),
        "response_protocol_invalid": frozenset(range(100, 600)),
        "generation_refused": frozenset(range(200, 300)),
        "output_truncated": frozenset(range(200, 300)),
        "output_invalid": frozenset(range(200, 300)),
    }
    for namespace in (
        "transcription",
        "topic_review",
        "subtitle_optimization",
    ):
        for condition in http_statuses:
            code = ErrorCode(f"{namespace}.{condition}")
            allow(
                code,
                "reason_code",
                (
                    provider_output_invalid[namespace]
                    if condition == "output_invalid"
                    else provider_reasons[condition]
                ),
            )
            allow(code, "http_status", http_statuses[condition])
            if condition == "generation_refused":
                allow(
                    code,
                    "finish_reason",
                    frozenset({"content_filter", "refusal"}),
                )
            elif condition == "output_truncated":
                allow(code, "finish_reason", frozenset({"length"}))
            elif condition == "output_invalid":
                allow(code, "finish_reason", frozenset({"stop"}))

    for code, values in reason_codes.items():
        allow(code, "reason_code", values)

    components = {
        ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED: frozenset(
            {"platform", "architecture"}
        ),
        ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED: frozenset(
            {"python", "virtual_environment"}
        ),
        ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID: frozenset(
            {"installation_manifest", "application"}
        ),
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE: frozenset({"ffmpeg"}),
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE: frozenset({"ffprobe"}),
        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE: frozenset(
            {"font", "fontconfig", "subtitle_font"}
        ),
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE: frozenset({"tls_ca"}),
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE: frozenset({"workspace"}),
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED: frozenset(
            {"atomic_publication", "publication_filesystem"}
        ),
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE: frozenset(
            {"diagnostics", "run_diagnostics"}
        ),
    }
    for code, values in components.items():
        allow(code, "component", values)

    operations = {
        ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED: frozenset(
            {"platform.detect", "architecture.detect"}
        ),
        ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED: frozenset(
            {"python.inspect", "python.verify_venv", "python.verify_prefix"}
        ),
        ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID: frozenset(
            {"manifest.read", "manifest.verify"}
        ),
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE: frozenset(
            {
                "ffmpeg.locate",
                "ffmpeg.inspect",
                "ffmpeg.transcode",
                "ffmpeg.subtitle_burn",
            }
        ),
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE: frozenset(
            {"ffprobe.locate", "ffprobe.inspect", "ffprobe.probe"}
        ),
        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE: frozenset(
            {"fontconfig.list", "fontconfig.match", "ffmpeg.subtitle_burn"}
        ),
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE: frozenset({"tls.load_ca"}),
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE: frozenset(
            {
                "workspace.create",
                "workspace.verify",
                "workspace.lock",
                "workspace.write_probe",
                "workspace.access",
            }
        ),
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED: frozenset(
            {
                "filesystem.atomic_replace_probe",
                "filesystem.file_sync_probe",
                "filesystem.directory_sync_probe",
                "filesystem.cleanup_probe",
            }
        ),
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE: frozenset(
            {
                "diagnostics.initialize",
                "diagnostics.append",
                "diagnostics.sync",
            }
        ),
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED: frozenset(
            {"ffmpeg.transcode", "ffprobe.probe"}
        ),
        ErrorCode.MEDIA_PROCESSING_FAILED: frozenset(
            {
                "ffmpeg.transcode",
                "ffmpeg.subtitle_burn",
                "ffprobe.probe",
            }
        ),
        ErrorCode.CACHE_INFRASTRUCTURE_FAILED: frozenset(
            {
                "cache.claim",
                "cache.read",
                "cache.quarantine",
                "cache.publish",
                "cache.clear",
            }
        ),
        ErrorCode.DIAGNOSTICS_WRITE_FAILED: frozenset(
            {
                "diagnostics.append",
                "diagnostics.sync",
                "diagnostics.publish_manifest",
            }
        ),
        ErrorCode.WORKSPACE_CLEANUP_FAILED: frozenset({"workspace.cleanup"}),
        ErrorCode.DELIVERY_BUILD_FAILED: frozenset(
            {
                "delivery.serialize",
                "delivery.render_transcript",
                "delivery.render_report",
            }
        ),
        ErrorCode.DELIVERY_EXPORT_FAILED: frozenset(
            {
                "delivery.export",
                "ffmpeg.transcode",
                "ffmpeg.subtitle_burn",
                "ffprobe.probe",
            }
        ),
        ErrorCode.DELIVERY_VERIFICATION_FAILED: frozenset(
            {
                "delivery.verify_schema",
                "delivery.verify_references",
                "delivery.verify_files",
                "delivery.verify_digest",
                "delivery.verify_media",
                "delivery.verify_transcript",
                "ffprobe.probe",
            }
        ),
        ErrorCode.DELIVERY_CLEANUP_FAILED: frozenset(
            {"delivery.cleanup", "workspace.cleanup"}
        ),
        ErrorCode.PUBLICATION_COMMIT_FAILED: frozenset(
            {
                "publication.verify_binding",
                "publication.verify_snapshot",
                "publication.commit",
                "publication.sync",
            }
        ),
        ErrorCode.PUBLICATION_BACKUP_FAILED: frozenset(
            {"publication.backup", "publication.sync"}
        ),
        ErrorCode.PUBLICATION_ROLLBACK_FAILED: frozenset(
            {"publication.rollback", "publication.sync"}
        ),
    }
    for code, values in operations.items():
        allow(code, "operation", values)

    allow(
        ErrorCode.DELIVERY_BUILD_FAILED,
        "artifact_role",
        _ARTIFACT_ROLES,
    )
    allow(
        ErrorCode.DELIVERY_EXPORT_FAILED,
        "artifact_role",
        frozenset({"short_video_media"}),
    )
    allow(
        ErrorCode.DELIVERY_VERIFICATION_FAILED,
        "artifact_role",
        _ARTIFACT_ROLES,
    )

    return MappingProxyType(
        {
            code: MappingProxyType(field_choices)
            for code, field_choices in choices.items()
        }
    )


_DIAGNOSTIC_VALUE_CHOICES = _build_diagnostic_value_choices()
_REQUIRED_DIAGNOSTICS = MappingProxyType(
    {
        ErrorCode.CONFIG_CREDENTIAL_MISSING: frozenset({"capability"}),
        ErrorCode.CONFIG_HTTPS_REQUIRED: frozenset({"field"}),
        ErrorCode.INPUT_REQUIRED_STREAM_MISSING: frozenset({"stream_type"}),
        ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE: frozenset(
            {"gap_count", "gap_duration_ms"}
        ),
        ErrorCode.INTERNAL_UNEXPECTED: frozenset(
            {"source_module", "function", "line"}
        ),
    }
)


def _string_matching(
    value: Any,
    *,
    pattern: re.Pattern[str],
    maximum_length: int,
) -> str:
    if not isinstance(value, str):
        raise TypeError("必须是字符串")
    if len(value) > maximum_length or pattern.fullmatch(value) is None:
        raise ValueError("不符合允许的字符、长度或格式")
    return value


def _enum_string(value: Any, allowed: FrozenSet[str]) -> str:
    if not isinstance(value, str):
        raise TypeError("必须是字符串枚举")
    if value not in allowed:
        raise ValueError("不在允许的稳定枚举中")
    return value


def _integer_between(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"必须位于 {minimum} 到 {maximum} 之间")
    return value


def _config_field(value: Any) -> str:
    normalized = _string_matching(
        value,
        pattern=_CONFIGURATION_FIELD,
        maximum_length=128,
    )
    if _SENSITIVE_DIAGNOSTIC_FRAGMENT.search(normalized):
        raise ValueError("疑似包含敏感值，不得作为配置字段诊断")
    return normalized


def _config_fields(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("必须是配置字段序列")
    if not 1 <= len(value) <= 16:
        raise ValueError("必须包含 1 到 16 个配置字段")
    fields = tuple(sorted(_config_field(item) for item in value))
    if len(set(fields)) != len(fields):
        raise ValueError("不得包含重复配置字段")
    return fields


def _schema_version(value: Any) -> str:
    return _string_matching(
        value,
        pattern=_CONFIGURATION_SCHEMA_VERSION,
        maximum_length=64,
    )


def _detected_version(value: Any) -> str:
    return _string_matching(
        value,
        pattern=_DETECTED_VERSION,
        maximum_length=64,
    )


def _required_version(value: Any) -> str:
    return _string_matching(
        value,
        pattern=_REQUIRED_VERSION,
        maximum_length=64,
    )


def _remote_request_id(value: Any) -> str:
    if not isinstance(value, RemoteRequestId):
        raise TypeError("必须是 Adapter 创建的 RemoteRequestId")
    return _string_matching(
        value,
        pattern=_REMOTE_REQUEST_ID,
        maximum_length=71,
    )


def _stderr_sha256(value: Any) -> str:
    return _string_matching(value, pattern=_SHA256, maximum_length=64)


def _source_module(value: Any) -> str:
    return _string_matching(
        value,
        pattern=_SOURCE_MODULE,
        maximum_length=128,
    )


def _safe_function(value: Any) -> str:
    normalized = _string_matching(
        value,
        pattern=_FUNCTION,
        maximum_length=128,
    )
    if _SENSITIVE_DIAGNOSTIC_FRAGMENT.search(normalized):
        raise ValueError("疑似包含敏感值，不得作为函数诊断")
    return normalized


def _function(value: Any) -> str:
    return _safe_function(value)


def _internal_line(value: Any) -> int:
    return _integer_between(value, 1, 2**31 - 1)


def _stable_code(value: Any) -> str:
    return _string_matching(value, pattern=_STABLE_CODE, maximum_length=128)


def _media_exit_code(value: Any) -> int:
    exit_code = _integer_between(value, -255, 255)
    if exit_code == 0:
        raise ValueError("失败的媒体子进程退出码不能为零")
    return exit_code


_DiagnosticValidator = Callable[[Any], Any]
_DIAGNOSTIC_SCHEMAS: Mapping[str, _DiagnosticValidator] = MappingProxyType(
    {
        "field": _config_field,
        "fields": _config_fields,
        "schema_version": _schema_version,
        "reason_code": _stable_code,
        "capability": lambda value: _enum_string(value, _CAPABILITIES),
        "component": _stable_code,
        "detected_version": _detected_version,
        "required_version": _required_version,
        "operation": _stable_code,
        "media_exit_code": _media_exit_code,
        "stream_type": lambda value: _enum_string(value, _STREAM_TYPES),
        "http_status": lambda value: _integer_between(value, 100, 599),
        "remote_request_id": _remote_request_id,
        "attempt": lambda value: _integer_between(value, 1, 32),
        "finish_reason": _stable_code,
        "gap_count": lambda value: _integer_between(value, 1, 2**31 - 1),
        "gap_duration_ms": lambda value: _integer_between(
            value, 1, 2**53 - 1
        ),
        "stderr_length": lambda value: _integer_between(
            value, 0, 2**53 - 1
        ),
        "stderr_sha256": _stderr_sha256,
        "artifact_role": lambda value: _enum_string(value, _ARTIFACT_ROLES),
        "source_module": _source_module,
        "function": _function,
        "line": _internal_line,
    }
)

_registered_diagnostic_fields = frozenset(
    field
    for definition in ERROR_REGISTRY.values()
    for field in definition.allowed_diagnostics
)
if _registered_diagnostic_fields != frozenset(_DIAGNOSTIC_SCHEMAS):
    raise RuntimeError("错误注册表的诊断字段必须且只能使用已定义的值 schema")

_contextual_diagnostic_fields = frozenset(
    {
        "reason_code",
        "component",
        "operation",
        "http_status",
        "finish_reason",
        "artifact_role",
    }
)
for _definition_with_context in ERROR_REGISTRY.values():
    for _contextual_field in _definition_with_context.allowed_diagnostics:
        if (
            _contextual_field in _contextual_diagnostic_fields
            and _contextual_field
            not in _definition_with_context.diagnostic_value_choices
        ):
            raise RuntimeError(
                f"{_definition_with_context.code.value} 的诊断字段 "
                f"{_contextual_field} 必须定义按错误码收窄的值闭集"
            )
    if not _definition_with_context.required_diagnostics.issubset(
        _definition_with_context.allowed_diagnostics
    ):
        raise RuntimeError(
            f"{_definition_with_context.code.value} 的必需诊断字段必须在允许集合内"
        )


_MEDIA_PROCESS_FAILURE_REASONS = frozenset(
    {
        "media.process_failed",
        "media.probe_failed",
        "media.subtitle_burn_failed",
    }
)
_MEDIA_PROCESS_FACTS = frozenset(
    {"media_exit_code", "stderr_length", "stderr_sha256"}
)
_EMPTY_STDERR_SHA256 = hashlib.sha256(b"").hexdigest()


def _validate_related_diagnostic_fields(source: Mapping[str, Any]) -> None:
    fields = frozenset(source)
    if {"field", "fields"}.issubset(fields):
        raise ValueError("诊断字段 field 与 fields 必须互斥，不能同时出现")
    for related_fields in (
        frozenset({"stderr_length", "stderr_sha256"}),
        frozenset({"gap_count", "gap_duration_ms"}),
        frozenset({"source_module", "function", "line"}),
    ):
        present = fields.intersection(related_fields)
        if present and present != related_fields:
            names = "、".join(sorted(related_fields))
            raise ValueError(f"诊断字段 {names} 必须同时出现")


def _validate_diagnostic_union(diagnostics: Mapping[str, Any]) -> None:
    if (
        diagnostics.get("stderr_length") == 0
        and diagnostics.get("stderr_sha256") != _EMPTY_STDERR_SHA256
    ):
        raise ValueError(
            "诊断字段 stderr_length、stderr_sha256："
            "空 stderr 必须使用空字节串的 SHA-256"
        )

    reason_code = diagnostics.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code.startswith("media."):
        return
    present_process_facts = _MEDIA_PROCESS_FACTS.intersection(diagnostics)
    if reason_code in _MEDIA_PROCESS_FAILURE_REASONS:
        missing_process_facts = _MEDIA_PROCESS_FACTS.difference(diagnostics)
        if missing_process_facts:
            names = "、".join(sorted(missing_process_facts))
            raise ValueError(
                f"诊断字段 {names}：media 进程失败原因必须提供完整子进程事实"
            )
    elif present_process_facts:
        names = "、".join(sorted(present_process_facts))
        raise ValueError(
            f"诊断字段 {names}：当前 media 原因不得携带子进程失败事实"
        )


def _freeze_diagnostics(
    definition: ErrorDefinition,
    diagnostics: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if diagnostics is None:
        source: Mapping[str, Any] = {}
    elif isinstance(diagnostics, MappingABC):
        source = diagnostics
    else:
        raise TypeError("diagnostics 必须是映射")
    if any(not isinstance(key, str) for key in source):
        raise TypeError("diagnostics 的字段名必须是字符串")
    unknown_fields = set(source).difference(definition.allowed_diagnostics)
    if unknown_fields:
        names = ", ".join(sorted(unknown_fields))
        raise ValueError(f"{definition.code.value} 不允许诊断字段：{names}")
    missing_fields = definition.required_diagnostics.difference(source)
    if missing_fields:
        names = "、".join(sorted(missing_fields))
        raise ValueError(
            f"{definition.code.value} 缺少必需诊断字段：{names}；"
            "这些诊断字段必须提供"
        )
    _validate_related_diagnostic_fields(source)
    frozen = {}
    for key, value in sorted(source.items()):
        try:
            frozen_value = _DIAGNOSTIC_SCHEMAS[key](value)
            if (
                definition.code is ErrorCode.CONFIG_HTTPS_REQUIRED
                and key == "field"
                and not frozen_value.endswith(".endpoint")
            ):
                raise ValueError(
                    f"不适用于 {definition.code.value} 的 HTTPS endpoint 字段"
                )
            contextual_choices = definition.diagnostic_value_choices.get(key)
            if (
                contextual_choices is not None
                and frozen_value not in contextual_choices
            ):
                raise ValueError(
                    f"不适用于 {definition.code.value} 的稳定值闭集"
                )
            frozen[key] = frozen_value
        except TypeError as exc:
            raise TypeError(f"诊断字段 {key}：{exc}") from exc
        except ValueError as exc:
            raise ValueError(f"诊断字段 {key}：{exc}") from exc
    _validate_diagnostic_union(frozen)
    return MappingProxyType(frozen)


def freeze_error_diagnostics(
    code: ErrorCode | str,
    diagnostics: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """按公共错误注册表校验并冻结脱敏诊断。"""
    return _freeze_diagnostics(
        get_error_definition(code),
        diagnostics,
    )


def _normalize_run_stage(value: RunStage | str) -> RunStage:
    try:
        return value if isinstance(value, RunStage) else RunStage(value)
    except (TypeError, ValueError) as exc:
        error_type = TypeError if not isinstance(value, str) else ValueError
        raise error_type("stage 必须是稳定小写标识闭集中的成员") from exc


def _normalize_error_module(value: ErrorModule | str) -> ErrorModule:
    try:
        return value if isinstance(value, ErrorModule) else ErrorModule(value)
    except (TypeError, ValueError) as exc:
        error_type = TypeError if not isinstance(value, str) else ValueError
        raise error_type("module 必须是稳定小写标识闭集中的成员") from exc


@dataclass(frozen=True, slots=True, init=False, eq=False)
class RunError:
    """由封闭注册表投影的不可变终态错误对象。"""

    error_id: ErrorId
    error_code: ErrorCode
    category: ErrorCategory
    stage: RunStage
    module: ErrorModule
    operation_id: OperationId | None
    event_sequence: int
    safe_message: str
    retryable_in_new_run: bool
    operator_action: OperatorAction
    diagnostics: Mapping[str, Any]

    def __new__(cls) -> "RunError":
        raise TypeError("RunError 只能通过 create 创建")

    @classmethod
    def create(
        cls,
        *,
        code: ErrorCode | str,
        stage: RunStage | str,
        module: ErrorModule | str,
        event_sequence: int,
        operation_id: OperationId | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> "RunError":
        """创建只含注册字段和白名单诊断的终态错误。"""
        definition = get_error_definition(code)
        if not isinstance(event_sequence, int):
            raise TypeError("event_sequence 必须是正整数")
        if isinstance(event_sequence, bool) or event_sequence < 1:
            raise ValueError("event_sequence 必须是正整数")
        if operation_id is not None and not isinstance(operation_id, OperationId):
            raise TypeError("operation_id 必须是 OperationId")

        instance = object.__new__(cls)
        object.__setattr__(instance, "error_id", ErrorId.new())
        object.__setattr__(instance, "error_code", definition.code)
        object.__setattr__(instance, "category", definition.category)
        object.__setattr__(instance, "stage", _normalize_run_stage(stage))
        object.__setattr__(instance, "module", _normalize_error_module(module))
        object.__setattr__(instance, "operation_id", operation_id)
        object.__setattr__(instance, "event_sequence", event_sequence)
        object.__setattr__(instance, "safe_message", definition.safe_message)
        object.__setattr__(
            instance,
            "retryable_in_new_run",
            definition.retryable_in_new_run,
        )
        object.__setattr__(
            instance, "operator_action", definition.operator_action
        )
        object.__setattr__(
            instance,
            "diagnostics",
            _freeze_diagnostics(definition, diagnostics),
        )
        return instance

    @property
    def exit_code(self) -> ExitCode:
        """返回注册定义中的稳定退出码。"""
        return self.category.exit_code
