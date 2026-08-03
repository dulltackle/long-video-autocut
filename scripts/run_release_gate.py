#!/usr/bin/env python3
"""准备并执行绑定不可变候选的发布前真实门禁。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any, NoReturn
from zipfile import BadZipFile, ZipFile


_REQUEST_SCHEMA = "release_gate_request.v1"
_PLAN_SCHEMA = "release_gate_plan.v1"
_CREDENTIAL_DESCRIPTOR_FD = "RELEASE_GATE_SYSTEMD_CREDENTIAL_FD"
_SYSTEMD_HOST_NETNS = "RELEASE_GATE_SYSTEMD_HOST_NETNS"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PACKAGE_NAME = re.compile(
    r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?"
)
_SAFE_WHEEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\.whl")
_FFMPEG_VERSION = re.compile(
    r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.[0-9]+){0,2}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.+~-]*)?"
)
_DETECTED_VERSION = re.compile(
    r"v?[0-9]+(?:\.[0-9]+){0,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_SNAPSHOT = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID = re.compile(
    r"run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_NETWORK_NAMESPACE = re.compile(r"net:\[[0-9]+\]")
_AUTOMATION_RUN_URL = re.compile(
    r"https://github\.com/dulltackle/long-video-autocut/"
    r"actions/runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?"
)
_CREDENTIAL_SHAPED_TEXT = re.compile(
    r"(?i)(?:sk-|gh[opsu]_|github_pat_|xox[baprs]-|AKIA|ASIA|AIza)"
)
_SYSTEMD_RELEASE_UNIT = re.compile(
    r"video-auto-editor-release-[a-z0-9][a-z0-9-]{0,63}-(cold|rerun)\.service"
)
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z"
)
_MAX_JSON_BYTES = 32 * 1024 * 1024
_REQUIRED_CAPABILITIES = (
    "transcription",
    "topic_review",
    "subtitle_optimization",
)
_REQUIRED_WARM_CACHE_HITS = (
    "transcript",
    "topic_review",
    "subtitle_optimization",
)
_MANUAL_REVIEW_CHECKS = (
    "topic_complete",
    "boundaries_natural",
    "audio_video_normal",
    "subtitles_faithful_readable",
    "title_summary_grounded",
    "excluded_content_absent",
)
_INDEPENDENT_VALIDATION_CHECKS = frozenset(
    {
        "digests",
        "exact_file_set",
        "faithful_transcript",
        "mp4",
        "path_safety",
        "references",
        "schema",
    }
)
_RELEASE_TOOLS = (
    "install-production.sh",
    "run_keyless_gate_network.sh",
    "run_release_gate.py",
    "systemd_credential_bridge.py",
    "validate_installed_delivery.py",
    "validate_release_evidence.py",
)
_CERTIFIED_PROVIDER_CONFIGURATION = {
    "transcription_provider": "stepaudio",
    "transcription_model": "stepaudio-2.5-asr",
    "transcription_endpoint": "https://api.stepfun.com/v1/audio/asr/sse",
    "text_model_provider": "stepfun",
    "text_model_endpoint": "https://api.stepfun.com/v1",
    "credential_environment": "STEPFUN_API_KEY",
    "topic_review_model": "step-2-mini",
    "subtitle_optimization_model": "step-2-mini",
}
_CERTIFIED_ENDPOINT_ORIGIN = "https://api.stepfun.com"
_PROVIDER_DATA_CATEGORIES = {
    "transcription": ["audio_shard"],
    "topic_review": [
        "business_constraints",
        "candidate_transcript",
        "course_context",
    ],
    "subtitle_optimization": [
        "fixed_instructions",
        "subtitle_window",
    ],
}
_CERTIFIED_SNAPSHOT_PACKAGES = frozenset(
    {
        "ca-certificates",
        "ffmpeg",
        "fontconfig",
        "fonts-noto-cjk",
        "python3.12",
        "python3.12-venv",
    }
)
_CERTIFIED_PYTHON_MINIMUM = (3, 12, 3)
_CERTIFIED_PYTHON_MAXIMUM = (3, 13, 0)


class ReleaseGateFailure(RuntimeError):
    """真实门禁输入或状态不满足失败关闭契约。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason_code: str) -> NoReturn:
    raise ReleaseGateFailure(reason_code)


def _reject_constant(value: str) -> NoReturn:
    del value
    _fail("json.non_finite")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("json.duplicate_field")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    file_path = _regular_file(path, "json.input_invalid")
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(file_path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_JSON_BYTES
        ):
            _fail("json.input_invalid")
        payload = bytearray()
        while len(payload) <= _MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, _MAX_JSON_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > _MAX_JSON_BYTES:
            _fail("json.input_invalid")
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except ReleaseGateFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("json.input_invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _closed_object(
    value: Any,
    fields: frozenset[str],
    reason_code: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(reason_code)
    actual = frozenset(value)
    if actual != fields:
        _fail(reason_code)
    return value


def _string(value: Any, reason_code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(reason_code)
    return value


def _automation_run_url(value: Any, reason_code: str) -> str:
    run_url = _string(value, reason_code)
    if _AUTOMATION_RUN_URL.fullmatch(run_url) is None:
        _fail(reason_code)
    return run_url


def _public_chinese_summary(value: Any, reason_code: str) -> str:
    summary = _string(value, reason_code)
    if (
        len(summary) > 500
        or re.search(r"[\u3400-\u9fff]", summary) is None
        or "/" in summary
        or "\\" in summary
        or any(ord(character) < 0x20 for character in summary)
        or _CREDENTIAL_SHAPED_TEXT.search(summary) is not None
    ):
        _fail(reason_code)
    return summary


def _regular_file(value: object, reason_code: str) -> Path:
    try:
        raw = value if isinstance(value, Path) else Path(_string(value, reason_code))
        if not raw.is_absolute() or raw.resolve(strict=True) != raw:
            _fail(reason_code)
        metadata = raw.lstat()
    except ReleaseGateFailure:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail(reason_code)
    if not stat.S_ISREG(metadata.st_mode):
        _fail(reason_code)
    return raw


def _directory(value: object, reason_code: str) -> Path:
    try:
        raw = value if isinstance(value, Path) else Path(_string(value, reason_code))
        if not raw.is_absolute() or raw.resolve(strict=True) != raw:
            _fail(reason_code)
        metadata = raw.lstat()
    except ReleaseGateFailure:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail(reason_code)
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(reason_code)
    return raw


def _private_directory(value: object, reason_code: str) -> Path:
    raw = _directory(value, reason_code)
    try:
        metadata = raw.lstat()
    except OSError:
        _fail(reason_code)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        _fail(reason_code)
    return raw


def _private_regular_file(value: object, reason_code: str) -> Path:
    path = _regular_file(value, reason_code)
    _private_directory(path.parent, reason_code)
    try:
        if path.stat().st_mode & 0o077:
            _fail(reason_code)
    except OSError:
        _fail(reason_code)
    return path


def _private_json_record(value: object, reason_code: str) -> Path:
    path = _private_regular_file(value, reason_code)
    try:
        if path.stat().st_mode & 0o777 != 0o600:
            _fail(reason_code)
    except OSError:
        _fail(reason_code)
    return path


def _plan_json_record(value: object, reason_code: str) -> Path:
    path = _regular_file(value, reason_code)
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
    except OSError:
        _fail(reason_code)
    current_uid = os.geteuid()
    current_gid = os.getegid()
    private_draft = (
        metadata.st_uid == current_uid
        and parent.st_uid == current_uid
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and stat.S_IMODE(parent.st_mode) == 0o700
    )
    sealed_copy = (
        metadata.st_uid == 0
        and metadata.st_gid == current_gid
        and parent.st_uid == 0
        and parent.st_gid == current_gid
        and stat.S_IMODE(metadata.st_mode) == 0o440
        and stat.S_IMODE(parent.st_mode) == 0o710
    )
    if not stat.S_ISREG(metadata.st_mode) or not stat.S_ISDIR(parent.st_mode):
        _fail(reason_code)
    if not private_draft and not sealed_copy:
        _fail(reason_code)
    return path


def _trusted_release_tool(
    value: object,
    expected_name: str,
    reason_code: str,
) -> Path:
    path = _regular_file(value, reason_code)
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
    except OSError:
        _fail(reason_code)
    if (
        path.name != expected_name
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & 0o022
    ):
        _fail(reason_code)
    return path


def _host_package_map(value: Any, reason_code: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        _fail(reason_code)
    packages: dict[str, str] = {}
    for name in sorted(value):
        if not isinstance(name, str) or _PACKAGE_NAME.fullmatch(name) is None:
            _fail(reason_code)
        packages[name] = _string(value[name], reason_code)
    return packages


def _host_installation_manifest(
    value: Any,
    *,
    identity: Mapping[str, Any],
    snapshot_id: str,
    reason_code: str,
) -> dict[str, Any]:
    manifest = _closed_object(
        value,
        frozenset(
            {
                "schema_version",
                "application",
                "apt_snapshot_id",
                "environment",
                "installation_prefix",
                "platform",
                "python",
                "runtime_lock",
                "snapshot_packages",
                "system_packages",
                "wheelhouse",
            }
        ),
        reason_code,
    )
    application = _closed_object(
        manifest["application"],
        frozenset({"name", "version", "wheel"}),
        reason_code,
    )
    wheel = _closed_object(
        application["wheel"],
        frozenset({"filename", "sha256"}),
        reason_code,
    )
    environment = _closed_object(
        manifest["environment"],
        frozenset(
            {"ffmpeg_version", "ffprobe_version", "font_family", "font_file"}
        ),
        reason_code,
    )
    platform = _closed_object(
        manifest["platform"],
        frozenset(
            {"architecture", "operating_system", "operating_system_version"}
        ),
        reason_code,
    )
    python = _closed_object(
        manifest["python"],
        frozenset({"implementation", "version"}),
        reason_code,
    )
    runtime_lock = _closed_object(
        manifest["runtime_lock"],
        frozenset({"filename", "sha256"}),
        reason_code,
    )
    python_version = _string(python["version"], reason_code)
    if _PYTHON_VERSION.fullmatch(python_version) is None:
        _fail(reason_code)
    parsed_python = tuple(int(part) for part in python_version.split("."))
    ffmpeg_version = _string(environment["ffmpeg_version"], reason_code)
    ffprobe_version = _string(environment["ffprobe_version"], reason_code)
    ffmpeg_match = _FFMPEG_VERSION.fullmatch(ffmpeg_version)
    font_file = Path(_string(environment["font_file"], reason_code))
    prefix_value = _string(manifest["installation_prefix"], reason_code)
    snapshot_packages = _host_package_map(
        manifest["snapshot_packages"], reason_code
    )
    system_packages = _host_package_map(
        manifest["system_packages"], reason_code
    )
    wheelhouse_value = manifest["wheelhouse"]
    if not isinstance(wheelhouse_value, list):
        _fail(reason_code)
    wheelhouse: list[dict[str, str]] = []
    for item in wheelhouse_value:
        artifact = _closed_object(
            item,
            frozenset({"filename", "sha256"}),
            reason_code,
        )
        filename = _string(artifact["filename"], reason_code)
        digest = _string(artifact["sha256"], reason_code)
        if (
            _SAFE_WHEEL_FILENAME.fullmatch(filename) is None
            or _SHA256.fullmatch(digest) is None
        ):
            _fail(reason_code)
        wheelhouse.append({"filename": filename, "sha256": digest})
    filenames = [artifact["filename"] for artifact in wheelhouse]
    if (
        manifest["schema_version"] != "production-installation-manifest.v1"
        or application["name"] != "video-auto-editor"
        or application["version"] != identity["version"]
        or wheel != {
            "filename": identity["wheel"]["filename"],
            "sha256": identity["wheel"]["sha256"],
        }
        or manifest["apt_snapshot_id"] != snapshot_id
        or not Path(prefix_value).is_absolute()
        or platform
        != {
            "architecture": "amd64",
            "operating_system": "ubuntu",
            "operating_system_version": "24.04",
        }
        or python["implementation"] != "CPython"
        or not (
            _CERTIFIED_PYTHON_MINIMUM
            <= parsed_python
            < _CERTIFIED_PYTHON_MAXIMUM
        )
        or ffmpeg_match is None
        or ffmpeg_version != ffprobe_version
        or not (
            (6, 1)
            <= (
                int(ffmpeg_match.group("major")),
                int(ffmpeg_match.group("minor")),
            )
            < (7, 0)
        )
        or environment["font_family"] != "Noto Sans CJK SC"
        or not font_file.is_absolute()
        or runtime_lock
        != {
            "filename": identity["runtime_lock"]["filename"],
            "sha256": identity["runtime_lock"]["sha256"],
        }
        or set(snapshot_packages) != _CERTIFIED_SNAPSHOT_PACKAGES
        or any(
            system_packages.get(name) != version
            for name, version in snapshot_packages.items()
        )
        or filenames != sorted(filenames)
        or len(filenames) != len(set(filenames))
    ):
        _fail(reason_code)
    return {
        **dict(manifest),
        "snapshot_packages": snapshot_packages,
        "system_packages": system_packages,
        "wheelhouse": wheelhouse,
    }


def _sha256_file(path: Path) -> str:
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            _fail("artifact.read_failed")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except ReleaseGateFailure:
        raise
    except OSError:
        _fail("artifact.read_failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _file_fact(path: Path) -> dict[str, str]:
    return {
        "filename": path.name,
        "path": str(path),
        "sha256": _sha256_file(path),
    }


def _wheel_version(wheel: Path) -> str:
    try:
        with ZipFile(wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.count("/") == 1
                and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                _fail("candidate.wheel_invalid")
            raw_metadata = archive.read(metadata_names[0]).decode("utf-8")
        metadata = Parser().parsestr(raw_metadata)
    except ReleaseGateFailure:
        raise
    except (OSError, UnicodeError, BadZipFile, KeyError):
        _fail("candidate.wheel_invalid")
    if metadata.get("Name") != "video-auto-editor":
        _fail("candidate.wheel_invalid")
    version = metadata.get("Version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        _fail("candidate.wheel_invalid")
    return version


def _artifact_reference(value: object, reason_code: str) -> dict[str, str]:
    return _file_fact(_regular_file(value, reason_code))


def _release_tool_digests(value: Any, reason_code: str) -> dict[str, str]:
    tools = _closed_object(value, frozenset(_RELEASE_TOOLS), reason_code)
    if any(
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        for digest in tools.values()
    ):
        _fail(reason_code)
    return {name: tools[name] for name in _RELEASE_TOOLS}


def _candidate_identity(
    candidate: Mapping[str, Any],
    *,
    release_version: str,
    snapshot_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    commit_sha = _string(candidate["commit_sha"], "candidate.commit_invalid")
    if _COMMIT_SHA.fullmatch(commit_sha) is None:
        _fail("candidate.commit_invalid")
    wheel = _regular_file(candidate["wheel"], "candidate.wheel_invalid")
    if wheel.suffix != ".whl" or _wheel_version(wheel) != release_version:
        _fail("candidate.wheel_invalid")
    build_lock = _regular_file(
        candidate["build_lock"],
        "candidate.build_lock_invalid",
    )
    runtime_lock = _regular_file(
        candidate["runtime_lock"],
        "candidate.runtime_lock_invalid",
    )
    if build_lock.name != "requirements-build.lock":
        _fail("candidate.build_lock_invalid")
    if runtime_lock.name != "requirements-runtime.lock":
        _fail("candidate.runtime_lock_invalid")

    identity = {
        "commit_sha": commit_sha,
        "version": release_version,
        "wheel": _file_fact(wheel),
        "build_lock": _file_fact(build_lock),
        "runtime_lock": _file_fact(runtime_lock),
    }
    installation = _installation_facts(
        candidate,
        identity=identity,
        snapshot_id=snapshot_id,
    )
    automation = _automation_facts(candidate, identity=identity)
    return identity, installation, automation


def _verify_host_installation_layout(
    *,
    prefix: Path,
    version: str,
    manifest_path: Path,
    ready_path: Path,
    console_path: Path,
    reason_code: str,
) -> None:
    version_directory = prefix / "versions" / version
    expected_console = version_directory / "venv" / "bin" / "video-auto-editor"
    if (
        manifest_path != version_directory / "installation-manifest.json"
        or ready_path != version_directory / "READY"
        or console_path != expected_console
    ):
        _fail(reason_code)
    paths = (
        (prefix, True),
        (prefix / "versions", True),
        (version_directory, True),
        (version_directory / "venv", True),
        (version_directory / "venv" / "bin", True),
        (manifest_path, False),
        (ready_path, False),
        (console_path, False),
    )
    try:
        for path, is_directory in paths:
            metadata = path.lstat()
            expected_type = (
                stat.S_ISDIR(metadata.st_mode)
                if is_directory
                else stat.S_ISREG(metadata.st_mode)
            )
            if (
                not expected_type
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                _fail(reason_code)
    except OSError:
        _fail(reason_code)
    if not os.access(console_path, os.X_OK):
        _fail(reason_code)


def _installation_facts(
    candidate: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    snapshot_id: str,
) -> dict[str, Any]:
    manifest_path = _regular_file(
        candidate["installation_manifest"],
        "installation.manifest_invalid",
    )
    ready_path = _regular_file(
        candidate["installation_ready"],
        "installation.ready_invalid",
    )
    manifest = _load_json(manifest_path)
    ready = _load_json(ready_path)
    if not isinstance(manifest, dict) or not isinstance(ready, dict):
        _fail("installation.binding_invalid")
    normalized_manifest = _host_installation_manifest(
        manifest,
        identity=identity,
        snapshot_id=snapshot_id,
        reason_code="installation.binding_invalid",
    )
    try:
        prefix = _directory(
            normalized_manifest["installation_prefix"],
            "installation.binding_invalid",
        )
        version_directory = prefix / "versions" / str(identity["version"])
        expected_console = version_directory / "venv" / "bin" / "video-auto-editor"
        _verify_host_installation_layout(
            prefix=prefix,
            version=str(identity["version"]),
            manifest_path=manifest_path,
            ready_path=ready_path,
            console_path=expected_console,
            reason_code="installation.binding_invalid",
        )
        manifest_wheel = normalized_manifest["application"]["wheel"]
        manifest_lock = normalized_manifest["runtime_lock"]
        bound = (
            normalized_manifest["schema_version"]
            == "production-installation-manifest.v1"
            and normalized_manifest["application"]["name"]
            == "video-auto-editor"
            and normalized_manifest["application"]["version"]
            == identity["version"]
            and manifest_wheel["filename"] == identity["wheel"]["filename"]
            and manifest_wheel["sha256"] == identity["wheel"]["sha256"]
            and manifest_lock["filename"]
            == identity["runtime_lock"]["filename"]
            and manifest_lock["sha256"]
            == identity["runtime_lock"]["sha256"]
            and normalized_manifest["apt_snapshot_id"] == snapshot_id
            and ready["schema_version"] == "production-installation-ready.v1"
            and ready["installation_manifest_sha256"]
            == _sha256_file(manifest_path)
            and manifest_path
            == version_directory / "installation-manifest.json"
            and ready_path == version_directory / "READY"
            and _regular_file(
                expected_console,
                "installation.binding_invalid",
            )
            == expected_console
            and os.access(expected_console, os.X_OK)
        )
    except (KeyError, TypeError):
        bound = False
    if not bound:
        _fail("installation.binding_invalid")
    return {
        "manifest": _file_fact(manifest_path),
        "ready": _file_fact(ready_path),
        "console": _file_fact(expected_console),
    }


def _automation_facts(
    candidate: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    references: dict[str, Any] = {}
    for field, schema in (
        ("keyless_gate_evidence", "keyless_gate_evidence.v1"),
        (
            "installed_acceptance_evidence",
            "installed_acceptance_evidence.v1",
        ),
    ):
        path = _regular_file(candidate[field], f"automation.{field}_invalid")
        evidence = _load_json(path)
        if not isinstance(evidence, dict):
            _fail(f"automation.{field}_invalid")
        evidence_candidate = evidence.get("candidate")
        if not isinstance(evidence_candidate, dict):
            _fail(f"automation.{field}_invalid")
        if (
            evidence.get("schema_version") != schema
            or evidence.get("success") is not True
            or evidence_candidate.get("commit_sha") != identity["commit_sha"]
            or evidence_candidate.get("wheel_filename")
            != identity["wheel"]["filename"]
            or evidence_candidate.get("wheel_sha256")
            != identity["wheel"]["sha256"]
        ):
            _fail(f"automation.{field}_invalid")
        references[field] = _file_fact(path)
    installed = _load_json(Path(references["installed_acceptance_evidence"]["path"]))
    keyless = _load_json(Path(references["keyless_gate_evidence"]["path"]))
    installed_candidate = installed["candidate"]
    if (
        installed_candidate.get("runtime_lock_filename")
        != identity["runtime_lock"]["filename"]
        or installed_candidate.get("runtime_lock_sha256")
        != identity["runtime_lock"]["sha256"]
    ):
        _fail("automation.installed_acceptance_evidence_invalid")
    references["release_tools"] = _release_tool_digests(
        keyless.get("release_tools") if isinstance(keyless, dict) else None,
        "automation.release_tools_invalid",
    )
    return references


def _input_fact(value: object, reason_code: str) -> dict[str, str]:
    return _file_fact(_private_regular_file(value, reason_code))


def _course_context_observation(path: Path) -> tuple[str, dict[str, Any]]:
    values = _load_json(path)
    if not isinstance(values, dict):
        _fail("inputs.course_context_invalid")
    allowed = {
        "schema_version",
        "course_topic",
        "attribution",
        "priority_topics",
        "excluded_content",
    }
    if (
        not {"schema_version", "course_topic"}.issubset(values)
        or not set(values).issubset(allowed)
        or values["schema_version"] != "course_context.v1"
        or not isinstance(values["course_topic"], str)
        or not values["course_topic"]
        or (
            values.get("attribution") is not None
            and not isinstance(values["attribution"], str)
        )
    ):
        _fail("inputs.course_context_invalid")
    for field in ("priority_topics", "excluded_content"):
        entries = values.get(field, [])
        if not isinstance(entries, list) or not all(
            isinstance(entry, str) and entry for entry in entries
        ):
            _fail("inputs.course_context_invalid")
    normalized = {
        "schema_version": values["schema_version"],
        "course_topic": values["course_topic"],
        "attribution": values.get("attribution"),
        "priority_topics": list(values.get("priority_topics", ())),
        "excluded_content": list(values.get("excluded_content", ())),
    }
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return (
        "sha256:" + hashlib.sha256(canonical).hexdigest(),
        {
            "provided": True,
            "attribution_provided": normalized["attribution"] is not None,
            "priority_topic_count": len(normalized["priority_topics"]),
            "excluded_content_count": len(normalized["excluded_content"]),
        },
    )


def _certified_configuration(
    path: Path,
    reason_code: str,
) -> dict[str, str]:
    values = _load_json(path)
    if not isinstance(values, dict) or values.get("schema_version") != (
        "configuration.v1"
    ):
        _fail(reason_code)
    transcription = values.get("transcription_provider_config", {})
    text_model = values.get("text_model_provider_config", {})
    topic_review = values.get("topic_review", {})
    subtitle_optimization = values.get("subtitle_optimization", {})
    if not all(
        isinstance(item, dict)
        for item in (
            transcription,
            text_model,
            topic_review,
            subtitle_optimization,
        )
    ):
        _fail(reason_code)
    observed = {
        "transcription_provider": values.get(
            "transcription_provider", "stepaudio"
        ),
        "transcription_model": transcription.get(
            "model", "stepaudio-2.5-asr"
        ),
        "transcription_endpoint": transcription.get(
            "endpoint", "https://api.stepfun.com/v1/audio/asr/sse"
        ),
        "text_model_provider": values.get("text_model_provider", "stepfun"),
        "text_model_endpoint": text_model.get(
            "endpoint", "https://api.stepfun.com/v1"
        ),
        "credential_environment": transcription.get(
            "key_environment_variable", "STEPFUN_API_KEY"
        ),
        "text_credential_environment": text_model.get(
            "key_environment_variable", "STEPFUN_API_KEY"
        ),
        "topic_review_model": topic_review.get("model", "step-2-mini"),
        "subtitle_optimization_model": subtitle_optimization.get(
            "model", "step-2-mini"
        ),
    }
    expected = {
        **_CERTIFIED_PROVIDER_CONFIGURATION,
        "text_credential_environment": "STEPFUN_API_KEY",
    }
    if observed != expected:
        _fail(reason_code)
    return dict(_CERTIFIED_PROVIDER_CONFIGURATION)


def _verify_file_fact(value: Any, reason_code: str) -> Path:
    fact = _closed_object(
        value,
        frozenset({"filename", "path", "sha256"}),
        reason_code,
    )
    path = _regular_file(fact["path"], reason_code)
    if (
        fact["filename"] != path.name
        or not isinstance(fact["sha256"], str)
        or _SHA256.fullmatch(fact["sha256"]) is None
        or _sha256_file(path) != fact["sha256"]
    ):
        _fail(reason_code)
    return path


def verify_plan(
    plan_path: Path,
    *,
    require_empty_workspace: bool = True,
) -> dict[str, Any]:
    plan_path = _plan_json_record(plan_path, "plan.input_invalid")
    plan = _closed_object(
        _load_json(plan_path),
        frozenset(
            {
                "schema_version",
                "candidate",
                "certified_host",
                "automation",
                "inputs",
                "execution",
                "release",
            }
        ),
        "plan.schema_invalid",
    )
    if plan["schema_version"] != _PLAN_SCHEMA:
        _fail("plan.schema_invalid")
    candidate = _closed_object(
        plan["candidate"],
        frozenset(
            {
                "commit_sha",
                "version",
                "wheel",
                "build_lock",
                "runtime_lock",
            }
        ),
        "plan.schema_invalid",
    )
    commit_sha = candidate["commit_sha"]
    version = candidate["version"]
    if (
        not isinstance(commit_sha, str)
        or _COMMIT_SHA.fullmatch(commit_sha) is None
        or not isinstance(version, str)
        or _VERSION.fullmatch(version) is None
    ):
        _fail("plan.schema_invalid")
    wheel = _verify_file_fact(candidate["wheel"], "candidate.drift")
    build_lock = _verify_file_fact(candidate["build_lock"], "candidate.drift")
    runtime_lock = _verify_file_fact(
        candidate["runtime_lock"],
        "candidate.drift",
    )
    if (
        _wheel_version(wheel) != version
        or build_lock.name != "requirements-build.lock"
        or runtime_lock.name != "requirements-runtime.lock"
    ):
        _fail("candidate.drift")

    host = _closed_object(
        plan["certified_host"],
        frozenset({"attestation_id", "apt_snapshot_id", "installation"}),
        "plan.schema_invalid",
    )
    if (
        not isinstance(host["attestation_id"], str)
        or _STABLE_ID.fullmatch(host["attestation_id"]) is None
        or not isinstance(host["apt_snapshot_id"], str)
        or _SNAPSHOT.fullmatch(host["apt_snapshot_id"]) is None
    ):
        _fail("plan.schema_invalid")
    installation = _closed_object(
        host["installation"],
        frozenset({"manifest", "ready", "console"}),
        "plan.schema_invalid",
    )
    manifest_path = _verify_file_fact(
        installation["manifest"],
        "installation.drift",
    )
    ready_path = _verify_file_fact(
        installation["ready"],
        "installation.drift",
    )
    console_path = _verify_file_fact(
        installation["console"],
        "installation.drift",
    )
    manifest = _load_json(manifest_path)
    ready = _load_json(ready_path)
    normalized_manifest = _host_installation_manifest(
        manifest,
        identity=candidate,
        snapshot_id=host["apt_snapshot_id"],
        reason_code="installation.drift",
    )
    try:
        installation_prefix = _directory(
            normalized_manifest["installation_prefix"],
            "installation.drift",
        )
        version_directory = installation_prefix / "versions" / version
        _verify_host_installation_layout(
            prefix=installation_prefix,
            version=version,
            manifest_path=manifest_path,
            ready_path=ready_path,
            console_path=console_path,
            reason_code="installation.drift",
        )
        installation_matches = (
            normalized_manifest["schema_version"]
            == "production-installation-manifest.v1"
            and normalized_manifest["application"]["name"]
            == "video-auto-editor"
            and normalized_manifest["application"]["version"] == version
            and normalized_manifest["application"]["wheel"]["filename"]
            == wheel.name
            and normalized_manifest["application"]["wheel"]["sha256"]
            == candidate["wheel"]["sha256"]
            and normalized_manifest["runtime_lock"]["filename"]
            == runtime_lock.name
            and normalized_manifest["runtime_lock"]["sha256"]
            == candidate["runtime_lock"]["sha256"]
            and normalized_manifest["apt_snapshot_id"]
            == host["apt_snapshot_id"]
            and ready["schema_version"] == "production-installation-ready.v1"
            and ready["installation_manifest_sha256"]
            == candidate_sha256(manifest_path)
            and manifest_path
            == version_directory / "installation-manifest.json"
            and ready_path == version_directory / "READY"
            and console_path
            == version_directory / "venv" / "bin" / "video-auto-editor"
            and os.access(console_path, os.X_OK)
        )
    except (KeyError, TypeError):
        installation_matches = False
    if not installation_matches:
        _fail("installation.drift")

    automation = _closed_object(
        plan["automation"],
        frozenset(
            {
                "run_url",
                "keyless_gate_evidence",
                "installed_acceptance_evidence",
                "release_tools",
            }
        ),
        "plan.schema_invalid",
    )
    _automation_run_url(automation["run_url"], "automation.drift")
    release_tools = _release_tool_digests(
        automation["release_tools"],
        "automation.drift",
    )
    for field, schema in (
        ("keyless_gate_evidence", "keyless_gate_evidence.v1"),
        (
            "installed_acceptance_evidence",
            "installed_acceptance_evidence.v1",
        ),
    ):
        evidence_path = _verify_file_fact(
            automation[field],
            "automation.drift",
        )
        evidence = _load_json(evidence_path)
        try:
            evidence_matches = (
                evidence["schema_version"] == schema
                and evidence["success"] is True
                and evidence["candidate"]["commit_sha"] == commit_sha
                and evidence["candidate"]["wheel_filename"] == wheel.name
                and evidence["candidate"]["wheel_sha256"]
                == candidate["wheel"]["sha256"]
            )
            if field == "installed_acceptance_evidence":
                evidence_matches = evidence_matches and (
                    evidence["candidate"]["runtime_lock_filename"]
                    == runtime_lock.name
                    and evidence["candidate"]["runtime_lock_sha256"]
                    == candidate["runtime_lock"]["sha256"]
                    and evidence["candidate"]["apt_snapshot_id"]
                    == host["apt_snapshot_id"]
                )
        except (KeyError, TypeError):
            evidence_matches = False
        if not evidence_matches:
            _fail("automation.drift")
        if field == "keyless_gate_evidence":
            try:
                evidence_tools = _release_tool_digests(
                    evidence["release_tools"],
                    "automation.drift",
                )
            except (KeyError, TypeError):
                _fail("automation.drift")
            if evidence_tools != release_tools:
                _fail("automation.drift")

    inputs = _closed_object(
        plan["inputs"],
        frozenset(
            {
                "source",
                "configuration",
                "course_context",
                "expected_transcript",
            }
        ),
        "plan.schema_invalid",
    )
    source = _closed_object(
        inputs["source"],
        frozenset(
            {
                "filename",
                "path",
                "sha256",
                "asset_id",
                "version",
                "language",
                "content_summary",
                "byte_length",
                "duration_ms",
            }
        ),
        "plan.schema_invalid",
    )
    source_path = _private_regular_file(source["path"], "inputs.drift")
    if (
        source["filename"] != source_path.name
        or source["sha256"] != _sha256_file(source_path)
        or source["byte_length"] != source_path.stat().st_size
        or not isinstance(source["duration_ms"], int)
        or isinstance(source["duration_ms"], bool)
        or source["duration_ms"] <= 0
        or not isinstance(source["asset_id"], str)
        or _STABLE_ID.fullmatch(source["asset_id"]) is None
        or not isinstance(source["version"], str)
        or _STABLE_ID.fullmatch(source["version"]) is None
        or source["language"] != "zh-CN"
    ):
        _fail("inputs.drift")
    _public_chinese_summary(source["content_summary"], "inputs.drift")
    sidecar_paths: dict[str, Path] = {}
    for field in ("configuration", "course_context"):
        input_path = _verify_file_fact(inputs[field], "inputs.drift")
        sidecar_paths[field] = _private_regular_file(
            input_path,
            "inputs.drift",
        )
    expected_transcript_path = _verify_file_fact(
        inputs["expected_transcript"],
        "inputs.drift",
    )
    _private_regular_file(expected_transcript_path, "inputs.drift")
    if (
        sidecar_paths["configuration"] != source_path.with_suffix(".config.json")
        or sidecar_paths["course_context"]
        != source_path.with_suffix(".context.json")
    ):
        _fail("inputs.drift")
    _certified_configuration(
        sidecar_paths["configuration"],
        "inputs.configuration_provider_invalid",
    )

    execution = _closed_object(
        plan["execution"],
        frozenset(
            {
                "console",
                "independent_validator",
                "credential_bridge",
                "network_guard",
                "workspace_parent",
                "initial_workspace_state",
                "credential_source",
                "credential_id",
                "cold_then_overwrite",
            }
        ),
        "plan.schema_invalid",
    )
    console = _verify_file_fact(execution["console"], "execution.drift")
    gate = _trusted_release_tool(
        Path(__file__).resolve(),
        "run_release_gate.py",
        "execution.drift",
    )
    validator = _trusted_release_tool(
        _verify_file_fact(
            execution["independent_validator"],
            "execution.drift",
        ),
        "validate_installed_delivery.py",
        "execution.drift",
    )
    credential_bridge = _trusted_release_tool(
        _verify_file_fact(
            execution["credential_bridge"],
            "execution.drift",
        ),
        "systemd_credential_bridge.py",
        "execution.drift",
    )
    network_guard = _trusted_release_tool(
        _verify_file_fact(
            execution["network_guard"],
            "execution.drift",
        ),
        "run_keyless_gate_network.sh",
        "execution.drift",
    )
    workspace_parent = _private_directory(
        execution["workspace_parent"],
        "execution.drift",
    )
    if (
        not os.access(console, os.X_OK)
        or console != console_path
        or _sha256_file(gate) != release_tools["run_release_gate.py"]
        or _sha256_file(validator)
        != release_tools["validate_installed_delivery.py"]
        or _sha256_file(credential_bridge)
        != release_tools["systemd_credential_bridge.py"]
        or _sha256_file(network_guard)
        != release_tools["run_keyless_gate_network.sh"]
        or not os.access(network_guard, os.X_OK)
        or execution["initial_workspace_state"]
        != "new_with_empty_processing_cache"
        or execution["credential_source"] != "systemd_credentials"
        or execution["credential_id"] != "stepfun_api_key"
        or execution["cold_then_overwrite"] is not True
    ):
        _fail("execution.drift")
    if require_empty_workspace:
        try:
            if any(workspace_parent.iterdir()):
                _fail("execution.drift")
        except OSError:
            _fail("execution.drift")

    release = _closed_object(
        plan["release"],
        frozenset({"version", "tag"}),
        "plan.schema_invalid",
    )
    if release != {"version": version, "tag": f"v{version}"}:
        _fail("plan.schema_invalid")
    return plan


def candidate_sha256(path: Path) -> str:
    """返回门禁交叉绑定使用的普通文件 SHA-256。"""

    return _sha256_file(path)


def _write_new_json(
    destination: Path,
    value: Mapping[str, Any],
    *,
    reason_code: str = "plan.write_failed",
) -> None:
    parent_descriptor = -1
    descriptor = -1
    created = False
    try:
        if (
            not destination.is_absolute()
            or destination.resolve(strict=False) != destination
        ):
            _fail("plan.destination_invalid")
        parent = _private_directory(
            destination.parent,
            "plan.destination_invalid",
        )
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            parent_flags |= os.O_NOFOLLOW
        parent_descriptor = os.open(parent, parent_flags)
        descriptor = os.open(
            destination.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent_descriptor)
    except ReleaseGateFailure:
        raise
    except (OSError, TypeError, ValueError):
        if created:
            try:
                os.unlink(destination.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        _fail(reason_code)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def prepare_plan(request_path: Path, plan_path: Path) -> None:
    if os.environ.get("STEPFUN_API_KEY"):
        _fail("credential.unexpected_environment")
    request_path = _private_regular_file(request_path, "request.input_invalid")
    request = _closed_object(
        _load_json(request_path),
        frozenset(
            {
                "schema_version",
                "candidate",
                "certified_host",
                "automation",
                "inputs",
                "execution",
                "release",
            }
        ),
        "request.schema_invalid",
    )
    if request["schema_version"] != _REQUEST_SCHEMA:
        _fail("request.schema_invalid")
    candidate = _closed_object(
        request["candidate"],
        frozenset(
            {
                "commit_sha",
                "wheel",
                "build_lock",
                "runtime_lock",
                "installation_manifest",
                "installation_ready",
                "keyless_gate_evidence",
                "installed_acceptance_evidence",
            }
        ),
        "request.candidate_invalid",
    )
    host = _closed_object(
        request["certified_host"],
        frozenset({"attestation_id", "apt_snapshot_id"}),
        "request.certified_host_invalid",
    )
    host_id = _string(host["attestation_id"], "request.certified_host_invalid")
    snapshot_id = _string(
        host["apt_snapshot_id"],
        "request.certified_host_invalid",
    )
    if (
        _STABLE_ID.fullmatch(host_id) is None
        or _SNAPSHOT.fullmatch(snapshot_id) is None
    ):
        _fail("request.certified_host_invalid")
    release = _closed_object(
        request["release"],
        frozenset({"version", "tag"}),
        "request.release_invalid",
    )
    version = _string(release["version"], "request.release_invalid")
    tag = _string(release["tag"], "request.release_invalid")
    if _VERSION.fullmatch(version) is None or tag != f"v{version}":
        _fail("request.release_invalid")
    identity, installation, automation_evidence = _candidate_identity(
        candidate,
        release_version=version,
        snapshot_id=snapshot_id,
    )
    installed_evidence = _load_json(
        Path(automation_evidence["installed_acceptance_evidence"]["path"])
    )
    if installed_evidence["candidate"].get("apt_snapshot_id") != snapshot_id:
        _fail("automation.installed_acceptance_evidence_invalid")

    automation = _closed_object(
        request["automation"],
        frozenset({"run_url"}),
        "request.automation_invalid",
    )
    run_url = _automation_run_url(
        automation["run_url"],
        "request.automation_invalid",
    )

    inputs = _closed_object(
        request["inputs"],
        frozenset(
            {
                "source",
                "configuration",
                "course_context",
                "expected_transcript",
            }
        ),
        "request.inputs_invalid",
    )
    source = _closed_object(
        inputs["source"],
        frozenset(
            {
                "path",
                "asset_id",
                "version",
                "language",
                "content_summary",
                "duration_ms",
            }
        ),
        "request.source_invalid",
    )
    source_path = _private_regular_file(
        source["path"],
        "request.source_invalid",
    )
    source_asset_id = _string(source["asset_id"], "request.source_invalid")
    source_version = _string(source["version"], "request.source_invalid")
    source_language = _string(source["language"], "request.source_invalid")
    content_summary = _public_chinese_summary(
        source["content_summary"],
        "request.source_invalid",
    )
    duration_ms = source["duration_ms"]
    if (
        _STABLE_ID.fullmatch(source_asset_id) is None
        or _STABLE_ID.fullmatch(source_version) is None
        or source_language != "zh-CN"
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
    ):
        _fail("request.source_invalid")
    source_fact: dict[str, Any] = _file_fact(source_path)
    source_fact.update(
        {
            "asset_id": source_asset_id,
            "version": source_version,
            "language": source_language,
            "content_summary": content_summary,
            "byte_length": source_path.stat().st_size,
            "duration_ms": duration_ms,
        }
    )
    configuration_path = _private_regular_file(
        inputs["configuration"],
        "request.configuration_invalid",
    )
    course_context_path = _private_regular_file(
        inputs["course_context"],
        "request.course_context_invalid",
    )
    if (
        configuration_path != source_path.with_suffix(".config.json")
        or course_context_path != source_path.with_suffix(".context.json")
    ):
        _fail("request.inputs_not_source_sidecars")
    _certified_configuration(
        configuration_path,
        "request.configuration_provider_invalid",
    )

    execution = _closed_object(
        request["execution"],
        frozenset(
            {
                "console",
                "independent_validator",
                "credential_bridge",
                "network_guard",
                "workspace_parent",
            }
        ),
        "request.execution_invalid",
    )
    console = _regular_file(execution["console"], "request.console_invalid")
    if (
        not os.access(console, os.X_OK)
        or console != Path(installation["console"]["path"])
    ):
        _fail("request.console_invalid")
    gate = _trusted_release_tool(
        Path(__file__).resolve(),
        "run_release_gate.py",
        "request.release_tools_invalid",
    )
    validator = _trusted_release_tool(
        execution["independent_validator"],
        "validate_installed_delivery.py",
        "request.release_tools_invalid",
    )
    credential_bridge = _trusted_release_tool(
        execution["credential_bridge"],
        "systemd_credential_bridge.py",
        "request.release_tools_invalid",
    )
    network_guard = _trusted_release_tool(
        execution["network_guard"],
        "run_keyless_gate_network.sh",
        "request.release_tools_invalid",
    )
    release_tools = automation_evidence["release_tools"]
    if (
        _sha256_file(gate) != release_tools["run_release_gate.py"]
        or _sha256_file(validator)
        != release_tools["validate_installed_delivery.py"]
        or _sha256_file(credential_bridge)
        != release_tools["systemd_credential_bridge.py"]
        or _sha256_file(network_guard)
        != release_tools["run_keyless_gate_network.sh"]
        or not os.access(network_guard, os.X_OK)
    ):
        _fail("request.release_tools_invalid")
    workspace_parent = _private_directory(
        execution["workspace_parent"],
        "request.workspace_invalid",
    )
    try:
        if any(workspace_parent.iterdir()):
            _fail("request.workspace_invalid")
    except OSError:
        _fail("request.workspace_invalid")

    plan = {
        "schema_version": _PLAN_SCHEMA,
        "candidate": identity,
        "certified_host": {
            "attestation_id": host_id,
            "apt_snapshot_id": snapshot_id,
            "installation": installation,
        },
        "automation": {
            "run_url": run_url,
            **automation_evidence,
        },
        "inputs": {
            "source": source_fact,
            "configuration": _file_fact(configuration_path),
            "course_context": _file_fact(course_context_path),
            "expected_transcript": _input_fact(
                inputs["expected_transcript"],
                "request.expected_transcript_invalid",
            ),
        },
        "execution": {
            "console": _file_fact(console),
            "independent_validator": _file_fact(validator),
            "credential_bridge": _file_fact(credential_bridge),
            "network_guard": _file_fact(network_guard),
            "workspace_parent": str(workspace_parent),
            "initial_workspace_state": "new_with_empty_processing_cache",
            "credential_source": "systemd_credentials",
            "credential_id": "stepfun_api_key",
            "cold_then_overwrite": True,
        },
        "release": {"version": version, "tag": tag},
    }
    _write_new_json(plan_path, plan)


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _child_environment(credential: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    for variable in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "KEYLESS_GATE_NETWORK_MODE",
        "KEYLESS_GATE_PARENT_NETNS",
        "KEYLESS_GATE_ORIGINAL_UID",
        "KEYLESS_GATE_ORIGINAL_GID",
        "KEYLESS_GATE_FORCE_PYTHON_GUARD",
        "KEYLESS_GATE_REQUIRE_NAMESPACE",
        _SYSTEMD_HOST_NETNS,
    ):
        environment.pop(variable, None)
    if credential is None:
        environment.pop("STEPFUN_API_KEY", None)
    else:
        environment["STEPFUN_API_KEY"] = credential
    return environment


def _contains_secret(value: str | bytes, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    return credential.encode("utf-8") in value


def _read_network_attestation(
    descriptor: int,
    *,
    expected_parent_namespace: str,
) -> dict[str, bool]:
    try:
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = os.read(descriptor, 4097 - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
    except OSError:
        _fail("cache_rerun.network_attestation_invalid")
    if len(payload) > 4096:
        _fail("cache_rerun.network_attestation_invalid")
    try:
        schema, parent, isolated, interfaces = bytes(payload).decode(
            "ascii"
        ).removesuffix("\n").split("\t")
    except (UnicodeDecodeError, ValueError):
        _fail("cache_rerun.network_attestation_invalid")
    if (
        schema != "release_gate_network.v1"
        or parent != expected_parent_namespace
        or _NETWORK_NAMESPACE.fullmatch(parent) is None
        or _NETWORK_NAMESPACE.fullmatch(isolated) is None
        or isolated == parent
        or interfaces != "lo"
    ):
        _fail("cache_rerun.network_attestation_invalid")
    return {"attestation_verified": True}


def _observed_live_run_directories(
    runs: Path,
    *,
    absent_allowed: bool,
) -> set[str]:
    try:
        metadata = runs.lstat()
    except FileNotFoundError:
        if absent_allowed:
            return set()
        _fail("run.diagnostics_missing")
    except OSError:
        _fail("run.diagnostics_invalid")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail("run.diagnostics_invalid")
    observed: set[str] = set()
    try:
        for item in runs.iterdir():
            item_metadata = item.lstat()
            if not stat.S_ISDIR(item_metadata.st_mode):
                _fail("run.diagnostics_invalid")
            observed.add(item.name)
    except ReleaseGateFailure:
        raise
    except OSError:
        _fail("run.diagnostics_invalid")
    return observed


def _run_live(
    *,
    console: Path,
    source: Path,
    workspace: Path,
    workspace_parent: Path,
    credential: str,
    overwrite: bool,
    network_guard: Path | None = None,
    host_network_namespace: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, bool] | None]:
    runs = workspace / "work" / "runs"
    before = _observed_live_run_directories(
        runs,
        absent_allowed=True,
    )
    command = [
        str(console),
        "live",
        str(source),
        "--workspace-dir",
        str(workspace),
    ]
    if overwrite:
        command.append("--overwrite")
        if network_guard is None:
            _fail("cache_rerun.network_guard_missing")
        if (
            not isinstance(host_network_namespace, str)
            or _NETWORK_NAMESPACE.fullmatch(host_network_namespace) is None
        ):
            _fail("cache_rerun.network_attestation_invalid")
        command = [str(network_guard), *command]
    elif network_guard is not None or host_network_namespace is not None:
        _fail("cold_run.network_guard_unexpected")
    if any(_contains_secret(argument, credential) for argument in command):
        _fail("credential.argv_leak")
    read_descriptor = -1
    write_descriptor = -1
    parent_namespace = ""
    try:
        child_environment = _child_environment(credential)
        passed_descriptors: tuple[int, ...] = ()
        if overwrite:
            child_environment["KEYLESS_GATE_REQUIRE_NAMESPACE"] = "1"
            child_environment[_SYSTEMD_HOST_NETNS] = host_network_namespace
            try:
                parent_namespace = host_network_namespace
                read_descriptor, write_descriptor = os.pipe2(os.O_CLOEXEC)
                os.set_inheritable(write_descriptor, True)
            except OSError:
                _fail("cache_rerun.network_attestation_invalid")
            child_environment["RELEASE_GATE_NETWORK_ATTESTATION_FD"] = str(
                write_descriptor
            )
            passed_descriptors = (write_descriptor,)
        completed = subprocess.run(
            tuple(command),
            cwd=workspace_parent,
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            pass_fds=passed_descriptors,
        )
    except OSError:
        _fail("run.launch_failed")
    finally:
        if write_descriptor > 2:
            try:
                os.close(write_descriptor)
            except OSError:
                pass
    network_attestation = None
    if overwrite:
        try:
            network_attestation = _read_network_attestation(
                read_descriptor,
                expected_parent_namespace=parent_namespace,
            )
        finally:
            if read_descriptor > 2:
                try:
                    os.close(read_descriptor)
                except OSError:
                    pass
    if _contains_secret(completed.stdout, credential) or _contains_secret(
        completed.stderr,
        credential,
    ):
        _fail("credential.output_leak")
    after = _observed_live_run_directories(
        runs,
        absent_allowed=False,
    )
    created = after - before
    if len(created) != 1:
        _fail("run.diagnostics_missing")
    run_id = next(iter(created))
    if _RUN_ID.fullmatch(run_id) is None:
        _fail("run.diagnostics_invalid")
    manifest_path = runs / run_id / "run.json"
    manifest = _load_json(manifest_path)
    if completed.returncode != 0:
        _fail("run.failed")
    if not isinstance(manifest, dict):
        _fail("run.diagnostics_invalid")
    return run_id, manifest, network_attestation


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_provider_token_usage(value: Any, *, count: int) -> bool:
    if value == {"status": "not_reported"}:
        return True
    if not isinstance(value, dict):
        return False
    status = value.get("status")
    expected_fields = {"status", "input_tokens", "output_tokens"}
    if status == "partially_reported":
        expected_fields.add("reported_request_count")
    if (
        set(value) != expected_fields
        or status not in {"reported", "partially_reported"}
        or not _nonnegative_integer(value.get("input_tokens"))
        or not _nonnegative_integer(value.get("output_tokens"))
    ):
        return False
    if status == "partially_reported":
        reported = value.get("reported_request_count")
        return (
            isinstance(reported, int)
            and not isinstance(reported, bool)
            and 1 <= reported < count
        )
    return True


def _summarize_run(
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    manifest_path: Path,
    delivery: Path,
    version: str,
    source_fact: Mapping[str, Any],
    course_context_fact: Mapping[str, Any],
    warm: bool,
) -> dict[str, Any]:
    context_sha256, context_observation = _course_context_observation(
        Path(course_context_fact["path"])
    )
    try:
        delivery_state = manifest["delivery"]
        delivery_artifacts = delivery_state["artifacts"]
        configuration = manifest["configuration"]
        result_configuration = configuration["result_configuration"]
        if (
            manifest["schema_version"] != "run_manifest.v1"
            or manifest["identity"]["run_id"] != run_id
            or manifest["identity"]["application_version"] != version
            or manifest["identity"]["release"] != {"status": "unknown"}
            or manifest["lifecycle"]["outcome"] != "succeeded"
            or manifest["lifecycle"]["exit_code"] != 0
            or manifest["lifecycle"]["result_kind"]
            != {"status": "available", "value": "clips"}
            or manifest["source"]
            != {
                "status": "available",
                "sha256": f"sha256:{source_fact['sha256']}",
                "byte_length": source_fact["byte_length"],
                "duration_ms": source_fact["duration_ms"],
                "course_context": {
                    "provided": True,
                    "sha256": {
                        "status": "available",
                        "value": context_sha256,
                    },
                },
            }
            or manifest["environment"]["certified_platform"]
            != "ubuntu_24_04_amd64"
            or manifest["environment"]["preflight_outcome"] != "succeeded"
            or frozenset(delivery_state)
            != {
                "build_state",
                "verification_state",
                "publication_state",
                "artifacts",
            }
            or delivery_state["build_state"] != "completed"
            or delivery_state["verification_state"] != "passed"
            or delivery_state["publication_state"] != "committed"
            or delivery_artifacts.get("status") != "observed"
            or delivery_artifacts.get("created_by_role")
            != delivery_artifacts.get("verified_by_role")
            or not isinstance(delivery_artifacts.get("created_by_role"), dict)
            or delivery_artifacts["created_by_role"].get("short_video", 0) < 1
            or configuration["status"] != "available"
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                configuration["configuration_fingerprint"],
            )
            is None
            or configuration["course_context"] != context_observation
            or result_configuration["transcription_provider"] != "stepaudio"
            or result_configuration["transcription_model"]
            != "stepaudio-2.5-asr"
            or result_configuration["text_model_provider"] != "stepfun"
            or result_configuration["topic_review"]["model"] != "step-2-mini"
            or result_configuration["subtitle_optimization"]["model"]
            != "step-2-mini"
            or manifest["errors"]
            != {
                "primary_error": {"status": "not_applicable"},
                "associated_errors": [],
                "recovery_incomplete": False,
            }
        ):
            _fail("run.contract_invalid")
        services = manifest["external_services"]
        cache = manifest["cache"]
        environment = manifest["environment"]
        font = environment["font"]
        python_version = environment["python_version"]
        if services["status"] != "observed" or cache["status"] != "observed":
            _fail("run.contract_invalid")
        if (
            frozenset(environment)
            != {
                "status",
                "certified_platform",
                "python_version",
                "ffmpeg_version",
                "ffprobe_version",
                "font",
                "installation_fingerprint",
                "preflight_outcome",
                "application_version",
            }
            or environment["status"] != "available"
            or environment["certified_platform"] != "ubuntu_24_04_amd64"
            or not isinstance(python_version, str)
            or _VERSION.fullmatch(python_version) is None
            or not (
                (3, 12, 3)
                <= tuple(map(int, python_version.split(".")))
                < (3, 13, 0)
            )
            or not isinstance(environment["ffmpeg_version"], str)
            or _DETECTED_VERSION.fullmatch(environment["ffmpeg_version"])
            is None
            or environment["ffprobe_version"] != environment["ffmpeg_version"]
            or font != {"family": "Noto Sans CJK SC", "available": True}
            or not isinstance(environment["installation_fingerprint"], str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                environment["installation_fingerprint"],
            )
            is None
            or environment["preflight_outcome"] != "succeeded"
            or environment["application_version"] != version
        ):
            _fail("run.contract_invalid")
        raw_services = services["services"]
        namespaces = cache["namespaces"]
    except ReleaseGateFailure:
        raise
    except (AttributeError, KeyError, TypeError):
        _fail("run.contract_invalid")
    if not isinstance(raw_services, list) or not isinstance(namespaces, dict):
        _fail("run.contract_invalid")
    service_summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for service in raw_services:
        if not isinstance(service, dict):
            _fail("run.external_services_invalid")
        capability = service.get("capability")
        provider = service.get("provider_id")
        model = service.get("model_id")
        contact = service.get("contact")
        requests = service.get("requests")
        if (
            frozenset(service)
            != {
                "capability",
                "adapter_id",
                "provider_id",
                "model_id",
                "configuration_fingerprint",
                "endpoint",
                "transport",
                "purpose",
                "allowed_data_categories",
                "contact",
                "requests",
            }
            or capability not in _REQUIRED_CAPABILITIES
            or capability in seen
            or not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
            or not isinstance(requests, dict)
            or frozenset(requests)
            != {
                "count",
                "succeeded",
                "failed",
                "attempt_count_total",
                "duration_ms_total",
                "duration_ms_max",
                "token_usage",
            }
            or not all(
                _nonnegative_integer(requests.get(field))
                for field in (
                    "count",
                    "succeeded",
                    "failed",
                    "attempt_count_total",
                    "duration_ms_total",
                    "duration_ms_max",
                )
            )
            or requests["succeeded"] + requests["failed"]
            != requests["count"]
            or requests["failed"] != 0
            or requests["attempt_count_total"] < requests["count"]
            or requests["duration_ms_max"] > requests["duration_ms_total"]
        ):
            _fail("run.external_services_invalid")
        expected_provider = (
            "stepaudio" if capability == "transcription" else "stepfun"
        )
        expected_model = (
            "stepaudio-2.5-asr"
            if capability == "transcription"
            else "step-2-mini"
        )
        count = requests["count"]
        if (
            provider != expected_provider
            or service["adapter_id"] != expected_provider
            or model != expected_model
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                service["configuration_fingerprint"],
            )
            is None
            or service["endpoint"]
            != {
                "status": "available",
                "origin": _CERTIFIED_ENDPOINT_ORIGIN,
            }
            or service["transport"] != "remote"
            or service["purpose"] != capability
            or service["allowed_data_categories"]
            != _PROVIDER_DATA_CATEGORIES[capability]
        ):
            _fail("run.external_services_invalid")
        if warm and (
            count != 0
            or requests["attempt_count_total"] != 0
            or requests["duration_ms_total"] != 0
            or requests["duration_ms_max"] != 0
            or contact != {"status": "not_contacted", "reason": "cache_hit"}
            or requests["token_usage"] != {"status": "not_applicable"}
        ):
            _fail("cache_rerun.remote_request_detected")
        if not warm and (
            count == 0
            or contact != {"status": "contacted"}
            or not _valid_provider_token_usage(
                requests["token_usage"],
                count=count,
            )
        ):
            _fail(
                "cold_run.provider_not_contacted"
            )
        seen.add(capability)
        service_summaries.append(
            {
                "capability": capability,
                "provider_id": provider,
                "model_id": model,
                "requests": {
                    "count": count,
                    "succeeded": requests["succeeded"],
                    "failed": requests["failed"],
                    "attempt_count_total": requests["attempt_count_total"],
                },
            }
        )
    if seen != set(_REQUIRED_CAPABILITIES):
        _fail("run.external_services_invalid")
    service_summaries.sort(
        key=lambda item: _REQUIRED_CAPABILITIES.index(item["capability"])
    )

    cache_summaries: dict[str, dict[str, int]] = {}
    allowed_cache_fields = {
        "queries",
        "hits",
        "misses",
        "corrupt_quarantined",
        "writes_published",
        "writes_already_present",
        "infrastructure_failures",
        "singleflight_wait_count",
        "singleflight_wait_ms_total",
    }
    for namespace, stats in namespaces.items():
        if (
            namespace
            not in {
                "transcript",
                "transcription_shard",
                "topic_review",
                "subtitle_optimization",
            }
            or not isinstance(stats, dict)
            or set(stats) != allowed_cache_fields
            or not all(_nonnegative_integer(value) for value in stats.values())
            or stats["queries"]
            != stats["hits"] + stats["misses"] + stats["corrupt_quarantined"]
            or stats["corrupt_quarantined"] != 0
            or stats["infrastructure_failures"] != 0
        ):
            _fail("run.cache_invalid")
        cache_summaries[namespace] = dict(stats)
    if warm:
        if any(
            stats["misses"] != 0
            or stats["writes_published"] != 0
            or stats["writes_already_present"] != 0
            for stats in cache_summaries.values()
        ):
            _fail("cache_rerun.cache_not_reused")
        for namespace in _REQUIRED_WARM_CACHE_HITS:
            stats = cache_summaries.get(namespace)
            if stats is None or stats["hits"] < 1 or stats["misses"] != 0:
                _fail("cache_rerun.required_hit_missing")
    else:
        if set(cache_summaries) != {
            "transcript",
            "transcription_shard",
            "topic_review",
            "subtitle_optimization",
        } or any(
            stats["hits"] != 0
            or stats["misses"] < 1
            or stats["writes_published"] < 1
            or stats["writes_already_present"] != 0
            for stats in cache_summaries.values()
        ):
            _fail("cold_run.cache_not_empty")

    delivery_summary = _delivery_summary(
        delivery,
        expected_run_id=run_id,
        version=version,
        source_fact=source_fact,
    )
    return {
        "run_id": run_id,
        "terminal_state": "succeeded",
        "result_kind": "clips",
        "run_manifest_sha256": _sha256_file(manifest_path),
        "delivery": delivery_summary,
        "environment": {
            "certified_platform": environment["certified_platform"],
            "python_version": environment["python_version"],
            "application_version": version,
            "ffmpeg_version": environment["ffmpeg_version"],
            "ffprobe_version": environment["ffprobe_version"],
            "font_family": environment["font"]["family"],
            "preflight_outcome": environment["preflight_outcome"],
            "installation_fingerprint": environment["installation_fingerprint"],
        },
        "configuration": {
            "configuration_fingerprint": configuration[
                "configuration_fingerprint"
            ],
            "course_context": context_observation,
        },
        "external_services": service_summaries,
        "remote_request_count": sum(
            service["requests"]["count"] for service in service_summaries
        ),
        "processing_cache": cache_summaries,
    }


def _delivery_summary(
    delivery: Path,
    *,
    expected_run_id: str,
    version: str,
    source_fact: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_path = delivery / "manifest.json"
    manifest = _load_json(manifest_path)
    try:
        files = manifest["files"]
        if (
            manifest["schema_version"] != "delivery_manifest.v1"
            or manifest["run_id"] != expected_run_id
            or manifest["terminal_state"] != "succeeded"
            or manifest["result_kind"] != "clips"
            or manifest["application_version"] != version
            or manifest["source"]
            != {
                "sha256": f"sha256:{source_fact['sha256']}",
                "byte_length": source_fact["byte_length"],
                "duration_ms": source_fact["duration_ms"],
            }
            or not isinstance(files, list)
        ):
            _fail("delivery.summary_invalid")
    except ReleaseGateFailure:
        raise
    except (KeyError, TypeError):
        _fail("delivery.summary_invalid")
    short_video_count = sum(
        isinstance(item, dict) and item.get("role") == "short_video_media"
        for item in files
    )
    if short_video_count < 1:
        _fail("cold_run.no_publish_ready_short_video")
    return {
        "manifest_sha256": _sha256_file(manifest_path),
        "result_kind": "clips",
        "short_video_count": short_video_count,
        "source": dict(manifest["source"]),
    }


def _independent_validate(
    *,
    validator: Path,
    delivery: Path,
    expected_transcript: Path,
    source: Path,
    version: str,
    result_path: Path,
    expected_run_id: str,
    credential: str,
) -> dict[str, Any]:
    command = (
        sys.executable,
        "-I",
        str(validator),
        "--delivery",
        str(delivery),
        "--expected-transcript",
        str(expected_transcript),
        "--source",
        str(source),
        "--expected-application-version",
        version,
        "--result",
        str(result_path),
    )
    if any(_contains_secret(argument, credential) for argument in command):
        _fail("credential.argv_leak")
    try:
        completed = subprocess.run(
            command,
            cwd=result_path.parent,
            env=_child_environment(None),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        _fail("independent_validation.launch_failed")
    if _contains_secret(completed.stdout, credential) or _contains_secret(
        completed.stderr,
        credential,
    ):
        _fail("credential.output_leak")
    if completed.returncode != 0:
        _fail("independent_validation.failed")
    result = _load_json(result_path)
    try:
        checks = result["checks"]
        valid = (
            result["schema_version"] == "independent_delivery_validation.v1"
            and result["success"] is True
            and result["run_id"] == expected_run_id
            and result["result_kind"] == "clips"
            and _nonnegative_integer(result["short_video_count"])
            and result["short_video_count"] >= 1
            and _nonnegative_integer(result["artifact_count"])
            and result["artifact_count"] >= result["short_video_count"]
            and isinstance(checks, dict)
            and frozenset(checks) == _INDEPENDENT_VALIDATION_CHECKS
            and all(value is True for value in checks.values())
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        _fail("independent_validation.failed")
    return {
        "schema_version": "independent_delivery_validation.v1",
        "passed": True,
        "run_id": expected_run_id,
        "result_kind": "clips",
        "short_video_count": result["short_video_count"],
        "artifact_count": result["artifact_count"],
        "evidence_sha256": _sha256_file(result_path),
        "checks": {
            name: True for name in sorted(_INDEPENDENT_VALIDATION_CHECKS)
        },
    }


_BUSINESS_ID_FIELDS = frozenset(
    {
        "run_id",
        "transcript_id",
        "transcript_chunk_id",
        "transcript_chunk_ids",
        "plan_id",
        "candidate_id",
        "source_candidate_id",
        "short_video_id",
        "short_video_ids",
        "series_id",
        "path",
    }
)


def _normalize_business_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_business_value(item)
            for key, item in sorted(value.items())
            if key not in _BUSINESS_ID_FIELDS
        }
    if isinstance(value, list):
        return [_normalize_business_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        _fail("semantic_equivalence.invalid")
    return value


def _business_projection(delivery: Path) -> tuple[dict[str, Any], str]:
    projection = {
        name: _normalize_business_value(_load_json(delivery / name))
        for name in ("transcript.json", "plan.json", "metadata.json")
    }
    try:
        payload = json.dumps(
            projection,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        _fail("semantic_equivalence.invalid")
    return projection, hashlib.sha256(payload).hexdigest()


def _file_contains_secret(path: Path, secret: bytes) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            overlap = b""
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    return False
                candidate = overlap + chunk
                if secret in candidate:
                    return True
                overlap = (
                    candidate[-(len(secret) - 1) :]
                    if len(secret) > 1
                    else b""
                )
        finally:
            os.close(descriptor)
    except OSError:
        _fail("credential.leak_scan_failed")


def _scan_text_evidence_for_secret(
    roots: Sequence[Path],
    credential: str,
    *,
    cleanup_generated_roots: Sequence[Path] = (),
) -> None:
    secret = credential.encode("utf-8")
    leaked_paths: list[Path] = []
    for root in roots:
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("credential.leak_scan_failed")
        try:
            if stat.S_ISDIR(root_metadata.st_mode):
                paths = (root, *tuple(root.rglob("*")))
            elif stat.S_ISREG(root_metadata.st_mode) or stat.S_ISLNK(
                root_metadata.st_mode
            ):
                paths = (root,)
            else:
                _fail("credential.leak_scan_failed")
        except OSError:
            _fail("credential.leak_scan_failed")
        for path in paths:
            try:
                metadata = path.lstat()
                leaked = secret in os.fsencode(path.name)
                if stat.S_ISLNK(metadata.st_mode):
                    leaked = leaked or secret in os.fsencode(os.readlink(path))
                elif stat.S_ISREG(metadata.st_mode):
                    leaked = leaked or _file_contains_secret(path, secret)
                elif not stat.S_ISDIR(metadata.st_mode):
                    _fail("credential.leak_scan_failed")
                if leaked:
                    leaked_paths.append(path)
            except FileNotFoundError:
                continue
            except OSError:
                _fail("credential.leak_scan_failed")
    if not leaked_paths:
        return
    for path in sorted(
        set(leaked_paths),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        generated = any(
            path == root or root in path.parents
            for root in cleanup_generated_roots
        )
        if not generated:
            continue
        try:
            metadata = path.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("credential.leak_cleanup_failed")
    _fail("credential.evidence_leak")


def _scan_release_scope_for_secret(
    plan: Mapping[str, Any],
    plan_path: Path,
    credential: str,
) -> None:
    inputs = plan["inputs"]
    automation = plan["automation"]
    installation = plan["certified_host"]["installation"]
    workspace_parent = Path(plan["execution"]["workspace_parent"])
    _scan_text_evidence_for_secret(
        (
            plan_path,
            Path(inputs["configuration"]["path"]),
            Path(inputs["course_context"]["path"]),
            Path(inputs["expected_transcript"]["path"]),
            Path(automation["keyless_gate_evidence"]["path"]),
            Path(automation["installed_acceptance_evidence"]["path"]),
            Path(installation["manifest"]["path"]),
            Path(installation["ready"]["path"]),
            workspace_parent,
        ),
        credential,
        cleanup_generated_roots=(workspace_parent,),
    )


def _input_fingerprint(plan: Mapping[str, Any]) -> str:
    inputs = plan["inputs"]
    payload = {
        "source": inputs["source"]["sha256"],
        "configuration": inputs["configuration"]["sha256"],
        "course_context": inputs["course_context"]["sha256"],
        "expected_transcript": inputs["expected_transcript"]["sha256"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _attempt_candidate(plan: Mapping[str, Any]) -> dict[str, str]:
    candidate = plan["candidate"]
    return {
        "commit_sha": candidate["commit_sha"],
        "wheel_sha256": candidate["wheel"]["sha256"],
    }


def _attempt_common_matches(
    record: Any,
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
    plan_path: Path,
) -> bool:
    try:
        return bool(
            isinstance(record, dict)
            and record["attempt_id"] == attempt_id
            and record["candidate"] == _attempt_candidate(plan)
            and record["input_fingerprint"] == _input_fingerprint(plan)
            and record["plan_sha256"] == _sha256_file(plan_path)
        )
    except (KeyError, TypeError):
        return False


def _timestamp_is_valid(value: Any) -> bool:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_cold_record(
    record: Any,
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    cold = _closed_object(
        record,
        frozenset(
            {
                "schema_version",
                "attempt_id",
                "status",
                "started_at",
                "ended_at",
                "candidate",
                "input_fingerprint",
                "plan_sha256",
                "workspace",
                "cold_run",
                "business_projection_sha256",
                "independent_validation",
                "credential_handling",
            }
        ),
        "attempt.state_invalid",
    )
    try:
        valid = (
            cold["schema_version"] == "release_gate_cold_run.v1"
            and cold["status"] == "awaiting_manual_review"
            and _attempt_common_matches(
                cold,
                attempt_id=attempt_id,
                plan=plan,
                plan_path=plan_path,
            )
            and _timestamp_is_valid(cold["started_at"])
            and _timestamp_is_valid(cold["ended_at"])
            and cold["workspace"]
            == {
                "initial_processing_cache_empty": True,
                "same_workspace_reserved_for_rerun": True,
            }
            and isinstance(cold["cold_run"], dict)
            and _RUN_ID.fullmatch(cold["cold_run"]["run_id"]) is not None
            and cold["cold_run"]["terminal_state"] == "succeeded"
            and _nonnegative_integer(cold["cold_run"]["remote_request_count"])
            and cold["cold_run"]["remote_request_count"] > 0
            and _nonnegative_integer(
                cold["cold_run"]["delivery"]["short_video_count"]
            )
            and cold["cold_run"]["delivery"]["short_video_count"] > 0
            and isinstance(cold["business_projection_sha256"], str)
            and _SHA256.fullmatch(cold["business_projection_sha256"])
            is not None
            and cold["independent_validation"]["passed"] is True
            and cold["credential_handling"]
            == {
                "source": "systemd_credentials",
                "leak_scan_passed": True,
            }
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        _fail("attempt.state_invalid")
    return cold


def _validate_review_record(
    record: Any,
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
    plan_path: Path,
    cold: Mapping[str, Any],
    cold_path: Path,
) -> dict[str, Any]:
    review = _closed_object(
        record,
        frozenset(
            {
                "schema_version",
                "attempt_id",
                "status",
                "candidate",
                "input_fingerprint",
                "plan_sha256",
                "cold_record_sha256",
                "review_source_sha256",
                "recorded_at",
                "operator_id",
                "reviewed_at",
                "run_id",
                "source_and_transcript_compared",
                "clips",
                "reviewed_clip_count",
                "all_checks_passed",
                "conclusion",
            }
        ),
        "attempt.state_invalid",
    )
    expected_count = cold["cold_run"]["delivery"]["short_video_count"]
    clips = review.get("clips")
    valid_clips = isinstance(clips, list) and len(clips) == expected_count
    if valid_clips:
        for ordinal, clip in enumerate(clips, start=1):
            try:
                checks = _closed_object(
                    clip,
                    frozenset({"ordinal", "checks"}),
                    "attempt.state_invalid",
                )["checks"]
                checks = _closed_object(
                    checks,
                    frozenset(_MANUAL_REVIEW_CHECKS),
                    "attempt.state_invalid",
                )
                if (
                    isinstance(clip["ordinal"], bool)
                    or clip["ordinal"] != ordinal
                    or not all(value is True for value in checks.values())
                ):
                    valid_clips = False
                    break
            except (KeyError, TypeError):
                valid_clips = False
                break
    try:
        valid = (
            review["schema_version"] == "release_gate_review_record.v1"
            and review["status"] == "passed"
            and _attempt_common_matches(
                review,
                attempt_id=attempt_id,
                plan=plan,
                plan_path=plan_path,
            )
            and review["cold_record_sha256"] == _sha256_file(cold_path)
            and isinstance(review["review_source_sha256"], str)
            and _SHA256.fullmatch(review["review_source_sha256"]) is not None
            and _timestamp_is_valid(review["recorded_at"])
            and isinstance(review["operator_id"], str)
            and _STABLE_ID.fullmatch(review["operator_id"]) is not None
            and _timestamp_is_valid(review["reviewed_at"])
            and review["run_id"] == cold["cold_run"]["run_id"]
            and review["source_and_transcript_compared"] is True
            and review["reviewed_clip_count"] == expected_count
            and not isinstance(review["reviewed_clip_count"], bool)
            and review["all_checks_passed"] is True
            and review["conclusion"] == "passed"
            and valid_clips
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        _fail("attempt.state_invalid")
    return review


def _manual_review_summary(review: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operator_id": review["operator_id"],
        "reviewed_at": review["reviewed_at"],
        "run_id": review["run_id"],
        "source_and_transcript_compared": True,
        "reviewed_clip_count": review["reviewed_clip_count"],
        "all_checks_passed": True,
        "conclusion": "passed",
    }


def _failure_fact_is_valid(value: Any, *, failure_phase: str) -> bool:
    if not isinstance(value, dict):
        return False
    reason_code = value.get("reason_code")
    if (
        not isinstance(reason_code, str)
        or re.fullmatch(r"[a-z][a-z0-9_.]{0,127}", reason_code) is None
        or value.get("same_candidate_rerun_allowed") is not False
    ):
        return False
    classification = value.get("classification")
    if classification == "candidate_rejected":
        return frozenset(value) == {
            "reason_code",
            "classification",
            "same_candidate_rerun_allowed",
        }
    if classification != "unclassified":
        return False
    allowed = value.get("permitted_same_candidate_rerun")
    if allowed == ["provider_transient"]:
        stable_error_code = value.get("stable_error_code")
        return bool(
            frozenset(value)
            == {
                "reason_code",
                "stable_error_code",
                "classification",
                "permitted_same_candidate_rerun",
                "same_candidate_rerun_allowed",
            }
            and failure_phase == "cold_run"
            and reason_code == "run.failed"
            and isinstance(stable_error_code, str)
            and re.fullmatch(
                r"(?:transcription|topic_review|subtitle_optimization)\."
                r"(?:rate_limited|request_timeout|service_unavailable)",
                stable_error_code,
            )
            is not None
        )
    if allowed == ["certified_host_infrastructure"]:
        return bool(
            frozenset(value)
            == {
                "reason_code",
                "stable_error_code",
                "classification",
                "permitted_same_candidate_rerun",
                "same_candidate_rerun_allowed",
            }
            and reason_code
            == "independent_validation.launch_failed"
            and value.get("stable_error_code") == reason_code
        )
    return False


def _provider_transient_original_is_valid(
    record: Mapping[str, Any],
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
) -> bool:
    failure = record.get("failure")
    if not isinstance(failure, dict) or failure.get(
        "permitted_same_candidate_rerun"
    ) != ["provider_transient"]:
        return True
    run_id = record.get("failed_run_id")
    stable_error_code = failure.get("stable_error_code")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        return False
    try:
        workspace_parent = Path(plan["execution"]["workspace_parent"])
        manifest = _load_json(
            workspace_parent
            / f"{attempt_id}.workspace"
            / "work"
            / "runs"
            / run_id
            / "run.json"
        )
        return bool(
            isinstance(manifest, dict)
            and manifest["identity"]["run_id"] == run_id
            and manifest["lifecycle"]["outcome"] == "failed"
            and manifest["errors"]["primary_error"]["error_code"]
            == stable_error_code
        )
    except (KeyError, TypeError):
        return False


def _validate_final_record(
    record: Any,
    *,
    attempt_id: str,
    plan: Mapping[str, Any],
    plan_path: Path,
    cold: Mapping[str, Any] | None,
    cold_path: Path,
    review: Mapping[str, Any] | None,
    review_path: Path,
) -> dict[str, Any]:
    if not isinstance(record, dict) or not _attempt_common_matches(
        record,
        attempt_id=attempt_id,
        plan=plan,
        plan_path=plan_path,
    ):
        _fail("attempt.state_invalid")
    status = record.get("status")
    if status == "passed":
        expected_fields = frozenset(
            {
                "schema_version",
                "attempt_id",
                "status",
                "started_at",
                "ended_at",
                "candidate",
                "input_fingerprint",
                "plan_sha256",
                "phase_records",
                "workspace",
                "cold_run",
                "manual_review",
                "cache_rerun",
                "semantic_equivalence",
                "independent_validation",
                "credential_handling",
            }
        )
        if frozenset(record) != expected_fields or cold is None or review is None:
            _fail("attempt.state_invalid")
        try:
            valid = (
                record["schema_version"] == "release_gate_attempt.v1"
                and _timestamp_is_valid(record["started_at"])
                and _timestamp_is_valid(record["ended_at"])
                and record["phase_records"]
                == {
                    "cold_record_sha256": _sha256_file(cold_path),
                    "review_record_sha256": _sha256_file(review_path),
                }
                and record["workspace"]
                == {
                    "initial_processing_cache_empty": True,
                    "same_workspace_for_rerun": True,
                }
                and record["cold_run"] == cold["cold_run"]
                and record["manual_review"] == _manual_review_summary(review)
                and record["cache_rerun"]["remote_request_count"] == 0
                and record["cache_rerun"]["previous_delivery_retained"] is True
                and record["cache_rerun"]["network_isolation"]
                == {
                    "mode": "linux_network_namespace",
                    "external_blocked": True,
                    "loopback_allowed": True,
                    "attestation_verified": True,
                    "guard_sha256": plan["execution"]["network_guard"][
                        "sha256"
                    ],
                }
                and record["semantic_equivalence"]
                == {
                    "passed": True,
                    "business_projection_sha256": cold[
                        "business_projection_sha256"
                    ],
                }
                and record["independent_validation"]["cold_run"]
                == cold["independent_validation"]
                and record["credential_handling"]
                == {
                    "source": "systemd_credentials",
                    "leak_scan_passed": True,
                }
            )
        except (KeyError, TypeError):
            valid = False
    elif status == "failed":
        expected_fields = frozenset(
            {
                "schema_version",
                "attempt_id",
                "status",
                "started_at",
                "ended_at",
                "candidate",
                "input_fingerprint",
                "plan_sha256",
                "phase_records",
                "failure_phase",
                "run_ids",
                "failed_run_id",
                "failure",
            }
        )
        try:
            valid = (
                frozenset(record) == expected_fields
                and record["schema_version"] == "release_gate_attempt.v1"
                and _timestamp_is_valid(record["started_at"])
                and _timestamp_is_valid(record["ended_at"])
                and record["phase_records"]
                == {
                    **(
                        {"cold_record_sha256": _sha256_file(cold_path)}
                        if cold is not None
                        else {}
                    ),
                    **(
                        {"review_record_sha256": _sha256_file(review_path)}
                        if review is not None
                        else {}
                    ),
                }
                and record["failure_phase"]
                in {"cold_run", "manual_review", "cache_rerun"}
                and (
                    (
                        record["failure_phase"] == "cold_run"
                        and cold is None
                        and review is None
                    )
                    or (
                        record["failure_phase"] == "manual_review"
                        and cold is not None
                        and review is None
                    )
                    or (
                        record["failure_phase"] == "cache_rerun"
                        and cold is not None
                        and review is not None
                    )
                )
                and isinstance(record["run_ids"], list)
                and all(
                    isinstance(run_id, str)
                    and _RUN_ID.fullmatch(run_id) is not None
                    for run_id in record["run_ids"]
                )
                and len(record["run_ids"]) == len(set(record["run_ids"]))
                and record["failed_run_id"]
                == (record["run_ids"][-1] if record["run_ids"] else None)
                and _failure_fact_is_valid(
                    record["failure"],
                    failure_phase=record["failure_phase"],
                )
                and _provider_transient_original_is_valid(
                    record,
                    attempt_id=attempt_id,
                    plan=plan,
                )
            )
        except (KeyError, TypeError):
            valid = False
    else:
        valid = False
    if not valid:
        _fail("attempt.state_invalid")
    return record


def _validate_classification_record(
    classification: Any,
    *,
    record: Mapping[str, Any],
    record_path: Path,
    plan: Mapping[str, Any],
    plan_path: Path,
) -> None:
    value = _closed_object(
        classification,
        frozenset(
            {
                "schema_version",
                "attempt_id",
                "candidate",
                "input_fingerprint",
                "plan_sha256",
                "failure_record_sha256",
                "classification",
                "operator_id",
                "classified_at",
                "same_candidate_rerun_allowed",
            }
        ),
        "attempt.classification_invalid",
    )
    try:
        allowed = record["failure"]["permitted_same_candidate_rerun"]
        valid = (
            record["status"] == "failed"
            and record["failure"]["classification"] == "unclassified"
            and isinstance(allowed, list)
            and value["schema_version"]
            == "release_gate_failure_classification.v1"
            and value["attempt_id"] == record["attempt_id"]
            and value["candidate"] == record["candidate"]
            and value["input_fingerprint"] == _input_fingerprint(plan)
            and value["plan_sha256"] == _sha256_file(plan_path)
            and value["failure_record_sha256"] == _sha256_file(record_path)
            and value["classification"] in allowed
            and value["classification"]
            in {"provider_transient", "certified_host_infrastructure"}
            and isinstance(value["operator_id"], str)
            and _STABLE_ID.fullmatch(value["operator_id"]) is not None
            and _timestamp_is_valid(value["classified_at"])
            and value["same_candidate_rerun_allowed"] is True
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        _fail("attempt.classification_invalid")


def _attempt_state(
    plan: Mapping[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    workspace_parent = Path(plan["execution"]["workspace_parent"])
    try:
        entries = {item.name: item for item in workspace_parent.iterdir()}
    except OSError:
        _fail("attempt.state_invalid")
    entry_pattern = re.compile(
        r"attempt-([0-9]{4})(?:\.workspace|\.private|\.cold\.json|"
        r"\.review\.json|\.classification\.json|\.json)"
    )
    numbers: set[int] = set()
    for name in entries:
        match = entry_pattern.fullmatch(name)
        if match is None:
            _fail("attempt.state_invalid")
        number = int(match.group(1))
        if number < 1:
            _fail("attempt.state_invalid")
        numbers.add(number)
    if numbers and sorted(numbers) != list(range(1, max(numbers) + 1)):
        _fail("attempt.state_invalid")

    attempts: list[dict[str, Any]] = []
    for number in sorted(numbers):
        attempt_id = f"attempt-{number:04d}"
        workspace = workspace_parent / f"{attempt_id}.workspace"
        private = workspace_parent / f"{attempt_id}.private"
        if workspace.name not in entries or private.name not in entries:
            _fail("attempt.state_invalid")
        _private_directory(workspace, "attempt.state_invalid")
        _private_directory(private, "attempt.state_invalid")
        cold_path = workspace_parent / f"{attempt_id}.cold.json"
        review_path = workspace_parent / f"{attempt_id}.review.json"
        record_path = workspace_parent / f"{attempt_id}.json"
        classification_path = (
            workspace_parent / f"{attempt_id}.classification.json"
        )
        cold = (
            _validate_cold_record(
                _load_json(
                    _private_json_record(cold_path, "attempt.state_invalid")
                ),
                attempt_id=attempt_id,
                plan=plan,
                plan_path=plan_path,
            )
            if cold_path.name in entries
            else None
        )
        if review_path.name in entries and cold is None:
            _fail("attempt.state_invalid")
        review = (
            _validate_review_record(
                _load_json(
                    _private_json_record(review_path, "attempt.state_invalid")
                ),
                attempt_id=attempt_id,
                plan=plan,
                plan_path=plan_path,
                cold=cold,
                cold_path=cold_path,
            )
            if review_path.name in entries
            else None
        )
        record = (
            _validate_final_record(
                _load_json(
                    _private_json_record(record_path, "attempt.state_invalid")
                ),
                attempt_id=attempt_id,
                plan=plan,
                plan_path=plan_path,
                cold=cold,
                cold_path=cold_path,
                review=review,
                review_path=review_path,
            )
            if record_path.name in entries
            else None
        )
        if classification_path.name in entries:
            if record is None:
                _fail("attempt.classification_invalid")
            _validate_classification_record(
                _load_json(
                    _private_json_record(
                        classification_path,
                        "attempt.classification_invalid",
                    )
                ),
                record=record,
                record_path=record_path,
                plan=plan,
                plan_path=plan_path,
            )
            classification = True
        else:
            classification = False
        attempts.append(
            {
                "number": number,
                "attempt_id": attempt_id,
                "workspace": workspace,
                "private": private,
                "cold_path": cold_path,
                "cold": cold,
                "review_path": review_path,
                "review": review,
                "record_path": record_path,
                "record": record,
                "classification": classification,
            }
        )

    pending = [attempt for attempt in attempts if attempt["record"] is None]
    if len(pending) > 1 or (pending and pending[0] is not attempts[-1]):
        _fail("attempt.state_invalid")
    for index, attempt in enumerate(attempts):
        record = attempt["record"]
        if record is None:
            continue
        if record["status"] == "passed" and index != len(attempts) - 1:
            _fail("attempt.state_invalid")
        if index != len(attempts) - 1:
            if record["status"] != "failed" or not attempt["classification"]:
                _fail("attempt.state_invalid")
    return {
        "workspace_parent": workspace_parent,
        "attempts": attempts,
        "pending": pending[0] if pending else None,
    }


def _new_attempt_context(
    plan: Mapping[str, Any],
    plan_path: Path,
) -> tuple[str, Path, Path, Path, Path]:
    state = _attempt_state(plan, plan_path)
    attempts = state["attempts"]
    if attempts:
        last = attempts[-1]
        record = last["record"]
        if record is None:
            if last["review"] is not None:
                _fail("attempt.awaiting_cache_rerun")
            if last["cold"] is not None:
                _fail("attempt.awaiting_manual_review")
            _fail("attempt.state_invalid")
        if record["status"] == "passed":
            _fail("attempt.already_passed")
        if not last["classification"]:
            if record["failure"].get("classification") == "candidate_rejected":
                _fail("attempt.candidate_rejected")
            _fail("attempt.classification_required")
    number = len(attempts) + 1
    attempt_id = f"attempt-{number:04d}"
    workspace_parent = state["workspace_parent"]
    return (
        attempt_id,
        workspace_parent / f"{attempt_id}.workspace",
        workspace_parent / f"{attempt_id}.private",
        workspace_parent / f"{attempt_id}.cold.json",
        workspace_parent / f"{attempt_id}.json",
    )


def _pending_attempt(
    plan: Mapping[str, Any],
    plan_path: Path,
) -> dict[str, Any]:
    state = _attempt_state(plan, plan_path)
    pending = state["pending"]
    if pending is not None:
        return pending
    attempts = state["attempts"]
    if attempts and attempts[-1]["record"]["status"] == "passed":
        _fail("attempt.already_passed")
    _fail("review.required")


def _initial_user_namespace(contents: str) -> bool:
    try:
        mappings = [
            tuple(int(value) for value in line.split())
            for line in contents.splitlines()
            if line.strip()
        ]
    except ValueError:
        return False
    return (
        len(mappings) == 1
        and mappings[0][0] == 0
        and mappings[0][1] == 0
        and mappings[0][2] >= 4_294_967_294
    )


def _release_credential(expected_phase: str) -> tuple[str, str]:
    if os.environ.pop("STEPFUN_API_KEY", None) is not None:
        _fail("credential.source_unverified")
    if os.environ.pop("CREDENTIALS_DIRECTORY", None) is not None:
        _fail("credential.source_unverified")
    raw_descriptor = os.environ.pop(_CREDENTIAL_DESCRIPTOR_FD, None)
    host_network_namespace = os.environ.pop(_SYSTEMD_HOST_NETNS, None)
    if raw_descriptor is None:
        _fail("credential.missing_or_invalid")
    source_verified = False
    descriptor = -1
    encoded = b""
    try:
        if (
            isinstance(raw_descriptor, str)
            and re.fullmatch(r"[1-9][0-9]*", raw_descriptor) is not None
        ):
            descriptor = int(raw_descriptor)
            before = os.fstat(descriptor)
            filesystem = os.fstatvfs(descriptor)
            descriptor_path = os.readlink(f"/proc/self/fd/{descriptor}")
            credential_path = Path(descriptor_path)
            unit = credential_path.parent.name
            unit_match = _SYSTEMD_RELEASE_UNIT.fullmatch(unit)
            uid_map = Path("/proc/self/uid_map").read_text(encoding="ascii")
            cgroup = Path("/proc/self/cgroup").read_text(encoding="ascii")
            encoded = os.pread(descriptor, 4097, 0)
            after = os.fstat(descriptor)
            source_verified = (
                descriptor > 2
                and os.geteuid() != 0
                and _initial_user_namespace(uid_map)
                and unit_match is not None
                and unit_match.group(1) == expected_phase
                and credential_path.parent.parent
                == Path("/run/credentials")
                and cgroup == f"0::/system.slice/{unit}\n"
                and isinstance(host_network_namespace, str)
                and _NETWORK_NAMESPACE.fullmatch(host_network_namespace)
                is not None
                and (
                    expected_phase != "rerun"
                    or os.readlink("/proc/self/ns/net")
                    != host_network_namespace
                )
                and stat.S_ISREG(before.st_mode)
                and before.st_uid == 0
                and stat.S_IMODE(before.st_mode) == 0o400
                and 0 < before.st_size <= 4096
                and len(encoded) == before.st_size
                and filesystem.f_flag & os.ST_RDONLY
                and Path(descriptor_path).name == "stepfun_api_key"
                and " (deleted)" not in descriptor_path
                and (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                )
                == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                )
            )
    except (OSError, OverflowError, UnicodeError, ValueError):
        source_verified = False
    finally:
        if descriptor > 2:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not source_verified:
        _fail("credential.source_unverified")
    try:
        credential = encoded.decode("utf-8")
    except UnicodeDecodeError:
        _fail("credential.missing_or_invalid")
    if "\x00" in credential or "\n" in credential or "\r" in credential:
        _fail("credential.missing_or_invalid")
    return credential, host_network_namespace


def _execute_cold_pass(plan_path: Path, credential: str) -> Path:
    plan = verify_plan(plan_path, require_empty_workspace=False)
    execution = plan["execution"]
    inputs = plan["inputs"]
    candidate = plan["candidate"]
    workspace_parent = Path(execution["workspace_parent"])
    attempt_id, workspace, private, cold_path, _record_path = (
        _new_attempt_context(plan, plan_path)
    )
    try:
        workspace.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
    except OSError:
        _fail("attempt.workspace_create_failed")
    started_at = _utc_timestamp()
    console = Path(execution["console"]["path"])
    source = Path(inputs["source"]["path"])
    validator = Path(execution["independent_validator"]["path"])
    expected_transcript = Path(inputs["expected_transcript"]["path"])
    _scan_text_evidence_for_secret(
        (
            plan_path,
            Path(inputs["configuration"]["path"]),
            Path(inputs["course_context"]["path"]),
            expected_transcript,
        ),
        credential,
    )

    cold_run_id, cold_manifest, cold_network_attestation = _run_live(
        console=console,
        source=source,
        workspace=workspace,
        workspace_parent=workspace_parent,
        credential=credential,
        overwrite=False,
    )
    if cold_network_attestation is not None:
        _fail("cold_run.network_guard_unexpected")
    cold_manifest_path = workspace / "work" / "runs" / cold_run_id / "run.json"
    cold_summary = _summarize_run(
        run_id=cold_run_id,
        manifest=cold_manifest,
        manifest_path=cold_manifest_path,
        delivery=workspace / "delivery",
        version=candidate["version"],
        source_fact=inputs["source"],
        course_context_fact=inputs["course_context"],
        warm=False,
    )
    expected_installation_fingerprint = (
        "sha256:"
        + plan["certified_host"]["installation"]["manifest"]["sha256"]
    )
    if cold_summary["environment"]["installation_fingerprint"] != (
        expected_installation_fingerprint
    ):
        _fail("run.environment_binding_invalid")
    _cold_projection, cold_projection_sha256 = _business_projection(
        workspace / "delivery"
    )
    cold_validation = _independent_validate(
        validator=validator,
        delivery=workspace / "delivery",
        expected_transcript=expected_transcript,
        source=source,
        version=candidate["version"],
        result_path=private / "cold-validation.json",
        expected_run_id=cold_run_id,
        credential=credential,
    )
    if (
        cold_validation["short_video_count"]
        != cold_summary["delivery"]["short_video_count"]
    ):
        _fail("independent_validation.failed")
    cold_summary["delivery"]["artifact_count"] = cold_validation[
        "artifact_count"
    ]
    _scan_text_evidence_for_secret(
        (
            plan_path,
            Path(inputs["configuration"]["path"]),
            Path(inputs["course_context"]["path"]),
            expected_transcript,
            workspace,
            private,
        ),
        credential,
        cleanup_generated_roots=(workspace, private),
    )
    record = {
        "schema_version": "release_gate_cold_run.v1",
        "attempt_id": attempt_id,
        "status": "awaiting_manual_review",
        "started_at": started_at,
        "ended_at": _utc_timestamp(),
        "candidate": _attempt_candidate(plan),
        "input_fingerprint": _input_fingerprint(plan),
        "plan_sha256": _sha256_file(plan_path),
        "workspace": {
            "initial_processing_cache_empty": True,
            "same_workspace_reserved_for_rerun": True,
        },
        "cold_run": cold_summary,
        "business_projection_sha256": cold_projection_sha256,
        "independent_validation": cold_validation,
        "credential_handling": {
            "source": "systemd_credentials",
            "leak_scan_passed": True,
        },
    }
    _write_new_json(cold_path, record, reason_code="attempt.write_failed")
    return cold_path


def _observed_run_ids(workspace: Path) -> list[str]:
    run_root = workspace / "work" / "runs"
    if not run_root.is_dir():
        return []
    observed: list[tuple[int, str]] = []
    try:
        for item in run_root.iterdir():
            if item.is_dir() and _RUN_ID.fullmatch(item.name) is not None:
                observed.append((item.stat().st_mtime_ns, item.name))
    except OSError:
        _fail("attempt.state_invalid")
    observed.sort()
    return [run_id for _modified_at, run_id in observed]


def _failure_fact(
    workspace: Path,
    failure: ReleaseGateFailure,
    run_ids: Sequence[str],
    *,
    phase: str,
) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "reason_code": failure.reason_code,
        "classification": "candidate_rejected",
        "same_candidate_rerun_allowed": False,
    }
    run_root = workspace / "work" / "runs"
    if (
        phase == "cold_run"
        and failure.reason_code == "run.failed"
        and run_ids
    ):
        try:
            latest_manifest = _load_json(run_root / run_ids[-1] / "run.json")
            stable_error_code = latest_manifest["errors"]["primary_error"][
                "error_code"
            ]
        except (KeyError, TypeError, ReleaseGateFailure):
            stable_error_code = None
        if isinstance(stable_error_code, str) and re.fullmatch(
            r"(?:transcription|topic_review|subtitle_optimization)\."
            r"(?:rate_limited|request_timeout|service_unavailable)",
            stable_error_code,
        ):
            fact = {
                "reason_code": failure.reason_code,
                "stable_error_code": stable_error_code,
                "classification": "unclassified",
                "permitted_same_candidate_rerun": ["provider_transient"],
                "same_candidate_rerun_allowed": False,
            }
    elif failure.reason_code == "independent_validation.launch_failed":
        fact = {
            "reason_code": failure.reason_code,
            "stable_error_code": failure.reason_code,
            "classification": "unclassified",
            "permitted_same_candidate_rerun": [
                "certified_host_infrastructure"
            ],
            "same_candidate_rerun_allowed": False,
        }
    return fact


def _record_failed_attempt(
    *,
    plan: Mapping[str, Any],
    plan_path: Path,
    failure: ReleaseGateFailure,
    expected_phase: str,
) -> None:
    state = _attempt_state(plan, plan_path)
    pending = state["pending"]
    if pending is None:
        return
    cold = pending["cold"]
    review = pending["review"]
    phase_matches = (
        (expected_phase == "cold_run" and cold is None and review is None)
        or (
            expected_phase == "manual_review"
            and failure.reason_code == "review.failed"
            and cold is not None
            and review is None
        )
        or (
            expected_phase == "cache_rerun"
            and failure.reason_code != "credential.missing_or_invalid"
            and cold is not None
            and review is not None
        )
    )
    if not phase_matches:
        return
    workspace = pending["workspace"]
    run_ids = _observed_run_ids(workspace)
    if cold is None:
        if len(run_ids) > 1:
            _fail("attempt.state_invalid")
    else:
        cold_run_id = cold["cold_run"]["run_id"]
        if cold_run_id not in run_ids:
            _fail("attempt.state_invalid")
        additional_run_ids = [
            run_id for run_id in run_ids if run_id != cold_run_id
        ]
        if len(additional_run_ids) > 1:
            _fail("attempt.state_invalid")
        run_ids = [cold_run_id, *additional_run_ids]
    started_at = cold["started_at"] if cold is not None else _utc_timestamp()
    record = {
        "schema_version": "release_gate_attempt.v1",
        "attempt_id": pending["attempt_id"],
        "status": "failed",
        "started_at": started_at,
        "ended_at": _utc_timestamp(),
        "candidate": _attempt_candidate(plan),
        "input_fingerprint": _input_fingerprint(plan),
        "plan_sha256": _sha256_file(plan_path),
        "phase_records": {
            **(
                {"cold_record_sha256": _sha256_file(pending["cold_path"])}
                if cold is not None
                else {}
            ),
            **(
                {"review_record_sha256": _sha256_file(pending["review_path"])}
                if review is not None
                else {}
            ),
        },
        "failure_phase": expected_phase,
        "run_ids": run_ids,
        "failed_run_id": run_ids[-1] if run_ids else None,
        "failure": _failure_fact(
            workspace,
            failure,
            run_ids,
            phase=expected_phase,
        ),
    }
    _write_new_json(
        pending["record_path"],
        record,
        reason_code="attempt.write_failed",
    )


def execute_gate(plan_path: Path) -> Path:
    credential: str | None = None
    try:
        credential, _host_network_namespace = _release_credential("cold")
        return _execute_cold_pass(plan_path, credential)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as unexpected:
        failure = (
            unexpected
            if isinstance(unexpected, ReleaseGateFailure)
            else ReleaseGateFailure("run.unexpected_failure")
        )
        recorded_failure = failure
        try:
            plan = verify_plan(plan_path, require_empty_workspace=False)
            if isinstance(credential, str) and credential:
                try:
                    _scan_release_scope_for_secret(
                        plan,
                        plan_path,
                        credential,
                    )
                except ReleaseGateFailure as scan_failure:
                    recorded_failure = scan_failure
            _record_failed_attempt(
                plan=plan,
                plan_path=plan_path,
                failure=recorded_failure,
                expected_phase="cold_run",
            )
        except ReleaseGateFailure as recording_failure:
            if recording_failure is not recorded_failure:
                raise recording_failure
        raise recorded_failure


def _private_review_file(review_path: Path) -> Path:
    return _private_json_record(review_path, "review.input_invalid")


def _record_review_pass(plan_path: Path, review_path: Path) -> Path:
    if os.environ.get("STEPFUN_API_KEY"):
        _fail("credential.unexpected_environment")
    plan = verify_plan(plan_path, require_empty_workspace=False)
    pending = _pending_attempt(plan, plan_path)
    cold = pending["cold"]
    if cold is None:
        _fail("review.required")
    if pending["review"] is not None:
        _fail("review.already_recorded")
    source_path = _private_review_file(review_path)
    source = _closed_object(
        _load_json(source_path),
        frozenset(
            {
                "schema_version",
                "operator_id",
                "reviewed_at",
                "run_id",
                "source_and_transcript_compared",
                "clips",
                "conclusion",
            }
        ),
        "review.schema_invalid",
    )
    operator_id = source.get("operator_id")
    reviewed_at = source.get("reviewed_at")
    clips = source.get("clips")
    conclusion = source.get("conclusion")
    if (
        source.get("schema_version") != "release_gate_manual_review.v1"
        or not isinstance(operator_id, str)
        or _STABLE_ID.fullmatch(operator_id) is None
        or not _timestamp_is_valid(reviewed_at)
        or not isinstance(clips, list)
        or conclusion not in {"passed", "failed"}
        or not isinstance(source.get("source_and_transcript_compared"), bool)
    ):
        _fail("review.schema_invalid")
    recorded_at = _utc_timestamp()
    try:
        cold_ended_at = datetime.fromisoformat(
            cold["ended_at"].replace("Z", "+00:00")
        )
        reviewed_timestamp = datetime.fromisoformat(
            reviewed_at.replace("Z", "+00:00")
        )
        recorded_timestamp = datetime.fromisoformat(
            recorded_at.replace("Z", "+00:00")
        )
    except (AttributeError, KeyError, ValueError):
        _fail("review.schema_invalid")
    if not cold_ended_at <= reviewed_timestamp <= recorded_timestamp:
        _fail("review.time_invalid")
    expected_count = cold["cold_run"]["delivery"]["short_video_count"]
    if source.get("run_id") != cold["cold_run"]["run_id"] or len(clips) != (
        expected_count
    ):
        _fail("review.binding_invalid")
    sanitized_clips: list[dict[str, Any]] = []
    all_checks_passed = True
    for expected_ordinal, raw_clip in enumerate(clips, start=1):
        clip = _closed_object(
            raw_clip,
            frozenset({"ordinal", "checks"}),
            "review.schema_invalid",
        )
        ordinal = clip["ordinal"]
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal != expected_ordinal
        ):
            _fail("review.binding_invalid")
        checks = _closed_object(
            clip["checks"],
            frozenset(_MANUAL_REVIEW_CHECKS),
            "review.schema_invalid",
        )
        if not all(isinstance(value, bool) for value in checks.values()):
            _fail("review.schema_invalid")
        all_checks_passed = all_checks_passed and all(
            value is True for value in checks.values()
        )
        sanitized_clips.append({"ordinal": ordinal, "checks": dict(checks)})
    if (
        conclusion != "passed"
        or source["source_and_transcript_compared"] is not True
        or not all_checks_passed
    ):
        _fail("review.failed")
    destination = pending["review_path"]
    record = {
        "schema_version": "release_gate_review_record.v1",
        "attempt_id": pending["attempt_id"],
        "status": "passed",
        "candidate": _attempt_candidate(plan),
        "input_fingerprint": _input_fingerprint(plan),
        "plan_sha256": _sha256_file(plan_path),
        "cold_record_sha256": _sha256_file(pending["cold_path"]),
        "review_source_sha256": _sha256_file(source_path),
        "recorded_at": recorded_at,
        "operator_id": operator_id,
        "reviewed_at": reviewed_at,
        "run_id": source["run_id"],
        "source_and_transcript_compared": True,
        "clips": sanitized_clips,
        "reviewed_clip_count": expected_count,
        "all_checks_passed": True,
        "conclusion": "passed",
    }
    _write_new_json(destination, record, reason_code="review.write_failed")
    return destination


def record_review(plan_path: Path, review_path: Path) -> Path:
    try:
        return _record_review_pass(plan_path, review_path)
    except ReleaseGateFailure as failure:
        try:
            plan = verify_plan(plan_path, require_empty_workspace=False)
            _record_failed_attempt(
                plan=plan,
                plan_path=plan_path,
                failure=failure,
                expected_phase="manual_review",
            )
        except ReleaseGateFailure as recording_failure:
            if recording_failure is not failure:
                raise recording_failure
        raise


def _rerun_pass(
    plan_path: Path,
    credential: str,
    host_network_namespace: str,
) -> Path:
    plan = verify_plan(plan_path, require_empty_workspace=False)
    pending = _pending_attempt(plan, plan_path)
    cold = pending["cold"]
    review = pending["review"]
    if cold is None or review is None:
        _fail("review.required")
    execution = plan["execution"]
    inputs = plan["inputs"]
    candidate = plan["candidate"]
    workspace_parent = Path(execution["workspace_parent"])
    workspace = pending["workspace"]
    private = pending["private"]
    console = Path(execution["console"]["path"])
    network_guard = Path(execution["network_guard"]["path"])
    source = Path(inputs["source"]["path"])
    validator = Path(execution["independent_validator"]["path"])
    expected_transcript = Path(inputs["expected_transcript"]["path"])
    _scan_text_evidence_for_secret(
        (
            plan_path,
            pending["cold_path"],
            pending["review_path"],
            Path(inputs["configuration"]["path"]),
            Path(inputs["course_context"]["path"]),
            expected_transcript,
            workspace,
            private,
        ),
        credential,
        cleanup_generated_roots=(workspace, private),
    )

    cold_run_id = cold["cold_run"]["run_id"]
    cold_manifest_path = workspace / "work" / "runs" / cold_run_id / "run.json"
    if _sha256_file(cold_manifest_path) != cold["cold_run"][
        "run_manifest_sha256"
    ]:
        _fail("cold_run.evidence_drift")
    cold_delivery = _delivery_summary(
        workspace / "delivery",
        expected_run_id=cold_run_id,
        version=candidate["version"],
        source_fact=inputs["source"],
    )
    cold_projection, cold_projection_sha256 = _business_projection(
        workspace / "delivery"
    )
    expected_cold_delivery = dict(cold["cold_run"]["delivery"])
    expected_cold_delivery.pop("artifact_count", None)
    if cold_delivery != expected_cold_delivery or (
        cold_projection_sha256 != cold["business_projection_sha256"]
    ):
        _fail("cold_run.evidence_drift")

    warm_run_id, warm_manifest, warm_network_attestation = _run_live(
        console=console,
        source=source,
        workspace=workspace,
        workspace_parent=workspace_parent,
        credential=credential,
        overwrite=True,
        network_guard=network_guard,
        host_network_namespace=host_network_namespace,
    )
    if warm_network_attestation != {"attestation_verified": True}:
        _fail("cache_rerun.network_attestation_invalid")
    warm_manifest_path = workspace / "work" / "runs" / warm_run_id / "run.json"
    warm_summary = _summarize_run(
        run_id=warm_run_id,
        manifest=warm_manifest,
        manifest_path=warm_manifest_path,
        delivery=workspace / "delivery",
        version=candidate["version"],
        source_fact=inputs["source"],
        course_context_fact=inputs["course_context"],
        warm=True,
    )
    expected_installation_fingerprint = (
        "sha256:"
        + plan["certified_host"]["installation"]["manifest"]["sha256"]
    )
    if (
        warm_summary["environment"]["installation_fingerprint"]
        != expected_installation_fingerprint
        or warm_summary["environment"] != cold["cold_run"]["environment"]
        or warm_summary["configuration"]
        != cold["cold_run"]["configuration"]
    ):
        _fail("run.environment_binding_invalid")
    previous = workspace / "delivery.previous"
    if not previous.is_dir() or previous.is_symlink():
        _fail("cache_rerun.previous_delivery_missing")
    previous_delivery = _delivery_summary(
        previous,
        expected_run_id=cold_run_id,
        version=candidate["version"],
        source_fact=inputs["source"],
    )
    if previous_delivery != expected_cold_delivery:
        _fail("cache_rerun.previous_delivery_drift")
    previous_projection, previous_projection_sha256 = _business_projection(previous)
    warm_projection, warm_projection_sha256 = _business_projection(
        workspace / "delivery"
    )
    if (
        previous_projection != cold_projection
        or warm_projection != cold_projection
        or previous_projection_sha256 != cold_projection_sha256
        or warm_projection_sha256 != cold_projection_sha256
    ):
        _fail("cache_rerun.semantic_drift")
    previous_validation = _independent_validate(
        validator=validator,
        delivery=previous,
        expected_transcript=expected_transcript,
        source=source,
        version=candidate["version"],
        result_path=private / "previous-validation.json",
        expected_run_id=cold_run_id,
        credential=credential,
    )
    warm_validation = _independent_validate(
        validator=validator,
        delivery=workspace / "delivery",
        expected_transcript=expected_transcript,
        source=source,
        version=candidate["version"],
        result_path=private / "warm-validation.json",
        expected_run_id=warm_run_id,
        credential=credential,
    )
    if (
        previous_validation["short_video_count"]
        != cold["cold_run"]["delivery"]["short_video_count"]
        or warm_validation["short_video_count"]
        != warm_summary["delivery"]["short_video_count"]
        or previous_validation["artifact_count"]
        != cold["cold_run"]["delivery"]["artifact_count"]
        or warm_validation["artifact_count"]
        != cold["cold_run"]["delivery"]["artifact_count"]
    ):
        _fail("independent_validation.failed")
    warm_summary["delivery"]["artifact_count"] = warm_validation[
        "artifact_count"
    ]
    _scan_text_evidence_for_secret(
        (
            plan_path,
            pending["cold_path"],
            pending["review_path"],
            Path(inputs["configuration"]["path"]),
            Path(inputs["course_context"]["path"]),
            expected_transcript,
            workspace,
            private,
        ),
        credential,
        cleanup_generated_roots=(workspace, private),
    )
    required_hits = {
        namespace: warm_summary["processing_cache"][namespace]["hits"]
        for namespace in _REQUIRED_WARM_CACHE_HITS
    }
    record = {
        "schema_version": "release_gate_attempt.v1",
        "attempt_id": pending["attempt_id"],
        "status": "passed",
        "started_at": cold["started_at"],
        "ended_at": _utc_timestamp(),
        "candidate": _attempt_candidate(plan),
        "input_fingerprint": _input_fingerprint(plan),
        "plan_sha256": _sha256_file(plan_path),
        "phase_records": {
            "cold_record_sha256": _sha256_file(pending["cold_path"]),
            "review_record_sha256": _sha256_file(pending["review_path"]),
        },
        "workspace": {
            "initial_processing_cache_empty": True,
            "same_workspace_for_rerun": True,
        },
        "cold_run": cold["cold_run"],
        "manual_review": _manual_review_summary(review),
        "cache_rerun": {
            **warm_summary,
            "required_cache_hits": required_hits,
            "previous_delivery_retained": True,
            "network_isolation": {
                "mode": "linux_network_namespace",
                "external_blocked": True,
                "loopback_allowed": True,
                "attestation_verified": True,
                "guard_sha256": execution["network_guard"]["sha256"],
            },
        },
        "semantic_equivalence": {
            "passed": True,
            "business_projection_sha256": cold_projection_sha256,
        },
        "independent_validation": {
            "cold_run": cold["independent_validation"],
            "previous_delivery": previous_validation,
            "cache_rerun": warm_validation,
        },
        "credential_handling": {
            "source": "systemd_credentials",
            "leak_scan_passed": True,
        },
    }
    _write_new_json(
        pending["record_path"],
        record,
        reason_code="attempt.write_failed",
    )
    return pending["record_path"]


def rerun_gate(plan_path: Path) -> Path:
    credential: str | None = None
    try:
        credential, host_network_namespace = _release_credential("rerun")
        return _rerun_pass(
            plan_path,
            credential,
            host_network_namespace,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as unexpected:
        failure = (
            unexpected
            if isinstance(unexpected, ReleaseGateFailure)
            else ReleaseGateFailure("run.unexpected_failure")
        )
        recorded_failure = failure
        try:
            plan = verify_plan(plan_path, require_empty_workspace=False)
            if isinstance(credential, str) and credential:
                try:
                    _scan_release_scope_for_secret(
                        plan,
                        plan_path,
                        credential,
                    )
                except ReleaseGateFailure as scan_failure:
                    recorded_failure = scan_failure
            _record_failed_attempt(
                plan=plan,
                plan_path=plan_path,
                failure=recorded_failure,
                expected_phase="cache_rerun",
            )
        except ReleaseGateFailure as recording_failure:
            if recording_failure is not recorded_failure:
                raise recording_failure
        raise recorded_failure


def classify_failure(
    *,
    plan_path: Path,
    attempt_path: Path,
    classification: str,
    operator_id: str,
) -> Path:
    if os.environ.get("STEPFUN_API_KEY"):
        _fail("credential.unexpected_environment")
    plan = verify_plan(plan_path, require_empty_workspace=False)
    workspace_parent = Path(plan["execution"]["workspace_parent"])
    try:
        if (
            not attempt_path.is_absolute()
            or attempt_path.resolve(strict=True) != attempt_path
            or attempt_path.parent != workspace_parent
            or re.fullmatch(r"attempt-[0-9]{4}\.json", attempt_path.name)
            is None
        ):
            _fail("attempt.classification_invalid")
    except ReleaseGateFailure:
        raise
    except (OSError, RuntimeError):
        _fail("attempt.classification_invalid")
    state = _attempt_state(plan, plan_path)
    matches = [
        attempt
        for attempt in state["attempts"]
        if attempt["record_path"] == attempt_path
    ]
    if len(matches) != 1 or matches[0]["record"] is None:
        _fail("attempt.classification_invalid")
    attempt = matches[0]
    record = attempt["record"]
    if attempt["classification"]:
        _fail("attempt.classification_not_allowed")
    stable_operator = _string(operator_id, "attempt.classification_invalid")
    if _STABLE_ID.fullmatch(stable_operator) is None:
        _fail("attempt.classification_invalid")
    try:
        failure = record["failure"]
        allowed = failure["permitted_same_candidate_rerun"]
        valid = (
            record["schema_version"] == "release_gate_attempt.v1"
            and record["status"] == "failed"
            and failure["classification"] == "unclassified"
            and isinstance(allowed, list)
            and classification in allowed
            and classification
            in {"provider_transient", "certified_host_infrastructure"}
            and record["candidate"]
            == {
                "commit_sha": plan["candidate"]["commit_sha"],
                "wheel_sha256": plan["candidate"]["wheel"]["sha256"],
            }
            and record["input_fingerprint"] == _input_fingerprint(plan)
            and record["plan_sha256"] == _sha256_file(plan_path)
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        _fail("attempt.classification_not_allowed")
    destination = attempt_path.with_name(f"{attempt_path.stem}.classification.json")
    document = {
        "schema_version": "release_gate_failure_classification.v1",
        "attempt_id": record["attempt_id"],
        "candidate": record["candidate"],
        "input_fingerprint": record["input_fingerprint"],
        "plan_sha256": record["plan_sha256"],
        "failure_record_sha256": _sha256_file(attempt_path),
        "classification": classification,
        "operator_id": stable_operator,
        "classified_at": _utc_timestamp(),
        "same_candidate_rerun_allowed": True,
    }
    _write_new_json(
        destination,
        document,
        reason_code="attempt.classification_write_failed",
    )
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="准备并执行发布前真实门禁",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="锁定候选与真实冷运行方案")
    prepare.add_argument("--request", required=True, type=Path)
    prepare.add_argument("--plan", required=True, type=Path)
    verify = commands.add_parser("verify", help="重新核对候选与方案未漂移")
    verify.add_argument("--plan", required=True, type=Path)
    execute = commands.add_parser(
        "execute",
        help="执行真实冷运行并停在人工复核门禁前",
    )
    execute.add_argument("--plan", required=True, type=Path)
    review = commands.add_parser(
        "record-review",
        help="不可变记录逐片人工内容复核",
    )
    review.add_argument("--plan", required=True, type=Path)
    review.add_argument("--review", required=True, type=Path)
    rerun = commands.add_parser(
        "rerun",
        help="在人工复核通过后执行零请求缓存复跑",
    )
    rerun.add_argument("--plan", required=True, type=Path)
    classify = commands.add_parser(
        "classify",
        help="记录允许同一候选复跑的封闭故障分类",
    )
    classify.add_argument("--plan", required=True, type=Path)
    classify.add_argument("--attempt", required=True, type=Path)
    classify.add_argument(
        "--classification",
        required=True,
        choices=("provider_transient", "certified_host_infrastructure"),
    )
    classify.add_argument("--operator-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepare_plan(arguments.request, arguments.plan)
        elif arguments.command == "verify":
            verify_plan(arguments.plan)
        elif arguments.command == "execute":
            execute_gate(arguments.plan)
        elif arguments.command == "record-review":
            record_review(arguments.plan, arguments.review)
        elif arguments.command == "rerun":
            rerun_gate(arguments.plan)
        elif arguments.command == "classify":
            classify_failure(
                plan_path=arguments.plan,
                attempt_path=arguments.attempt,
                classification=arguments.classification,
                operator_id=arguments.operator_id,
            )
        else:  # pragma: no cover - argparse 封闭子命令
            _fail("command.invalid")
    except ReleaseGateFailure as failure:
        print(f"真实门禁失败：{failure.reason_code}", file=sys.stderr)
        return 1
    if arguments.command == "prepare":
        print("真实门禁方案已锁定")
    elif arguments.command == "execute":
        print("真实冷运行已通过，等待人工复核")
    elif arguments.command == "record-review":
        print("人工内容复核已不可变记录")
    elif arguments.command == "rerun":
        print("零请求缓存复跑已通过")
    elif arguments.command == "classify":
        print("同一候选复跑分类已不可变记录")
    else:
        print("真实门禁方案仍绑定同一候选")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
