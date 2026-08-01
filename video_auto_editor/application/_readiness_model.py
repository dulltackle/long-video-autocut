"""严格预检公共 seam 的不可变输入与安全输出。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from video_auto_editor.diagnostics import (
    CertifiedPlatform,
    ExternalDataCategory,
    Facts,
    PreflightOutcome,
    ProviderCapability,
    ProviderTransport,
)
from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.workspace import RunWorkspace

_STABLE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_FONT_FAMILY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """本地子进程边界返回的有限、无路径结果。"""

    return_code: int
    stdout: str

    def __post_init__(self) -> None:
        if not isinstance(self.return_code, int) or isinstance(self.return_code, bool):
            raise TypeError("本地命令退出码必须是整数")
        if not isinstance(self.stdout, str):
            raise TypeError("本地命令标准输出必须是字符串")


@dataclass(frozen=True, slots=True)
class TLSObservation:
    """默认 TLS 信任库的最小安全观察。"""

    verification_enabled: bool
    ca_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.verification_enabled, bool):
            raise TypeError("TLS 校验状态必须是布尔值")
        if not isinstance(self.ca_count, int) or isinstance(self.ca_count, bool):
            raise TypeError("TLS CA 数量必须是整数")
        if self.ca_count < 0:
            raise ValueError("TLS CA 数量不能为负数")


class ProviderPurpose(str, Enum):
    """首次远程请求前必须披露的固定业务用途。"""

    TRANSCRIBE_AUDIO = "transcribe_audio"
    REVIEW_TOPICS = "review_topics"
    OPTIMIZE_SUBTITLES = "optimize_subtitles"


class _ReadinessAdapter(Protocol):
    def check_readiness(self) -> object: ...


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """组合根交给 Readiness 的单项生产 Adapter 绑定。"""

    capability: ProviderCapability
    adapter: _ReadinessAdapter = field(repr=False, compare=False)
    adapter_id: str
    provider_id: str
    model_id: str
    endpoint: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise TypeError("供应商绑定必须使用 ProviderCapability")
        if not callable(getattr(self.adapter, "check_readiness", None)):
            raise TypeError("供应商绑定 Adapter 必须提供 check_readiness()")
        for value, field_name in (
            (self.adapter_id, "Adapter 标识"),
            (self.provider_id, "供应商标识"),
        ):
            if (
                not isinstance(value, str)
                or _STABLE_IDENTIFIER.fullmatch(value) is None
            ):
                raise ValueError(f"{field_name}格式不合法")
        if (
            not isinstance(self.model_id, str)
            or _MODEL_IDENTIFIER.fullmatch(self.model_id) is None
        ):
            raise ValueError("供应商模型标识格式不合法")
        if not isinstance(self.endpoint, str) or not self.endpoint:
            raise TypeError("供应商 endpoint 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class ReadinessRequest:
    """一次聚合预检所需的最小不可变输入。"""

    run_workspace: RunWorkspace = field(repr=False, compare=False)
    subtitle_font: str
    transcription: ProviderBinding
    topic_review: ProviderBinding
    subtitle_optimization: ProviderBinding

    def __post_init__(self) -> None:
        if not isinstance(self.run_workspace, RunWorkspace):
            raise TypeError("严格预检必须使用 Workspace 签发的 RunWorkspace")
        if (
            not isinstance(self.subtitle_font, str)
            or _FONT_FAMILY.fullmatch(self.subtitle_font) is None
        ):
            raise ValueError("烧录字幕字体家族格式不合法")
        expected = (
            (self.transcription, ProviderCapability.TRANSCRIPTION),
            (self.topic_review, ProviderCapability.TOPIC_REVIEW),
            (
                self.subtitle_optimization,
                ProviderCapability.SUBTITLE_OPTIMIZATION,
            ),
        )
        for binding, capability in expected:
            if not isinstance(binding, ProviderBinding):
                raise TypeError("严格预检供应商绑定必须使用 ProviderBinding")
            if binding.capability is not capability:
                raise ValueError("严格预检供应商绑定与固定业务能力不一致")

    @property
    def provider_bindings(self) -> tuple[ProviderBinding, ...]:
        """按固定业务顺序返回三项供应商绑定。"""
        return (
            self.transcription,
            self.topic_review,
            self.subtitle_optimization,
        )


@dataclass(frozen=True, slots=True)
class ReadinessIssue:
    """按公共错误注册表约束的稳定、脱敏预检阻塞项。"""

    error_code: ErrorCode
    diagnostics: Mapping[str, Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.error_code, ErrorCode):
            raise TypeError("预检阻塞项必须使用稳定 ErrorCode")
        object.__setattr__(
            self,
            "diagnostics",
            freeze_error_diagnostics(self.error_code, self.diagnostics),
        )

    @property
    def safe_message(self) -> str:
        return get_error_definition(self.error_code).safe_message


@dataclass(frozen=True, slots=True)
class EnvironmentProjection:
    """成功与失败预检都可安全观察的环境投影。"""

    certified_platform: CertifiedPlatform | None
    python_version: DetectedVersion | None
    ffmpeg_version: DetectedVersion | None
    ffprobe_version: DetectedVersion | None
    font_family: str
    font_available: bool
    installation_fingerprint: str
    preflight_outcome: PreflightOutcome

    def __post_init__(self) -> None:
        if self.certified_platform is not None and not isinstance(
            self.certified_platform,
            CertifiedPlatform,
        ):
            raise TypeError("认证平台必须使用 CertifiedPlatform 或 None")
        for value, field_name in (
            (self.python_version, "Python"),
            (self.ffmpeg_version, "FFmpeg"),
            (self.ffprobe_version, "ffprobe"),
        ):
            if value is not None and not isinstance(value, DetectedVersion):
                raise TypeError(f"{field_name} 版本必须由 Readiness 签发或为 None")
        if (
            not isinstance(self.font_family, str)
            or _FONT_FAMILY.fullmatch(self.font_family) is None
        ):
            raise ValueError("环境投影字体家族格式不合法")
        if not isinstance(self.font_available, bool):
            raise TypeError("环境投影字体可用性必须是布尔值")
        if (
            not isinstance(self.installation_fingerprint, str)
            or _FINGERPRINT.fullmatch(self.installation_fingerprint) is None
        ):
            raise ValueError("安装环境指纹必须是规范 SHA-256")
        if not isinstance(self.preflight_outcome, PreflightOutcome):
            raise TypeError("环境投影预检结果必须使用 PreflightOutcome")

    def to_diagnostic_fact(self) -> object | None:
        """字段足以准确表达时形成 ``Facts.environment``，否则返回 None。"""
        if (
            self.certified_platform is None
            or self.python_version is None
            or self.ffmpeg_version is None
            or self.ffprobe_version is None
        ):
            return None
        return Facts.environment(
            certified_platform=self.certified_platform,
            python_version=self.python_version,
            ffmpeg_version=self.ffmpeg_version,
            ffprobe_version=self.ffprobe_version,
            font_family=self.font_family,
            font_available=self.font_available,
            installation_fingerprint=self.installation_fingerprint,
            preflight_outcome=self.preflight_outcome,
        )


@dataclass(frozen=True, slots=True)
class ProviderDisclosure:
    """一个固定业务能力在首次请求前的安全外发计划。"""

    capability: ProviderCapability
    purpose: ProviderPurpose
    adapter_id: str
    provider_id: str
    model_id: str
    data_categories: tuple[ExternalDataCategory, ...]
    configuration_fingerprint: str
    endpoint_origin: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, ProviderCapability):
            raise TypeError("供应商披露必须使用 ProviderCapability")
        if not isinstance(self.purpose, ProviderPurpose):
            raise TypeError("供应商披露必须使用固定 ProviderPurpose")
        for value, field_name in (
            (self.adapter_id, "Adapter 标识"),
            (self.provider_id, "供应商标识"),
        ):
            if (
                not isinstance(value, str)
                or _STABLE_IDENTIFIER.fullmatch(value) is None
            ):
                raise ValueError(f"{field_name}格式不合法")
        if (
            not isinstance(self.model_id, str)
            or _MODEL_IDENTIFIER.fullmatch(self.model_id) is None
        ):
            raise ValueError("披露模型标识格式不合法")
        if (
            not isinstance(self.data_categories, tuple)
            or any(
                not isinstance(item, ExternalDataCategory)
                for item in self.data_categories
            )
            or len(set(self.data_categories)) != len(self.data_categories)
        ):
            raise TypeError("计划外发数据类别必须是无重复的不可变元组")
        if (
            not isinstance(self.configuration_fingerprint, str)
            or _FINGERPRINT.fullmatch(self.configuration_fingerprint) is None
        ):
            raise ValueError("供应商配置指纹必须是规范 SHA-256")
        if self.endpoint_origin is not None and not isinstance(
            self.endpoint_origin,
            str,
        ):
            raise TypeError("供应商 endpoint origin 必须是字符串或 None")

    def to_diagnostic_fact(self) -> object | None:
        """HTTPS origin 合法时形成 ``Facts.external_service``。"""
        if self.endpoint_origin is None:
            return None
        return Facts.external_service(
            capability=self.capability,
            adapter_id=self.adapter_id,
            provider_id=self.provider_id,
            model_id=self.model_id,
            configuration_fingerprint=self.configuration_fingerprint,
            endpoint_origin=self.endpoint_origin,
            transport=ProviderTransport.REMOTE,
            allowed_data_categories=self.data_categories,
        )


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """严格本地预检的一次完整、安全快照。"""

    ready: bool
    issues: tuple[ReadinessIssue, ...]
    environment: EnvironmentProjection
    provider_disclosures: tuple[ProviderDisclosure, ...]
    environment_fact: object | None = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ready, bool):
            raise TypeError("聚合准备状态必须是布尔值")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(item, ReadinessIssue) for item in self.issues
        ):
            raise TypeError("聚合准备问题必须是 ReadinessIssue 不可变元组")
        if self.ready == bool(self.issues):
            raise ValueError("聚合准备状态必须与阻塞问题保持一致")
        if not isinstance(self.environment, EnvironmentProjection):
            raise TypeError("聚合准备报告必须包含安全环境投影")
        if (
            self.environment.preflight_outcome is PreflightOutcome.SUCCEEDED
        ) != self.ready:
            raise ValueError("环境投影结果必须与聚合准备状态一致")
        if (
            not isinstance(self.provider_disclosures, tuple)
            or len(self.provider_disclosures) != 3
            or any(
                not isinstance(item, ProviderDisclosure)
                for item in self.provider_disclosures
            )
        ):
            raise TypeError("聚合准备报告必须包含固定三项供应商披露")


__all__ = [
    "CommandResult",
    "EnvironmentProjection",
    "ProviderBinding",
    "ProviderDisclosure",
    "ProviderPurpose",
    "ReadinessIssue",
    "ReadinessReport",
    "ReadinessRequest",
    "TLSObservation",
]
