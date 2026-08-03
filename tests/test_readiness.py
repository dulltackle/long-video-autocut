import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

import video_auto_editor.application.readiness as readiness_module
from video_auto_editor.application.readiness import (
    CommandResult,
    InstallationObservation,
    ProviderBinding,
    ProviderPurpose,
    Readiness,
    ReadinessRequest,
    TLSObservation,
)
from video_auto_editor.diagnostics import (
    ExternalDataCategory,
    ProviderCapability,
)
from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.text_model import (
    ReadinessIssue as TextReadinessIssue,
)
from video_auto_editor.text_model import (
    ReadinessReport as TextReadinessReport,
)
from video_auto_editor.text_model import (
    TextModelReadinessCode,
)
from video_auto_editor.transcription import (
    ReadinessIssue as TranscriptionReadinessIssue,
)
from video_auto_editor.transcription import (
    ReadinessReport as TranscriptionReadinessReport,
)
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedPathCapability,
    Workspace,
    WorkspaceFailure,
)


class _SystemProbe:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def platform_name(self) -> str:
        return "linux"

    def architecture(self) -> str:
        return "x86_64"

    def os_release(self) -> dict[str, str]:
        return {"ID": "ubuntu", "VERSION_ID": "24.04"}

    def python_implementation(self) -> str:
        return "CPython"

    def python_version(self) -> tuple[int, int, int]:
        return (3, 12, 3)

    def is_virtual_environment(self) -> bool:
        return True

    def installation_observation(self) -> InstallationObservation:
        return InstallationObservation.verified(
            manifest_sha256="a" * 64,
        )

    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}"

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        if command[1:] == ("-version",):
            return CommandResult(0, f"{command[0]} version 6.1.1-3ubuntu5\n")
        if command[1:] == ("-hide_banner", "-filters"):
            return CommandResult(0, " T.. subtitles V->V Render subtitles\n")
        if command[1:] == ("-hide_banner", "-encoders"):
            return CommandResult(0, " V....D libx264\n A..... aac\n")
        if command[0] == "fc-list":
            return CommandResult(0, "")
        if command[0] == "fc-match":
            return CommandResult(
                0,
                "Noto Sans CJK SC\n/usr/share/fonts/opentype/noto/NotoSansCJK.ttc\n",
            )
        if command[0] == "ffmpeg":
            return CommandResult(0, "")
        if command[0] == "ffprobe":
            return CommandResult(
                0,
                '{"format":{"format_name":"mov,mp4","duration":"1.000"},'
                '"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            )
        raise AssertionError(f"发生了未编排的本地命令：{command!r}")

    def font_file_is_readable(self, _path: str) -> bool:
        return True

    def tls_observation(self) -> TLSObservation:
        return TLSObservation(verification_enabled=True, ca_count=141)


class _TranscriptionAdapter:
    def __init__(
        self,
        report: TranscriptionReadinessReport | None = None,
    ) -> None:
        self._report = report or TranscriptionReadinessReport(ready=True)
        self.readiness_calls = 0
        self.business_calls = 0

    def check_readiness(self) -> TranscriptionReadinessReport:
        self.readiness_calls += 1
        return self._report

    def transcribe(self, _request: object) -> None:
        self.business_calls += 1
        raise AssertionError("预检不得发起 StepAudio 业务请求")


class _TextCapability:
    def __init__(
        self,
        fingerprint: str,
        report: TextReadinessReport | None = None,
    ) -> None:
        self._fingerprint = fingerprint
        self._report = report
        self.readiness_calls = 0
        self.business_calls = 0

    def check_readiness(self) -> TextReadinessReport:
        self.readiness_calls += 1
        return self._report or TextReadinessReport(
            ready=True, configuration_fingerprint=self._fingerprint
        )

    def review(self, _request: object) -> None:
        self.business_calls += 1
        raise AssertionError("预检不得发起主题评审业务请求")

    def optimize(self, _request: object) -> None:
        self.business_calls += 1
        raise AssertionError("预检不得发起字幕优化业务请求")


def _request(run_workspace, adapters) -> ReadinessRequest:
    transcription, topic_review, subtitle_optimization = adapters
    return ReadinessRequest(
        run_workspace=run_workspace,
        subtitle_font="Noto Sans CJK SC",
        transcription=ProviderBinding(
            capability=ProviderCapability.TRANSCRIPTION,
            adapter=transcription,
            adapter_id="stepaudio",
            provider_id="stepaudio",
            model_id="stepaudio-2.5-asr",
            endpoint="https://speech.example.test:443/v1/audio/asr/sse",
        ),
        topic_review=ProviderBinding(
            capability=ProviderCapability.TOPIC_REVIEW,
            adapter=topic_review,
            adapter_id="stepfun",
            provider_id="stepfun",
            model_id="step-2-mini",
            endpoint="https://text.example.test/v1",
        ),
        subtitle_optimization=ProviderBinding(
            capability=ProviderCapability.SUBTITLE_OPTIMIZATION,
            adapter=subtitle_optimization,
            adapter_id="stepfun",
            provider_id="stepfun",
            model_id="step-2-mini",
            endpoint="https://text.example.test/v1",
        ),
    )


def test_strict_readiness_returns_safe_environment_and_fixed_provider_plan(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace-secret-canary")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )
    system = _SystemProbe()

    with workspace.acquire_run(RunId.new()) as run_workspace:
        first = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=system,
        )
        second = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=system,
        )

    assert first == second
    assert first.ready is True
    assert first.issues == ()
    assert first.environment_fact is not None
    assert first.environment.python_version == "3.12.3"
    assert first.environment.ffmpeg_version == "6.1.1-3ubuntu5"
    assert first.environment.ffprobe_version == "6.1.1-3ubuntu5"
    assert first.environment.font_family == "Noto Sans CJK SC"
    assert first.environment.font_available is True
    assert first.environment.installation_fingerprint.startswith("sha256:")
    assert tuple(item.capability for item in first.provider_disclosures) == (
        ProviderCapability.TRANSCRIPTION,
        ProviderCapability.TOPIC_REVIEW,
        ProviderCapability.SUBTITLE_OPTIMIZATION,
    )
    assert tuple(item.purpose for item in first.provider_disclosures) == (
        ProviderPurpose.TRANSCRIBE_AUDIO,
        ProviderPurpose.REVIEW_TOPICS,
        ProviderPurpose.OPTIMIZE_SUBTITLES,
    )
    assert first.provider_disclosures[0].data_categories == (
        ExternalDataCategory.AUDIO_SHARD,
    )
    assert first.provider_disclosures[1].data_categories == (
        ExternalDataCategory.BUSINESS_CONSTRAINTS,
        ExternalDataCategory.CANDIDATE_TRANSCRIPT,
        ExternalDataCategory.COURSE_CONTEXT,
    )
    assert first.provider_disclosures[2].data_categories == (
        ExternalDataCategory.FIXED_INSTRUCTIONS,
        ExternalDataCategory.SUBTITLE_WINDOW,
    )
    assert first.provider_disclosures[0].endpoint_origin == (
        "https://speech.example.test"
    )
    assert first.provider_disclosures[1].endpoint_origin == (
        "https://text.example.test"
    )
    assert all(
        disclosure.to_diagnostic_fact() is not None
        for disclosure in first.provider_disclosures
    )
    assert all(adapter.readiness_calls == 2 for adapter in adapters)
    assert all(adapter.business_calls == 0 for adapter in adapters)
    assert all("secret-canary" not in repr(value) for value in (first, second))
    assert any(
        command[1:] == ("-hide_banner", "-filters") for command in system.commands
    )
    assert any(
        command[1:] == ("-hide_banner", "-encoders") for command in system.commands
    )
    assert sum(command[0] == "ffprobe" for command in system.commands) == 4
    with pytest.raises(ValueError, match="字体家族"):
        replace(first.environment, font_family=None)
    with pytest.raises(ValueError, match="Adapter 标识"):
        replace(first.provider_disclosures[0], adapter_id=None)


class _UnavailableSystemProbe(_SystemProbe):
    def platform_name(self) -> str:
        return "darwin"

    def architecture(self) -> str:
        return "arm64"

    def os_release(self) -> dict[str, str]:
        return {"ID": "not-ubuntu", "VERSION_ID": "23.10"}

    def python_implementation(self) -> str:
        return "PyPy"

    def python_version(self) -> tuple[int, int, int]:
        return (3, 13, 0)

    def is_virtual_environment(self) -> bool:
        return False

    def which(self, _command: str) -> str | None:
        return None

    def tls_observation(self) -> TLSObservation:
        return TLSObservation(verification_enabled=False, ca_count=0)


class _InvalidInstallationSystemProbe(_SystemProbe):
    def installation_observation(self) -> InstallationObservation:
        return InstallationObservation.invalid(
            "manifest.digest_mismatch",
        )


def _production_installation_manifest(installation_prefix: Path) -> dict:
    snapshot_packages = {
        "ca-certificates": "20240203",
        "ffmpeg": "7:6.1.1-3ubuntu5",
        "fontconfig": "2.15.0-1.1ubuntu2",
        "fonts-noto-cjk": "1:20230817+repack1-3",
        "python3.12": "3.12.3-1ubuntu0.8",
        "python3.12-venv": "3.12.3-1ubuntu0.8",
    }
    return {
        "application": {
            "name": "video-auto-editor",
            "version": "4.7.0",
            "wheel": {
                "filename": "video_auto_editor-4.7.0-py3-none-any.whl",
                "sha256": "1" * 64,
            },
        },
        "apt_snapshot_id": "20260725T000000Z",
        "environment": {
            "ffmpeg_version": "6.1.1-3ubuntu5",
            "ffprobe_version": "6.1.1-3ubuntu5",
            "font_family": "Noto Sans CJK SC",
            "font_file": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        },
        "installation_prefix": str(installation_prefix),
        "platform": {
            "architecture": "amd64",
            "operating_system": "ubuntu",
            "operating_system_version": "24.04",
        },
        "python": {
            "implementation": "CPython",
            "version": "3.12.3",
        },
        "runtime_lock": {
            "filename": "requirements-runtime.lock",
            "sha256": "2" * 64,
        },
        "schema_version": "production-installation-manifest.v1",
        "snapshot_packages": snapshot_packages,
        "system_packages": dict(snapshot_packages),
        "wheelhouse": [],
    }


def _observe_local_installation(
    tmp_path: Path,
    monkeypatch,
    *,
    manifest: dict | None = None,
    manifest_payload: bytes | None = None,
    distribution_outside: bool = False,
    version_container: str = "versions",
    version_name: str = "4.7.0",
    environment_name: str = "venv",
    python_outside: bool = False,
    cli_outside: bool = False,
    shadow_import: bool = False,
    direct_url_sha256: str = "1" * 64,
    editable: bool = False,
    swap_version_parent_on_manifest_open: bool = False,
    installation_document_fault: str | None = None,
    invoke_through_current: bool = False,
) -> tuple[InstallationObservation, bytes]:
    installation_prefix = tmp_path / "production"
    version_directory = installation_prefix / version_container / version_name
    environment_prefix = version_directory / environment_name
    site_packages = environment_prefix / "lib" / "python3.12" / "site-packages"
    distribution_module = (
        site_packages / "video_auto_editor" / "application" / "readiness.py"
    )
    distribution_module.parent.mkdir(parents=True)
    distribution_module.write_text("# installed module\n", encoding="utf-8")
    imported_module = distribution_module
    if shadow_import:
        imported_module = (
            environment_prefix
            / "shadow"
            / "video_auto_editor"
            / "application"
            / "readiness.py"
        )
        imported_module.parent.mkdir(parents=True)
        imported_module.write_text("# shadow module\n", encoding="utf-8")
    bin_directory = environment_prefix / "bin"
    bin_directory.mkdir()
    python_executable = bin_directory / "python"
    cli_executable = bin_directory / "video-auto-editor"
    python_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    python_executable.chmod(0o755)
    cli_executable.chmod(0o755)
    invoked_cli = cli_executable
    if invoke_through_current:
        current = installation_prefix / "current"
        current.symlink_to(Path("versions") / version_name, target_is_directory=True)
        invoked_cli = current / environment_name / "bin" / "video-auto-editor"
    outside_bin = tmp_path / "outside-bin"
    outside_bin.mkdir()
    outside_python = outside_bin / "python"
    outside_cli = outside_bin / "video-auto-editor"
    outside_python.write_text("#!/bin/sh\n", encoding="utf-8")
    outside_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    outside_python.chmod(0o755)
    outside_cli.chmod(0o755)

    document = (
        _production_installation_manifest(installation_prefix)
        if manifest is None
        else manifest
    )
    manifest_bytes = (
        (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
        if manifest_payload is None
        else manifest_payload
    )
    manifest_path = version_directory / "installation-manifest.json"
    ready_path = version_directory / "READY"
    manifest_path.write_bytes(manifest_bytes)
    ready_path.write_text(
        json.dumps(
            {
                "installation_manifest_sha256": hashlib.sha256(
                    manifest_bytes
                ).hexdigest(),
                "schema_version": "production-installation-ready.v1",
            }
        ),
        encoding="utf-8",
    )
    if installation_document_fault == "symlink":
        external_manifest = tmp_path / "external-manifest.json"
        external_manifest.write_bytes(manifest_bytes)
        manifest_path.unlink()
        manifest_path.symlink_to(external_manifest)
    elif installation_document_fault == "nonregular":
        ready_path.unlink()
        ready_path.mkdir()
    elif installation_document_fault == "oversized":
        ready_path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    external_site_packages = tmp_path / "external-site-packages"
    external_site_packages.mkdir()
    distribution_root = (
        external_site_packages if distribution_outside else site_packages
    )
    if distribution_outside:
        external_module = (
            distribution_root / "video_auto_editor" / "application" / "readiness.py"
        )
        external_module.parent.mkdir(parents=True)
        external_module.write_text("# external module\n", encoding="utf-8")
    direct_url_relative = Path("video_auto_editor-4.7.0.dist-info/direct_url.json")
    direct_url_path = distribution_root / direct_url_relative
    direct_url_path.parent.mkdir(parents=True, exist_ok=True)
    direct_url = (
        {
            "dir_info": {"editable": True},
            "url": (tmp_path / "checkout").as_uri(),
        }
        if editable
        else {
            "archive_info": {"hashes": {"sha256": direct_url_sha256}},
            "url": (tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl").as_uri(),
        }
    )
    direct_url_path.write_text(json.dumps(direct_url), encoding="utf-8")

    class _Distribution:
        version = "4.7.0"
        files = (
            Path("video_auto_editor/application/readiness.py"),
            direct_url_relative,
        )

        @staticmethod
        def locate_file(relative_path):
            if str(relative_path) in {"", "."}:
                return distribution_root
            return distribution_root / relative_path

    monkeypatch.setattr(readiness_module.sys, "prefix", str(environment_prefix))
    monkeypatch.setattr(
        readiness_module.sys,
        "executable",
        str(outside_python if python_outside else python_executable),
    )
    monkeypatch.setattr(
        readiness_module.sys,
        "argv",
        [str(outside_cli if cli_outside else invoked_cli)],
    )
    monkeypatch.setattr(readiness_module, "__file__", str(imported_module))
    monkeypatch.setattr(
        readiness_module.metadata,
        "distribution",
        lambda _name: _Distribution(),
    )
    if swap_version_parent_on_manifest_open:
        original_open = readiness_module.os.open
        replacement_performed = False

        def _replace_parent_then_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replacement_performed
            if (
                not replacement_performed
                and Path(path).name == "installation-manifest.json"
            ):
                replacement_performed = True
                moved = version_directory.with_name(f"{version_directory.name}.moved")
                version_directory.rename(moved)
                version_directory.symlink_to(moved, target_is_directory=True)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(readiness_module.os, "open", _replace_parent_then_open)
    elif installation_document_fault == "replaced":
        original_open = readiness_module.os.open
        replacement_performed = False

        def _replace_ready_then_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal replacement_performed
            if not replacement_performed and Path(path).name == "READY":
                replacement_performed = True
                contents = ready_path.read_bytes()
                ready_path.rename(ready_path.with_name("READY.old"))
                ready_path.write_bytes(contents)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(readiness_module.os, "open", _replace_ready_then_open)
    return (
        readiness_module._LocalSystemProbe().installation_observation(),
        manifest_bytes,
    )


def test_installation_manifest_identity_is_a_hard_local_readiness_requirement(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_InvalidInstallationSystemProbe(),
        )

    assert report.ready is False
    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID,
    )
    assert report.issues[0].diagnostics == {
        "component": "installation_manifest",
        "operation": "manifest.verify",
        "reason_code": "manifest.digest_mismatch",
    }
    assert report.environment_fact is not None
    assert all(adapter.business_calls == 0 for adapter in adapters)


@pytest.mark.parametrize("distribution_outside", [False, True])
def test_local_installation_probe_binds_distribution_and_import_to_venv(
    tmp_path,
    monkeypatch,
    distribution_outside,
):
    observation, manifest_bytes = _observe_local_installation(
        tmp_path,
        monkeypatch,
        distribution_outside=distribution_outside,
    )

    if distribution_outside:
        assert observation == InstallationObservation.invalid(
            "manifest.prefix_mismatch"
        )
    else:
        assert observation == InstallationObservation.verified(
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest()
        )


def test_local_installation_probe_rejects_incomplete_manifest_schema(
    tmp_path,
    monkeypatch,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    del manifest["application"]["wheel"]

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


def test_local_installation_probe_rejects_invalid_manifest_digest_field_type(
    tmp_path,
    monkeypatch,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    manifest["application"]["wheel"]["sha256"] = 7

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


def test_local_installation_probe_rejects_duplicate_json_fields(
    tmp_path,
    monkeypatch,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    canonical = json.dumps(manifest, sort_keys=True).encode("utf-8")
    duplicate = (
        b'{"schema_version":"production-installation-manifest.v1",'
        + canonical[1:]
        + b"\n"
    )

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest_payload=duplicate,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


def test_local_installation_probe_rejects_non_finite_json_numbers(
    tmp_path,
    monkeypatch,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    manifest["environment"]["ffmpeg_version"] = float("nan")

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


def test_local_installation_probe_rejects_invalid_manifest_scalar_types(
    tmp_path,
    monkeypatch,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    manifest["apt_snapshot_id"] = 20260725

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


@pytest.mark.parametrize(
    "invalid_case",
    [
        "unknown_top_level",
        "unknown_application_field",
        "missing_environment_field",
        "invalid_platform",
        "unsupported_python",
        "unsafe_runtime_lock_filename",
        "relative_installation_prefix",
        "relative_font_file",
        "incomplete_snapshot_packages",
        "mismatched_system_package",
        "invalid_wheelhouse_entry",
        "unsorted_wheelhouse",
    ],
)
def test_local_installation_probe_strictly_validates_every_manifest_section(
    tmp_path,
    monkeypatch,
    invalid_case,
):
    installation_prefix = tmp_path / "production"
    manifest = _production_installation_manifest(installation_prefix)
    if invalid_case == "unknown_top_level":
        manifest["unknown"] = "value"
    elif invalid_case == "unknown_application_field":
        manifest["application"]["unknown"] = "value"
    elif invalid_case == "missing_environment_field":
        del manifest["environment"]["font_family"]
    elif invalid_case == "invalid_platform":
        manifest["platform"]["architecture"] = "x86_64"
    elif invalid_case == "unsupported_python":
        manifest["python"]["version"] = "3.13.0"
    elif invalid_case == "unsafe_runtime_lock_filename":
        manifest["runtime_lock"]["filename"] = "../requirements-runtime.lock"
    elif invalid_case == "relative_installation_prefix":
        manifest["installation_prefix"] = "production"
    elif invalid_case == "relative_font_file":
        manifest["environment"]["font_file"] = "NotoSansCJK-Regular.ttc"
    elif invalid_case == "incomplete_snapshot_packages":
        del manifest["snapshot_packages"]["ffmpeg"]
    elif invalid_case == "mismatched_system_package":
        manifest["system_packages"]["ffmpeg"] = "different"
    elif invalid_case == "invalid_wheelhouse_entry":
        manifest["wheelhouse"] = [
            {"filename": "dependency.whl", "sha256": "3" * 64, "extra": True}
        ]
    elif invalid_case == "unsorted_wheelhouse":
        manifest["wheelhouse"] = [
            {"filename": "z.whl", "sha256": "3" * 64},
            {"filename": "a.whl", "sha256": "4" * 64},
        ]

    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    assert observation == InstallationObservation.invalid("manifest.schema_invalid")


@pytest.mark.parametrize(
    ("version_container", "version_name", "environment_name"),
    [
        ("releases", "4.7.0", "venv"),
        ("versions", "4.6.0", "venv"),
        ("versions", "4.7.0", "environment"),
    ],
)
def test_local_installation_probe_rejects_nonstandard_version_layout(
    tmp_path,
    monkeypatch,
    version_container,
    version_name,
    environment_name,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        version_container=version_container,
        version_name=version_name,
        environment_name=environment_name,
    )

    assert observation == InstallationObservation.invalid("manifest.prefix_mismatch")


@pytest.mark.parametrize(
    ("python_outside", "cli_outside"), [(True, False), (False, True)]
)
def test_local_installation_probe_binds_python_and_cli_to_version_environment(
    tmp_path,
    monkeypatch,
    python_outside,
    cli_outside,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        python_outside=python_outside,
        cli_outside=cli_outside,
    )

    assert observation == InstallationObservation.invalid("manifest.prefix_mismatch")


def test_local_installation_probe_accepts_current_version_cli_symlink(
    tmp_path,
    monkeypatch,
):
    observation, manifest_bytes = _observe_local_installation(
        tmp_path,
        monkeypatch,
        invoke_through_current=True,
    )

    assert observation == InstallationObservation.verified(
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest()
    )


def test_local_installation_probe_rejects_in_prefix_shadow_import(
    tmp_path,
    monkeypatch,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        shadow_import=True,
    )

    assert observation == InstallationObservation.invalid("manifest.prefix_mismatch")


@pytest.mark.parametrize("editable", [False, True])
def test_local_installation_probe_binds_distribution_to_manifest_wheel(
    tmp_path,
    monkeypatch,
    editable,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        direct_url_sha256="3" * 64,
        editable=editable,
    )

    assert observation == InstallationObservation.invalid("manifest.digest_mismatch")


def test_local_installation_probe_rejects_version_directory_replacement(
    tmp_path,
    monkeypatch,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        swap_version_parent_on_manifest_open=True,
    )

    assert observation == InstallationObservation.invalid("manifest.unreadable")


@pytest.mark.parametrize(
    "installation_document_fault",
    ["symlink", "nonregular", "oversized", "replaced"],
)
def test_local_installation_probe_rejects_untrusted_installation_documents(
    tmp_path,
    monkeypatch,
    installation_document_fault,
):
    observation, _ = _observe_local_installation(
        tmp_path,
        monkeypatch,
        installation_document_fault=installation_document_fault,
    )

    assert observation == InstallationObservation.invalid("manifest.unreadable")


def test_all_local_and_adapter_failures_are_collected_ordered_and_deduplicated(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "private-course-name.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace-private-canary")
    duplicate_tls = TranscriptionReadinessIssue(
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
        {
            "component": "tls_ca",
            "operation": "tls.load_ca",
            "reason_code": "tls.verification_unavailable",
        },
    )
    transcription = _TranscriptionAdapter(
        TranscriptionReadinessReport(
            ready=False,
            issues=(
                TranscriptionReadinessIssue(
                    ErrorCode.CONFIG_CREDENTIAL_MISSING,
                    {"capability": "transcription"},
                ),
                duplicate_tls,
                duplicate_tls,
            ),
        )
    )
    text_issues = (
        TextReadinessIssue(TextModelReadinessCode.CREDENTIAL_MISSING),
        TextReadinessIssue(
            TextModelReadinessCode.HTTPS_REQUIRED,
            {"field": "text_model_provider_config.endpoint"},
        ),
        TextReadinessIssue(
            TextModelReadinessCode.TLS_VERIFICATION_UNAVAILABLE,
            {
                "component": "tls_ca",
                "operation": "tls.load_ca",
                "reason_code": "tls.verification_unavailable",
            },
        ),
    )
    topic_review = _TextCapability(
        "b" * 64,
        TextReadinessReport(
            ready=False,
            configuration_fingerprint="b" * 64,
            issues=text_issues,
        ),
    )
    subtitle_optimization = _TextCapability(
        "c" * 64,
        TextReadinessReport(
            ready=False,
            configuration_fingerprint="c" * 64,
            issues=text_issues,
        ),
    )
    adapters = (transcription, topic_review, subtitle_optimization)
    system = _UnavailableSystemProbe()

    def fail_workspace_inspection(
        _capability: ManagedDirectoryCapability,
    ) -> tuple[object, ...]:
        raise WorkspaceFailure(
            ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
            {
                "component": "workspace",
                "operation": "workspace.access",
                "reason_code": "workspace.permission_denied",
            },
        )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        monkeypatch.setattr(
            ManagedDirectoryCapability,
            "inspect_tree",
            fail_workspace_inspection,
        )
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=system,
        )

    registry_order = {code: index for index, code in enumerate(ERROR_REGISTRY)}
    issue_order = [registry_order[issue.error_code] for issue in report.issues]
    issue_keys = [
        (issue.error_code, tuple(issue.diagnostics.items())) for issue in report.issues
    ]
    codes = tuple(issue.error_code for issue in report.issues)

    assert report.ready is False
    assert issue_order == sorted(issue_order)
    assert len(issue_keys) == len(set(issue_keys))
    assert codes.count(ErrorCode.CONFIG_CREDENTIAL_MISSING) == 3
    assert codes.count(ErrorCode.CONFIG_HTTPS_REQUIRED) == 1
    assert ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED in codes
    assert ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED in codes
    assert ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE in codes
    assert ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE in codes
    assert ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE in codes
    assert ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE in codes
    assert ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE in codes
    assert {
        issue.diagnostics.get("reason_code")
        for issue in report.issues
        if issue.error_code is ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED
    } == {
        "platform.os_unsupported",
        "platform.release_unsupported",
        "platform.architecture_unsupported",
    }
    assert {
        issue.diagnostics.get("reason_code")
        for issue in report.issues
        if issue.error_code is ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED
    } == {
        "python.implementation_unsupported",
        "python.version_too_new",
        "python.venv_required",
    }
    assert {
        issue.diagnostics.get("capability")
        for issue in report.issues
        if issue.error_code is ErrorCode.CONFIG_CREDENTIAL_MISSING
    } == {"transcription", "topic_review", "subtitle_optimization"}
    assert report.environment_fact is None
    assert report.environment.certified_platform is None
    assert report.environment.ffmpeg_version is None
    assert report.environment.ffprobe_version is None
    assert report.environment.font_available is False
    assert all(adapter.readiness_calls == 1 for adapter in adapters)
    assert all(adapter.business_calls == 0 for adapter in adapters)
    assert system.commands == []
    serialized = repr(report)
    for forbidden in (
        "workspace-private-canary",
        "private-course-name.mp4",
    ):
        assert forbidden not in serialized


class _MediaMismatchSystemProbe(_SystemProbe):
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.commands.append(command)
        if command == ("ffmpeg", "-version"):
            return CommandResult(0, "ffmpeg version 6.1.1\n")
        if command == ("ffprobe", "-version"):
            return CommandResult(0, "ffprobe version 6.1.2\n")
        if command[1:] == ("-hide_banner", "-filters"):
            return CommandResult(0, " T.. scale V->V Scale video\n")
        if command[1:] == ("-hide_banner", "-encoders"):
            return CommandResult(0, " A..... aac\n")
        if command[0] == "fc-list":
            return CommandResult(0, "")
        if command[0] == "fc-match":
            return CommandResult(
                0,
                "DejaVu Sans\n/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf\n",
            )
        raise AssertionError(f"发生了未编排的本地命令：{command!r}")


def test_media_versions_capabilities_and_exact_font_are_all_required(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_MediaMismatchSystemProbe(),
        )

    reasons_by_code = {
        code: {
            issue.diagnostics.get("reason_code")
            for issue in report.issues
            if issue.error_code is code
        }
        for code in (
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
            ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
        )
    }

    assert report.ready is False
    assert reasons_by_code[ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE] == {
        "tool.version_mismatch",
        "ffmpeg.filter_missing",
        "ffmpeg.encoder_missing",
    }
    assert reasons_by_code[ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE] == {
        "tool.version_mismatch"
    }
    assert reasons_by_code[ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE] == {
        "font.family_mismatch"
    }
    assert report.environment.ffmpeg_version == "6.1.1"
    assert report.environment.ffprobe_version == "6.1.2"
    assert report.environment.font_available is False
    assert report.environment_fact is not None
    assert all(adapter.business_calls == 0 for adapter in adapters)


class _BurnFailureSystemProbe(_SystemProbe):
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        if command[0] == "ffmpeg" and "-vf" in command:
            del cwd, timeout_seconds
            self.commands.append(command)
            return CommandResult(9, "sensitive stderr is never exposed")
        return super().run(command, cwd=cwd, timeout_seconds=timeout_seconds)


def test_one_second_chinese_subtitle_burn_is_a_hard_local_smoke_test(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )
    system = _BurnFailureSystemProbe()

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=system,
        )

    burn_command = next(command for command in system.commands if "-vf" in command)
    reasons = {issue.diagnostics.get("reason_code") for issue in report.issues}

    assert report.ready is False
    assert reasons == {"ffmpeg.smoke_test_failed", "font.burn_test_failed"}
    assert burn_command[burn_command.index("-t") + 1] == "1"
    assert "Noto Sans CJK SC" in burn_command[burn_command.index("-vf") + 1]
    assert burn_command[burn_command.index("-c:v") + 1] == "libx264"
    assert burn_command[burn_command.index("-c:a") + 1] == "aac"
    assert report.environment.font_available is False
    assert report.environment_fact is not None
    assert "sensitive stderr" not in repr(report)
    assert all(adapter.business_calls == 0 for adapter in adapters)


def test_media_smoke_temp_directory_failure_is_aggregated_before_later_checks(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "private-course-name.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "private-workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    def fail_temporary_directory(*_args, **_kwargs):
        raise OSError("temporary-directory-secret-canary")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        monkeypatch.setattr(
            tempfile,
            "TemporaryDirectory",
            fail_temporary_directory,
        )
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_SystemProbe(),
        )
        temporary_entries = run_workspace.temporary.inspect_tree()

    assert report.ready is False
    assert {
        (issue.error_code, issue.diagnostics.get("reason_code"))
        for issue in report.issues
    } == {
        (
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            "ffmpeg.smoke_test_failed",
        ),
        (
            ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
            "font.burn_test_failed",
        ),
    }
    assert any(
        entry.relative_path.startswith("readiness-atomic-")
        for entry in temporary_entries
    )
    assert all(adapter.readiness_calls == 1 for adapter in adapters)
    assert all(adapter.business_calls == 0 for adapter in adapters)
    assert "temporary-directory-secret-canary" not in repr(report)


def test_media_smoke_cleanup_failure_is_reported_without_erasing_capability(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )
    real_temporary_directory = tempfile.TemporaryDirectory

    class CleanupFailureDirectory:
        def __init__(self, *args, **kwargs):
            self._inner = real_temporary_directory(*args, **kwargs)
            self.name = self._inner.name

        def cleanup(self):
            self._inner.cleanup()
            raise OSError("cleanup-secret-canary")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        monkeypatch.setattr(
            tempfile,
            "TemporaryDirectory",
            CleanupFailureDirectory,
        )
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_SystemProbe(),
        )

    assert report.ready is False
    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
    )
    assert report.issues[0].diagnostics == {
        "component": "atomic_publication",
        "operation": "filesystem.cleanup_probe",
        "reason_code": "filesystem.cleanup_failed",
    }
    assert report.environment.font_available is True
    assert all(adapter.readiness_calls == 1 for adapter in adapters)
    assert all(adapter.business_calls == 0 for adapter in adapters)
    assert "cleanup-secret-canary" not in repr(report)


class _NoTLSSystemProbe(_SystemProbe):
    def tls_observation(self) -> TLSObservation:
        return TLSObservation(verification_enabled=False, ca_count=0)


def test_tls_verification_and_nonempty_ca_store_are_both_required(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_NoTLSSystemProbe(),
        )

    tls_issues = tuple(
        issue
        for issue in report.issues
        if issue.error_code is ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE
    )

    assert report.ready is False
    assert tuple(issue.diagnostics["reason_code"] for issue in tls_issues) == (
        "tls.verification_unavailable",
        "tls.ca_store_empty",
    )
    assert report.environment_fact is not None
    assert all(adapter.business_calls == 0 for adapter in adapters)


def test_workspace_atomic_persistence_failure_is_safe_and_does_not_stop_adapters(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "private-source.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "private-workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    def fail_atomic_write(
        _location: ManagedPathCapability,
        _contents: bytes,
    ) -> int:
        raise WorkspaceFailure(
            ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
            {
                "component": "workspace",
                "operation": "workspace.access",
                "reason_code": "workspace.io_failed",
            },
        )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        monkeypatch.setattr(
            ManagedPathCapability,
            "publish_bytes_atomically",
            fail_atomic_write,
        )
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_SystemProbe(),
        )

    assert report.ready is False
    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
    )
    assert report.issues[0].diagnostics == {
        "component": "atomic_publication",
        "operation": "filesystem.atomic_replace_probe",
        "reason_code": "filesystem.atomic_replace_failed",
    }
    assert report.environment_fact is not None
    assert all(adapter.readiness_calls == 1 for adapter in adapters)
    assert all(adapter.business_calls == 0 for adapter in adapters)
    for forbidden in ("private-source.mp4", "private-workspace", "workspace.io_failed"):
        assert forbidden not in repr(report)


class _PythonVersionSystemProbe(_SystemProbe):
    def __init__(self, version: tuple[int, int, int]) -> None:
        super().__init__()
        self._version = version

    def python_version(self) -> tuple[int, int, int]:
        return self._version


@pytest.mark.parametrize(
    ("version", "reason_code"),
    [
        ((3, 12, 2), "python.version_too_old"),
        ((3, 13, 0), "python.version_too_new"),
    ],
)
def test_python_certification_range_has_both_hard_boundaries(
    tmp_path,
    version,
    reason_code,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_PythonVersionSystemProbe(version),
        )

    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
    )
    assert report.issues[0].diagnostics == {
        "component": "python",
        "detected_version": ".".join(str(item) for item in version),
        "operation": "python.inspect",
        "reason_code": reason_code,
        "required_version": ">=3.12.3,<3.13",
    }
    assert report.environment_fact is not None


class _FFmpegVersionSystemProbe(_SystemProbe):
    def __init__(self, version: str) -> None:
        super().__init__()
        self._version = version

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        if command[1:] == ("-version",):
            del cwd, timeout_seconds
            self.commands.append(command)
            return CommandResult(0, f"{command[0]} version {self._version}\n")
        return super().run(command, cwd=cwd, timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("version", ["6.0.9", "7.0.0"])
def test_ffmpeg_and_ffprobe_must_both_be_in_the_certified_major_range(
    tmp_path,
    version,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_FFmpegVersionSystemProbe(version),
        )

    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
    )
    assert all(
        issue.diagnostics["reason_code"] == "tool.version_unsupported"
        for issue in report.issues
    )
    assert report.environment.ffmpeg_version == version
    assert report.environment.ffprobe_version == version
    assert report.environment_fact is not None


class _InvalidProbeJsonSystemProbe(_SystemProbe):
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        if command[0] == "ffprobe" and command[1:] != ("-version",):
            del cwd, timeout_seconds
            self.commands.append(command)
            return CommandResult(
                0,
                '{"format":{"format_name":"mov,mp4","duration":"1.000"},'
                '"streams":[{"codec_type":"video"}]}',
            )
        return super().run(command, cwd=cwd, timeout_seconds=timeout_seconds)


def test_ffprobe_must_read_json_container_video_audio_and_duration(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    adapters = (
        _TranscriptionAdapter(),
        _TextCapability("a" * 64),
        _TextCapability("a" * 64),
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        report = Readiness.check(
            _request(run_workspace, adapters),
            _system_probe=_InvalidProbeJsonSystemProbe(),
        )

    assert tuple(issue.error_code for issue in report.issues) == (
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
    )
    assert {issue.diagnostics["reason_code"] for issue in report.issues} == {
        "ffmpeg.smoke_test_failed",
        "ffprobe.probe_failed",
        "font.burn_test_failed",
    }
    assert report.environment.font_available is False
