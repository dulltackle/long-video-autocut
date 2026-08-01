"""业务模块可以提交的封闭脱敏诊断事实。"""

import re
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from ipaddress import IPv6Address
from threading import RLock
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from video_auto_editor.configuration import (
    ConfigurationDiagnosticProjection,
    assert_diagnostic_projection_authentic,
)
from video_auto_editor.delivery.capability import PublishedDelivery
from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ErrorCode,
    RemoteRequestId,
)
from video_auto_editor.runtime.identity import ShortVideoId

from ._model import (
    ArtifactRole,
    CacheNamespace,
    CacheOutcome,
    CertifiedPlatform,
    DeliveryBuildState,
    DeliveryVerificationState,
    ExternalDataCategory,
    ExternalRequestOutcome,
    InterruptionSignal,
    PreflightOutcome,
    ProviderCapability,
    ProviderTransport,
    PublicationState,
    RecoveredNoticeKind,
    ZeroRequestReason,
)

_FACT_SEAL = object()


class _FactKind(str, Enum):
    CONFIGURATION = "configuration"
    CACHE = "cache"
    TRANSCRIPTION_EXECUTION = "transcription_execution"
    SOURCE = "source"
    ENVIRONMENT = "environment"
    INTERRUPTION = "interruption"
    EXTERNAL_SERVICE = "external_service"
    EXTERNAL_SERVICE_ZERO_REQUESTS = "external_service_zero_requests"
    EXTERNAL_REQUEST = "external_request"
    DELIVERY_STATE = "delivery_state"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_VERIFIED = "artifact_verified"
    RECOVERED_NOTICE = "recovered_notice"


@dataclass(frozen=True, slots=True)
class _CacheFact:
    namespace: CacheNamespace
    outcome: CacheOutcome
    singleflight_wait_ms: int | None
    reason_code: str | None
    quarantine_digest_prefix: str | None
    error_code: ErrorCode | None


@dataclass(frozen=True, slots=True)
class _TranscriptionExecutionFact:
    retry_count: int
    recovery_count: int


@dataclass(frozen=True, slots=True)
class _SourceFact:
    sha256: str
    byte_length: int
    duration_ms: int
    course_context_provided: bool
    course_context_sha256: str | None


@dataclass(frozen=True, slots=True)
class _EnvironmentFact:
    certified_platform: CertifiedPlatform
    python_version: DetectedVersion
    ffmpeg_version: DetectedVersion
    ffprobe_version: DetectedVersion
    font_family: str
    font_available: bool
    installation_fingerprint: str
    preflight_outcome: PreflightOutcome


@dataclass(frozen=True, slots=True)
class _ExternalServiceFact:
    capability: ProviderCapability
    adapter_id: str
    provider_id: str
    model_id: str
    configuration_fingerprint: str
    endpoint_origin: str | None
    transport: ProviderTransport
    allowed_data_categories: tuple[ExternalDataCategory, ...]


@dataclass(frozen=True, slots=True)
class _ExternalServiceZeroRequestsFact:
    capability: ProviderCapability
    reason: ZeroRequestReason


@dataclass(frozen=True, slots=True)
class _ExternalRequestFact:
    capability: ProviderCapability
    outcome: ExternalRequestOutcome
    attempt_count: int
    remote_request_id: RemoteRequestId | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class _DeliveryStateFact:
    phase: str
    state: str
    published_delivery: PublishedDelivery | None


@dataclass(frozen=True, slots=True)
class _ArtifactFact:
    role: ArtifactRole
    relative_path: str


@dataclass(frozen=True, slots=True)
class _RecoveredNoticeFact:
    kind: RecoveredNoticeKind
    count: int


@dataclass(frozen=True, slots=True)
class _FactAuthority:
    kind: _FactKind
    payload: object
    signature: object


class DiagnosticFact:
    """只能由 ``Facts`` 工厂签发的不可变安全事实。"""

    __slots__ = ("_kind", "_payload", "_seal", "__weakref__")

    def __new__(cls) -> "DiagnosticFact":
        raise TypeError("DiagnosticFact 只能由 Facts 创建")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("DiagnosticFact 不能由业务模块扩展")

    @classmethod
    def _create(
        cls,
        kind: _FactKind,
        payload: object,
    ) -> "DiagnosticFact":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_kind", kind)
        object.__setattr__(instance, "_payload", payload)
        object.__setattr__(instance, "_seal", _FACT_SEAL)
        authority = _FactAuthority(
            kind=kind,
            payload=payload,
            signature=_fact_signature(payload),
        )
        with _FACT_AUTHORITIES_LOCK:
            _FACT_AUTHORITIES[instance] = authority
        return instance

    def _assert_authentic(self) -> None:
        try:
            with _FACT_AUTHORITIES_LOCK:
                authority = _FACT_AUTHORITIES[self]
                authentic = (
                    type(self) is DiagnosticFact
                    and self._seal is _FACT_SEAL
                    and self._kind is authority.kind
                    and self._payload is authority.payload
                    and _fact_signature(self._payload)
                    == authority.signature
                )
        except (
            AttributeError,
            KeyError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            authentic = False
        if not authentic:
            raise TypeError("诊断事实必须由 Facts 创建")


_FACT_AUTHORITIES: WeakKeyDictionary[
    DiagnosticFact,
    _FactAuthority,
] = WeakKeyDictionary()
_FACT_AUTHORITIES_LOCK = RLock()


class Facts:
    """创建严格白名单事实，不接受消息或任意属性字典。"""

    __slots__ = ()

    @staticmethod
    def configuration(
        projection: ConfigurationDiagnosticProjection,
    ) -> DiagnosticFact:
        """记录 Configuration 已形成的既有安全投影。"""
        if not isinstance(projection, ConfigurationDiagnosticProjection):
            raise TypeError(
                "配置诊断事实必须使用 ConfigurationDiagnosticProjection"
            )
        assert_diagnostic_projection_authentic(projection)
        return DiagnosticFact._create(
            _FactKind.CONFIGURATION,
            projection,
        )

    @staticmethod
    def cache(
        namespace: CacheNamespace,
        outcome: CacheOutcome,
        *,
        singleflight_wait_ms: int | None = None,
        reason_code: str | None = None,
        quarantine_digest_prefix: str | None = None,
        error_code: ErrorCode | None = None,
    ) -> DiagnosticFact:
        """记录不含物理路径、完整身份或 payload 的缓存事实。"""
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("缓存命名空间必须使用 CacheNamespace")
        if not isinstance(outcome, CacheOutcome):
            raise TypeError("缓存结果必须使用 CacheOutcome")
        if singleflight_wait_ms is not None:
            _nonnegative_integer(
                singleflight_wait_ms,
                field="单航班等待毫秒数",
            )
        if reason_code is not None and (
            not isinstance(reason_code, str)
            or _STABLE_REASON_CODE.fullmatch(reason_code) is None
        ):
            raise ValueError("缓存原因码格式不合法")
        if quarantine_digest_prefix is not None:
            if (
                not isinstance(quarantine_digest_prefix, str)
                or _DIGEST_PREFIX.fullmatch(quarantine_digest_prefix) is None
            ):
                if isinstance(quarantine_digest_prefix, str) and re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    quarantine_digest_prefix,
                ):
                    raise ValueError("诊断不得记录完整缓存身份摘要")
                raise ValueError("缓存隔离摘要前缀格式不合法")
        if error_code is not None and not isinstance(error_code, ErrorCode):
            raise TypeError("缓存基础设施错误必须使用 ErrorCode")

        if outcome is CacheOutcome.CORRUPT_QUARANTINED:
            if reason_code is None or quarantine_digest_prefix is None:
                raise ValueError("损坏隔离必须提供稳定原因和有限摘要前缀")
            if error_code is not None:
                raise ValueError("已隔离损坏不是基础设施失败")
        elif outcome is CacheOutcome.INFRASTRUCTURE_FAILED:
            if error_code is not ErrorCode.CACHE_INFRASTRUCTURE_FAILED:
                raise ValueError("缓存基础设施失败必须关联稳定错误码")
            if quarantine_digest_prefix is not None:
                raise ValueError("基础设施失败不得携带缓存身份摘要")
        elif any(
            value is not None
            for value in (
                reason_code,
                quarantine_digest_prefix,
                error_code,
            )
        ):
            raise ValueError("普通缓存结果不得携带故障诊断")

        return DiagnosticFact._create(
            _FactKind.CACHE,
            _CacheFact(
                namespace=namespace,
                outcome=outcome,
                singleflight_wait_ms=singleflight_wait_ms,
                reason_code=reason_code,
                quarantine_digest_prefix=quarantine_digest_prefix,
                error_code=error_code,
            ),
        )

    @staticmethod
    def transcription_execution(
        *,
        retry_count: int,
        recovery_count: int,
    ) -> DiagnosticFact:
        """记录整场转写完成后可验证的中性聚合执行次数。"""
        _nonnegative_integer(retry_count, field="语音识别内部重试次数")
        _nonnegative_integer(recovery_count, field="语音覆盖补救次数")
        return DiagnosticFact._create(
            _FactKind.TRANSCRIPTION_EXECUTION,
            _TranscriptionExecutionFact(
                retry_count=retry_count,
                recovery_count=recovery_count,
            ),
        )

    @staticmethod
    def source(
        *,
        sha256: str,
        byte_length: int,
        duration_ms: int,
        course_context_provided: bool,
        course_context_sha256: str | None = None,
    ) -> DiagnosticFact:
        """记录不含素材路径、文件名或正文的来源证明。"""
        source_digest = _sha256(sha256, field="素材摘要")
        _nonnegative_integer(byte_length, field="素材字节长度")
        _nonnegative_integer(duration_ms, field="素材时长毫秒数")
        if not isinstance(course_context_provided, bool):
            raise TypeError("课程上下文存在性必须是布尔值")
        if course_context_provided:
            if course_context_sha256 is None:
                raise ValueError("已提供课程上下文时必须提供摘要")
            context_digest = _sha256(
                course_context_sha256,
                field="课程上下文摘要",
            )
        else:
            if course_context_sha256 is not None:
                raise ValueError("未提供课程上下文时不得提供摘要")
            context_digest = None
        return DiagnosticFact._create(
            _FactKind.SOURCE,
            _SourceFact(
                sha256=source_digest,
                byte_length=byte_length,
                duration_ms=duration_ms,
                course_context_provided=course_context_provided,
                course_context_sha256=context_digest,
            ),
        )

    @staticmethod
    def environment(
        *,
        certified_platform: CertifiedPlatform,
        python_version: DetectedVersion,
        ffmpeg_version: DetectedVersion,
        ffprobe_version: DetectedVersion,
        font_family: str,
        font_available: bool,
        installation_fingerprint: str,
        preflight_outcome: PreflightOutcome,
    ) -> DiagnosticFact:
        """记录 Readiness 已认证的环境安全投影。"""
        if not isinstance(certified_platform, CertifiedPlatform):
            raise TypeError("认证平台必须使用 CertifiedPlatform")
        for value, field in (
            (python_version, "Python"),
            (ffmpeg_version, "FFmpeg"),
            (ffprobe_version, "ffprobe"),
        ):
            if not isinstance(value, DetectedVersion):
                raise TypeError(f"{field} 版本必须由 Readiness 签发")
        if (
            not isinstance(font_family, str)
            or _FONT_FAMILY.fullmatch(font_family) is None
        ):
            raise ValueError("字体家族不是可披露的规范名称")
        if not isinstance(font_available, bool):
            raise TypeError("字体可用性必须是布尔值")
        fingerprint = _sha256(
            installation_fingerprint,
            field="安装清单指纹",
        )
        if not isinstance(preflight_outcome, PreflightOutcome):
            raise TypeError("预检结果必须使用 PreflightOutcome")
        return DiagnosticFact._create(
            _FactKind.ENVIRONMENT,
            _EnvironmentFact(
                certified_platform=certified_platform,
                python_version=python_version,
                ffmpeg_version=ffmpeg_version,
                ffprobe_version=ffprobe_version,
                font_family=font_family,
                font_available=font_available,
                installation_fingerprint=fingerprint,
                preflight_outcome=preflight_outcome,
            ),
        )

    @staticmethod
    def interruption(signal: InterruptionSignal) -> DiagnosticFact:
        """记录应用首次接受的受控中断信号。"""
        if not isinstance(signal, InterruptionSignal):
            raise TypeError("中断信号必须使用 InterruptionSignal")
        return DiagnosticFact._create(_FactKind.INTERRUPTION, signal)

    @staticmethod
    def external_service(
        *,
        capability: ProviderCapability,
        adapter_id: str,
        provider_id: str,
        model_id: str,
        configuration_fingerprint: str,
        endpoint_origin: str | None,
        transport: ProviderTransport,
        allowed_data_categories: tuple[ExternalDataCategory, ...],
    ) -> DiagnosticFact:
        """记录选定 Adapter 的计划外发白名单，不接收配置字典。"""
        if not isinstance(capability, ProviderCapability):
            raise TypeError("供应商能力必须使用 ProviderCapability")
        adapter = _stable_identifier(adapter_id, field="Adapter 标识")
        provider = _stable_identifier(provider_id, field="供应商标识")
        model = _stable_identifier(model_id, field="模型标识")
        fingerprint = _sha256(
            configuration_fingerprint,
            field="供应商配置指纹",
        )
        if not isinstance(transport, ProviderTransport):
            raise TypeError("供应商传输类型必须使用 ProviderTransport")
        origin = _endpoint_origin(endpoint_origin, transport=transport)
        categories = _external_data_categories(allowed_data_categories)
        _validate_external_data_categories(
            capability,
            categories,
            transport=transport,
        )
        return DiagnosticFact._create(
            _FactKind.EXTERNAL_SERVICE,
            _ExternalServiceFact(
                capability=capability,
                adapter_id=adapter,
                provider_id=provider,
                model_id=model,
                configuration_fingerprint=fingerprint,
                endpoint_origin=origin,
                transport=transport,
                allowed_data_categories=categories,
            ),
        )

    @staticmethod
    def external_service_zero_requests(
        capability: ProviderCapability,
        reason: ZeroRequestReason,
    ) -> DiagnosticFact:
        """明确声明已选服务在本次运行中没有发生远程请求。"""
        if not isinstance(capability, ProviderCapability):
            raise TypeError("供应商能力必须使用 ProviderCapability")
        if not isinstance(reason, ZeroRequestReason):
            raise TypeError("零请求原因必须使用 ZeroRequestReason")
        return DiagnosticFact._create(
            _FactKind.EXTERNAL_SERVICE_ZERO_REQUESTS,
            _ExternalServiceZeroRequestsFact(
                capability=capability,
                reason=reason,
            ),
        )

    @staticmethod
    def external_request(
        capability: ProviderCapability,
        outcome: ExternalRequestOutcome,
        *,
        attempt_count: int,
        remote_request_id: RemoteRequestId | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> DiagnosticFact:
        """记录一次请求的供应商无关、安全结果。"""
        if not isinstance(capability, ProviderCapability):
            raise TypeError("供应商能力必须使用 ProviderCapability")
        if not isinstance(outcome, ExternalRequestOutcome):
            raise TypeError("外部请求结果必须使用 ExternalRequestOutcome")
        _positive_integer(attempt_count, field="外部请求尝试次数")
        if remote_request_id is not None and not isinstance(
            remote_request_id,
            RemoteRequestId,
        ):
            raise TypeError("远端请求标识必须先由 Adapter 脱敏")
        if (input_tokens is None) != (output_tokens is None):
            raise ValueError("供应商 token 用量必须完整提供或完整省略")
        if input_tokens is not None:
            _nonnegative_integer(input_tokens, field="输入 token 数")
            _nonnegative_integer(output_tokens, field="输出 token 数")
        return DiagnosticFact._create(
            _FactKind.EXTERNAL_REQUEST,
            _ExternalRequestFact(
                capability=capability,
                outcome=outcome,
                attempt_count=attempt_count,
                remote_request_id=remote_request_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    @staticmethod
    def delivery_build(state: DeliveryBuildState) -> DiagnosticFact:
        """记录交付构建状态转换。"""
        if not isinstance(state, DeliveryBuildState):
            raise TypeError("交付构建状态必须使用 DeliveryBuildState")
        return DiagnosticFact._create(
            _FactKind.DELIVERY_STATE,
            _DeliveryStateFact(
                phase="build",
                state=state.value,
                published_delivery=None,
            ),
        )

    @staticmethod
    def delivery_verification(
        state: DeliveryVerificationState,
    ) -> DiagnosticFact:
        """记录独立完整性验证状态转换。"""
        if not isinstance(state, DeliveryVerificationState):
            raise TypeError(
                "交付验证状态必须使用 DeliveryVerificationState"
            )
        return DiagnosticFact._create(
            _FactKind.DELIVERY_STATE,
            _DeliveryStateFact(
                phase="verification",
                state=state.value,
                published_delivery=None,
            ),
        )

    @staticmethod
    def publication(
        state: PublicationState,
        *,
        published_delivery: PublishedDelivery | None = None,
    ) -> DiagnosticFact:
        """记录发布状态；提交态必须携带真实发布 capability。"""
        if not isinstance(state, PublicationState):
            raise TypeError("发布状态必须使用 PublicationState")
        if state is PublicationState.COMMITTED:
            if not isinstance(published_delivery, PublishedDelivery):
                raise TypeError("发布提交事实必须包含 PublishedDelivery")
            published_delivery.run_id
        elif published_delivery is not None:
            raise ValueError("非提交发布状态不得携带 PublishedDelivery")
        return DiagnosticFact._create(
            _FactKind.DELIVERY_STATE,
            _DeliveryStateFact(
                phase="publication",
                state=state.value,
                published_delivery=published_delivery,
            ),
        )

    @staticmethod
    def artifact_created(
        role: ArtifactRole,
        *,
        relative_path: str,
    ) -> DiagnosticFact:
        """记录临时交付根下的安全相对产物角色。"""
        return _artifact_fact(
            _FactKind.ARTIFACT_CREATED,
            role,
            relative_path,
        )

    @staticmethod
    def artifact_verified(
        role: ArtifactRole,
        *,
        relative_path: str,
    ) -> DiagnosticFact:
        """记录消费者视角已经验证的安全相对产物角色。"""
        return _artifact_fact(
            _FactKind.ARTIFACT_VERIFIED,
            role,
            relative_path,
        )

    @staticmethod
    def recovered(
        kind: RecoveredNoticeKind,
        *,
        count: int = 1,
    ) -> DiagnosticFact:
        """记录最终已恢复、因此不进入 errors 的事项。"""
        if not isinstance(kind, RecoveredNoticeKind):
            raise TypeError("已恢复事项必须使用 RecoveredNoticeKind")
        _positive_integer(count, field="已恢复事项数量")
        return DiagnosticFact._create(
            _FactKind.RECOVERED_NOTICE,
            _RecoveredNoticeFact(kind=kind, count=count),
        )


_STABLE_REASON_CODE = re.compile(
    r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,7}"
)
_DIGEST_PREFIX = re.compile(r"sha256:[0-9a-f]{8,16}")
_FULL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_STABLE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FONT_FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}")


def _nonnegative_integer(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field}必须是整数")
    if value < 0:
        raise ValueError(f"{field}不能为负数")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    normalized = _nonnegative_integer(value, field=field)
    if normalized < 1:
        raise ValueError(f"{field}必须是正整数")
    return normalized


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _FULL_SHA256.fullmatch(value) is None:
        raise ValueError(f"{field}必须是完整规范 SHA-256")
    return value


def _stable_identifier(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or _STABLE_IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{field}格式不合法")
    return value


def _endpoint_origin(
    value: object,
    *,
    transport: ProviderTransport,
) -> str | None:
    if transport is ProviderTransport.LOCAL:
        if value is not None:
            raise ValueError("本地 Adapter 不得披露远端 endpoint origin")
        return None
    if not isinstance(value, str):
        raise TypeError("远程 Adapter 必须披露 HTTPS endpoint origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("供应商 endpoint 只能披露 HTTPS origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("供应商 endpoint 端口不合法") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        try:
            host = f"[{IPv6Address(host).compressed}]"
        except ValueError as exc:
            raise ValueError("供应商 endpoint IPv6 origin 不合法") from exc
    normalized_port = "" if port in {None, 443} else f":{port}"
    return f"https://{host}{normalized_port}"


def _external_data_categories(
    value: object,
) -> tuple[ExternalDataCategory, ...]:
    if not isinstance(value, tuple) or any(
        not isinstance(item, ExternalDataCategory) for item in value
    ):
        raise TypeError("允许外发数据类别必须是 ExternalDataCategory 元组")
    if len(set(value)) != len(value):
        raise ValueError("允许外发数据类别不能重复")
    return tuple(sorted(value, key=lambda item: item.value))


def _validate_external_data_categories(
    capability: ProviderCapability,
    categories: tuple[ExternalDataCategory, ...],
    *,
    transport: ProviderTransport,
) -> None:
    allowed = {
        ProviderCapability.TRANSCRIPTION: {
            ExternalDataCategory.AUDIO_SHARD,
        },
        ProviderCapability.TOPIC_REVIEW: {
            ExternalDataCategory.CANDIDATE_TRANSCRIPT,
            ExternalDataCategory.COURSE_CONTEXT,
            ExternalDataCategory.BUSINESS_CONSTRAINTS,
        },
        ProviderCapability.SUBTITLE_OPTIMIZATION: {
            ExternalDataCategory.SUBTITLE_WINDOW,
            ExternalDataCategory.FIXED_INSTRUCTIONS,
        },
    }[capability]
    if transport is ProviderTransport.LOCAL:
        if categories:
            raise ValueError("本地 Adapter 的外发数据类别必须为空")
        return
    if not categories or not set(categories).issubset(allowed):
        raise ValueError("供应商外发数据类别超出能力白名单")


def _artifact_fact(
    kind: _FactKind,
    role: ArtifactRole,
    relative_path: object,
) -> DiagnosticFact:
    if not isinstance(role, ArtifactRole):
        raise TypeError("交付文件角色必须使用 ArtifactRole")
    if not isinstance(relative_path, str):
        raise TypeError("交付文件位置必须是安全相对路径")
    fixed_paths = {
        ArtifactRole.MANIFEST: "manifest.json",
        ArtifactRole.TRANSCRIPT_JSON: "transcript.json",
        ArtifactRole.TRANSCRIPT_SRT: "transcript.srt",
        ArtifactRole.PLAN: "plan.json",
        ArtifactRole.METADATA: "metadata.json",
        ArtifactRole.REPORT: "report.md",
    }
    if role is ArtifactRole.SHORT_VIDEO:
        prefix = "clips/"
        suffix = ".mp4"
        if (
            not relative_path.startswith(prefix)
            or not relative_path.endswith(suffix)
        ):
            raise ValueError("短视频必须使用固定交付相对路径")
        identity = relative_path[len(prefix) : -len(suffix)]
        try:
            ShortVideoId(identity)
        except (TypeError, ValueError) as exc:
            raise ValueError("短视频必须使用固定交付相对路径") from exc
    elif relative_path != fixed_paths[role]:
        raise ValueError("交付文件位置必须是受约束相对路径")
    return DiagnosticFact._create(
        kind,
        _ArtifactFact(role=role, relative_path=relative_path),
    )


def _fact_signature(value: object) -> object:
    """形成只用于检测 capability 篡改的递归不可变快照。"""
    if isinstance(value, Enum):
        return (type(value), value.value)
    if isinstance(value, PublishedDelivery):
        return (
            PublishedDelivery,
            id(value),
            str(value.run_id),
        )
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value),
            tuple(
                (
                    field.name,
                    _fact_signature(getattr(value, field.name)),
                )
                for field in fields(value)
            ),
        )
    if isinstance(value, tuple):
        return tuple(_fact_signature(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("诊断事实包含未知内部值")
