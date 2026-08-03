"""聚合认证环境、媒体、workspace 与生产 Adapter 的严格预检。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import cast
from urllib.parse import unquote, urlsplit

from video_auto_editor.diagnostics import (
    CertifiedPlatform,
    ExternalDataCategory,
    PreflightOutcome,
    ProviderCapability,
)
from video_auto_editor.runtime.errors import (
    ERROR_REGISTRY,
    DetectedVersion,
    ErrorCode,
)
from video_auto_editor.text_model import (
    ReadinessReport as TextReadinessReport,
)
from video_auto_editor.text_model import (
    TextModelReadinessCode,
)
from video_auto_editor.transcription import (
    ReadinessReport as TranscriptionReadinessReport,
)
from video_auto_editor.workspace import WorkspaceFailure

from ._readiness_model import (
    CommandResult,
    EnvironmentProjection,
    ProviderBinding,
    ProviderDisclosure,
    ProviderPurpose,
    ReadinessIssue,
    ReadinessReport,
    ReadinessRequest,
    TLSObservation,
)

_PYTHON_MINIMUM = (3, 12, 3)
_PYTHON_MAXIMUM = (3, 13, 0)
_PYTHON_REQUIREMENT = ">=3.12.3,<3.13"
_FFMPEG_MINIMUM = (6, 1)
_FFMPEG_MAXIMUM = (7, 0)
_FFMPEG_REQUIREMENT = ">=6.1,<7"
_VERSION_LINE = re.compile(
    r"^(?:ffmpeg|ffprobe) version n?"
    r"(?P<version>[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?)\b",
    re.MULTILINE,
)
_VERSION_NUMBERS = re.compile(r"^(?P<major>[0-9]+)\.(?P<minor>[0-9]+)")
_ERROR_ORDER = {code: index for index, code in enumerate(ERROR_REGISTRY)}
_WORKSPACE_CANARY = b"readiness.atomic-write.v1\n"
_MEDIA_PROBE_SRT = """1
00:00:00,000 --> 00:00:00,900
中文预检
"""
_SHA256 = re.compile(r"[0-9a-f]{64}")
_APPLICATION_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_APT_SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?")
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_INSTALLATION_FAILURE_REASONS = frozenset(
    {
        "manifest.digest_mismatch",
        "manifest.missing",
        "manifest.prefix_mismatch",
        "manifest.schema_invalid",
        "manifest.unreadable",
        "manifest.version_mismatch",
    }
)
_MAX_INSTALLATION_DOCUMENT_BYTES = 2 * 1024 * 1024
_INSTALLATION_MANIFEST_FIELDS = frozenset(
    {
        "application",
        "apt_snapshot_id",
        "environment",
        "installation_prefix",
        "platform",
        "python",
        "runtime_lock",
        "schema_version",
        "snapshot_packages",
        "system_packages",
        "wheelhouse",
    }
)
_SNAPSHOT_PACKAGE_NAMES = frozenset(
    {
        "ca-certificates",
        "ffmpeg",
        "fontconfig",
        "fonts-noto-cjk",
        "python3.12",
        "python3.12-venv",
    }
)


@dataclass(frozen=True, slots=True)
class InstallationObservation:
    """安装清单与就绪标记的最小脱敏校验结果。"""

    manifest_sha256: str | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.manifest_sha256 is not None and (
            not isinstance(self.manifest_sha256, str)
            or _SHA256.fullmatch(self.manifest_sha256) is None
        ):
            raise ValueError("安装清单摘要必须是规范 SHA-256")
        if self.reason_code is not None and (
            not isinstance(self.reason_code, str)
            or self.reason_code not in _INSTALLATION_FAILURE_REASONS
        ):
            raise ValueError("安装清单失败原因必须来自稳定闭集")
        if (self.manifest_sha256 is None) == (self.reason_code is None):
            raise ValueError("安装清单观察必须且只能表达成功或失败")

    @property
    def valid(self) -> bool:
        return self.manifest_sha256 is not None

    @classmethod
    def verified(cls, *, manifest_sha256: str) -> InstallationObservation:
        return cls(manifest_sha256=manifest_sha256, reason_code=None)

    @classmethod
    def invalid(cls, reason_code: str) -> InstallationObservation:
        return cls(manifest_sha256=None, reason_code=reason_code)


class _LocalSystemProbe:
    """只访问本机进程、文件元数据和默认 TLS 信任库的生产实现。"""

    __slots__ = ()

    def platform_name(self) -> str:
        return sys.platform

    def architecture(self) -> str:
        return platform.machine()

    def os_release(self) -> Mapping[str, str]:
        return MappingProxyType(dict(platform.freedesktop_os_release()))

    def python_implementation(self) -> str:
        return platform.python_implementation()

    def python_version(self) -> tuple[int, int, int]:
        return (
            sys.version_info.major,
            sys.version_info.minor,
            sys.version_info.micro,
        )

    def is_virtual_environment(self) -> bool:
        return sys.prefix != sys.base_prefix

    def installation_observation(self) -> InstallationObservation:
        """校验当前版本目录中的安装清单与 READY 摘要绑定。"""
        try:
            environment_prefix = Path(sys.prefix).resolve(strict=True)
            version_directory = environment_prefix.parent
            installation_prefix = version_directory.parent.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return InstallationObservation.invalid("manifest.unreadable")
        if (
            environment_prefix.name != "venv"
            or version_directory.parent.name != "versions"
        ):
            return InstallationObservation.invalid("manifest.prefix_mismatch")

        manifest_path = version_directory / "installation-manifest.json"
        ready_path = version_directory / "READY"
        if not manifest_path.exists() or not ready_path.exists():
            return InstallationObservation.invalid("manifest.missing")
        try:
            version_descriptor = _open_anchored_directory(version_directory)
            try:
                manifest_bytes = _read_regular_installation_file_at(
                    version_descriptor,
                    "installation-manifest.json",
                )
                ready_bytes = _read_regular_installation_file_at(
                    version_descriptor,
                    "READY",
                )
                if not _directory_descriptor_matches_path(
                    version_descriptor,
                    version_directory,
                ):
                    raise OSError("版本目录读取期间发生替换")
            finally:
                os.close(version_descriptor)
        except FileNotFoundError:
            return InstallationObservation.invalid("manifest.missing")
        except OSError:
            return InstallationObservation.invalid("manifest.unreadable")

        try:
            manifest = _load_strict_json(manifest_bytes)
            ready = _load_strict_json(ready_bytes)
        except (UnicodeError, ValueError):
            return InstallationObservation.invalid("manifest.schema_invalid")
        if not isinstance(manifest, dict) or not isinstance(ready, dict):
            return InstallationObservation.invalid("manifest.schema_invalid")
        if (
            not _installation_manifest_has_complete_shape(manifest)
            or manifest.get("schema_version") != ("production-installation-manifest.v1")
            or set(ready)
            != {
                "installation_manifest_sha256",
                "schema_version",
            }
        ):
            return InstallationObservation.invalid("manifest.schema_invalid")
        if ready.get("schema_version") != "production-installation-ready.v1":
            return InstallationObservation.invalid("manifest.schema_invalid")

        digest = hashlib.sha256(manifest_bytes).hexdigest()
        ready_digest = ready.get("installation_manifest_sha256")
        if (
            not isinstance(ready_digest, str)
            or _SHA256.fullmatch(ready_digest) is None
            or not secrets.compare_digest(digest, ready_digest)
        ):
            return InstallationObservation.invalid("manifest.digest_mismatch")

        application = manifest.get("application")
        try:
            distribution = metadata.distribution("video-auto-editor")
        except metadata.PackageNotFoundError:
            distribution = None
        if (
            not isinstance(application, dict)
            or application.get("name") != "video-auto-editor"
            or distribution is None
            or application.get("version") != distribution.version
        ):
            return InstallationObservation.invalid("manifest.version_mismatch")
        if manifest.get("installation_prefix") != str(installation_prefix):
            return InstallationObservation.invalid("manifest.prefix_mismatch")
        if application.get("version") != version_directory.name:
            return InstallationObservation.invalid("manifest.prefix_mismatch")
        try:
            python_executable = Path(os.path.abspath(sys.executable))
            cli_executable = Path(sys.argv[0]).resolve(strict=True)
            expected_python = environment_prefix / "bin" / "python"
            expected_cli = environment_prefix / "bin" / "video-auto-editor"
            distribution_root = Path(str(distribution.locate_file(""))).resolve(
                strict=True
            )
            imported_module = Path(__file__).resolve(strict=True)
            identity_paths = _distribution_identity_paths(distribution)
        except (IndexError, OSError, RuntimeError, TypeError):
            return InstallationObservation.invalid("manifest.prefix_mismatch")
        if identity_paths is None:
            return InstallationObservation.invalid("manifest.prefix_mismatch")
        distribution_module, direct_url_path = identity_paths
        if (
            not python_executable.is_relative_to(environment_prefix)
            or not cli_executable.is_relative_to(environment_prefix)
            or not os.path.samefile(python_executable, expected_python)
            or not os.path.samefile(cli_executable, expected_cli)
            or not distribution_root.is_relative_to(environment_prefix)
            or not imported_module.is_relative_to(environment_prefix)
            or not distribution_module.is_relative_to(environment_prefix)
            or not direct_url_path.is_relative_to(environment_prefix)
            or not stat.S_ISREG(Path(__file__).lstat().st_mode)
            or not stat.S_ISREG(distribution_module.lstat().st_mode)
            or not os.path.samefile(imported_module, distribution_module)
        ):
            return InstallationObservation.invalid("manifest.prefix_mismatch")
        try:
            direct_url_bytes = _read_regular_installation_file(direct_url_path)
            direct_url = _load_strict_json(direct_url_bytes)
        except (OSError, UnicodeError, ValueError):
            return InstallationObservation.invalid("manifest.digest_mismatch")
        wheel = application["wheel"]
        if not isinstance(wheel, dict) or not _direct_url_matches_wheel(
            direct_url,
            filename=wheel.get("filename"),
            sha256=wheel.get("sha256"),
        ):
            return InstallationObservation.invalid("manifest.digest_mismatch")
        return InstallationObservation.verified(manifest_sha256=digest)

    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        environment = {
            "LC_ALL": "C",
            "PATH": os.environ.get("PATH", os.defpath),
        }
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "")
        except OSError:
            return CommandResult(127, "")
        return CommandResult(completed.returncode, completed.stdout)

    def font_file_is_readable(self, path: str) -> bool:
        try:
            candidate = Path(path)
            return candidate.is_file() and os.access(candidate, os.R_OK)
        except OSError:
            return False

    def tls_observation(self) -> TLSObservation:
        context = ssl.create_default_context()
        return TLSObservation(
            verification_enabled=(
                context.check_hostname is True
                and context.verify_mode == ssl.CERT_REQUIRED
            ),
            ca_count=context.cert_store_stats().get("x509_ca", 0),
        )


@dataclass(frozen=True, slots=True)
class _EnvironmentState:
    certified_platform: CertifiedPlatform | None
    installation_fingerprint: str | None
    python_version: DetectedVersion | None
    ffmpeg_version: DetectedVersion | None
    ffprobe_version: DetectedVersion | None
    font_available: bool
    platform_observation: tuple[object, ...]
    python_observation: tuple[object, ...]
    media_observation: tuple[object, ...]
    tls_observation: tuple[object, ...]
    workspace_ready: bool


class Readiness:
    """在一个公共 seam 后聚合全部本地严格预检与供应商披露。"""

    __slots__ = ()

    @staticmethod
    def check(
        request: ReadinessRequest,
        *,
        _system_probe: object | None = None,
    ) -> ReadinessReport:
        """一次收集本地阻塞项；绝不调用 Adapter 的业务方法。"""
        if not isinstance(request, ReadinessRequest):
            raise TypeError("Readiness.check() 只接受 ReadinessRequest")
        candidate = _LocalSystemProbe() if _system_probe is None else _system_probe
        _validate_system_probe(candidate)
        probe = cast(_LocalSystemProbe, candidate)
        issues: list[ReadinessIssue] = []

        certified_platform, platform_observation = _check_platform(probe, issues)
        python_version, python_observation = _check_python(probe, issues)
        installation_fingerprint = _check_installation(probe, issues)
        (
            ffmpeg_version,
            ffprobe_version,
            font_available,
            media_observation,
        ) = _check_media_and_font(probe, request.subtitle_font, issues)
        tls_observation = _check_tls(probe, issues)
        workspace_ready = _check_workspace(request, issues)
        adapter_fingerprints = _check_adapters(request, issues)

        unique_issues = _ordered_unique(issues)
        ready = not unique_issues
        state = _EnvironmentState(
            certified_platform=certified_platform,
            installation_fingerprint=installation_fingerprint,
            python_version=python_version,
            ffmpeg_version=ffmpeg_version,
            ffprobe_version=ffprobe_version,
            font_available=font_available,
            platform_observation=platform_observation,
            python_observation=python_observation,
            media_observation=media_observation,
            tls_observation=tls_observation,
            workspace_ready=workspace_ready,
        )
        environment = _environment_projection(
            request,
            state,
            ready=ready,
        )
        disclosures = tuple(
            _provider_disclosure(
                binding,
                adapter_fingerprint=adapter_fingerprints.get(binding.capability),
            )
            for binding in request.provider_bindings
        )
        return ReadinessReport(
            ready=ready,
            issues=unique_issues,
            environment=environment,
            provider_disclosures=disclosures,
            environment_fact=environment.to_diagnostic_fact(),
        )


def _validate_system_probe(probe: object) -> None:
    methods = (
        "platform_name",
        "architecture",
        "os_release",
        "python_implementation",
        "python_version",
        "is_virtual_environment",
        "installation_observation",
        "which",
        "run",
        "font_file_is_readable",
        "tls_observation",
    )
    if any(not callable(getattr(probe, method, None)) for method in methods):
        raise TypeError("Readiness 系统探针没有满足本地效果 seam")


def _check_platform(
    probe: _LocalSystemProbe,
    issues: list[ReadinessIssue],
) -> tuple[CertifiedPlatform | None, tuple[object, ...]]:
    try:
        platform_name = probe.platform_name().casefold()
    except Exception:  # noqa: BLE001 - 系统探针异常只能投影为稳定问题
        platform_name = ""
    try:
        release = probe.os_release()
        os_id = str(release.get("ID", "")).casefold()
        version_id = str(release.get("VERSION_ID", ""))
    except Exception:  # noqa: BLE001
        os_id = ""
        version_id = ""
    try:
        architecture = probe.architecture().casefold()
    except Exception:  # noqa: BLE001
        architecture = ""
    normalized_arch = "amd64" if architecture in {"amd64", "x86_64"} else architecture

    if platform_name != "linux" or os_id != "ubuntu":
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED,
                component="platform",
                operation="platform.detect",
                reason_code="platform.os_unsupported",
            )
        )
    if version_id != "24.04":
        diagnostics: dict[str, object] = {
            "component": "platform",
            "operation": "platform.detect",
            "reason_code": "platform.release_unsupported",
            "required_version": "24.04",
        }
        if _safe_detected_version(version_id) is not None:
            diagnostics["detected_version"] = version_id
        issues.append(
            ReadinessIssue(ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED, diagnostics)
        )
    if normalized_arch != "amd64":
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED,
                component="architecture",
                operation="architecture.detect",
                reason_code="platform.architecture_unsupported",
            )
        )

    certified = (
        CertifiedPlatform.UBUNTU_24_04_AMD64
        if (
            platform_name == "linux"
            and os_id == "ubuntu"
            and version_id == "24.04"
            and normalized_arch == "amd64"
        )
        else None
    )
    return certified, (platform_name, os_id, version_id, normalized_arch)


def _check_python(
    probe: _LocalSystemProbe,
    issues: list[ReadinessIssue],
) -> tuple[DetectedVersion | None, tuple[object, ...]]:
    try:
        implementation = probe.python_implementation()
    except Exception:  # noqa: BLE001
        implementation = ""
    try:
        observed_version = probe.python_version()
        if (
            not isinstance(observed_version, tuple)
            or len(observed_version) != 3
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item < 0
                for item in observed_version
            )
        ):
            raise TypeError
        version: tuple[int, int, int] | None = observed_version
        version_text = ".".join(str(item) for item in observed_version)
        detected = DetectedVersion.from_readiness(version_text)
    except Exception:  # noqa: BLE001
        version = None
        version_text = ""
        detected = None
    try:
        in_virtual_environment = probe.is_virtual_environment()
    except Exception:  # noqa: BLE001
        in_virtual_environment = False

    if implementation != "CPython":
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
                component="python",
                operation="python.inspect",
                reason_code="python.implementation_unsupported",
            )
        )
    if detected is None or version is None:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
                component="python",
                operation="python.inspect",
                reason_code="python.version_too_old",
                required_version=_PYTHON_REQUIREMENT,
            )
        )
    elif version < _PYTHON_MINIMUM:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
                component="python",
                operation="python.inspect",
                reason_code="python.version_too_old",
                detected_version=version_text,
                required_version=_PYTHON_REQUIREMENT,
            )
        )
    elif version >= _PYTHON_MAXIMUM:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
                component="python",
                operation="python.inspect",
                reason_code="python.version_too_new",
                detected_version=version_text,
                required_version=_PYTHON_REQUIREMENT,
            )
        )
    if in_virtual_environment is not True:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
                component="virtual_environment",
                operation="python.verify_venv",
                reason_code="python.venv_required",
            )
        )
    return detected, (implementation, version_text, in_virtual_environment)


def _check_installation(
    probe: _LocalSystemProbe,
    issues: list[ReadinessIssue],
) -> str | None:
    try:
        observation = probe.installation_observation()
        if not isinstance(observation, InstallationObservation):
            raise TypeError
    except Exception:  # noqa: BLE001 - 探针异常必须收敛为稳定失败
        observation = InstallationObservation.invalid("manifest.unreadable")
    if not observation.valid:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID,
                component="installation_manifest",
                operation="manifest.verify",
                reason_code=observation.reason_code,
            )
        )
        return None
    return f"sha256:{observation.manifest_sha256}"


def _check_media_and_font(
    probe: _LocalSystemProbe,
    font_family: str,
    issues: list[ReadinessIssue],
) -> tuple[
    DetectedVersion | None,
    DetectedVersion | None,
    bool,
    tuple[object, ...],
]:
    ffmpeg_path = _locate(probe, "ffmpeg")
    ffprobe_path = _locate(probe, "ffprobe")
    fc_list_path = _locate(probe, "fc-list")
    fc_match_path = _locate(probe, "fc-match")
    if ffmpeg_path is None:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                component="ffmpeg",
                operation="ffmpeg.locate",
                reason_code="tool.missing",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )
    if ffprobe_path is None:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
                component="ffprobe",
                operation="ffprobe.locate",
                reason_code="tool.missing",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )

    ffmpeg_version = _inspect_tool_version(
        probe,
        "ffmpeg",
        ffmpeg_path,
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        "ffmpeg.inspect",
        issues,
    )
    ffprobe_version = _inspect_tool_version(
        probe,
        "ffprobe",
        ffprobe_path,
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
        "ffprobe.inspect",
        issues,
    )
    if (
        ffmpeg_version is not None
        and ffprobe_version is not None
        and ffmpeg_version != ffprobe_version
    ):
        issues.extend(
            (
                _issue(
                    ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                    component="ffmpeg",
                    operation="ffmpeg.inspect",
                    reason_code="tool.version_mismatch",
                    detected_version=str(ffmpeg_version),
                    required_version=str(ffprobe_version),
                ),
                _issue(
                    ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
                    component="ffprobe",
                    operation="ffprobe.inspect",
                    reason_code="tool.version_mismatch",
                    detected_version=str(ffprobe_version),
                    required_version=str(ffmpeg_version),
                ),
            )
        )

    subtitles_available = _ffmpeg_capability(
        probe,
        ffmpeg_path,
        ("-hide_banner", "-filters"),
        r"\bsubtitles\b",
    )
    if ffmpeg_path is not None and not subtitles_available:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                component="ffmpeg",
                operation="ffmpeg.inspect",
                reason_code="ffmpeg.filter_missing",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )
    encoders = _command_stdout(
        probe,
        ffmpeg_path,
        ("-hide_banner", "-encoders"),
    )
    libx264_available = (
        encoders is not None
        and re.search(
            r"\blibx264\b",
            encoders,
        )
        is not None
    )
    aac_available = encoders is not None and re.search(r"\baac\b", encoders) is not None
    if ffmpeg_path is not None and not (libx264_available and aac_available):
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                component="ffmpeg",
                operation="ffmpeg.inspect",
                reason_code="ffmpeg.encoder_missing",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )

    font_static_available = _check_font(
        probe,
        font_family,
        fc_list_path,
        fc_match_path,
        issues,
    )
    smoke_succeeded = False
    if ffmpeg_path is not None and ffprobe_path is not None and font_static_available:
        smoke_succeeded = _media_smoke_test(
            probe,
            font_family,
            issues,
        )
    media_observation = (
        str(ffmpeg_version) if ffmpeg_version is not None else None,
        str(ffprobe_version) if ffprobe_version is not None else None,
        subtitles_available,
        libx264_available,
        aac_available,
        font_static_available,
        smoke_succeeded,
    )
    return (
        ffmpeg_version,
        ffprobe_version,
        font_static_available and smoke_succeeded,
        media_observation,
    )


def _locate(probe: _LocalSystemProbe, command: str) -> str | None:
    try:
        path = probe.which(command)
    except Exception:  # noqa: BLE001
        return None
    return path if isinstance(path, str) and path else None


def _inspect_tool_version(
    probe: _LocalSystemProbe,
    command: str,
    executable: str | None,
    error_code: ErrorCode,
    operation: str,
    issues: list[ReadinessIssue],
) -> DetectedVersion | None:
    if executable is None:
        return None
    result = _safe_run(probe, (command, "-version"), timeout_seconds=10)
    match = _VERSION_LINE.search(result.stdout) if result.return_code == 0 else None
    if match is None:
        issues.append(
            _issue(
                error_code,
                component=command,
                operation=operation,
                reason_code="tool.version_unreadable",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )
        return None
    version_text = match.group("version")
    try:
        detected = DetectedVersion.from_readiness(version_text)
    except (TypeError, ValueError):
        issues.append(
            _issue(
                error_code,
                component=command,
                operation=operation,
                reason_code="tool.version_unreadable",
                required_version=_FFMPEG_REQUIREMENT,
            )
        )
        return None
    numeric = _VERSION_NUMBERS.match(version_text)
    supported = numeric is not None and (
        _FFMPEG_MINIMUM
        <= (int(numeric.group("major")), int(numeric.group("minor")))
        < _FFMPEG_MAXIMUM
    )
    if not supported:
        issues.append(
            _issue(
                error_code,
                component=command,
                operation=operation,
                reason_code="tool.version_unsupported",
                detected_version=version_text,
                required_version=_FFMPEG_REQUIREMENT,
            )
        )
    return detected


def _ffmpeg_capability(
    probe: _LocalSystemProbe,
    executable: str | None,
    arguments: tuple[str, ...],
    pattern: str,
) -> bool:
    output = _command_stdout(probe, executable, arguments)
    return output is not None and re.search(pattern, output) is not None


def _command_stdout(
    probe: _LocalSystemProbe,
    executable: str | None,
    arguments: tuple[str, ...],
) -> str | None:
    if executable is None:
        return None
    result = _safe_run(probe, (Path(executable).name, *arguments), timeout_seconds=10)
    return result.stdout if result.return_code == 0 else None


def _check_font(
    probe: _LocalSystemProbe,
    font_family: str,
    fc_list_path: str | None,
    fc_match_path: str | None,
    issues: list[ReadinessIssue],
) -> bool:
    if fc_list_path is None or fc_match_path is None:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
                component="fontconfig",
                operation=(
                    "fontconfig.list" if fc_list_path is None else "fontconfig.match"
                ),
                reason_code="font.fontconfig_missing",
            )
        )
        return False
    listed = _safe_run(
        probe,
        ("fc-list", "-q", font_family),
        timeout_seconds=10,
    )
    if listed.return_code != 0:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
                component="subtitle_font",
                operation="fontconfig.list",
                reason_code="font.not_found",
            )
        )
        return False
    matched = _safe_run(
        probe,
        ("fc-match", "--format=%{family}\n%{file}\n", font_family),
        timeout_seconds=10,
    )
    lines = matched.stdout.splitlines() if matched.return_code == 0 else []
    matched_families = {
        item.strip() for item in (lines[0].split(",") if lines else []) if item.strip()
    }
    if font_family not in matched_families:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
                component="subtitle_font",
                operation="fontconfig.match",
                reason_code="font.family_mismatch",
            )
        )
        return False
    font_path = lines[1].strip() if len(lines) >= 2 else ""
    try:
        readable = bool(font_path) and probe.font_file_is_readable(font_path)
    except Exception:  # noqa: BLE001
        readable = False
    if not readable:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
                component="font",
                operation="fontconfig.match",
                reason_code="font.file_unreadable",
            )
        )
        return False
    return True


def _media_smoke_test(
    probe: _LocalSystemProbe,
    font_family: str,
    issues: list[ReadinessIssue],
) -> bool:
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="video-auto-editor-readiness-"
        )
        smoke_directory = Path(temporary_directory.name)
        subtitle_path = smoke_directory / "readiness.srt"
        subtitle_path.write_text(_MEDIA_PROBE_SRT, encoding="utf-8")
        escaped_font = font_family.replace("\\", r"\\").replace("'", r"\'")
        burn = _safe_run(
            probe,
            (
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=25",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "1",
                "-vf",
                (f"subtitles=readiness.srt:force_style='FontName={escaped_font}'"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                "-f",
                "mp4",
                "-y",
                "readiness.mp4",
            ),
            cwd=smoke_directory,
            timeout_seconds=30,
        )
        if burn.return_code != 0:
            issues.extend(_smoke_issues())
            return False
        probe_result = _safe_run(
            probe,
            (
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "readiness.mp4",
            ),
            cwd=smoke_directory,
            timeout_seconds=15,
        )
        if probe_result.return_code != 0 or not _valid_smoke_probe(probe_result.stdout):
            issues.extend(
                (
                    _issue(
                        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                        component="ffmpeg",
                        operation="ffmpeg.subtitle_burn",
                        reason_code="ffmpeg.smoke_test_failed",
                    ),
                    _issue(
                        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
                        component="ffprobe",
                        operation="ffprobe.probe",
                        reason_code="ffprobe.probe_failed",
                    ),
                    _issue(
                        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
                        component="subtitle_font",
                        operation="ffmpeg.subtitle_burn",
                        reason_code="font.burn_test_failed",
                    ),
                )
            )
            return False
        return True
    except OSError:
        issues.extend(_smoke_issues())
        return False
    finally:
        if temporary_directory is not None:
            try:
                temporary_directory.cleanup()
            except OSError:
                issues.append(
                    _issue(
                        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
                        component="atomic_publication",
                        operation="filesystem.cleanup_probe",
                        reason_code="filesystem.cleanup_failed",
                    )
                )


def _smoke_issues() -> tuple[ReadinessIssue, ...]:
    return (
        _issue(
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            component="ffmpeg",
            operation="ffmpeg.subtitle_burn",
            reason_code="ffmpeg.smoke_test_failed",
        ),
        _issue(
            ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
            component="subtitle_font",
            operation="ffmpeg.subtitle_burn",
            reason_code="font.burn_test_failed",
        ),
    )


def _valid_smoke_probe(stdout: str) -> bool:
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict):
            return False
        format_payload = payload.get("format")
        streams = payload.get("streams")
        if not isinstance(format_payload, dict) or not isinstance(streams, list):
            return False
        format_name = format_payload.get("format_name")
        duration = float(format_payload.get("duration", 0))
        stream_types = {
            item.get("codec_type") for item in streams if isinstance(item, dict)
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(format_name, str)
        and "mp4" in format_name.split(",")
        and duration >= 0.9
        and {"video", "audio"}.issubset(stream_types)
    )


def _check_tls(
    probe: _LocalSystemProbe,
    issues: list[ReadinessIssue],
) -> tuple[object, ...]:
    try:
        observation = probe.tls_observation()
        if not isinstance(observation, TLSObservation):
            raise TypeError
    except Exception:  # noqa: BLE001
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
                component="tls_ca",
                operation="tls.load_ca",
                reason_code="tls.ca_store_unavailable",
            )
        )
        return (False, None)
    if not observation.verification_enabled:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
                component="tls_ca",
                operation="tls.load_ca",
                reason_code="tls.verification_unavailable",
            )
        )
    if observation.ca_count < 1:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
                component="tls_ca",
                operation="tls.load_ca",
                reason_code="tls.ca_store_empty",
            )
        )
    return (observation.verification_enabled, observation.ca_count)


def _check_workspace(
    request: ReadinessRequest,
    issues: list[ReadinessIssue],
) -> bool:
    workspace = request.run_workspace
    try:
        for capability in (
            workspace.temporary,
            workspace.delivery_staging,
            workspace.published_delivery,
            workspace.previous_delivery,
            workspace.diagnostics,
            workspace.cache,
        ):
            capability.inspect_tree()
    except (WorkspaceFailure, OSError):
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
                component="workspace",
                operation="workspace.verify",
                reason_code="filesystem.write_failed",
            )
        )
        return False

    canary = workspace.temporary.location(
        f"readiness-atomic-{secrets.token_hex(16)}.bin"
    )
    try:
        written = canary.publish_bytes_atomically(_WORKSPACE_CANARY)
        persisted = canary.read_bytes()
    except (WorkspaceFailure, OSError, FileExistsError):
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
                component="atomic_publication",
                operation="filesystem.atomic_replace_probe",
                reason_code="filesystem.atomic_replace_failed",
            )
        )
        return False
    if written != len(_WORKSPACE_CANARY) or persisted != _WORKSPACE_CANARY:
        issues.append(
            _issue(
                ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
                component="atomic_publication",
                operation="filesystem.file_sync_probe",
                reason_code="filesystem.file_sync_failed",
            )
        )
        return False
    return True


def _check_adapters(
    request: ReadinessRequest,
    issues: list[ReadinessIssue],
) -> dict[ProviderCapability, str]:
    fingerprints: dict[ProviderCapability, str] = {}
    transcription_report = request.transcription.adapter.check_readiness()
    if not isinstance(transcription_report, TranscriptionReadinessReport):
        raise TypeError("语音识别 Adapter 必须返回 Transcription ReadinessReport")
    issues.extend(
        ReadinessIssue(item.error_code, item.diagnostics)
        for item in transcription_report.issues
    )

    for binding in (request.topic_review, request.subtitle_optimization):
        report = binding.adapter.check_readiness()
        if not isinstance(report, TextReadinessReport):
            raise TypeError("文本业务模块必须返回 TextModel ReadinessReport")
        fingerprints[binding.capability] = report.configuration_fingerprint
        issues.extend(_text_issues(binding.capability, report))
    return fingerprints


def _text_issues(
    capability: ProviderCapability,
    report: TextReadinessReport,
) -> tuple[ReadinessIssue, ...]:
    mapped = []
    for issue in report.issues:
        if issue.code is TextModelReadinessCode.CREDENTIAL_MISSING:
            mapped.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_CREDENTIAL_MISSING,
                    {"capability": capability.value},
                )
            )
        elif issue.code is TextModelReadinessCode.HTTPS_REQUIRED:
            field = issue.diagnostics.get("field")
            mapped.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_HTTPS_REQUIRED,
                    {
                        "field": (
                            field
                            if isinstance(field, str)
                            else "text_model_provider_config.endpoint"
                        )
                    },
                )
            )
        elif issue.code is TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE:
            diagnostics = {
                key: value
                for key, value in issue.diagnostics.items()
                if key in {"component", "operation", "reason_code"}
            }
            mapped.append(
                ReadinessIssue(
                    ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
                    diagnostics,
                )
            )
        else:
            field = issue.diagnostics.get("field")
            mapped.append(
                ReadinessIssue(
                    ErrorCode.CONFIG_VALUE_INVALID,
                    {
                        "field": (
                            field
                            if isinstance(field, str)
                            else _text_issue_field(issue.code)
                        ),
                        "reason_code": "value.invalid_format",
                    },
                )
            )
    return tuple(mapped)


def _text_issue_field(code: TextModelReadinessCode) -> str:
    return {
        TextModelReadinessCode.TIMEOUT_INVALID: (
            "text_model_provider_config.timeout_seconds"
        ),
        TextModelReadinessCode.RETRY_POLICY_INVALID: (
            "text_model_provider_config.retry_policy"
        ),
        TextModelReadinessCode.CONCURRENCY_INVALID: (
            "text_model_provider_config.max_concurrency"
        ),
        TextModelReadinessCode.HEADERS_INVALID: ("text_model_provider_config.headers"),
        TextModelReadinessCode.TRANSPORT_CONFIGURATION_INVALID: (
            "text_model_provider_config.transport"
        ),
    }.get(code, "text_model_provider_config")


def _provider_disclosure(
    binding: ProviderBinding,
    *,
    adapter_fingerprint: str | None,
) -> ProviderDisclosure:
    purpose = {
        ProviderCapability.TRANSCRIPTION: ProviderPurpose.TRANSCRIBE_AUDIO,
        ProviderCapability.TOPIC_REVIEW: ProviderPurpose.REVIEW_TOPICS,
        ProviderCapability.SUBTITLE_OPTIMIZATION: (ProviderPurpose.OPTIMIZE_SUBTITLES),
    }[binding.capability]
    categories = {
        ProviderCapability.TRANSCRIPTION: (ExternalDataCategory.AUDIO_SHARD,),
        ProviderCapability.TOPIC_REVIEW: (
            ExternalDataCategory.BUSINESS_CONSTRAINTS,
            ExternalDataCategory.CANDIDATE_TRANSCRIPT,
            ExternalDataCategory.COURSE_CONTEXT,
        ),
        ProviderCapability.SUBTITLE_OPTIMIZATION: (
            ExternalDataCategory.FIXED_INSTRUCTIONS,
            ExternalDataCategory.SUBTITLE_WINDOW,
        ),
    }[binding.capability]
    fingerprint_payload = {
        "adapter_fingerprint": adapter_fingerprint,
        "adapter_id": binding.adapter_id,
        "capability": binding.capability.value,
        "endpoint": binding.endpoint,
        "model_id": binding.model_id,
        "provider_id": binding.provider_id,
        "schema_version": "provider-disclosure.v1",
    }
    return ProviderDisclosure(
        capability=binding.capability,
        purpose=purpose,
        adapter_id=binding.adapter_id,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        data_categories=categories,
        configuration_fingerprint=_fingerprint(fingerprint_payload),
        endpoint_origin=_https_origin(binding.endpoint),
    )


def _environment_projection(
    request: ReadinessRequest,
    state: _EnvironmentState,
    *,
    ready: bool,
) -> EnvironmentProjection:
    fingerprint = state.installation_fingerprint or _fingerprint(
        {
            "font_available": state.font_available,
            "font_family": request.subtitle_font,
            "installation_manifest": "invalid",
            "media": state.media_observation,
            "platform": state.platform_observation,
            "python": state.python_observation,
            "schema_version": "readiness-environment.v1",
            "tls": state.tls_observation,
            "workspace_ready": state.workspace_ready,
        }
    )
    return EnvironmentProjection(
        certified_platform=state.certified_platform,
        python_version=state.python_version,
        ffmpeg_version=state.ffmpeg_version,
        ffprobe_version=state.ffprobe_version,
        font_family=request.subtitle_font,
        font_available=state.font_available,
        installation_fingerprint=fingerprint,
        preflight_outcome=(
            PreflightOutcome.SUCCEEDED if ready else PreflightOutcome.FAILED
        ),
    )


def _safe_run(
    probe: _LocalSystemProbe,
    command: tuple[str, ...],
    *,
    cwd: Path | None = None,
    timeout_seconds: int,
) -> CommandResult:
    try:
        result = probe.run(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
    except Exception:  # noqa: BLE001
        return CommandResult(127, "")
    return result if isinstance(result, CommandResult) else CommandResult(127, "")


def _safe_detected_version(value: str) -> DetectedVersion | None:
    try:
        return DetectedVersion.from_readiness(value)
    except (TypeError, ValueError):
        return None


def _issue(
    error_code: ErrorCode,
    **diagnostics: object,
) -> ReadinessIssue:
    return ReadinessIssue(error_code, diagnostics)


def _ordered_unique(issues: list[ReadinessIssue]) -> tuple[ReadinessIssue, ...]:
    unique: list[ReadinessIssue] = []
    for issue in issues:
        if issue not in unique:
            unique.append(issue)
    unique.sort(key=lambda item: _ERROR_ORDER[item.error_code])
    return tuple(unique)


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _https_origin(endpoint: str) -> str | None:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = parsed.hostname.casefold()
    if ":" in host:
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ipaddress.AddressValueError:
            return None
    normalized_port = "" if port in {None, 443} else f":{port}"
    return f"https://{host}{normalized_port}"


def _read_regular_installation_file(path: Path) -> bytes:
    parent_descriptor = _open_anchored_directory(path.parent)
    try:
        return _read_regular_installation_file_at(parent_descriptor, path.name)
    finally:
        os.close(parent_descriptor)


def _open_anchored_directory(path: Path) -> int:
    if not path.is_absolute():
        raise OSError("安装记录父目录不是绝对路径")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_regular_installation_file_at(
    parent_descriptor: int,
    filename: str,
) -> bytes:
    parent_status = os.fstat(parent_descriptor)
    metadata_result = os.stat(
        filename,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    _validate_installation_file_status(metadata_result, parent_status)
    descriptor = os.open(
        filename,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        _validate_installation_file_status(opened, parent_status)
        if _file_identity(opened) != _file_identity(metadata_result):
            raise OSError("安装记录读取期间发生替换")
        chunks: list[bytes] = []
        remaining = _MAX_INSTALLATION_DOCUMENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > _MAX_INSTALLATION_DOCUMENT_BYTES:
            raise OSError("安装记录超过允许大小")
        after_read = os.fstat(descriptor)
        current = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _file_snapshot(after_read) != _file_snapshot(opened) or _file_snapshot(
            current
        ) != _file_snapshot(opened):
            raise OSError("安装记录读取期间发生变化")
        return contents
    finally:
        os.close(descriptor)


def _validate_installation_file_status(
    file_status: os.stat_result,
    parent_status: os.stat_result,
) -> None:
    if (
        not stat.S_ISREG(file_status.st_mode)
        or file_status.st_nlink != 1
        or file_status.st_dev != parent_status.st_dev
        or file_status.st_uid != parent_status.st_uid
        or stat.S_IMODE(file_status.st_mode) & 0o022
    ):
        raise OSError("安装记录不是受信常规文件")
    if file_status.st_size > _MAX_INSTALLATION_DOCUMENT_BYTES:
        raise OSError("安装记录超过允许大小")


def _file_identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _file_snapshot(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_nlink,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _directory_descriptor_matches_path(descriptor: int, path: Path) -> bool:
    try:
        path_status = path.lstat()
        opened_status = os.fstat(descriptor)
    except OSError:
        return False
    return stat.S_ISDIR(path_status.st_mode) and _file_identity(
        path_status
    ) == _file_identity(opened_status)


def _installation_manifest_has_complete_shape(manifest: dict[object, object]) -> bool:
    application = manifest.get("application")
    environment = manifest.get("environment")
    platform_document = manifest.get("platform")
    python_document = manifest.get("python")
    runtime_lock = manifest.get("runtime_lock")
    snapshot_packages = manifest.get("snapshot_packages")
    system_packages = manifest.get("system_packages")
    wheelhouse = manifest.get("wheelhouse")
    if (
        set(manifest) != _INSTALLATION_MANIFEST_FIELDS
        or not isinstance(application, dict)
        or set(application) != {"name", "version", "wheel"}
        or not isinstance(application.get("wheel"), dict)
        or set(application["wheel"]) != {"filename", "sha256"}
        or not isinstance(environment, dict)
        or set(environment)
        != {"ffmpeg_version", "ffprobe_version", "font_family", "font_file"}
        or not isinstance(platform_document, dict)
        or set(platform_document)
        != {"architecture", "operating_system", "operating_system_version"}
        or not isinstance(python_document, dict)
        or set(python_document) != {"implementation", "version"}
        or not isinstance(runtime_lock, dict)
        or set(runtime_lock) != {"filename", "sha256"}
        or not isinstance(snapshot_packages, dict)
        or not isinstance(system_packages, dict)
        or not isinstance(wheelhouse, list)
    ):
        return False
    artifacts_are_shaped = all(
        isinstance(artifact, dict) and set(artifact) == {"filename", "sha256"}
        for artifact in wheelhouse
    )
    if not artifacts_are_shaped:
        return False
    wheel = application["wheel"]
    if not isinstance(wheel, dict):
        return False
    digests = [
        wheel["sha256"],
        runtime_lock["sha256"],
        *(artifact["sha256"] for artifact in wheelhouse),
    ]
    if not all(
        isinstance(digest, str) and _SHA256.fullmatch(digest) is not None
        for digest in digests
    ):
        return False
    scalar_texts = (
        manifest.get("installation_prefix"),
        environment["ffmpeg_version"],
        environment["ffprobe_version"],
        environment["font_family"],
        environment["font_file"],
    )
    if not all(_is_safe_manifest_text(value) for value in scalar_texts):
        return False
    apt_snapshot_id = manifest.get("apt_snapshot_id")
    if (
        manifest.get("schema_version") != "production-installation-manifest.v1"
        or application["name"] != "video-auto-editor"
        or not isinstance(application["version"], str)
        or _APPLICATION_VERSION.fullmatch(application["version"]) is None
        or not _is_safe_filename(wheel["filename"], suffix=".whl")
        or not _is_safe_filename(runtime_lock["filename"])
        or not isinstance(apt_snapshot_id, str)
        or _APT_SNAPSHOT_ID.fullmatch(apt_snapshot_id) is None
        or platform_document
        != {
            "architecture": "amd64",
            "operating_system": "ubuntu",
            "operating_system_version": "24.04",
        }
        or python_document.get("implementation") != "CPython"
        or not _is_certified_python_version(python_document.get("version"))
        or not _is_absolute_manifest_path(manifest.get("installation_prefix"))
        or not _is_absolute_manifest_path(environment["font_file"])
        or not _is_package_inventory(snapshot_packages)
        or set(snapshot_packages) != _SNAPSHOT_PACKAGE_NAMES
        or not _is_package_inventory(system_packages)
        or any(
            system_packages.get(name) != version
            for name, version in snapshot_packages.items()
        )
    ):
        return False
    wheelhouse_filenames = [artifact["filename"] for artifact in wheelhouse]
    return (
        all(
            _is_safe_filename(filename, suffix=".whl")
            for filename in wheelhouse_filenames
        )
        and len(set(wheelhouse_filenames)) == len(wheelhouse_filenames)
        and wheelhouse_filenames == sorted(wheelhouse_filenames)
    )


def _is_safe_manifest_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _is_safe_filename(value: object, *, suffix: str | None = None) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_FILENAME.fullmatch(value) is not None
        and (suffix is None or value.endswith(suffix))
    )


def _is_absolute_manifest_path(value: object) -> bool:
    if not _is_safe_manifest_text(value):
        return False
    try:
        return Path(cast(str, value)).is_absolute()
    except (OSError, ValueError):
        return False


def _is_certified_python_version(value: object) -> bool:
    if not isinstance(value, str) or _PYTHON_VERSION.fullmatch(value) is None:
        return False
    version = tuple(int(part) for part in value.split("."))
    return _PYTHON_MINIMUM <= version < _PYTHON_MAXIMUM


def _is_package_inventory(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(name, str)
            and _PACKAGE_NAME.fullmatch(name) is not None
            and _is_safe_manifest_text(version)
            for name, version in value.items()
        )
    )


def _distribution_identity_paths(
    distribution: metadata.Distribution,
) -> tuple[Path, Path] | None:
    files = distribution.files
    if files is None:
        return None
    module_entry = None
    direct_url_entries = []
    for entry in files:
        relative = Path(str(entry))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        if relative.as_posix() == "video_auto_editor/application/readiness.py":
            if module_entry is not None:
                return None
            module_entry = entry
        if relative.name == "direct_url.json" and relative.parent.name.endswith(
            ".dist-info"
        ):
            direct_url_entries.append(entry)
    if module_entry is None or len(direct_url_entries) != 1:
        return None
    module_path = Path(str(distribution.locate_file(module_entry))).resolve(strict=True)
    direct_url_path = Path(
        str(distribution.locate_file(direct_url_entries[0]))
    ).resolve(strict=True)
    return module_path, direct_url_path


def _direct_url_matches_wheel(
    document: object,
    *,
    filename: object,
    sha256: object,
) -> bool:
    if (
        not isinstance(document, dict)
        or set(document) != {"archive_info", "url"}
        or not isinstance(document.get("archive_info"), dict)
        or not isinstance(document.get("url"), str)
        or not _is_safe_filename(filename, suffix=".whl")
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        return False
    archive_info = document["archive_info"]
    if not isinstance(archive_info, dict) or not set(archive_info).issubset(
        {"hash", "hashes"}
    ):
        return False
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"sha256"}:
        return False
    recorded_sha256 = hashes.get("sha256")
    if (
        not isinstance(recorded_sha256, str)
        or _SHA256.fullmatch(recorded_sha256) is None
        or not secrets.compare_digest(recorded_sha256, sha256)
    ):
        return False
    legacy_hash = archive_info.get("hash")
    if legacy_hash is not None and legacy_hash != f"sha256={sha256}":
        return False
    try:
        origin = urlsplit(document["url"])
    except (TypeError, ValueError):
        return False
    return (
        origin.scheme == "file"
        and origin.netloc in {"", "localhost"}
        and Path(unquote(origin.path)).name == filename
    )


def _load_strict_json(payload: bytes) -> object:
    return json.loads(
        payload,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_non_finite_json_number,
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("JSON 文档包含重复字段")
        document[key] = value
    return document


def _reject_non_finite_json_number(value: str) -> object:
    raise ValueError(f"JSON 文档包含非有限数字：{value}")


__all__ = ["Readiness"]
