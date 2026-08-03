#!/usr/bin/env python3
"""严格校验私有发布汇总，并独占封存可公开的发布证据。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_SOURCE_SCHEMA = "release_evidence_source.v1"
_EVIDENCE_SCHEMA = "release_evidence.v1"
_INSTALLATION_SCHEMA = "production-installation-manifest.v1"
_KEYLESS_SCHEMA = "keyless_gate_evidence.v1"
_INSTALLED_SCHEMA = "installed_acceptance_evidence.v1"
_DELIVERY_VALIDATION_SCHEMA = "independent_delivery_validation.v1"
_PLAN_SCHEMA = "release_gate_plan.v1"
_COLD_RECORD_SCHEMA = "release_gate_cold_run.v1"
_REVIEW_RECORD_SCHEMA = "release_gate_review_record.v1"
_ATTEMPT_SCHEMA = "release_gate_attempt.v1"
_CLASSIFICATION_SCHEMA = "release_gate_failure_classification.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RUN_ID = re.compile(
    r"run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_ATTEMPT_ID = re.compile(r"attempt-[0-9]{4}")
_SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CREDENTIAL_SHAPED_ID = re.compile(
    r"(?i)(?:sk-|gh[opsu]_|github_pat_|xox[baprs]-|AKIA|ASIA|AIza)"
)
_SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}")
_PACKAGE_NAME = re.compile(
    r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?"
)
_FFMPEG_VERSION = re.compile(
    r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)(?:\.[0-9]+){0,2}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.+~-]*)?"
)
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_PROVIDER_TRANSIENT_ERROR = re.compile(
    r"(?:transcription|topic_review|subtitle_optimization)\."
    r"(?:rate_limited|request_timeout|service_unavailable)"
)
_CACHE_NAMESPACES = (
    "transcript",
    "transcription_shard",
    "topic_review",
    "subtitle_optimization",
)
_PROVIDER_CAPABILITIES = (
    "transcription",
    "topic_review",
    "subtitle_optimization",
)
_KEYLESS_LAYERS = (
    "unit_schema",
    "module_interfaces",
    "adapter_contracts",
    "fault_injection",
    "installation_contract",
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
_INSTALLED_CASES = (
    "short_video_success",
    "effective_empty",
    "typed_failure",
    "overwrite",
    "rollback",
    "cache_maintenance",
    "sigint",
    "sigterm",
    "repeated_signal",
    "postcommit_signal",
)
_CACHE_FIELDS = frozenset(
    {
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
)
_DELIVERY_CHECKS = frozenset(
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
_MANUAL_CHECKS = frozenset(
    {
        "topic_complete",
        "boundaries_natural",
        "audio_video_normal",
        "subtitles_faithful_readable",
        "title_summary_grounded",
        "excluded_content_absent",
    }
)
_RETRY_CLASSIFICATIONS = frozenset(
    {"provider_transient", "certified_host_infrastructure"}
)
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
_CERTIFIED_MODELS = {
    "transcription": ("stepaudio", "stepaudio-2.5-asr"),
    "topic_review": ("stepfun", "step-2-mini"),
    "subtitle_optimization": ("stepfun", "step-2-mini"),
}
_KNOWN_LIMITATIONS = {
    "certified_platform_scope": "首次生产版本只认证 Ubuntu 24.04 amd64。",
}
_INSTALLED_CASE_EXPECTATIONS: dict[str, tuple[tuple[int, ...], int, int | None]] = {
    "short_video_success": ((0,), 1, 1),
    "effective_empty": ((0,), 1, 0),
    "typed_failure": ((30,), 1, None),
    "overwrite": ((60, 0), 2, None),
    "rollback": ((143,), 1, None),
    "cache_maintenance": ((0, 0, 10), 0, None),
    "sigint": ((130,), 1, None),
    "sigterm": ((143,), 1, None),
    "repeated_signal": ((130, 0), 1, None),
    "postcommit_signal": ((0,), 1, None),
}
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_INSTALLATION_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_WHEEL_BYTES = 128 * 1024 * 1024
_MAX_LOCK_BYTES = 2 * 1024 * 1024
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


class _DuplicateField(ValueError):
    pass


class EvidenceFailure(RuntimeError):
    """只携带可安全公开的稳定失败原因码。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateField(key)
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError


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
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        return False
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(named.st_mode)
        and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
    )


def _read_regular_file(
    path: Path,
    reason: str,
    *,
    maximum_bytes: int,
    required_mode: int | None = None,
    required_parent_mode: int | None = None,
) -> bytes:
    descriptor = -1
    parent_descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if required_parent_mode is not None:
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            if (
                stat.S_IMODE(os.fstat(parent_descriptor).st_mode)
                != required_parent_mode
                or not _directory_descriptor_matches_path(
                    parent_descriptor, path.parent
                )
            ):
                raise OSError
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum_bytes
            or (
                required_mode is not None
                and stat.S_IMODE(before.st_mode) != required_mode
            )
        ):
            raise OSError
        chunks = []
        observed_size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1))
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > maximum_bytes:
                raise OSError
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = (
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if parent_descriptor >= 0
            else path.stat(follow_symlinks=False)
        )
        if _file_snapshot(before) != _file_snapshot(after) or (
            observed_size != before.st_size
        ) or not stat.S_ISREG(named.st_mode) or (
            named.st_dev,
            named.st_ino,
            named.st_size,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise OSError
        if parent_descriptor >= 0 and not _directory_descriptor_matches_path(
            parent_descriptor, path.parent
        ):
            raise OSError
        return b"".join(chunks)
    except (OSError, ValueError):
        raise EvidenceFailure(reason) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _streaming_file_fact(
    path: Path,
    reason: str,
    *,
    required_mode: int | None = None,
    required_parent_mode: int | None = None,
) -> dict[str, Any]:
    descriptor = -1
    parent_descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if required_parent_mode is not None:
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            if (
                stat.S_IMODE(os.fstat(parent_descriptor).st_mode)
                != required_parent_mode
                or not _directory_descriptor_matches_path(
                    parent_descriptor, path.parent
                )
            ):
                raise OSError
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            required_mode is not None
            and stat.S_IMODE(before.st_mode) != required_mode
        ):
            raise OSError
        digest = hashlib.sha256()
        observed_size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            observed_size += len(chunk)
        after = os.fstat(descriptor)
        named = (
            os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if parent_descriptor >= 0
            else path.stat(follow_symlinks=False)
        )
        if (
            _file_snapshot(before) != _file_snapshot(after)
            or observed_size != before.st_size
            or not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino, named.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or (
                parent_descriptor >= 0
                and not _directory_descriptor_matches_path(
                    parent_descriptor, path.parent
                )
            )
        ):
            raise OSError
        return {
            "filename": path.name,
            "path": str(path),
            "sha256": digest.hexdigest(),
            "byte_length": observed_size,
        }
    except (OSError, ValueError):
        raise EvidenceFailure(reason) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)


def _load_json(
    path: Path,
    reason: str,
    *,
    maximum_bytes: int = _MAX_JSON_BYTES,
    required_mode: int | None = None,
    required_parent_mode: int | None = None,
) -> tuple[dict[str, Any], bytes]:
    contents = _read_regular_file(
        path,
        reason,
        maximum_bytes=maximum_bytes,
        required_mode=required_mode,
        required_parent_mode=required_parent_mode,
    )
    try:
        value = json.loads(
            contents,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise EvidenceFailure(reason) from None
    if not isinstance(value, dict):
        raise EvidenceFailure(reason)
    return value, contents


def _load_release_plan(
    path: Path,
    reason: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        metadata = path.stat(follow_symlinks=False)
        parent = path.parent.stat(follow_symlinks=False)
    except OSError:
        raise EvidenceFailure(reason) from None
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
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not stat.S_ISDIR(parent.st_mode)
        or (not private_draft and not sealed_copy)
    ):
        raise EvidenceFailure(reason)
    return _load_json(
        path,
        reason,
        required_mode=0o600 if private_draft else 0o440,
        required_parent_mode=0o700 if private_draft else 0o710,
    )


def _object(value: Any, fields: set[str] | frozenset[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise EvidenceFailure(reason)
    return value


def _array(value: Any, reason: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceFailure(reason)
    return value


def _string(value: Any, reason: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise EvidenceFailure(reason)
    if len(value) > 4096 or any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise EvidenceFailure(reason)
    return value


def _integer(value: Any, reason: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceFailure(reason)
    if value < (1 if positive else 0):
        raise EvidenceFailure(reason)
    return value


def _boolean(value: Any, reason: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceFailure(reason)
    return value


def _digest(value: Any, reason: str) -> str:
    value = _string(value, reason)
    if _SHA256.fullmatch(value) is None:
        raise EvidenceFailure(reason)
    return value


def _file_digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _commit(value: Any, reason: str) -> str:
    value = _string(value, reason)
    if _COMMIT_SHA.fullmatch(value) is None:
        raise EvidenceFailure(reason)
    return value


def _snapshot_id(value: Any, reason: str) -> str:
    value = _string(value, reason)
    if _SNAPSHOT_ID.fullmatch(value) is None:
        raise EvidenceFailure(reason)
    return value


def _run_id(value: Any, reason: str) -> str:
    value = _string(value, reason)
    if _RUN_ID.fullmatch(value) is None:
        raise EvidenceFailure(reason)
    return value


def _timestamp(value: Any, reason: str) -> str:
    value = _string(value, reason)
    if not value.endswith("Z"):
        raise EvidenceFailure(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EvidenceFailure(reason) from None
    if parsed.tzinfo != timezone.utc:
        raise EvidenceFailure(reason)
    return value


def _timestamp_value(value: Any, reason: str) -> datetime:
    normalized = _timestamp(value, reason)
    return datetime.fromisoformat(normalized[:-1] + "+00:00")


def _url(value: Any, reason: str) -> str:
    value = _string(value, reason)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise EvidenceFailure(reason) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.fullmatch(
            r"/dulltackle/long-video-autocut/actions/runs/[1-9][0-9]*"
            r"(?:/attempts/[1-9][0-9]*)?",
            parsed.path,
        )
        is None
    ):
        raise EvidenceFailure(reason)
    return value


def _safe_filename(
    value: Any,
    reason: str,
    *,
    suffix: str | None = None,
) -> str:
    filename = _string(value, reason)
    if (
        _SAFE_FILENAME.fullmatch(filename) is None
        or _CREDENTIAL_SHAPED_ID.search(filename) is not None
        or (suffix is not None and not filename.endswith(suffix))
    ):
        raise EvidenceFailure(reason)
    return filename


def _public_text(value: Any, reason: str) -> str:
    text = _string(value, reason)
    if _CREDENTIAL_SHAPED_ID.search(text) is not None:
        raise EvidenceFailure(reason)
    return text


def _public_chinese_summary(value: Any, reason: str) -> str:
    summary = _public_text(value, reason)
    if (
        len(summary) > 500
        or re.search(r"[\u3400-\u9fff]", summary) is None
        or "/" in summary
        or "\\" in summary
        or any(ord(character) < 0x20 for character in summary)
    ):
        raise EvidenceFailure(reason)
    return summary


def _public_identifier(value: Any, reason: str) -> str:
    identifier = _string(value, reason)
    if (
        _SAFE_ID.fullmatch(identifier) is None
        or _CREDENTIAL_SHAPED_ID.search(identifier) is not None
    ):
        raise EvidenceFailure(reason)
    return identifier


def _absolute_manifest_path(value: Any, reason: str) -> str:
    path_value = _string(value, reason)
    try:
        path = Path(path_value)
    except (OSError, ValueError):
        raise EvidenceFailure(reason) from None
    if not path.is_absolute() or str(path) != path_value or ".." in path.parts:
        raise EvidenceFailure(reason)
    return path_value


def _artifact_summary(
    path: Path,
    declared: Mapping[str, Any],
    reason: str,
    *,
    maximum_bytes: int,
) -> tuple[dict[str, str], bytes]:
    value = _object(declared, {"filename", "sha256"}, reason)
    contents = _read_regular_file(path, reason, maximum_bytes=maximum_bytes)
    filename = _string(value["filename"], reason)
    digest = _digest(value["sha256"], reason)
    if filename != path.name or digest != _file_digest(contents):
        raise EvidenceFailure(reason)
    return {"filename": filename, "sha256": digest}, contents


def _validate_candidate_wheel(
    contents: bytes,
    candidate: Mapping[str, Any],
) -> None:
    reason = "candidate.wheel_invalid"
    _safe_filename(candidate["wheel_filename"], reason, suffix=".whl")
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError
            metadata_names = [
                name
                for name in names
                if name.count("/") == 1
                and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError
            information = archive.getinfo(metadata_names[0])
            if information.file_size > 1024 * 1024:
                raise ValueError
            raw_metadata = archive.read(information).decode("utf-8")
        metadata = Parser().parsestr(raw_metadata)
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        KeyError,
    ):
        raise EvidenceFailure(reason) from None
    if (
        metadata.get_all("Name") != ["video-auto-editor"]
        or metadata.get_all("Version") != [candidate["application_version"]]
    ):
        raise EvidenceFailure(reason)


def _validate_network(value: Any, reason: str) -> dict[str, Any]:
    network = _object(
        value,
        {"external_blocked", "loopback_allowed", "mode"},
        reason,
    )
    mode = _string(network["mode"], reason)
    if (
        network["external_blocked"] is not True
        or network["loopback_allowed"] is not True
        or mode != "network_namespace"
    ):
        raise EvidenceFailure(reason)
    return {
        "external_blocked": True,
        "loopback_allowed": True,
        "mode": mode,
    }


def _validate_source(value: dict[str, Any]) -> dict[str, Any]:
    reason = "source.schema_invalid"
    source = _object(
        value,
        {
            "schema_version",
            "candidate",
            "locks",
            "apt_snapshot_id",
            "automatic_gate_runs",
            "inputs",
            "runs",
            "independent_validations",
            "semantic_equivalence",
            "manual_review",
            "retry_attempts",
            "known_limitations",
        },
        reason,
    )
    if source["schema_version"] != _SOURCE_SCHEMA:
        raise EvidenceFailure(reason)
    candidate = _object(
        source["candidate"],
        {"application_version", "commit_sha", "wheel_filename", "wheel_sha256"},
        reason,
    )
    application_version = _string(candidate["application_version"], reason)
    if _VERSION.fullmatch(application_version) is None:
        raise EvidenceFailure(reason)
    candidate = {
        "application_version": application_version,
        "commit_sha": _commit(candidate["commit_sha"], reason),
        "wheel_filename": _string(candidate["wheel_filename"], reason),
        "wheel_sha256": _digest(candidate["wheel_sha256"], reason),
    }
    locks = _object(source["locks"], {"build", "runtime"}, reason)
    for name in ("build", "runtime"):
        locks[name] = _object(locks[name], {"filename", "sha256"}, reason)
        _string(locks[name]["filename"], reason)
        _digest(locks[name]["sha256"], reason)
    snapshot = _snapshot_id(source["apt_snapshot_id"], reason)
    gate_runs = _object(
        source["automatic_gate_runs"],
        {"keyless", "installed_acceptance"},
        reason,
    )
    for name in ("keyless", "installed_acceptance"):
        gate = _object(gate_runs[name], {"url"}, reason)
        gate_runs[name] = {"url": _url(gate["url"], reason)}
    if gate_runs["keyless"]["url"] != gate_runs["installed_acceptance"]["url"]:
        raise EvidenceFailure(reason)
    inputs = _validate_inputs(source["inputs"], reason)
    runs = _object(source["runs"], {"cold", "warm"}, reason)
    runs = {
        "cold": _validate_run(runs["cold"], warm=False),
        "warm": _validate_run(runs["warm"], warm=True),
    }
    _validate_run_pair(runs, reason)
    if any(
        run["environment"]["application_version"]
        != candidate["application_version"]
        for run in runs.values()
    ):
        raise EvidenceFailure(reason)
    validations = _object(
        source["independent_validations"], {"cold", "warm"}, reason
    )
    validations = {
        name: _validate_delivery_validation(validations[name], runs[name])
        for name in ("cold", "warm")
    }
    equivalence = _validate_semantic_equivalence(
        source["semantic_equivalence"], reason
    )
    manual_review = _validate_manual_review(
        source["manual_review"],
        runs["cold"]["run_id"],
        runs["cold"]["delivery"]["short_video_count"],
        reason,
    )
    retry_attempts = _validate_retry_attempts(
        source["retry_attempts"],
        candidate,
        {runs["cold"]["run_id"], runs["warm"]["run_id"]},
        reason,
    )
    limitations = _array(source["known_limitations"], reason)
    limitations = [_validate_known_limitation(item, reason) for item in limitations]
    if len(limitations) != len(_KNOWN_LIMITATIONS) or {
        item["code"] for item in limitations
    } != set(_KNOWN_LIMITATIONS):
        raise EvidenceFailure(reason)
    return {
        "candidate": candidate,
        "locks": locks,
        "apt_snapshot_id": snapshot,
        "automatic_gate_runs": gate_runs,
        "inputs": inputs,
        "runs": runs,
        "independent_validations": validations,
        "semantic_equivalence": equivalence,
        "manual_review": manual_review,
        "retry_attempts": retry_attempts,
        "known_limitations": limitations,
    }


def _validate_known_limitation(value: Any, reason: str) -> dict[str, str]:
    limitation = _object(value, {"code", "statement"}, reason)
    code = _string(limitation["code"], reason)
    statement = _string(limitation["statement"], reason)
    if _KNOWN_LIMITATIONS.get(code) != statement:
        raise EvidenceFailure(reason)
    return {"code": code, "statement": statement}


def _validate_inputs(value: Any, reason: str) -> dict[str, Any]:
    inputs = _object(
        value,
        {
            "source",
            "configuration",
            "course_context",
            "expected_transcript",
        },
        reason,
    )
    source = _object(
        inputs["source"],
        {
            "asset_id",
            "version",
            "language",
            "content_summary",
            "sha256",
            "byte_length",
            "duration_ms",
        },
        reason,
    )
    asset_id = _public_identifier(source["asset_id"], reason)
    version = _public_identifier(source["version"], reason)
    language = _string(source["language"], reason)
    content_summary = _public_chinese_summary(
        source["content_summary"], reason
    )
    if language != "zh-CN":
        raise EvidenceFailure(reason)
    result: dict[str, Any] = {
        "source": {
            "asset_id": asset_id,
            "version": version,
            "language": language,
            "content_summary": content_summary,
            "sha256": _digest(source["sha256"], reason),
            "byte_length": _integer(source["byte_length"], reason, positive=True),
            "duration_ms": _integer(source["duration_ms"], reason, positive=True),
        }
    }
    for name, schema in (
        ("configuration", "configuration.v1"),
        ("course_context", "course_context.v1"),
        (
            "expected_transcript",
            "installed_acceptance_transcript.v1",
        ),
    ):
        summary = _object(inputs[name], {"schema_version", "sha256"}, reason)
        if summary["schema_version"] != schema:
            raise EvidenceFailure(reason)
        result[name] = {
            "schema_version": schema,
            "sha256": _digest(summary["sha256"], reason),
        }
    return result


def _validate_run(value: Any, *, warm: bool) -> dict[str, Any]:
    reason = "source.run_invalid"
    run = _object(
        value,
        {
            "run_id",
            "terminal",
            "diagnostic_manifest_sha256",
            "delivery",
            "environment",
            "configuration",
            "providers",
            "cache",
        },
        reason,
    )
    terminal_value = _object(
        run["terminal"], {"outcome", "exit_code", "result_kind"}, reason
    )
    terminal = {
        "outcome": _string(terminal_value["outcome"], reason),
        "exit_code": _integer(terminal_value["exit_code"], reason),
        "result_kind": _string(terminal_value["result_kind"], reason),
    }
    if terminal != {
        "outcome": "succeeded",
        "exit_code": 0,
        "result_kind": "clips",
    }:
        raise EvidenceFailure(reason)
    delivery = _object(
        run["delivery"],
        {"manifest_sha256", "result_kind", "artifact_count", "short_video_count"},
        reason,
    )
    if delivery["result_kind"] != "clips":
        raise EvidenceFailure(reason)
    delivery = {
        "manifest_sha256": _digest(delivery["manifest_sha256"], reason),
        "result_kind": "clips",
        "artifact_count": _integer(delivery["artifact_count"], reason, positive=True),
        "short_video_count": _integer(
            delivery["short_video_count"], reason, positive=True
        ),
    }
    environment_value = _object(
        run["environment"],
        {
            "certified_platform",
            "python_version",
            "application_version",
            "ffmpeg_version",
            "ffprobe_version",
            "font_family",
            "preflight_outcome",
            "installation_fingerprint",
        },
        reason,
    )
    python_version = _string(environment_value["python_version"], reason)
    try:
        parsed_python_version = tuple(
            int(part) for part in python_version.split(".")
        )
    except ValueError:
        raise EvidenceFailure(reason) from None
    ffmpeg_version = _string(environment_value["ffmpeg_version"], reason)
    installation_fingerprint = _string(
        environment_value["installation_fingerprint"], reason
    )
    if (
        environment_value["certified_platform"]
        != "ubuntu_24_04_amd64"
        or _PYTHON_VERSION.fullmatch(python_version) is None
        or not (
            _CERTIFIED_PYTHON_MINIMUM
            <= parsed_python_version
            < _CERTIFIED_PYTHON_MAXIMUM
        )
        or _VERSION.fullmatch(
            _string(environment_value["application_version"], reason)
        )
        is None
        or _FFMPEG_VERSION.fullmatch(ffmpeg_version) is None
        or environment_value["ffprobe_version"] != ffmpeg_version
        or environment_value["font_family"] != "Noto Sans CJK SC"
        or environment_value["preflight_outcome"] != "succeeded"
        or not installation_fingerprint.startswith("sha256:")
    ):
        raise EvidenceFailure(reason)
    _digest(installation_fingerprint.removeprefix("sha256:"), reason)
    environment = {
        key: environment_value[key]
        for key in (
            "certified_platform",
            "python_version",
            "application_version",
            "ffmpeg_version",
            "ffprobe_version",
            "font_family",
            "preflight_outcome",
            "installation_fingerprint",
        )
    }
    configuration_value = _object(
        run["configuration"],
        {"configuration_fingerprint", "course_context"},
        reason,
    )
    configuration_fingerprint = _string(
        configuration_value["configuration_fingerprint"], reason
    )
    if not configuration_fingerprint.startswith("sha256:"):
        raise EvidenceFailure(reason)
    _digest(configuration_fingerprint.removeprefix("sha256:"), reason)
    context_value = _object(
        configuration_value["course_context"],
        {
            "provided",
            "attribution_provided",
            "priority_topic_count",
            "excluded_content_count",
        },
        reason,
    )
    configuration = {
        "configuration_fingerprint": configuration_fingerprint,
        "course_context": {
            "provided": _boolean(context_value["provided"], reason),
            "attribution_provided": _boolean(
                context_value["attribution_provided"], reason
            ),
            "priority_topic_count": _integer(
                context_value["priority_topic_count"], reason
            ),
            "excluded_content_count": _integer(
                context_value["excluded_content_count"], reason
            ),
        },
    }
    providers = _object(run["providers"], set(_PROVIDER_CAPABILITIES), reason)
    providers = {
        capability: _validate_provider(
            providers[capability],
            capability=capability,
            warm=warm,
        )
        for capability in _PROVIDER_CAPABILITIES
    }
    caches = _object(run["cache"], set(_CACHE_NAMESPACES), reason)
    caches = {
        namespace: _validate_cache(
            caches[namespace],
            namespace=namespace,
            warm=warm,
        )
        for namespace in _CACHE_NAMESPACES
    }
    return {
        "run_id": _run_id(run["run_id"], reason),
        "terminal": terminal,
        "diagnostic_manifest_sha256": _digest(
            run["diagnostic_manifest_sha256"], reason
        ),
        "delivery": delivery,
        "environment": environment,
        "configuration": configuration,
        "providers": providers,
        "cache": caches,
    }


def _validate_provider(
    value: Any,
    *,
    capability: str,
    warm: bool,
) -> dict[str, Any]:
    reason = "source.provider_invalid"
    provider = _object(value, {"provider_id", "model_id", "requests"}, reason)
    provider_id = _string(provider["provider_id"], reason)
    model_id = _string(provider["model_id"], reason)
    if (provider_id, model_id) != _CERTIFIED_MODELS[capability]:
        raise EvidenceFailure(reason)
    requests = _object(
        provider["requests"],
        {"count", "succeeded", "failed", "attempt_count_total"},
        reason,
    )
    requests = {name: _integer(requests[name], reason) for name in requests}
    count = requests["count"]
    if (
        requests["succeeded"] != count
        or requests["failed"] != 0
        or requests["attempt_count_total"] < count
        or (warm and (count != 0 or requests["attempt_count_total"] != 0))
        or (not warm and count == 0)
    ):
        raise EvidenceFailure(reason)
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "requests": requests,
    }


def _validate_cache(
    value: Any,
    *,
    namespace: str,
    warm: bool,
) -> dict[str, int]:
    reason = "source.cache_invalid"
    stats = _object(value, _CACHE_FIELDS, reason)
    result = {name: _integer(stats[name], reason) for name in _CACHE_FIELDS}
    if result["queries"] != (
        result["hits"] + result["misses"] + result["corrupt_quarantined"]
    ):
        raise EvidenceFailure(reason)
    if result["corrupt_quarantined"] or result["infrastructure_failures"]:
        raise EvidenceFailure(reason)
    if warm and namespace == "transcription_shard":
        valid = all(count == 0 for count in result.values())
    elif warm:
        valid = (
            result["queries"] > 0
            and result["hits"] == result["queries"]
            and result["misses"] == 0
            and result["writes_published"] == 0
            and result["writes_already_present"] == 0
        )
    else:
        valid = (
            result["queries"] > 0
            and result["hits"] == 0
            and result["misses"] == result["queries"]
            and result["writes_published"] > 0
            and result["writes_already_present"] == 0
        )
    if not valid:
        raise EvidenceFailure(reason)
    return result


def _validate_run_pair(runs: Mapping[str, Any], reason: str) -> None:
    cold = runs["cold"]
    warm = runs["warm"]
    if (
        cold["run_id"] == warm["run_id"]
        or cold["delivery"]["short_video_count"]
        != warm["delivery"]["short_video_count"]
        or cold["delivery"]["artifact_count"]
        != warm["delivery"]["artifact_count"]
        or cold["environment"] != warm["environment"]
        or cold["configuration"] != warm["configuration"]
    ):
        raise EvidenceFailure(reason)
    for capability in _PROVIDER_CAPABILITIES:
        if {
            key: cold["providers"][capability][key]
            for key in ("provider_id", "model_id")
        } != {
            key: warm["providers"][capability][key]
            for key in ("provider_id", "model_id")
        }:
            raise EvidenceFailure(reason)


def _validate_delivery_validation(
    value: Any, run: Mapping[str, Any]
) -> dict[str, Any]:
    reason = "source.independent_validation_invalid"
    validation = _object(
        value,
        {
            "schema_version",
            "success",
            "run_id",
            "result_kind",
            "short_video_count",
            "artifact_count",
            "evidence_sha256",
            "checks",
        },
        reason,
    )
    checks = _object(validation["checks"], _DELIVERY_CHECKS, reason)
    if (
        validation["schema_version"] != _DELIVERY_VALIDATION_SCHEMA
        or validation["success"] is not True
        or _run_id(validation["run_id"], reason) != run["run_id"]
        or validation["result_kind"] != run["delivery"]["result_kind"]
        or _integer(validation["short_video_count"], reason, positive=True)
        != run["delivery"]["short_video_count"]
        or _integer(validation["artifact_count"], reason, positive=True)
        != run["delivery"]["artifact_count"]
        or any(checks[name] is not True for name in _DELIVERY_CHECKS)
    ):
        raise EvidenceFailure(reason)
    return {
        "schema_version": _DELIVERY_VALIDATION_SCHEMA,
        "success": True,
        "run_id": run["run_id"],
        "result_kind": run["delivery"]["result_kind"],
        "short_video_count": run["delivery"]["short_video_count"],
        "artifact_count": run["delivery"]["artifact_count"],
        "evidence_sha256": _digest(validation["evidence_sha256"], reason),
        "checks": {name: True for name in sorted(_DELIVERY_CHECKS)},
    }


def _validate_semantic_equivalence(value: Any, reason: str) -> dict[str, Any]:
    equivalence = _object(
        value,
        {"equivalent", "cold_projection_sha256", "warm_projection_sha256"},
        reason,
    )
    cold = _digest(equivalence["cold_projection_sha256"], reason)
    warm = _digest(equivalence["warm_projection_sha256"], reason)
    if equivalence["equivalent"] is not True or cold != warm:
        raise EvidenceFailure(reason)
    return {
        "equivalent": True,
        "cold_projection_sha256": cold,
        "warm_projection_sha256": warm,
    }


def _validate_manual_review(
    value: Any,
    cold_run_id: str,
    short_video_count: int,
    reason: str,
) -> dict[str, Any]:
    review = _object(
        value,
        {
            "schema_version",
            "operator_id",
            "reviewed_at",
            "run_id",
            "source_and_transcript_compared",
            "clips",
            "conclusion",
        },
        reason,
    )
    operator_id = _string(review["operator_id"], reason)
    if (
        review["schema_version"] != "release_gate_manual_review.v1"
        or _SAFE_ID.fullmatch(operator_id) is None
        or _run_id(review["run_id"], reason) != cold_run_id
        or review["source_and_transcript_compared"] is not True
        or review["conclusion"] != "passed"
    ):
        raise EvidenceFailure(reason)
    clips = _array(review["clips"], reason)
    if len(clips) != short_video_count:
        raise EvidenceFailure(reason)
    normalized = []
    for expected_ordinal, item in enumerate(clips, start=1):
        clip = _object(item, {"ordinal", "checks"}, reason)
        checks = _object(clip["checks"], _MANUAL_CHECKS, reason)
        if _integer(clip["ordinal"], reason, positive=True) != expected_ordinal or any(
            checks[name] is not True for name in _MANUAL_CHECKS
        ):
            raise EvidenceFailure(reason)
        normalized.append(
            {
                "ordinal": expected_ordinal,
                "checks": {name: True for name in sorted(_MANUAL_CHECKS)},
            }
        )
    return {
        "schema_version": "release_gate_manual_review.v1",
        "operator_id_sha256": hashlib.sha256(operator_id.encode("utf-8")).hexdigest(),
        "reviewed_at": _timestamp(review["reviewed_at"], reason),
        "run_id": cold_run_id,
        "source_and_transcript_compared": True,
        "conclusion": "passed",
        "reviewed_short_video_count": short_video_count,
        "clips": normalized,
    }


def _validate_retry_attempts(
    value: Any,
    candidate: Mapping[str, Any],
    successful_run_ids: set[str],
    reason: str,
) -> list[dict[str, Any]]:
    attempts = _array(value, reason)
    normalized = []
    observed_ids = set(successful_run_ids)
    for item in attempts:
        attempt = _object(
            item,
            {
                "run_id",
                "occurred_at",
                "classification",
                "stable_error_code",
                "candidate",
            },
            reason,
        )
        run = _run_id(attempt["run_id"], reason)
        classification = _string(attempt["classification"], reason)
        error_code = _string(attempt["stable_error_code"], reason)
        attempt_candidate = _object(
            attempt["candidate"], {"commit_sha", "wheel_sha256"}, reason
        )
        error_matches_classification = (
            classification == "provider_transient"
            and _PROVIDER_TRANSIENT_ERROR.fullmatch(error_code) is not None
        ) or (
            classification == "certified_host_infrastructure"
            and error_code == "independent_validation.launch_failed"
        )
        if (
            run in observed_ids
            or classification not in _RETRY_CLASSIFICATIONS
            or _ERROR_CODE.fullmatch(error_code) is None
            or not error_matches_classification
            or _commit(attempt_candidate["commit_sha"], reason)
            != candidate["commit_sha"]
            or _digest(attempt_candidate["wheel_sha256"], reason)
            != candidate["wheel_sha256"]
        ):
            raise EvidenceFailure(reason)
        observed_ids.add(run)
        normalized.append(
            {
                "run_id": run,
                "occurred_at": _timestamp(attempt["occurred_at"], reason),
                "classification": classification,
                "stable_error_code": error_code,
                "candidate": dict(attempt_candidate),
            }
        )
    return normalized


def _validate_installation(
    value: dict[str, Any],
    *,
    reason: str = "installation_manifest.invalid",
) -> dict[str, Any]:
    manifest = _object(
        value,
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
        },
        reason,
    )
    if manifest["schema_version"] != _INSTALLATION_SCHEMA:
        raise EvidenceFailure(reason)
    application = _object(
        manifest["application"], {"name", "version", "wheel"}, reason
    )
    wheel = _object(application["wheel"], {"filename", "sha256"}, reason)
    application_version = _string(application["version"], reason)
    if (
        application["name"] != "video-auto-editor"
        or _VERSION.fullmatch(application_version) is None
    ):
        raise EvidenceFailure(reason)
    environment = _object(
        manifest["environment"],
        {"ffmpeg_version", "ffprobe_version", "font_family", "font_file"},
        reason,
    )
    platform = _object(
        manifest["platform"],
        {"architecture", "operating_system", "operating_system_version"},
        reason,
    )
    python = _object(manifest["python"], {"implementation", "version"}, reason)
    python_implementation = _string(python["implementation"], reason)
    python_version = _string(python["version"], reason)
    if _PYTHON_VERSION.fullmatch(python_version) is None:
        raise EvidenceFailure(reason)
    parsed_python_version = tuple(int(part) for part in python_version.split("."))
    if platform != {
        "architecture": "amd64",
        "operating_system": "ubuntu",
        "operating_system_version": "24.04",
    } or (
        python_implementation != "CPython"
        or not (
            _CERTIFIED_PYTHON_MINIMUM
            <= parsed_python_version
            < _CERTIFIED_PYTHON_MAXIMUM
        )
    ):
        raise EvidenceFailure(reason)
    installation_prefix = _absolute_manifest_path(
        manifest["installation_prefix"], reason
    )
    environment_values = {
        "ffmpeg_version": _public_text(environment["ffmpeg_version"], reason),
        "ffprobe_version": _public_text(environment["ffprobe_version"], reason),
        "font_family": _string(environment["font_family"], reason),
    }
    ffmpeg_match = _FFMPEG_VERSION.fullmatch(
        environment_values["ffmpeg_version"]
    )
    if (
        ffmpeg_match is None
        or environment_values["ffmpeg_version"]
        != environment_values["ffprobe_version"]
        or not (
            (6, 1)
            <= (
                int(ffmpeg_match.group("major")),
                int(ffmpeg_match.group("minor")),
            )
            < (7, 0)
        )
        or environment_values["font_family"] != "Noto Sans CJK SC"
    ):
        raise EvidenceFailure(reason)
    _absolute_manifest_path(environment["font_file"], reason)
    runtime_lock = _object(
        manifest["runtime_lock"], {"filename", "sha256"}, reason
    )
    runtime_lock_summary = {
        "filename": _safe_filename(runtime_lock["filename"], reason),
        "sha256": _digest(runtime_lock["sha256"], reason),
    }
    if runtime_lock_summary["filename"] != "requirements-runtime.lock":
        raise EvidenceFailure(reason)
    snapshot_packages = _package_map(manifest["snapshot_packages"], reason)
    system_packages = _package_map(manifest["system_packages"], reason)
    if (
        set(snapshot_packages) != _CERTIFIED_SNAPSHOT_PACKAGES
        or any(
            system_packages.get(name) != version
            for name, version in snapshot_packages.items()
        )
    ):
        raise EvidenceFailure(reason)
    wheelhouse = []
    for item in _array(manifest["wheelhouse"], reason):
        artifact = _object(item, {"filename", "sha256"}, reason)
        wheelhouse.append(
            {
                "filename": _safe_filename(
                    artifact["filename"], reason, suffix=".whl"
                ),
                "sha256": _digest(artifact["sha256"], reason),
            }
        )
    wheelhouse_filenames = [artifact["filename"] for artifact in wheelhouse]
    if (
        wheelhouse_filenames != sorted(wheelhouse_filenames)
        or len(set(wheelhouse_filenames)) != len(wheelhouse_filenames)
    ):
        raise EvidenceFailure(reason)
    return {
        "application": {
            "name": "video-auto-editor",
            "version": application_version,
            "wheel": {
                "filename": _safe_filename(
                    wheel["filename"], reason, suffix=".whl"
                ),
                "sha256": _digest(wheel["sha256"], reason),
            },
        },
        "apt_snapshot_id": _snapshot_id(manifest["apt_snapshot_id"], reason),
        "installation_prefix": installation_prefix,
        "environment": environment_values,
        "platform": {
            key: _string(platform[key], reason)
            for key in ("architecture", "operating_system", "operating_system_version")
        },
        "python": {
            "implementation": python_implementation,
            "version": python_version,
        },
        "runtime_lock": runtime_lock_summary,
        "snapshot_packages": snapshot_packages,
        "system_packages": system_packages,
        "wheelhouse": wheelhouse,
    }


def _package_map(value: Any, reason: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise EvidenceFailure(reason)
    packages: dict[str, str] = {}
    for name in sorted(value):
        if (
            not isinstance(name, str)
            or _PACKAGE_NAME.fullmatch(name) is None
            or _CREDENTIAL_SHAPED_ID.search(name) is not None
        ):
            raise EvidenceFailure(reason)
        packages[name] = _public_text(value[name], reason)
    return packages


def _validate_keyless(value: dict[str, Any]) -> dict[str, Any]:
    reason = "keyless_evidence.invalid"
    evidence = _object(
        value,
        {
            "schema_version",
            "candidate",
            "credential_mode",
            "network",
            "layers",
            "release_tools",
            "success",
        },
        reason,
    )
    if (
        evidence["schema_version"] != _KEYLESS_SCHEMA
        or evidence["credential_mode"] != "absent"
        or evidence["success"] is not True
    ):
        raise EvidenceFailure(reason)
    candidate = _object(
        evidence["candidate"], {"commit_sha", "wheel_filename", "wheel_sha256"}, reason
    )
    candidate = {
        "commit_sha": _commit(candidate["commit_sha"], reason),
        "wheel_filename": _string(candidate["wheel_filename"], reason),
        "wheel_sha256": _digest(candidate["wheel_sha256"], reason),
    }
    layers = _object(evidence["layers"], set(_KEYLESS_LAYERS), reason)
    release_tools = _object(
        evidence["release_tools"], set(_RELEASE_TOOLS), reason
    )
    release_tools = {
        name: _digest(release_tools[name], reason) for name in _RELEASE_TOOLS
    }
    normalized_layers = {}
    total = 0
    for name in _KEYLESS_LAYERS:
        outcome = _object(
            layers[name],
            {
                "collected",
                "passed",
                "failed",
                "errors",
                "deselected",
                "skipped",
                "xfail",
                "xpass",
                "retries",
                "exit_code",
            },
            reason,
        )
        outcome = {field: _integer(outcome[field], reason) for field in outcome}
        if (
            outcome["collected"] == 0
            or outcome["passed"] != outcome["collected"]
            or any(
                outcome[field] != 0
                for field in (
                    "failed",
                    "errors",
                    "deselected",
                    "skipped",
                    "xfail",
                    "xpass",
                    "retries",
                    "exit_code",
                )
            )
        ):
            raise EvidenceFailure(reason)
        normalized_layers[name] = outcome
        total += outcome["collected"]
    return {
        "candidate": candidate,
        "credential_mode": "absent",
        "network": _validate_network(evidence["network"], reason),
        "layers": normalized_layers,
        "release_tools": release_tools,
        "statistics": {"collected": total, "passed": total},
    }


def _validate_installed(value: dict[str, Any]) -> dict[str, Any]:
    reason = "installed_evidence.invalid"
    evidence = _object(
        value,
        {
            "schema_version",
            "candidate",
            "installation",
            "network",
            "cases",
            "statistics",
            "success",
        },
        reason,
    )
    if evidence["schema_version"] != _INSTALLED_SCHEMA or evidence["success"] is not True:
        raise EvidenceFailure(reason)
    candidate = _object(
        evidence["candidate"],
        {
            "apt_snapshot_id",
            "commit_sha",
            "runtime_lock_filename",
            "runtime_lock_sha256",
            "wheel_filename",
            "wheel_sha256",
        },
        reason,
    )
    candidate = {
        "apt_snapshot_id": _string(candidate["apt_snapshot_id"], reason),
        "commit_sha": _commit(candidate["commit_sha"], reason),
        "runtime_lock_filename": _string(candidate["runtime_lock_filename"], reason),
        "runtime_lock_sha256": _digest(candidate["runtime_lock_sha256"], reason),
        "wheel_filename": _string(candidate["wheel_filename"], reason),
        "wheel_sha256": _digest(candidate["wheel_sha256"], reason),
    }
    installation = _object(
        evidence["installation"],
        {
            "application_version",
            "console",
            "environment_prefix",
            "manifest_sha256",
            "prefix",
            "python",
            "verified",
        },
        reason,
    )
    if installation["verified"] is not True:
        raise EvidenceFailure(reason)
    application_version = _string(installation["application_version"], reason)
    if _VERSION.fullmatch(application_version) is None:
        raise EvidenceFailure(reason)
    prefix = _absolute_manifest_path(installation["prefix"], reason)
    environment_prefix = _absolute_manifest_path(
        installation["environment_prefix"], reason
    )
    console = _absolute_manifest_path(installation["console"], reason)
    python_path = _absolute_manifest_path(installation["python"], reason)
    expected_environment = str(
        Path(prefix) / "versions" / application_version / "venv"
    )
    if (
        environment_prefix != expected_environment
        or console != str(Path(expected_environment) / "bin" / "video-auto-editor")
        or python_path != str(Path(expected_environment) / "bin" / "python")
    ):
        raise EvidenceFailure(reason)
    manifest_sha = _digest(installation["manifest_sha256"], reason)
    cases = _object(evidence["cases"], set(_INSTALLED_CASES), reason)
    normalized_cases = {}
    observed_run_ids: set[str] = set()
    for case_id in _INSTALLED_CASES:
        expected_exits, expected_run_count, expected_short_video_count = (
            _INSTALLED_CASE_EXPECTATIONS[case_id]
        )
        fields = {"exit_codes", "run_ids", "status"}
        if expected_short_video_count is not None:
            fields.add("short_video_count")
        case = _object(cases[case_id], fields, reason)
        if case["status"] != "passed":
            raise EvidenceFailure(reason)
        exit_codes = [
            _integer(item, reason) for item in _array(case["exit_codes"], reason)
        ]
        run_ids = [
            _run_id(item, reason) for item in _array(case["run_ids"], reason)
        ]
        if (
            exit_codes != list(expected_exits)
            or len(run_ids) != expected_run_count
            or any(run_id in observed_run_ids for run_id in run_ids)
        ):
            raise EvidenceFailure(reason)
        observed_run_ids.update(run_ids)
        normalized = {
            "exit_codes": exit_codes,
            "run_ids": run_ids,
            "status": "passed",
        }
        if expected_short_video_count is not None:
            if (
                _integer(case["short_video_count"], reason)
                != expected_short_video_count
            ):
                raise EvidenceFailure(reason)
            normalized["short_video_count"] = expected_short_video_count
        normalized_cases[case_id] = normalized
    statistics = _object(
        evidence["statistics"], {"failed", "passed", "total"}, reason
    )
    statistics = {name: _integer(statistics[name], reason) for name in statistics}
    if statistics != {"failed": 0, "passed": len(cases), "total": len(cases)}:
        raise EvidenceFailure(reason)
    return {
        "candidate": candidate,
        "installation": {
            "application_version": application_version,
            "manifest_sha256": manifest_sha,
            "prefix": prefix,
            "verified": True,
        },
        "network": _validate_network(evidence["network"], reason),
        "cases": normalized_cases,
        "statistics": statistics,
    }


def _canonical_existing_path(value: Any, reason: str) -> Path:
    raw = _string(value, reason)
    path = Path(raw)
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise OSError
    except (OSError, RuntimeError):
        raise EvidenceFailure(reason) from None
    return path


def _trusted_release_tool_path(
    path: Path,
    *,
    expected_path: Path,
    reason: str,
) -> Path:
    try:
        canonical_expected = expected_path.resolve(strict=True)
        metadata = path.stat(follow_symlinks=False)
        parent = path.parent.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        raise EvidenceFailure(reason) from None
    if (
        path != canonical_expected
        or path.name != expected_path.name
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_mode & 0o022
    ):
        raise EvidenceFailure(reason)
    return path


def _validate_root_owned_installation_layout(
    *,
    prefix: Path,
    version: str,
    manifest_path: Path,
    ready_path: Path,
    console_path: Path,
    reason: str,
) -> None:
    version_directory = prefix / "versions" / version
    expected_console = version_directory / "venv" / "bin" / "video-auto-editor"
    if (
        manifest_path != version_directory / "installation-manifest.json"
        or ready_path != version_directory / "READY"
        or console_path != expected_console
    ):
        raise EvidenceFailure(reason)
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
                raise EvidenceFailure(reason)
    except OSError:
        raise EvidenceFailure(reason) from None
    if not os.access(console_path, os.X_OK):
        raise EvidenceFailure(reason)


def _validate_plan_file_fact(
    value: Any,
    reason: str,
    *,
    expected_path: Path | None = None,
) -> tuple[dict[str, str], Path]:
    fact = _object(value, {"filename", "path", "sha256"}, reason)
    path = _canonical_existing_path(fact["path"], reason)
    observed = _streaming_file_fact(path, reason)
    normalized = {
        "filename": _safe_filename(fact["filename"], reason),
        "path": str(path),
        "sha256": _digest(fact["sha256"], reason),
    }
    if (
        expected_path is not None
        and path != expected_path.resolve(strict=True)
    ) or any(normalized[field] != observed[field] for field in normalized):
        raise EvidenceFailure(reason)
    return normalized, path


def _input_fingerprint(plan_inputs: Mapping[str, Any]) -> str:
    payload = {
        "source": plan_inputs["source"]["sha256"],
        "configuration": plan_inputs["configuration"]["sha256"],
        "course_context": plan_inputs["course_context"]["sha256"],
        "expected_transcript": plan_inputs["expected_transcript"]["sha256"],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _course_context_binding(path: Path, reason: str) -> tuple[str, dict[str, Any]]:
    value, _contents = _load_json(path, reason)
    allowed = {
        "schema_version",
        "course_topic",
        "attribution",
        "priority_topics",
        "excluded_content",
    }
    if (
        not {"schema_version", "course_topic"}.issubset(value)
        or not set(value).issubset(allowed)
        or value["schema_version"] != "course_context.v1"
    ):
        raise EvidenceFailure(reason)
    topic = _string(value["course_topic"], reason)
    attribution = value.get("attribution")
    if attribution is not None:
        attribution = _string(attribution, reason)
    normalized_lists: dict[str, list[str]] = {}
    for field in ("priority_topics", "excluded_content"):
        normalized_lists[field] = [
            _string(item, reason) for item in _array(value.get(field, []), reason)
        ]
    normalized = {
        "schema_version": "course_context.v1",
        "course_topic": topic,
        "attribution": attribution,
        **normalized_lists,
    }
    try:
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise EvidenceFailure(reason) from None
    return (
        "sha256:" + hashlib.sha256(canonical).hexdigest(),
        {
            "provided": True,
            "attribution_provided": attribution is not None,
            "priority_topic_count": len(normalized_lists["priority_topics"]),
            "excluded_content_count": len(normalized_lists["excluded_content"]),
        },
    )


def _certified_configuration(path: Path, reason: str) -> dict[str, str]:
    values, _contents = _load_json(path, reason)
    if not isinstance(values, dict) or values.get("schema_version") != (
        "configuration.v1"
    ):
        raise EvidenceFailure(reason)
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
        raise EvidenceFailure(reason)
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
    if observed != {
        **_CERTIFIED_PROVIDER_CONFIGURATION,
        "text_credential_environment": "STEPFUN_API_KEY",
    }:
        raise EvidenceFailure(reason)
    return dict(_CERTIFIED_PROVIDER_CONFIGURATION)


def _validate_release_plan(
    plan_path: Path,
    *,
    source: Mapping[str, Any],
    wheel_path: Path,
    build_lock_path: Path,
    runtime_lock_path: Path,
    keyless_evidence_path: Path,
    installed_evidence_path: Path,
    keyless: Mapping[str, Any],
) -> dict[str, Any]:
    reason = "release_gate.plan_invalid"
    try:
        canonical_plan = plan_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EvidenceFailure(reason) from None
    if (
        plan_path.name != "plan.json"
        or not plan_path.is_absolute()
        or canonical_plan != plan_path
    ):
        raise EvidenceFailure(reason)
    plan, plan_bytes = _load_release_plan(plan_path, reason)
    plan = _object(
        plan,
        {
            "schema_version",
            "candidate",
            "certified_host",
            "automation",
            "inputs",
            "execution",
            "release",
        },
        reason,
    )
    if plan["schema_version"] != _PLAN_SCHEMA:
        raise EvidenceFailure(reason)
    candidate = _object(
        plan["candidate"],
        {"commit_sha", "version", "wheel", "build_lock", "runtime_lock"},
        reason,
    )
    if (
        _commit(candidate["commit_sha"], reason) != source["candidate"]["commit_sha"]
        or candidate["version"] != source["candidate"]["application_version"]
    ):
        raise EvidenceFailure(reason)
    for field, path, expected in (
        ("wheel", wheel_path, source["candidate"]),
        ("build_lock", build_lock_path, source["locks"]["build"]),
        ("runtime_lock", runtime_lock_path, source["locks"]["runtime"]),
    ):
        fact, _ = _validate_plan_file_fact(
            candidate[field], reason, expected_path=path
        )
        if fact["filename"] != expected[
            "wheel_filename" if field == "wheel" else "filename"
        ] or fact["sha256"] != expected[
            "wheel_sha256" if field == "wheel" else "sha256"
        ]:
            raise EvidenceFailure(reason)

    host = _object(
        plan["certified_host"],
        {"attestation_id", "apt_snapshot_id", "installation"},
        reason,
    )
    if (
        _public_identifier(host["attestation_id"], reason) == ""
        or _snapshot_id(host["apt_snapshot_id"], reason)
        != source["apt_snapshot_id"]
    ):
        raise EvidenceFailure(reason)
    installation = _object(
        host["installation"], {"manifest", "ready", "console"}, reason
    )
    manifest_fact, manifest_path = _validate_plan_file_fact(
        installation["manifest"], reason
    )
    ready_fact, ready_path = _validate_plan_file_fact(
        installation["ready"], reason
    )
    console_fact, console_path = _validate_plan_file_fact(
        installation["console"], reason
    )
    host_manifest, _ = _load_json(manifest_path, reason)
    certified_host_installation = _validate_installation(
        host_manifest,
        reason=reason,
    )
    ready, _ = _load_json(ready_path, reason)
    try:
        prefix = _canonical_existing_path(
            certified_host_installation["installation_prefix"], reason
        )
        version_directory = prefix / "versions" / candidate["version"]
        _validate_root_owned_installation_layout(
            prefix=prefix,
            version=candidate["version"],
            manifest_path=manifest_path,
            ready_path=ready_path,
            console_path=console_path,
            reason=reason,
        )
        host_installation_matches = (
            host_manifest["schema_version"] == _INSTALLATION_SCHEMA
            and certified_host_installation["application"]
            == {
                "name": "video-auto-editor",
                "version": candidate["version"],
                "wheel": {
                    "filename": source["candidate"]["wheel_filename"],
                    "sha256": source["candidate"]["wheel_sha256"],
                },
            }
            and certified_host_installation["runtime_lock"]
            == source["locks"]["runtime"]
            and certified_host_installation["apt_snapshot_id"]
            == source["apt_snapshot_id"]
            and manifest_path
            == version_directory / "installation-manifest.json"
            and ready_path == version_directory / "READY"
            and console_path
            == version_directory / "venv" / "bin" / "video-auto-editor"
        )
    except (KeyError, TypeError):
        host_installation_matches = False
    if (
        not host_installation_matches
        or ready
        != {
            "schema_version": "production-installation-ready.v1",
            "installation_manifest_sha256": manifest_fact["sha256"],
        }
        or ready_fact["filename"] != "READY"
        or console_fact["filename"] != "video-auto-editor"
    ):
        raise EvidenceFailure(reason)

    automation = _object(
        plan["automation"],
        {
            "run_url",
            "keyless_gate_evidence",
            "installed_acceptance_evidence",
            "release_tools",
        },
        reason,
    )
    run_url = _url(automation["run_url"], reason)
    if run_url != source["automatic_gate_runs"]["keyless"]["url"]:
        raise EvidenceFailure(reason)
    for field, path in (
        ("keyless_gate_evidence", keyless_evidence_path),
        ("installed_acceptance_evidence", installed_evidence_path),
    ):
        _validate_plan_file_fact(automation[field], reason, expected_path=path)
    release_tools = _object(
        automation["release_tools"], set(_RELEASE_TOOLS), reason
    )
    release_tools = {
        name: _digest(release_tools[name], reason) for name in _RELEASE_TOOLS
    }
    if release_tools != keyless["release_tools"]:
        raise EvidenceFailure(reason)

    plan_inputs = _object(
        plan["inputs"],
        {"source", "configuration", "course_context", "expected_transcript"},
        reason,
    )
    raw_source = _object(
        plan_inputs["source"],
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
        },
        reason,
    )
    source_path = _canonical_existing_path(raw_source["path"], reason)
    observed_source = _streaming_file_fact(
        source_path,
        reason,
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    expected_source = source["inputs"]["source"]
    if (
        raw_source["filename"] != observed_source["filename"]
        or _digest(raw_source["sha256"], reason) != observed_source["sha256"]
        or _integer(raw_source["byte_length"], reason, positive=True)
        != observed_source["byte_length"]
        or {
            key: raw_source[key]
            for key in (
                "asset_id",
                "version",
                "language",
                "content_summary",
                "sha256",
                "byte_length",
                "duration_ms",
            )
        }
        != expected_source
    ):
        raise EvidenceFailure(reason)
    for field, schema in (
        ("configuration", "configuration.v1"),
        ("course_context", "course_context.v1"),
        ("expected_transcript", "installed_acceptance_transcript.v1"),
    ):
        fact, input_path = _validate_plan_file_fact(plan_inputs[field], reason)
        document, _ = _load_json(input_path, reason)
        if (
            document.get("schema_version") != schema
            or fact["sha256"] != source["inputs"][field]["sha256"]
        ):
            raise EvidenceFailure(reason)
        if field == "course_context":
            context_sha256, context_observation = _course_context_binding(
                input_path, reason
            )
        elif field == "configuration":
            _certified_configuration(input_path, reason)

    execution = _object(
        plan["execution"],
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
        },
        reason,
    )
    execution_console, _ = _validate_plan_file_fact(
        execution["console"], reason, expected_path=console_path
    )
    validator_fact, validator_path = _validate_plan_file_fact(
        execution["independent_validator"], reason
    )
    bridge_fact, bridge_path = _validate_plan_file_fact(
        execution["credential_bridge"], reason
    )
    guard_fact, guard_path = _validate_plan_file_fact(
        execution["network_guard"], reason
    )
    script_root = Path(__file__).resolve().parent
    expected_tool_paths = {
        name: _trusted_release_tool_path(
            (script_root / name).resolve(strict=True),
            expected_path=script_root / name,
            reason=reason,
        )
        for name in _RELEASE_TOOLS
    }
    observed_static_tools = {
        name: _streaming_file_fact(path, reason)["sha256"]
        for name, path in expected_tool_paths.items()
    }
    if (
        execution_console != console_fact
        or validator_path
        != expected_tool_paths["validate_installed_delivery.py"]
        or bridge_path != expected_tool_paths["systemd_credential_bridge.py"]
        or guard_path != expected_tool_paths["run_keyless_gate_network.sh"]
        or validator_fact["sha256"]
        != release_tools["validate_installed_delivery.py"]
        or bridge_fact["sha256"]
        != release_tools["systemd_credential_bridge.py"]
        or guard_fact["sha256"]
        != release_tools["run_keyless_gate_network.sh"]
        or observed_static_tools != release_tools
        or not os.access(guard_path, os.X_OK)
    ):
        raise EvidenceFailure(reason)
    workspace_parent = _canonical_existing_path(
        execution["workspace_parent"], reason
    )
    workspace_status = workspace_parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(workspace_status.st_mode)
        or stat.S_IMODE(workspace_status.st_mode) != 0o700
        or execution["initial_workspace_state"]
        != "new_with_empty_processing_cache"
        or execution["credential_source"] != "systemd_credentials"
        or execution["credential_id"] != "stepfun_api_key"
        or execution["cold_then_overwrite"] is not True
    ):
        raise EvidenceFailure(reason)
    release = _object(plan["release"], {"version", "tag"}, reason)
    version = source["candidate"]["application_version"]
    if release != {"version": version, "tag": f"v{version}"}:
        raise EvidenceFailure(reason)
    return {
        "document": plan,
        "sha256": _file_digest(plan_bytes),
        "input_fingerprint": _input_fingerprint(plan_inputs),
        "workspace_parent": workspace_parent,
        "source_path": source_path,
        "source_fact": dict(source["inputs"]["source"]),
        "course_context_sha256": context_sha256,
        "course_context_observation": context_observation,
        "installation_manifest_sha256": manifest_fact["sha256"],
        "certified_host_installation": certified_host_installation,
        "artifacts": {
            "release_gate_plan": {
                "filename": "plan.json",
                "sha256": _file_digest(plan_bytes),
            },
            "certified_host_installation_manifest": {
                "filename": manifest_fact["filename"],
                "sha256": manifest_fact["sha256"],
            },
            "certified_host_installation_ready": {
                "filename": ready_fact["filename"],
                "sha256": ready_fact["sha256"],
            },
            "certified_host_console": {
                "filename": console_fact["filename"],
                "sha256": console_fact["sha256"],
            },
            "release_tools": {
                name: {"filename": name, "sha256": release_tools[name]}
                for name in _RELEASE_TOOLS
            },
        },
    }


def _normalized_cache_for_binding(value: Any, *, warm: bool, reason: str) -> dict[str, Any]:
    cache = _object(value, set(value) if isinstance(value, dict) else set(), reason)
    if not set(cache).issubset(_CACHE_NAMESPACES):
        raise EvidenceFailure(reason)
    normalized = {
        name: {
            field: _integer(stats[field], reason)
            for field in _CACHE_FIELDS
        }
        for name, stats in cache.items()
        if set(_object(stats, _CACHE_FIELDS, reason)) == _CACHE_FIELDS
    }
    if warm and "transcription_shard" not in normalized:
        normalized["transcription_shard"] = {field: 0 for field in _CACHE_FIELDS}
    if set(normalized) != set(_CACHE_NAMESPACES):
        raise EvidenceFailure(reason)
    return normalized


def _provider_projection_from_raw(value: Any, *, warm: bool, reason: str) -> dict[str, Any]:
    services = _object(
        value, {"status", "services"}, reason
    )
    if services["status"] != "observed":
        raise EvidenceFailure(reason)
    projection: dict[str, Any] = {}
    for item in _array(services["services"], reason):
        if not isinstance(item, dict) or set(item) != {
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
        }:
            raise EvidenceFailure(reason)
        try:
            capability = item["capability"]
            provider = {
                "provider_id": item["provider_id"],
                "model_id": item["model_id"],
                "requests": {
                    key: item["requests"][key]
                    for key in (
                        "count",
                        "succeeded",
                        "failed",
                        "attempt_count_total",
                    )
                },
            }
            contact = item["contact"]
            requests = item["requests"]
        except (KeyError, TypeError):
            raise EvidenceFailure(reason) from None
        if capability in projection or capability not in _PROVIDER_CAPABILITIES:
            raise EvidenceFailure(reason)
        expected_provider = (
            "stepaudio" if capability == "transcription" else "stepfun"
        )
        if (
            item["adapter_id"] != expected_provider
            or item["provider_id"] != expected_provider
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                item["configuration_fingerprint"],
            )
            is None
            or item["endpoint"]
            != {
                "status": "available",
                "origin": _CERTIFIED_ENDPOINT_ORIGIN,
            }
            or item["transport"] != "remote"
            or item["purpose"] != capability
            or item["allowed_data_categories"]
            != _PROVIDER_DATA_CATEGORIES[capability]
        ):
            raise EvidenceFailure(reason)
        try:
            normalized = _validate_provider(
                provider, capability=capability, warm=warm
            )
        except EvidenceFailure:
            raise EvidenceFailure(reason) from None
        if warm:
            if (
                contact != {"status": "not_contacted", "reason": "cache_hit"}
                or requests.get("duration_ms_total") != 0
                or requests.get("duration_ms_max") != 0
                or requests.get("token_usage") != {"status": "not_applicable"}
            ):
                raise EvidenceFailure(reason)
        elif contact != {"status": "contacted"}:
            raise EvidenceFailure(reason)
        projection[capability] = normalized
    if set(projection) != set(_PROVIDER_CAPABILITIES):
        raise EvidenceFailure(reason)
    return projection


def _provider_projection_from_summary(
    value: Any, *, warm: bool, reason: str
) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for item in _array(value, reason):
        item = _object(
            item,
            {"capability", "provider_id", "model_id", "requests"},
            reason,
        )
        capability = item["capability"]
        if capability in projection or capability not in _PROVIDER_CAPABILITIES:
            raise EvidenceFailure(reason)
        try:
            projection[capability] = _validate_provider(
                {
                    "provider_id": item["provider_id"],
                    "model_id": item["model_id"],
                    "requests": item["requests"],
                },
                capability=capability,
                warm=warm,
            )
        except EvidenceFailure:
            raise EvidenceFailure(reason) from None
    if set(projection) != set(_PROVIDER_CAPABILITIES):
        raise EvidenceFailure(reason)
    return projection


def _business_projection_sha256(delivery: Path, reason: str) -> str:
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in sorted(value.items())
                if key not in _BUSINESS_ID_FIELDS
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
            raise EvidenceFailure(reason)
        return value

    projection = {
        name: normalize(_load_json(delivery / name, reason)[0])
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
    except (TypeError, ValueError, UnicodeError):
        raise EvidenceFailure(reason) from None
    return hashlib.sha256(payload).hexdigest()


def _bind_run_originals(
    source_run: Mapping[str, Any],
    summary: Any,
    *,
    workspace: Path,
    delivery: Path,
    version: str,
    plan: Mapping[str, Any],
    warm: bool,
    reason: str,
) -> dict[str, Any]:
    extra = (
        {
            "required_cache_hits",
            "previous_delivery_retained",
            "network_isolation",
        }
        if warm
        else set()
    )
    run_summary = _object(
        summary,
        {
            "run_id",
            "terminal_state",
            "result_kind",
            "run_manifest_sha256",
            "delivery",
            "environment",
            "configuration",
            "external_services",
            "remote_request_count",
            "processing_cache",
            *extra,
        },
        reason,
    )
    run_id = _run_id(run_summary["run_id"], reason)
    if run_id != source_run["run_id"]:
        raise EvidenceFailure(reason)
    run_path = workspace / "work" / "runs" / run_id / "run.json"
    run_document, run_bytes = _load_json(run_path, reason)
    if (
        _file_digest(run_bytes) != run_summary["run_manifest_sha256"]
        or run_summary["run_manifest_sha256"]
        != source_run["diagnostic_manifest_sha256"]
    ):
        raise EvidenceFailure(reason)
    required_run_fields = {
        "schema_version",
        "identity",
        "lifecycle",
        "source",
        "environment",
        "configuration",
        "cache",
        "external_services",
        "delivery",
        "errors",
    }
    if not required_run_fields.issubset(run_document):
        raise EvidenceFailure(reason)
    expected_source = {
        "status": "available",
        "sha256": f"sha256:{plan['source_fact']['sha256']}",
        "byte_length": plan["source_fact"]["byte_length"],
        "duration_ms": plan["source_fact"]["duration_ms"],
        "course_context": {
            "provided": True,
            "sha256": {
                "status": "available",
                "value": plan["course_context_sha256"],
            },
        },
    }
    expected_delivery_state = {
        "build_state": "completed",
        "verification_state": "passed",
        "publication_state": "committed",
    }
    try:
        identity = _object(
            run_document["identity"],
            {"run_id", "application_version", "release"},
            reason,
        )
        lifecycle = _object(
            run_document["lifecycle"],
            {
                "started_at",
                "ended_at",
                "duration_ms",
                "outcome",
                "exit_code",
                "result_kind",
                "interruption",
            },
            reason,
        )
        delivery_state = _object(
            run_document["delivery"],
            {"build_state", "verification_state", "publication_state", "artifacts"},
            reason,
        )
        delivery_artifacts = _object(
            delivery_state["artifacts"],
            {"status", "created_by_role", "verified_by_role"},
            reason,
        )
        created_roles = delivery_artifacts["created_by_role"]
        environment = _object(
            run_document["environment"],
            {
                "status",
                "certified_platform",
                "python_version",
                "ffmpeg_version",
                "ffprobe_version",
                "font",
                "installation_fingerprint",
                "preflight_outcome",
                "application_version",
            },
            reason,
        )
        font = _object(environment["font"], {"family", "available"}, reason)
        python_version = _string(environment["python_version"], reason)
        parsed_python_version = tuple(
            int(part) for part in python_version.split(".")
        )
        ffmpeg_version = _string(environment["ffmpeg_version"], reason)
        configuration = run_document["configuration"]
        result_configuration = configuration["result_configuration"]
        run_contract_valid = (
            run_document["schema_version"] == "run_manifest.v1"
            and identity
            == {
                "run_id": run_id,
                "application_version": version,
                "release": {"status": "unknown"},
            }
            and lifecycle["outcome"] == "succeeded"
            and lifecycle["exit_code"] == 0
            and lifecycle["result_kind"]
            == {"status": "available", "value": "clips"}
            and run_document["source"] == expected_source
            and {
                key: delivery_state[key] for key in expected_delivery_state
            }
            == expected_delivery_state
            and delivery_artifacts["status"] == "observed"
            and isinstance(created_roles, dict)
            and created_roles == delivery_artifacts["verified_by_role"]
            and _integer(created_roles.get("short_video"), reason, positive=True)
            >= 1
            and environment["status"] == "available"
            and environment["certified_platform"] == "ubuntu_24_04_amd64"
            and environment["application_version"] == version
            and _PYTHON_VERSION.fullmatch(python_version) is not None
            and (3, 12, 3) <= parsed_python_version < (3, 13, 0)
            and _FFMPEG_VERSION.fullmatch(ffmpeg_version) is not None
            and environment["ffprobe_version"] == ffmpeg_version
            and font == {"family": "Noto Sans CJK SC", "available": True}
            and environment["preflight_outcome"] == "succeeded"
            and environment["installation_fingerprint"]
            == f"sha256:{plan['installation_manifest_sha256']}"
            and configuration["status"] == "available"
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                configuration["configuration_fingerprint"],
            )
            is not None
            and configuration["course_context"]
            == plan["course_context_observation"]
            and result_configuration["transcription_provider"] == "stepaudio"
            and result_configuration["transcription_model"]
            == "stepaudio-2.5-asr"
            and result_configuration["text_model_provider"] == "stepfun"
            and result_configuration["topic_review"]["model"]
            == "step-2-mini"
            and result_configuration["subtitle_optimization"]["model"]
            == "step-2-mini"
            and run_document["errors"]
            == {
                "primary_error": {"status": "not_applicable"},
                "associated_errors": [],
                "recovery_incomplete": False,
            }
        )
    except (KeyError, TypeError, ValueError):
        run_contract_valid = False
    if (
        not run_contract_valid
        or run_summary["terminal_state"] != "succeeded"
        or run_summary["result_kind"] != "clips"
        or source_run["terminal"]
        != {"outcome": "succeeded", "exit_code": 0, "result_kind": "clips"}
    ):
        raise EvidenceFailure(reason)
    summary_environment = _object(
        run_summary["environment"],
        {
            "certified_platform",
            "python_version",
            "application_version",
            "ffmpeg_version",
            "ffprobe_version",
            "font_family",
            "preflight_outcome",
            "installation_fingerprint",
        },
        reason,
    )
    if summary_environment != {
        "certified_platform": environment["certified_platform"],
        "python_version": environment["python_version"],
        "application_version": version,
        "ffmpeg_version": environment["ffmpeg_version"],
        "ffprobe_version": environment["ffprobe_version"],
        "font_family": font["family"],
        "preflight_outcome": environment["preflight_outcome"],
        "installation_fingerprint": environment["installation_fingerprint"],
    }:
        raise EvidenceFailure(reason)
    summary_configuration = _object(
        run_summary["configuration"],
        {"configuration_fingerprint", "course_context"},
        reason,
    )
    if summary_configuration != {
        "configuration_fingerprint": configuration["configuration_fingerprint"],
        "course_context": plan["course_context_observation"],
    }:
        raise EvidenceFailure(reason)
    if (
        source_run["environment"] != summary_environment
        or source_run["configuration"] != summary_configuration
    ):
        raise EvidenceFailure(reason)
    providers = _provider_projection_from_raw(
        run_document["external_services"], warm=warm, reason=reason
    )
    summary_providers = _provider_projection_from_summary(
        run_summary["external_services"], warm=warm, reason=reason
    )
    if providers != source_run["providers"] or summary_providers != providers:
        raise EvidenceFailure(reason)
    raw_cache_container = _object(
        run_document["cache"], {"status", "namespaces"}, reason
    )
    if raw_cache_container["status"] != "observed":
        raise EvidenceFailure(reason)
    raw_cache = _normalized_cache_for_binding(
        raw_cache_container["namespaces"], warm=warm, reason=reason
    )
    summary_cache = _normalized_cache_for_binding(
        run_summary["processing_cache"], warm=warm, reason=reason
    )
    if raw_cache != source_run["cache"] or summary_cache != raw_cache:
        raise EvidenceFailure(reason)
    if run_summary["remote_request_count"] != sum(
        provider["requests"]["count"] for provider in providers.values()
    ):
        raise EvidenceFailure(reason)

    delivery_document, delivery_bytes = _load_json(
        delivery / "manifest.json", reason
    )
    delivery_summary = _object(
        run_summary["delivery"],
        {
            "manifest_sha256",
            "result_kind",
            "short_video_count",
            "artifact_count",
            "source",
        },
        reason,
    )
    expected_delivery_source = {
        "sha256": f"sha256:{plan['source_fact']['sha256']}",
        "byte_length": plan["source_fact"]["byte_length"],
        "duration_ms": plan["source_fact"]["duration_ms"],
    }
    files = delivery_document.get("files")
    if (
        delivery_document.get("schema_version") != "delivery_manifest.v1"
        or delivery_document.get("run_id") != run_id
        or delivery_document.get("terminal_state") != "succeeded"
        or delivery_document.get("result_kind") != "clips"
        or delivery_document.get("application_version") != version
        or delivery_document.get("source") != expected_delivery_source
        or not isinstance(files, list)
        or _file_digest(delivery_bytes) != delivery_summary["manifest_sha256"]
        or {
            key: delivery_summary[key]
            for key in (
                "manifest_sha256",
                "result_kind",
                "short_video_count",
                "artifact_count",
            )
        }
        != source_run["delivery"]
        or delivery_summary["source"] != expected_delivery_source
        or delivery_summary["short_video_count"]
        != sum(
            isinstance(item, dict) and item.get("role") == "short_video_media"
            for item in files
        )
    ):
        raise EvidenceFailure(reason)
    if warm:
        expected_hits = {
            namespace: raw_cache[namespace]["hits"]
            for namespace in ("transcript", "topic_review", "subtitle_optimization")
        }
        if (
            run_summary["required_cache_hits"] != expected_hits
            or run_summary["previous_delivery_retained"] is not True
            or run_summary["network_isolation"]
            != {
                "mode": "linux_network_namespace",
                "external_blocked": True,
                "loopback_allowed": True,
                "attestation_verified": True,
                "guard_sha256": plan["artifacts"]["release_tools"][
                    "run_keyless_gate_network.sh"
                ]["sha256"],
            }
        ):
            raise EvidenceFailure(reason)
    artifacts = {
        "_configuration": dict(summary_configuration),
        "run_manifest": {
            "filename": "run.json",
            "sha256": _file_digest(run_bytes),
        },
        "delivery_manifest": {
            "filename": "manifest.json",
            "sha256": _file_digest(delivery_bytes),
        },
    }
    if warm:
        artifacts["network_isolation"] = dict(
            run_summary["network_isolation"]
        )
    return artifacts


def _raw_validation_summary(
    path: Path,
    source_validation: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    value, contents = _load_json(
        path,
        reason,
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    value = _object(
        value,
        {
            "schema_version",
            "success",
            "run_id",
            "result_kind",
            "short_video_count",
            "artifact_count",
            "checks",
        },
        reason,
    )
    digest = _file_digest(contents)
    if (
        digest != source_validation["evidence_sha256"]
        or value
        != {
            key: item
            for key, item in source_validation.items()
            if key != "evidence_sha256"
        }
    ):
        raise EvidenceFailure(reason)
    return {
        **{key: item for key, item in value.items() if key != "success"},
        "passed": True,
        "evidence_sha256": digest,
    }


def _private_directory(path: Path, reason: str) -> None:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError:
        raise EvidenceFailure(reason) from None
    if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o700:
        raise EvidenceFailure(reason)


def _validate_failed_run_original(
    workspace: Path,
    run_id: str,
    *,
    plan: Mapping[str, Any],
    version: str,
    stable_error_code: str,
    classification: str,
    reason: str,
) -> None:
    run, _contents = _load_json(
        workspace / "work" / "runs" / run_id / "run.json",
        reason,
    )
    if not {
        "schema_version",
        "identity",
        "lifecycle",
        "source",
        "environment",
        "configuration",
        "cache",
        "external_services",
        "delivery",
        "errors",
    }.issubset(run):
        raise EvidenceFailure(reason)
    try:
        expected_source = {
            "status": "available",
            "sha256": f"sha256:{plan['source_fact']['sha256']}",
            "byte_length": plan["source_fact"]["byte_length"],
            "duration_ms": plan["source_fact"]["duration_ms"],
            "course_context": {
                "provided": True,
                "sha256": {
                    "status": "available",
                    "value": plan["course_context_sha256"],
                },
            },
        }
        valid = (
            run["schema_version"] == "run_manifest.v1"
            and run["identity"]["run_id"] == run_id
            and run["identity"]["application_version"] == version
            and run["identity"]["release"] == {"status": "unknown"}
            and run["source"] == expected_source
            and run["environment"]["certified_platform"]
            == "ubuntu_24_04_amd64"
            and run["environment"]["application_version"] == version
            and run["environment"]["installation_fingerprint"]
            == f"sha256:{plan['installation_manifest_sha256']}"
            and run["configuration"]["course_context"]
            == plan["course_context_observation"]
            and (
                classification != "provider_transient"
                or (
                    run["lifecycle"]["outcome"] == "failed"
                    and run["errors"]["primary_error"]["error_code"]
                    == stable_error_code
                )
            )
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise EvidenceFailure(reason)


def _validate_failed_attempt(
    *,
    attempt_id: str,
    workspace_parent: Path,
    expected_candidate: Mapping[str, str],
    plan: Mapping[str, Any],
    source_retry: Mapping[str, Any],
    entries: set[str],
    reason: str,
) -> dict[str, Any]:
    workspace = workspace_parent / f"{attempt_id}.workspace"
    private = workspace_parent / f"{attempt_id}.private"
    record_path = workspace_parent / f"{attempt_id}.json"
    classification_path = workspace_parent / f"{attempt_id}.classification.json"
    _private_directory(workspace, reason)
    _private_directory(private, reason)
    record, record_bytes = _load_json(
        record_path,
        reason,
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    record = _object(
        record,
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
        },
        reason,
    )
    failure_phase = record["failure_phase"]
    expected_phase_files: tuple[str, ...]
    if failure_phase == "cold_run":
        expected_phase_files = ()
    elif failure_phase == "manual_review":
        expected_phase_files = ("cold",)
    elif failure_phase == "cache_rerun":
        expected_phase_files = ("cold", "review")
    else:
        raise EvidenceFailure(reason)
    phase_records: dict[str, str] = {}
    for phase in ("cold", "review"):
        suffix = "cold.json" if phase == "cold" else "review.json"
        path = workspace_parent / f"{attempt_id}.{suffix}"
        present = path.name in entries
        if present != (phase in expected_phase_files):
            raise EvidenceFailure(reason)
        if present:
            _document, contents = _load_json(
                path,
                reason,
                required_mode=0o600,
                required_parent_mode=0o700,
            )
            phase_records[f"{phase}_record_sha256"] = _file_digest(contents)
    run_ids = _array(record["run_ids"], reason)
    normalized_run_ids = [_run_id(value, reason) for value in run_ids]
    failed_run_id = _run_id(record["failed_run_id"], reason)
    if (
        record["schema_version"] != _ATTEMPT_SCHEMA
        or record["attempt_id"] != attempt_id
        or record["status"] != "failed"
        or record["candidate"] != expected_candidate
        or record["input_fingerprint"] != plan["input_fingerprint"]
        or record["plan_sha256"] != plan["sha256"]
        or _timestamp(record["started_at"], reason) > _timestamp(
            record["ended_at"], reason
        )
        or record["phase_records"] != phase_records
        or not normalized_run_ids
        or len(normalized_run_ids) != len(set(normalized_run_ids))
        or failed_run_id != normalized_run_ids[-1]
    ):
        raise EvidenceFailure(reason)
    failure = record["failure"]
    expected_classification = source_retry["classification"]
    stable_error_code = source_retry["stable_error_code"]
    if expected_classification == "provider_transient":
        if failure_phase != "cold_run":
            raise EvidenceFailure(reason)
        expected_failure = {
            "reason_code": "run.failed",
            "stable_error_code": stable_error_code,
            "classification": "unclassified",
            "permitted_same_candidate_rerun": ["provider_transient"],
            "same_candidate_rerun_allowed": False,
        }
    elif expected_classification == "certified_host_infrastructure":
        expected_failure = {
            "reason_code": "independent_validation.launch_failed",
            "stable_error_code": "independent_validation.launch_failed",
            "classification": "unclassified",
            "permitted_same_candidate_rerun": [
                "certified_host_infrastructure"
            ],
            "same_candidate_rerun_allowed": False,
        }
    else:
        raise EvidenceFailure(reason)
    if failure != expected_failure:
        raise EvidenceFailure(reason)
    for run_id in normalized_run_ids:
        _validate_failed_run_original(
            workspace,
            run_id,
            plan=plan,
            version=str(plan["document"]["candidate"]["version"]),
            stable_error_code=stable_error_code,
            classification=(
                expected_classification if run_id == failed_run_id else "prior"
            ),
            reason=reason,
        )

    classification, classification_bytes = _load_json(
        classification_path,
        reason,
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    classification = _object(
        classification,
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
        },
        reason,
    )
    operator_id = _string(classification["operator_id"], reason)
    if (
        classification["schema_version"] != _CLASSIFICATION_SCHEMA
        or classification["attempt_id"] != attempt_id
        or classification["candidate"] != expected_candidate
        or classification["input_fingerprint"] != plan["input_fingerprint"]
        or classification["plan_sha256"] != plan["sha256"]
        or classification["failure_record_sha256"] != _file_digest(record_bytes)
        or classification["classification"] != expected_classification
        or _SAFE_ID.fullmatch(operator_id) is None
        or _timestamp(classification["classified_at"], reason)
        < _timestamp(record["ended_at"], reason)
        or classification["same_candidate_rerun_allowed"] is not True
        or failed_run_id != source_retry["run_id"]
        or record["ended_at"] != source_retry["occurred_at"]
        or record["candidate"] != source_retry["candidate"]
    ):
        raise EvidenceFailure(reason)
    return {
        "failure_record": {
            "filename": record_path.name,
            "sha256": _file_digest(record_bytes),
        },
        "classification_record": {
            "filename": classification_path.name,
            "sha256": _file_digest(classification_bytes),
        },
    }


def _validate_retry_history(
    *,
    attempt_path: Path,
    source: Mapping[str, Any],
    plan: Mapping[str, Any],
    reason: str,
) -> list[dict[str, Any]]:
    workspace_parent = plan["workspace_parent"]
    try:
        names = {entry.name for entry in workspace_parent.iterdir()}
    except OSError:
        raise EvidenceFailure(reason) from None
    pattern = re.compile(
        r"attempt-([0-9]{4})(?:\.workspace|\.private|\.cold\.json|"
        r"\.review\.json|\.classification\.json|\.json)"
    )
    numbers: set[int] = set()
    for name in names:
        match = pattern.fullmatch(name)
        if match is None:
            raise EvidenceFailure(reason)
        number = int(match.group(1))
        if number < 1:
            raise EvidenceFailure(reason)
        numbers.add(number)
    successful_number = int(attempt_path.stem.removeprefix("attempt-"))
    if (
        sorted(numbers) != list(range(1, successful_number + 1))
        or successful_number != len(source["retry_attempts"]) + 1
        or f"{attempt_path.stem}.classification.json" in names
    ):
        raise EvidenceFailure(reason)
    expected_candidate = {
        "commit_sha": source["candidate"]["commit_sha"],
        "wheel_sha256": source["candidate"]["wheel_sha256"],
    }
    artifacts = []
    for number, source_retry in enumerate(source["retry_attempts"], start=1):
        artifacts.append(
            _validate_failed_attempt(
                attempt_id=f"attempt-{number:04d}",
                workspace_parent=workspace_parent,
                expected_candidate=expected_candidate,
                plan=plan,
                source_retry=source_retry,
                entries=names,
                reason=reason,
            )
        )
    return artifacts


def _validate_release_attempt(
    attempt_path: Path,
    *,
    source: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    reason = "release_gate.attempt_invalid"
    workspace_parent = plan["workspace_parent"]
    try:
        canonical_attempt = attempt_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EvidenceFailure(reason) from None
    if (
        not attempt_path.is_absolute()
        or canonical_attempt != attempt_path
        or attempt_path.parent != workspace_parent
        or _ATTEMPT_ID.fullmatch(attempt_path.stem) is None
    ):
        raise EvidenceFailure(reason)
    attempt, attempt_bytes = _load_json(
        attempt_path,
        reason,
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    attempt = _object(
        attempt,
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
        },
        reason,
    )
    attempt_id = attempt_path.stem
    expected_candidate = {
        "commit_sha": source["candidate"]["commit_sha"],
        "wheel_sha256": source["candidate"]["wheel_sha256"],
    }
    if (
        attempt["schema_version"] != _ATTEMPT_SCHEMA
        or attempt["attempt_id"] != attempt_id
        or attempt["status"] != "passed"
        or attempt["candidate"] != expected_candidate
        or attempt["input_fingerprint"] != plan["input_fingerprint"]
        or attempt["plan_sha256"] != plan["sha256"]
        or _timestamp(attempt["started_at"], reason) != attempt["started_at"]
        or _timestamp(attempt["ended_at"], reason) != attempt["ended_at"]
        or attempt["workspace"]
        != {
            "initial_processing_cache_empty": True,
            "same_workspace_for_rerun": True,
        }
        or attempt["credential_handling"]
        != {"source": "systemd_credentials", "leak_scan_passed": True}
    ):
        raise EvidenceFailure(reason)
    workspace = workspace_parent / f"{attempt_id}.workspace"
    private = workspace_parent / f"{attempt_id}.private"
    for directory in (workspace, private):
        status = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o700:
            raise EvidenceFailure(reason)
    cold_path = workspace_parent / f"{attempt_id}.cold.json"
    review_path = workspace_parent / f"{attempt_id}.review.json"
    cold, cold_bytes = _load_json(
        cold_path, reason, required_mode=0o600, required_parent_mode=0o700
    )
    review, review_bytes = _load_json(
        review_path, reason, required_mode=0o600, required_parent_mode=0o700
    )
    if attempt["phase_records"] != {
        "cold_record_sha256": _file_digest(cold_bytes),
        "review_record_sha256": _file_digest(review_bytes),
    }:
        raise EvidenceFailure(reason)
    cold = _object(
        cold,
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
        },
        reason,
    )
    if (
        cold["schema_version"] != _COLD_RECORD_SCHEMA
        or cold["attempt_id"] != attempt_id
        or cold["status"] != "awaiting_manual_review"
        or cold["candidate"] != expected_candidate
        or cold["input_fingerprint"] != plan["input_fingerprint"]
        or cold["plan_sha256"] != plan["sha256"]
        or _timestamp(cold["started_at"], reason) != attempt["started_at"]
        or _timestamp(cold["ended_at"], reason) != cold["ended_at"]
        or cold["workspace"]
        != {
            "initial_processing_cache_empty": True,
            "same_workspace_reserved_for_rerun": True,
        }
        or cold["cold_run"] != attempt["cold_run"]
        or cold["credential_handling"] != attempt["credential_handling"]
    ):
        raise EvidenceFailure(reason)
    review = _object(
        review,
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
        },
        reason,
    )
    manual = source["manual_review"]
    operator_id = _string(review["operator_id"], reason)
    if (
        review["schema_version"] != _REVIEW_RECORD_SCHEMA
        or review["attempt_id"] != attempt_id
        or review["status"] != "passed"
        or review["candidate"] != expected_candidate
        or review["input_fingerprint"] != plan["input_fingerprint"]
        or review["plan_sha256"] != plan["sha256"]
        or review["cold_record_sha256"] != _file_digest(cold_bytes)
        or _digest(review["review_source_sha256"], reason)
        != review["review_source_sha256"]
        or _timestamp(review["recorded_at"], reason) != review["recorded_at"]
        or _SAFE_ID.fullmatch(operator_id) is None
        or hashlib.sha256(operator_id.encode()).hexdigest()
        != manual["operator_id_sha256"]
        or review["reviewed_at"] != manual["reviewed_at"]
        or review["run_id"] != manual["run_id"]
        or review["source_and_transcript_compared"] is not True
        or review["clips"] != manual["clips"]
        or _integer(review["reviewed_clip_count"], reason, positive=True)
        != manual["reviewed_short_video_count"]
        or review["all_checks_passed"] is not True
        or review["conclusion"] != "passed"
    ):
        raise EvidenceFailure(reason)
    expected_manual_summary = {
        "operator_id": review["operator_id"],
        "reviewed_at": review["reviewed_at"],
        "run_id": review["run_id"],
        "source_and_transcript_compared": True,
        "reviewed_clip_count": review["reviewed_clip_count"],
        "all_checks_passed": True,
        "conclusion": "passed",
    }
    if attempt["manual_review"] != expected_manual_summary:
        raise EvidenceFailure(reason)
    if not (
        _timestamp_value(attempt["started_at"], reason)
        <= _timestamp_value(cold["ended_at"], reason)
        <= _timestamp_value(review["reviewed_at"], reason)
        <= _timestamp_value(review["recorded_at"], reason)
        <= _timestamp_value(attempt["ended_at"], reason)
        <= datetime.now(timezone.utc) + timedelta(minutes=5)
    ):
        raise EvidenceFailure(reason)

    cold_artifacts = _bind_run_originals(
        source["runs"]["cold"],
        attempt["cold_run"],
        workspace=workspace,
        delivery=workspace / "delivery.previous",
        version=source["candidate"]["application_version"],
        plan=plan,
        warm=False,
        reason=reason,
    )
    warm_artifacts = _bind_run_originals(
        source["runs"]["warm"],
        attempt["cache_rerun"],
        workspace=workspace,
        delivery=workspace / "delivery",
        version=source["candidate"]["application_version"],
        plan=plan,
        warm=True,
        reason=reason,
    )
    validations = _object(
        attempt["independent_validation"],
        {"cold_run", "previous_delivery", "cache_rerun"},
        reason,
    )
    cold_validation = _raw_validation_summary(
        private / "cold-validation.json",
        source["independent_validations"]["cold"],
        reason=reason,
    )
    previous_validation = _raw_validation_summary(
        private / "previous-validation.json",
        source["independent_validations"]["cold"],
        reason=reason,
    )
    warm_validation = _raw_validation_summary(
        private / "warm-validation.json",
        source["independent_validations"]["warm"],
        reason=reason,
    )
    if validations != {
        "cold_run": cold_validation,
        "previous_delivery": previous_validation,
        "cache_rerun": warm_validation,
    } or cold["independent_validation"] != cold_validation:
        raise EvidenceFailure(reason)
    cold_projection = _business_projection_sha256(
        workspace / "delivery.previous", reason
    )
    warm_projection = _business_projection_sha256(workspace / "delivery", reason)
    equivalence = source["semantic_equivalence"]
    if (
        cold_projection != warm_projection
        or cold_projection != cold["business_projection_sha256"]
        or attempt["semantic_equivalence"]
        != {"passed": True, "business_projection_sha256": cold_projection}
        or equivalence["cold_projection_sha256"] != cold_projection
        or equivalence["warm_projection_sha256"] != warm_projection
    ):
        raise EvidenceFailure(reason)
    if cold_artifacts.pop("_configuration") != warm_artifacts.pop(
        "_configuration"
    ):
        raise EvidenceFailure(reason)
    retry_artifacts = _validate_retry_history(
        attempt_path=attempt_path,
        source=source,
        plan=plan,
        reason=reason,
    )
    return {
        "artifacts": {
            **plan["artifacts"],
            "release_gate_attempt": {
                "filename": attempt_path.name,
                "sha256": _file_digest(attempt_bytes),
            },
            "release_gate_cold_record": {
                "filename": cold_path.name,
                "sha256": _file_digest(cold_bytes),
            },
            "release_gate_review_record": {
                "filename": review_path.name,
                "sha256": _file_digest(review_bytes),
            },
            "release_gate_retry_history": retry_artifacts,
        },
        "run_artifacts": {"cold": cold_artifacts, "warm": warm_artifacts},
    }


def _cross_check(
    source: Mapping[str, Any],
    wheel: Mapping[str, str],
    build_lock: Mapping[str, str],
    runtime_lock: Mapping[str, str],
    installation: Mapping[str, Any],
    installation_digest: str,
    keyless: Mapping[str, Any],
    installed: Mapping[str, Any],
) -> None:
    candidate = source["candidate"]
    expected_candidate = {
        "commit_sha": candidate["commit_sha"],
        "wheel_filename": wheel["filename"],
        "wheel_sha256": wheel["sha256"],
    }
    if (
        candidate["wheel_filename"] != wheel["filename"]
        or candidate["wheel_sha256"] != wheel["sha256"]
        or source["locks"]["build"] != build_lock
        or source["locks"]["runtime"] != runtime_lock
        or keyless["candidate"] != expected_candidate
        or installed["candidate"]
        != {
            "apt_snapshot_id": source["apt_snapshot_id"],
            "commit_sha": candidate["commit_sha"],
            "runtime_lock_filename": runtime_lock["filename"],
            "runtime_lock_sha256": runtime_lock["sha256"],
            "wheel_filename": wheel["filename"],
            "wheel_sha256": wheel["sha256"],
        }
        or installation["application"]["version"] != candidate["application_version"]
        or installation["application"]["wheel"] != wheel
        or installation["runtime_lock"] != runtime_lock
        or installation["apt_snapshot_id"] != source["apt_snapshot_id"]
        or installed["installation"]["application_version"]
        != candidate["application_version"]
        or installed["installation"]["prefix"]
        != installation["installation_prefix"]
        or installed["installation"]["manifest_sha256"] != installation_digest
    ):
        raise EvidenceFailure("candidate.identity_mismatch")


def _build_evidence(
    *,
    source: Mapping[str, Any],
    source_digest: str,
    wheel: Mapping[str, str],
    build_lock: Mapping[str, str],
    runtime_lock: Mapping[str, str],
    installation: Mapping[str, Any],
    certified_host_installation: Mapping[str, Any],
    installation_digest: str,
    keyless: Mapping[str, Any],
    keyless_digest: str,
    installed: Mapping[str, Any],
    installed_digest: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _EVIDENCE_SCHEMA,
        "success": True,
        "candidate": dict(source["candidate"]),
        "apt_snapshot_id": source["apt_snapshot_id"],
        "artifacts": {
            "wheel": dict(wheel),
            "release_evidence_source": {
                "filename": "release-evidence-source.json",
                "sha256": source_digest,
            },
            "installation_manifest": {
                "filename": "installation-manifest.json",
                "sha256": installation_digest,
            },
            **provenance["artifacts"],
            "release_gate_runs": provenance["run_artifacts"],
        },
        "dependencies": {
            "build_lock": dict(build_lock),
            "runtime_lock": dict(runtime_lock),
            "ci_installed_acceptance": {
                "snapshot_packages": installation["snapshot_packages"],
                "system_packages": installation["system_packages"],
                "wheelhouse": installation["wheelhouse"],
            },
            "certified_host": {
                "snapshot_packages": certified_host_installation[
                    "snapshot_packages"
                ],
                "system_packages": certified_host_installation[
                    "system_packages"
                ],
                "wheelhouse": certified_host_installation["wheelhouse"],
            },
        },
        "installation": {
            "ci_installed_acceptance": {
                "verified": True,
                "platform": installation["platform"],
                "python": installation["python"],
                "environment": installation["environment"],
            },
            "certified_host": {
                "verified": True,
                "platform": certified_host_installation["platform"],
                "python": certified_host_installation["python"],
                "environment": certified_host_installation["environment"],
            },
        },
        "automatic_gates": {
            "keyless": {
                "url": source["automatic_gate_runs"]["keyless"]["url"],
                "evidence_sha256": keyless_digest,
                "credential_mode": keyless["credential_mode"],
                "network": keyless["network"],
                "layers": keyless["layers"],
                "statistics": keyless["statistics"],
            },
            "installed_acceptance": {
                "url": source["automatic_gate_runs"]["installed_acceptance"]["url"],
                "evidence_sha256": installed_digest,
                "network": installed["network"],
                "cases": installed["cases"],
                "statistics": installed["statistics"],
            },
        },
        "inputs": source["inputs"],
        "runs": source["runs"],
        "independent_validations": source["independent_validations"],
        "semantic_equivalence": source["semantic_equivalence"],
        "manual_review": source["manual_review"],
        "retry_attempts": source["retry_attempts"],
        "known_limitations": source["known_limitations"],
    }


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _seal_exclusive(destination: Path, contents: bytes) -> None:
    if destination.name != "release-evidence.json":
        raise EvidenceFailure("output.path_invalid")
    directory_descriptor = -1
    file_descriptor = -1
    temporary_name: str | None = None
    target_linked = False
    committed = False
    try:
        requested_parent = destination.parent
        lexical_parent = Path(os.path.abspath(requested_parent))
        parent = requested_parent.resolve(strict=True)
        if (
            parent != lexical_parent
            or not parent.is_dir()
            or destination.name in {"", ".", ".."}
        ):
            raise OSError
        directory_descriptor = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        if not _directory_descriptor_matches_path(directory_descriptor, parent):
            raise OSError
        for _ in range(16):
            candidate = (
                f".{destination.name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                file_descriptor = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise OSError
        remaining = memoryview(contents)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fchmod(file_descriptor, 0o444)
        os.fsync(file_descriptor)
        opened_file = os.fstat(file_descriptor)
        named_file = os.stat(
            temporary_name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(named_file.st_mode)
            or (opened_file.st_dev, opened_file.st_ino, opened_file.st_size)
            != (named_file.st_dev, named_file.st_ino, named_file.st_size)
            or stat.S_IMODE(opened_file.st_mode) != 0o444
        ):
            raise OSError
        os.link(
            temporary_name,
            destination.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        target_linked = True
        target_status = os.stat(
            destination.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (target_status.st_dev, target_status.st_ino, target_status.st_size) != (
            opened_file.st_dev,
            opened_file.st_ino,
            opened_file.st_size,
        ):
            raise OSError
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_name = None
        os.fsync(directory_descriptor)
        if (
            requested_parent.resolve(strict=True) != parent
            or not _directory_descriptor_matches_path(directory_descriptor, parent)
        ):
            raise OSError
        committed = True
    except FileExistsError:
        raise EvidenceFailure("output.exists") from None
    except (OSError, RuntimeError):
        if target_linked and not committed and directory_descriptor >= 0:
            try:
                os.unlink(destination.name, dir_fd=directory_descriptor)
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
            except OSError:
                pass
        raise EvidenceFailure("output.write_failed") from None
    finally:
        if temporary_name is not None and directory_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def validate_and_seal(
    *,
    source_path: Path,
    plan_path: Path,
    attempt_path: Path,
    wheel_path: Path,
    build_lock_path: Path,
    runtime_lock_path: Path,
    installation_manifest_path: Path,
    keyless_evidence_path: Path,
    installed_evidence_path: Path,
    output_path: Path,
) -> str:
    if build_lock_path.name != "requirements-build.lock":
        raise EvidenceFailure("candidate.build_lock_invalid")
    if runtime_lock_path.name != "requirements-runtime.lock":
        raise EvidenceFailure("candidate.runtime_lock_invalid")
    if installation_manifest_path.name != "installation-manifest.json":
        raise EvidenceFailure("installation_manifest.invalid")
    if source_path.name != "release-evidence-source.json":
        raise EvidenceFailure("source.read_invalid")
    if not source_path.is_absolute():
        raise EvidenceFailure("source.read_invalid")
    source_value, source_bytes = _load_json(
        source_path,
        "source.read_invalid",
        required_mode=0o600,
        required_parent_mode=0o700,
    )
    installation_value, installation_bytes = _load_json(
        installation_manifest_path,
        "installation_manifest.read_invalid",
        maximum_bytes=_MAX_INSTALLATION_MANIFEST_BYTES,
    )
    keyless_value, keyless_bytes = _load_json(
        keyless_evidence_path, "keyless_evidence.read_invalid"
    )
    installed_value, installed_bytes = _load_json(
        installed_evidence_path, "installed_evidence.read_invalid"
    )
    source = _validate_source(source_value)
    wheel, wheel_contents = _artifact_summary(
        wheel_path,
        {
            "filename": source["candidate"]["wheel_filename"],
            "sha256": source["candidate"]["wheel_sha256"],
        },
        "candidate.wheel_invalid",
        maximum_bytes=_MAX_WHEEL_BYTES,
    )
    _validate_candidate_wheel(wheel_contents, source["candidate"])
    build_lock, _ = _artifact_summary(
        build_lock_path,
        source["locks"]["build"],
        "candidate.build_lock_invalid",
        maximum_bytes=_MAX_LOCK_BYTES,
    )
    runtime_lock, _ = _artifact_summary(
        runtime_lock_path,
        source["locks"]["runtime"],
        "candidate.runtime_lock_invalid",
        maximum_bytes=_MAX_LOCK_BYTES,
    )
    installation = _validate_installation(installation_value)
    keyless = _validate_keyless(keyless_value)
    installed = _validate_installed(installed_value)
    installation_digest = _file_digest(installation_bytes)
    _cross_check(
        source,
        wheel,
        build_lock,
        runtime_lock,
        installation,
        installation_digest,
        keyless,
        installed,
    )
    plan = _validate_release_plan(
        plan_path,
        source=source,
        wheel_path=wheel_path,
        build_lock_path=build_lock_path,
        runtime_lock_path=runtime_lock_path,
        keyless_evidence_path=keyless_evidence_path,
        installed_evidence_path=installed_evidence_path,
        keyless=keyless,
    )
    provenance = _validate_release_attempt(
        attempt_path,
        source=source,
        plan=plan,
    )
    evidence = _build_evidence(
        source=source,
        source_digest=_file_digest(source_bytes),
        wheel=wheel,
        build_lock=build_lock,
        runtime_lock=runtime_lock,
        installation=installation,
        certified_host_installation=plan["certified_host_installation"],
        installation_digest=installation_digest,
        keyless=keyless,
        keyless_digest=_file_digest(keyless_bytes),
        installed=installed,
        installed_digest=_file_digest(installed_bytes),
        provenance=provenance,
    )
    contents = _json_bytes(evidence)
    _seal_exclusive(output_path, contents)
    return _file_digest(contents)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="严格校验并独占封存可公开的生产发布证据"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--attempt", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--build-lock", required=True, type=Path)
    parser.add_argument("--runtime-lock", required=True, type=Path)
    parser.add_argument("--installation-manifest", required=True, type=Path)
    parser.add_argument("--keyless-evidence", required=True, type=Path)
    parser.add_argument("--installed-evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        digest = validate_and_seal(
            source_path=arguments.source,
            plan_path=arguments.plan,
            attempt_path=arguments.attempt,
            wheel_path=arguments.wheel,
            build_lock_path=arguments.build_lock,
            runtime_lock_path=arguments.runtime_lock,
            installation_manifest_path=arguments.installation_manifest,
            keyless_evidence_path=arguments.keyless_evidence,
            installed_evidence_path=arguments.installed_evidence,
            output_path=arguments.output,
        )
    except EvidenceFailure as failure:
        print(f"发布证据封存失败：{failure.reason_code}", file=sys.stderr)
        return 1
    except Exception:
        print("发布证据封存失败：evidence.invalid", file=sys.stderr)
        return 1
    print(f"release-evidence.json SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
