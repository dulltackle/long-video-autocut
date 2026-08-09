#!/usr/bin/env python3
"""在仓库外验证已安装候选 CLI 并形成候选绑定证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_EVIDENCE_SCHEMA = "installed_acceptance_evidence.v1"
_INSTALLATION_SCHEMA = "production-installation-manifest.v1"
_READY_SCHEMA = "production-installation-ready.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SNAPSHOT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_RUN_ID = re.compile(
    r"run_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_TRANSCRIPT_TEXT = "嗯，忠实原文必须保留语气词。"
_CASE_IDS = (
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


class _DuplicateField(ValueError):
    pass


class AcceptanceFailure(RuntimeError):
    """只携带稳定门禁原因码的失败。"""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField(key)
        result[key] = value
    return result


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        if not path.is_file() or path.is_symlink():
            raise OSError
        contents = path.read_bytes()
        value = json.loads(contents, object_pairs_hook=_strict_object)
        if not isinstance(value, dict):
            raise ValueError
        return value, contents
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise AcceptanceFailure("installation.identity_invalid") from None


def _file_digest(path: Path, reason_code: str) -> str:
    try:
        if not path.is_file() or path.is_symlink():
            raise OSError
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise AcceptanceFailure(reason_code) from None


def _candidate_identity(
    *,
    wheel: Path,
    runtime_lock: Path,
    commit_sha: str,
    apt_snapshot_id: str,
) -> dict[str, str]:
    if _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise AcceptanceFailure("candidate.identity_invalid")
    if _SNAPSHOT_ID.fullmatch(apt_snapshot_id) is None:
        raise AcceptanceFailure("candidate.identity_invalid")
    return {
        "apt_snapshot_id": apt_snapshot_id,
        "commit_sha": commit_sha,
        "runtime_lock_filename": runtime_lock.name,
        "runtime_lock_sha256": _file_digest(
            runtime_lock,
            "candidate.runtime_lock_invalid",
        ),
        "wheel_filename": wheel.name,
        "wheel_sha256": _file_digest(wheel, "candidate.wheel_invalid"),
    }


def _network_attestation() -> dict[str, object]:
    mode = os.environ.get("KEYLESS_GATE_NETWORK_MODE", "")
    if mode not in {"network_namespace", "python_guard"}:
        raise AcceptanceFailure("network.isolation_missing")
    if mode == "network_namespace":
        try:
            current = os.readlink("/proc/self/ns/net")
            parent = os.environ["KEYLESS_GATE_PARENT_NETNS"]
            interfaces = tuple(sorted(name for _, name in socket.if_nameindex()))
        except (OSError, KeyError):
            raise AcceptanceFailure("network.isolation_invalid") from None
        if current == parent or interfaces != ("lo",):
            raise AcceptanceFailure("network.isolation_invalid")
    return {
        "external_blocked": False,
        "loopback_allowed": False,
        "mode": mode,
    }


def _resolved_without_symlink(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AcceptanceFailure("installation.identity_invalid") from None
    return resolved


def _verify_installation(
    prefix: Path,
    candidate: Mapping[str, str],
) -> dict[str, object]:
    prefix = _resolved_without_symlink(prefix)
    current = prefix / "current"
    if not current.is_symlink():
        raise AcceptanceFailure("installation.identity_invalid")
    try:
        version_directory = current.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AcceptanceFailure("installation.identity_invalid") from None
    if version_directory.parent != prefix / "versions":
        raise AcceptanceFailure("installation.identity_invalid")
    manifest, manifest_bytes = _load_json(
        version_directory / "installation-manifest.json"
    )
    ready, _ready_bytes = _load_json(version_directory / "READY")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if ready != {
        "installation_manifest_sha256": manifest_digest,
        "schema_version": _READY_SCHEMA,
    }:
        raise AcceptanceFailure("installation.identity_invalid")
    try:
        application = manifest["application"]
        runtime_lock = manifest["runtime_lock"]
        actual = {
            "apt_snapshot_id": manifest["apt_snapshot_id"],
            "runtime_lock_filename": runtime_lock["filename"],
            "runtime_lock_sha256": runtime_lock["sha256"],
            "wheel_filename": application["wheel"]["filename"],
            "wheel_sha256": application["wheel"]["sha256"],
        }
        schema_version = manifest["schema_version"]
        installation_prefix = manifest["installation_prefix"]
        application_version = application["version"]
    except (KeyError, TypeError):
        raise AcceptanceFailure("installation.identity_invalid") from None
    if (
        schema_version != _INSTALLATION_SCHEMA
        or not isinstance(application_version, str)
        or not isinstance(installation_prefix, str)
        or actual
        != {
            key: candidate[key]
            for key in (
                "apt_snapshot_id",
                "runtime_lock_filename",
                "runtime_lock_sha256",
                "wheel_filename",
                "wheel_sha256",
            )
        }
        or installation_prefix != str(prefix)
        or version_directory.name != application_version
    ):
        raise AcceptanceFailure("installation.identity_invalid")
    environment_prefix = version_directory / "venv"
    console = environment_prefix / "bin" / "video-auto-editor"
    python = environment_prefix / "bin" / "python"
    try:
        environment_stat = environment_prefix.stat(follow_symlinks=False)
        console_stat = console.stat(follow_symlinks=False)
        python_stat = python.stat(follow_symlinks=True)
    except OSError:
        raise AcceptanceFailure("installation.identity_invalid") from None
    if (
        not stat.S_ISDIR(environment_stat.st_mode)
        or not stat.S_ISREG(console_stat.st_mode)
        or not os.access(console, os.X_OK)
        or not stat.S_ISREG(python_stat.st_mode)
        or not os.access(python, os.X_OK)
    ):
        raise AcceptanceFailure("installation.identity_invalid")
    return {
        "application_version": application_version,
        "console": str(console),
        "environment_prefix": str(environment_prefix),
        "manifest_sha256": manifest_digest,
        "prefix": str(prefix),
        "python": str(python),
        "verified": True,
    }


def _empty_evidence(
    candidate: Mapping[str, str],
    network: Mapping[str, object],
) -> dict[str, Any]:
    return {
        "candidate": dict(candidate),
        "cases": {},
        "installation": {"verified": False},
        "network": dict(network),
        "schema_version": _EVIDENCE_SCHEMA,
        "statistics": {"failed": 0, "passed": 0, "total": 0},
        "success": False,
    }


def _record_failure(
    evidence: dict[str, Any],
    reason_code: str,
) -> None:
    for case_id, case in tuple(evidence["cases"].items()):
        if case.get("status") == "running":
            evidence["cases"][case_id] = {
                "exit_codes": [],
                "reason_code": reason_code,
                "run_ids": [],
                "status": "failed",
            }
    passed = sum(
        case.get("status") == "passed" for case in evidence["cases"].values()
    )
    failed = sum(
        case.get("status") == "failed" for case in evidence["cases"].values()
    )
    evidence["statistics"] = {
        "failed": failed,
        "passed": passed,
        "total": len(evidence["cases"]),
    }
    evidence["failure_reason"] = reason_code


def _write_evidence(destination: Path, evidence: Mapping[str, Any]) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    target = parent / destination.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                (
                    json.dumps(
                        evidence,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _read_case_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_bytes(),
            object_pairs_hook=_strict_object,
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise AcceptanceFailure(reason_code) from None


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise AcceptanceFailure(reason_code)


def _verify_harness(harness_root: Path) -> dict[str, Path]:
    try:
        root = harness_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AcceptanceFailure("harness.invalid") from None
    required = {
        name: root / name
        for name in (
            "installed_acceptance_composition.py",
            "keyless_gate_network_guard.py",
            "sitecustomize.py",
            "validate_installed_delivery.py",
        )
    }
    for path in required.values():
        try:
            mode = path.lstat().st_mode
        except OSError:
            raise AcceptanceFailure("harness.invalid") from None
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise AcceptanceFailure("harness.invalid")
    required["root"] = root
    return required


def _probe_network_policy(
    *,
    python: Path,
    harness_root: Path,
    work_root: Path,
) -> None:
    audit = work_root / "network-policy-audit.log"
    probe = (
        "import socket\n"
        "server=socket.socket()\n"
        "server.bind(('127.0.0.1',0))\n"
        "server.listen(1)\n"
        "client=socket.create_connection(server.getsockname(),timeout=2)\n"
        "accepted,_=server.accept()\n"
        "client.close(); accepted.close(); server.close()\n"
        "try:\n"
        " socket.create_connection(('203.0.113.1',443),timeout=0.1)\n"
        "except OSError:\n"
        " pass\n"
        "else:\n"
        " raise SystemExit(91)\n"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "KEYLESS_GATE_NETWORK_AUDIT": str(audit),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(harness_root),
        }
    )
    completed = subprocess.run(
        [str(python), "-c", probe],
        cwd=work_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    _require(
        completed.returncode == 0
        and audit.is_file()
        and audit.read_bytes() == b"blocked\n",
        "network.policy_not_enforced",
    )


def _write_synthetic_inputs(work_root: Path) -> tuple[Path, Path]:
    inputs = work_root / "inputs"
    inputs.mkdir(mode=0o700)
    source = inputs / "course.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AcceptanceFailure("media.ffmpeg_missing")
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(source),
        ],
        cwd=inputs,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    _require(completed.returncode == 0 and source.is_file(), "media.build_failed")
    source.with_suffix(".config.json").write_bytes(
        _json_bytes(
            {
                "schema_version": "configuration.v1",
                "clip_policy": {
                    "min_duration_seconds": 1,
                    "target_duration_seconds": 3,
                    "max_duration_seconds": 6,
                },
            }
        )
    )
    expected = inputs / "expected-transcript.json"
    expected.write_bytes(
        _json_bytes(
            {
                "schema_version": "installed_acceptance_transcript.v1",
                "speech_presence": "present",
                "source_duration_ms": 6_000,
                "chunks": [
                    {
                        "start_ms": 200,
                        "end_ms": 4_800,
                        "text": _TRANSCRIPT_TEXT,
                    }
                ],
            }
        )
    )
    return source, expected


def _tree_snapshot(directory: Path) -> dict[str, tuple[object, ...]]:
    if not directory.is_dir() or directory.is_symlink():
        raise AcceptanceFailure("case.tree_invalid")
    snapshot: dict[str, tuple[object, ...]] = {}
    try:
        for entry in sorted(directory.rglob("*")):
            relative = entry.relative_to(directory).as_posix()
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise AcceptanceFailure("case.tree_invalid")
            if stat.S_ISDIR(mode):
                snapshot[relative] = ("directory", stat.S_IMODE(mode))
            elif stat.S_ISREG(mode):
                contents = entry.read_bytes()
                snapshot[relative] = (
                    "file",
                    stat.S_IMODE(mode),
                    len(contents),
                    hashlib.sha256(contents).hexdigest(),
                )
            else:
                raise AcceptanceFailure("case.tree_invalid")
    except OSError:
        raise AcceptanceFailure("case.tree_invalid") from None
    return snapshot


def _run_directories(workspace: Path) -> set[str]:
    runs = workspace / "work" / "runs"
    if not runs.exists():
        return set()
    try:
        return {
            entry.name
            for entry in runs.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        }
    except OSError:
        raise AcceptanceFailure("case.diagnostics_invalid") from None


def _new_run_manifest(
    workspace: Path,
    before: set[str],
    reason_code: str,
) -> tuple[str, dict[str, Any]]:
    after = _run_directories(workspace)
    created = after - before
    _require(len(created) == 1, reason_code)
    run_id = next(iter(created))
    _require(_RUN_ID.fullmatch(run_id) is not None, reason_code)
    manifest = _read_case_json(
        workspace / "work" / "runs" / run_id / "run.json",
        reason_code,
    )
    return run_id, manifest


def _case_environment(
    *,
    scenario: str | None,
    harness_root: Path,
    case_root: Path,
    audit_name: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    for variable in (
        "STEPFUN_API_KEY",
        "INSTALLED_ACCEPTANCE_FIRST_SIGNAL",
        "INSTALLED_ACCEPTANCE_RENDEZVOUS",
        "INSTALLED_ACCEPTANCE_SCENARIO",
        "INSTALLED_ACCEPTANCE_SIGNAL",
    ):
        environment.pop(variable, None)
    environment.update(
        {
            "INSTALLED_ACCEPTANCE_PROCESS_AUDIT": str(
                case_root / f"{audit_name}-process.json"
            ),
            "KEYLESS_GATE_NETWORK_AUDIT": str(
                case_root / f"{audit_name}-network.log"
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(harness_root),
        }
    )
    if scenario is not None:
        environment["INSTALLED_ACCEPTANCE_SCENARIO"] = scenario
    if extra is not None:
        environment.update(extra)
    return environment


def _verify_process_audit(
    *,
    case_root: Path,
    audit_name: str,
    console: Path,
    environment_prefix: Path,
    source_root: Path,
) -> None:
    audit = _read_case_json(
        case_root / f"{audit_name}-process.json",
        "case.process_identity_invalid",
    )
    try:
        package_file = Path(audit["candidate_package_file"]).resolve(strict=True)
        observed_console = Path(audit["console"]).resolve(strict=True)
        observed_cwd = Path(audit["cwd"]).resolve(strict=True)
    except (KeyError, OSError, RuntimeError, TypeError):
        raise AcceptanceFailure("case.process_identity_invalid") from None
    _require(
        set(audit)
        == {
            "candidate_package_file",
            "console",
            "cwd",
            "production_credentials_present",
        }
        and package_file.is_relative_to(environment_prefix)
        and not package_file.is_relative_to(source_root)
        and os.path.samefile(observed_console, console)
        and observed_cwd == case_root
        and audit["production_credentials_present"] == [],
        "case.process_identity_invalid",
    )


def _run_live(
    *,
    console: Path,
    source: Path,
    workspace: Path,
    case_root: Path,
    scenario: str,
    harness_root: Path,
    environment_prefix: Path,
    source_root: Path,
    audit_name: str,
    overwrite: bool = False,
    timeout: int = 60,
) -> tuple[subprocess.CompletedProcess[bytes], str, dict[str, Any]]:
    before = _run_directories(workspace)
    arguments = [
        str(console),
        "live",
        str(source),
        "--workspace-dir",
        str(workspace),
    ]
    if overwrite:
        arguments.append("--overwrite")
    completed = subprocess.run(
        arguments,
        cwd=case_root,
        env=_case_environment(
            scenario=scenario,
            harness_root=harness_root,
            case_root=case_root,
            audit_name=audit_name,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    _verify_process_audit(
        case_root=case_root,
        audit_name=audit_name,
        console=console,
        environment_prefix=environment_prefix,
        source_root=source_root,
    )
    run_id, manifest = _new_run_manifest(
        workspace,
        before,
        "case.diagnostics_invalid",
    )
    return completed, run_id, manifest


def _wait_for_rendezvous(
    path: Path,
    process: subprocess.Popen[bytes],
    reason_code: str,
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.is_file():
            _require(path.read_bytes() == b"ready\n", reason_code)
            return
        if process.poll() is not None:
            break
        time.sleep(0.02)
    raise AcceptanceFailure(reason_code)


def _run_signalled_live(
    *,
    console: Path,
    source: Path,
    workspace: Path,
    case_root: Path,
    scenario: str,
    first_signal: int,
    harness_root: Path,
    environment_prefix: Path,
    source_root: Path,
    audit_name: str,
    overwrite: bool = False,
    second_signal: int | None = None,
    require_manifest: bool = True,
) -> tuple[int, bytes, bytes, str, dict[str, Any] | None]:
    before = _run_directories(workspace)
    rendezvous = case_root / f"{audit_name}-ready"
    first_observed = case_root / f"{audit_name}-first-observed"
    extra = {
        "INSTALLED_ACCEPTANCE_RENDEZVOUS": str(rendezvous),
        "INSTALLED_ACCEPTANCE_SIGNAL": str(first_signal),
    }
    if second_signal is not None:
        extra["INSTALLED_ACCEPTANCE_FIRST_SIGNAL"] = str(first_observed)
    environment = _case_environment(
        scenario=scenario,
        harness_root=harness_root,
        case_root=case_root,
        audit_name=audit_name,
        extra=extra,
    )
    arguments = [
        str(console),
        "live",
        str(source),
        "--workspace-dir",
        str(workspace),
    ]
    if overwrite:
        arguments.append("--overwrite")
    process = subprocess.Popen(
        arguments,
        cwd=case_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_rendezvous(
            rendezvous,
            process,
            "case.signal_rendezvous_failed",
        )
        process.send_signal(first_signal)
        if second_signal is not None:
            _wait_for_rendezvous(
                first_observed,
                process,
                "case.signal_rendezvous_failed",
            )
            process.send_signal(second_signal)
        stdout, stderr = process.communicate(timeout=20)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    _verify_process_audit(
        case_root=case_root,
        audit_name=audit_name,
        console=console,
        environment_prefix=environment_prefix,
        source_root=source_root,
    )
    after = _run_directories(workspace)
    created = after - before
    _require(len(created) == 1, "case.diagnostics_invalid")
    run_id = next(iter(created))
    _require(_RUN_ID.fullmatch(run_id) is not None, "case.diagnostics_invalid")
    manifest = (
        _read_case_json(
            workspace / "work" / "runs" / run_id / "run.json",
            "case.diagnostics_invalid",
        )
        if require_manifest
        else None
    )
    return process.returncode, stdout, stderr, run_id, manifest


def _run_repeated_signal(
    *,
    console: Path,
    source: Path,
    workspace: Path,
    case_root: Path,
    harness_root: Path,
    environment_prefix: Path,
    source_root: Path,
) -> int:
    rendezvous = case_root / "repeated-signal-ready"
    first_observed = case_root / "repeated-signal-first-observed"
    environment = _case_environment(
        scenario="repeated_signal",
        harness_root=harness_root,
        case_root=case_root,
        audit_name="repeated-signal",
        extra={
            "INSTALLED_ACCEPTANCE_RENDEZVOUS": str(rendezvous),
            "INSTALLED_ACCEPTANCE_FIRST_SIGNAL": str(first_observed),
        },
    )
    process = subprocess.Popen(
        [
            str(console),
            "live",
            str(source),
            "--workspace-dir",
            str(workspace),
        ],
        cwd=case_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_rendezvous(
            rendezvous,
            process,
            "case.signal_rendezvous_failed",
        )
        process.send_signal(signal.SIGINT)
        _wait_for_rendezvous(
            first_observed,
            process,
            "case.signal_rendezvous_failed",
        )
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    _verify_process_audit(
        case_root=case_root,
        audit_name="repeated-signal",
        console=console,
        environment_prefix=environment_prefix,
        source_root=source_root,
    )
    return process.returncode


def _validate_delivery(
    *,
    python: Path,
    validator: Path,
    delivery: Path,
    source: Path,
    application_version: str,
    expected_transcript: Path,
    case_root: Path,
    label: str,
) -> dict[str, Any]:
    result_path = case_root / f"{label}-validation.json"
    completed = subprocess.run(
        [
            str(python),
            "-I",
            str(validator),
            "--delivery",
            str(delivery),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            application_version,
            "--result",
            str(result_path),
        ],
        cwd=case_root,
        env={"LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=60,
    )
    _require(completed.returncode == 0, "case.delivery_validation_failed")
    result = _read_case_json(result_path, "case.delivery_validation_failed")
    _require(result.get("success") is True, "case.delivery_validation_failed")
    return result


def _case_result(
    *,
    exit_codes: Sequence[int],
    run_ids: Sequence[str] = (),
    short_video_count: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exit_codes": list(exit_codes),
        "run_ids": list(run_ids),
        "status": "passed",
    }
    if short_video_count is not None:
        result["short_video_count"] = short_video_count
    return result


def _start_case(cases: dict[str, dict[str, Any]], case_id: str) -> None:
    _require(case_id not in cases, "cases.incomplete")
    cases[case_id] = {"status": "running"}


def _assert_terminal_manifest(
    manifest: Mapping[str, Any],
    *,
    outcome: str,
    exit_code: int,
    reason_code: str,
) -> None:
    try:
        lifecycle = manifest["lifecycle"]
    except (KeyError, TypeError):
        raise AcceptanceFailure(reason_code) from None
    _require(
        isinstance(lifecycle, dict)
        and lifecycle.get("outcome") == outcome
        and lifecycle.get("exit_code") == exit_code,
        reason_code,
    )


def _assert_run_temporary_removed(workspace: Path, run_id: str) -> None:
    _require(
        not (workspace / "work" / "tmp" / run_id).exists(),
        "case.cleanup_failed",
    )


def _execute_matrix(
    *,
    console: Path,
    python: Path,
    environment_prefix: Path,
    application_version: str,
    source_root: Path,
    harness: Mapping[str, Path],
    work_root: Path,
    source: Path,
    expected_transcript: Path,
    cases: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    harness_root = harness["root"]
    validator = harness["validate_installed_delivery.py"]
    _start_case(cases, "short_video_success")
    clips_root = work_root / "short-video-success"
    clips_root.mkdir(mode=0o700)
    clips_workspace = clips_root / "workspace"
    completed, clips_run_id, clips_manifest = _run_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="clips",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="clips",
    )
    _require(
        completed.returncode == 0 and "终态: succeeded".encode() in completed.stdout,
        "case.short_video_success.failed",
    )
    _assert_terminal_manifest(
        clips_manifest,
        outcome="succeeded",
        exit_code=0,
        reason_code="case.short_video_success.failed",
    )
    clips_validation = _validate_delivery(
        python=python,
        validator=validator,
        delivery=clips_workspace / "delivery",
        source=source,
        application_version=application_version,
        expected_transcript=expected_transcript,
        case_root=clips_root,
        label="clips",
    )
    _require(
        clips_validation.get("result_kind") == "clips"
        and clips_validation.get("short_video_count") == 1,
        "case.short_video_success.failed",
    )
    _assert_run_temporary_removed(clips_workspace, clips_run_id)
    cases["short_video_success"] = _case_result(
        exit_codes=(0,),
        run_ids=(clips_run_id,),
        short_video_count=1,
    )

    _start_case(cases, "effective_empty")
    empty_root = work_root / "effective-empty"
    empty_root.mkdir(mode=0o700)
    empty_workspace = empty_root / "workspace"
    completed, empty_run_id, empty_manifest = _run_live(
        console=console,
        source=source,
        workspace=empty_workspace,
        case_root=empty_root,
        scenario="empty",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="empty",
    )
    _require(completed.returncode == 0, "case.effective_empty.failed")
    _assert_terminal_manifest(
        empty_manifest,
        outcome="succeeded",
        exit_code=0,
        reason_code="case.effective_empty.failed",
    )
    empty_validation = _validate_delivery(
        python=python,
        validator=validator,
        delivery=empty_workspace / "delivery",
        source=source,
        application_version=application_version,
        expected_transcript=expected_transcript,
        case_root=empty_root,
        label="empty",
    )
    _require(
        empty_validation.get("result_kind") == "empty"
        and empty_validation.get("short_video_count") == 0,
        "case.effective_empty.failed",
    )
    _assert_run_temporary_removed(empty_workspace, empty_run_id)
    cases["effective_empty"] = _case_result(
        exit_codes=(0,),
        run_ids=(empty_run_id,),
        short_video_count=0,
    )

    _start_case(cases, "typed_failure")
    current_before_failure = _tree_snapshot(clips_workspace / "delivery")
    previous_before_failure = _tree_snapshot(
        clips_workspace / "delivery.previous"
    )
    completed, failure_run_id, failure_manifest = _run_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="typed_failure",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="typed-failure",
        overwrite=True,
    )
    try:
        primary_error = failure_manifest["errors"]["primary_error"]
    except (KeyError, TypeError):
        raise AcceptanceFailure("case.typed_failure.failed") from None
    _require(
        completed.returncode == 30
        and "终态: failed".encode() in completed.stderr
        and primary_error.get("error_code")
        == "transcription.service_unavailable"
        and _tree_snapshot(clips_workspace / "delivery")
        == current_before_failure
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == previous_before_failure,
        "case.typed_failure.failed",
    )
    _assert_terminal_manifest(
        failure_manifest,
        outcome="failed",
        exit_code=30,
        reason_code="case.typed_failure.failed",
    )
    _assert_run_temporary_removed(clips_workspace, failure_run_id)
    cases["typed_failure"] = _case_result(
        exit_codes=(30,),
        run_ids=(failure_run_id,),
    )

    _start_case(cases, "overwrite")
    current_before_overwrite = _tree_snapshot(clips_workspace / "delivery")
    previous_before_overwrite = _tree_snapshot(
        clips_workspace / "delivery.previous"
    )
    rejected, rejected_run_id, rejected_manifest = _run_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="clips",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="overwrite-rejected",
    )
    try:
        rejected_error = rejected_manifest["errors"]["primary_error"]
    except (KeyError, TypeError):
        raise AcceptanceFailure("case.overwrite.failed") from None
    _require(
        rejected.returncode == 60
        and rejected_error.get("error_code") == "publication.commit_failed"
        and rejected_error.get("diagnostics", {}).get("reason_code")
        == "publication.destination_not_empty"
        and _tree_snapshot(clips_workspace / "delivery")
        == current_before_overwrite
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == previous_before_overwrite,
        "case.overwrite.failed",
    )
    overwritten, overwrite_run_id, overwrite_manifest = _run_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="clips",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="overwrite-allowed",
        overwrite=True,
    )
    _require(
        overwritten.returncode == 0
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == current_before_overwrite,
        "case.overwrite.failed",
    )
    _assert_terminal_manifest(
        overwrite_manifest,
        outcome="succeeded",
        exit_code=0,
        reason_code="case.overwrite.failed",
    )
    try:
        cache_facts = overwrite_manifest["cache"]["namespaces"]
    except (KeyError, TypeError):
        raise AcceptanceFailure("case.overwrite.failed") from None
    _require(
        cache_facts["topic_review"]["hits"] >= 1
        and cache_facts["subtitle_optimization"]["hits"] >= 1,
        "case.overwrite.failed",
    )
    overwrite_validation = _validate_delivery(
        python=python,
        validator=validator,
        delivery=clips_workspace / "delivery",
        source=source,
        application_version=application_version,
        expected_transcript=expected_transcript,
        case_root=clips_root,
        label="overwrite",
    )
    _require(
        overwrite_validation.get("short_video_count") == 1,
        "case.overwrite.failed",
    )
    _assert_run_temporary_removed(clips_workspace, rejected_run_id)
    _assert_run_temporary_removed(clips_workspace, overwrite_run_id)
    cases["overwrite"] = _case_result(
        exit_codes=(60, 0),
        run_ids=(rejected_run_id, overwrite_run_id),
    )

    _start_case(cases, "rollback")
    current_before_rollback = _tree_snapshot(clips_workspace / "delivery")
    previous_before_rollback = _tree_snapshot(
        clips_workspace / "delivery.previous"
    )
    (
        rollback_exit,
        _rollback_stdout,
        rollback_stderr,
        rollback_run_id,
        rollback_manifest,
    ) = _run_signalled_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="rollback",
        first_signal=signal.SIGTERM,
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="rollback",
        overwrite=True,
    )
    assert rollback_manifest is not None
    _require(
        rollback_exit == 143
        and "终态: interrupted".encode() in rollback_stderr
        and _tree_snapshot(clips_workspace / "delivery")
        == current_before_rollback
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == previous_before_rollback
        and rollback_manifest.get("delivery", {}).get("publication_state")
        == "rolled_back",
        "case.rollback.failed",
    )
    _assert_terminal_manifest(
        rollback_manifest,
        outcome="interrupted",
        exit_code=143,
        reason_code="case.rollback.failed",
    )
    _assert_run_temporary_removed(clips_workspace, rollback_run_id)
    cases["rollback"] = _case_result(
        exit_codes=(143,),
        run_ids=(rollback_run_id,),
    )

    _start_case(cases, "cache_maintenance")
    cache = clips_workspace / "work" / "cache"
    cache_stat = cache.stat(follow_symlinks=False)
    _require(
        stat.S_ISDIR(cache_stat.st_mode) and bool(_tree_snapshot(cache)),
        "case.cache_maintenance.failed",
    )
    current_before_cache = _tree_snapshot(clips_workspace / "delivery")
    previous_before_cache = _tree_snapshot(
        clips_workspace / "delivery.previous"
    )
    runs_before_cache = _tree_snapshot(clips_workspace / "work" / "runs")
    cache_exits: list[int] = []
    for index in range(2):
        cleared = subprocess.run(
            [str(console), "cache", "clear", str(clips_workspace)],
            cwd=clips_root,
            env=_case_environment(
                scenario=None,
                harness_root=harness_root,
                case_root=clips_root,
                audit_name=f"cache-{index}",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
        cache_exits.append(cleared.returncode)
        _require(
            cleared.returncode == 0
            and cleared.stdout == "处理缓存已清空\n".encode(),
            "case.cache_maintenance.failed",
        )
    cache_after = cache.stat(follow_symlinks=False)
    unmanaged = clips_root / "unmanaged"
    unmanaged.mkdir(mode=0o700)
    sentinel = unmanaged / "keep.txt"
    sentinel.write_bytes(b"keep")
    rejected_cache = subprocess.run(
        [str(console), "cache", "clear", str(unmanaged)],
        cwd=clips_root,
        env=_case_environment(
            scenario=None,
            harness_root=harness_root,
            case_root=clips_root,
            audit_name="cache-unmanaged",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    cache_exits.append(rejected_cache.returncode)
    _require(
        cache_exits == [0, 0, 10]
        and not _tree_snapshot(cache)
        and cache_after.st_ino == cache_stat.st_ino
        and stat.S_IMODE(cache_after.st_mode) == 0o700
        and _tree_snapshot(clips_workspace / "work" / "runs")
        == runs_before_cache
        and _tree_snapshot(clips_workspace / "delivery")
        == current_before_cache
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == previous_before_cache
        and sentinel.read_bytes() == b"keep",
        "case.cache_maintenance.failed",
    )
    cases["cache_maintenance"] = _case_result(exit_codes=cache_exits)

    for case_id, scenario, signal_number, expected_exit in (
        ("sigint", "sigint", signal.SIGINT, 130),
        ("sigterm", "sigterm", signal.SIGTERM, 143),
    ):
        _start_case(cases, case_id)
        signal_root = work_root / case_id
        signal_root.mkdir(mode=0o700)
        signal_workspace = signal_root / "workspace"
        exit_code, _stdout, stderr, run_id, manifest = _run_signalled_live(
            console=console,
            source=source,
            workspace=signal_workspace,
            case_root=signal_root,
            scenario=scenario,
            first_signal=signal_number,
            harness_root=harness_root,
            environment_prefix=environment_prefix,
            source_root=source_root,
            audit_name=case_id,
        )
        assert manifest is not None
        _require(
            exit_code == expected_exit
            and "终态: interrupted".encode() in stderr
            and not _tree_snapshot(signal_workspace / "delivery")
            and manifest.get("errors", {}).get("primary_error")
            == {"status": "not_applicable"},
            f"case.{case_id}.failed",
        )
        _assert_terminal_manifest(
            manifest,
            outcome="interrupted",
            exit_code=expected_exit,
            reason_code=f"case.{case_id}.failed",
        )
        _assert_run_temporary_removed(signal_workspace, run_id)
        cases[case_id] = _case_result(
            exit_codes=(expected_exit,),
            run_ids=(run_id,),
        )

    _start_case(cases, "repeated_signal")
    repeated_root = work_root / "repeated-signal"
    repeated_root.mkdir(mode=0o700)
    repeated_workspace = repeated_root / "workspace"
    repeated_exit = _run_repeated_signal(
        console=console,
        source=source,
        workspace=repeated_workspace,
        case_root=repeated_root,
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
    )
    _require(
        repeated_exit == 130
        and not repeated_workspace.exists(),
        "case.repeated_signal.failed",
    )
    recovered, recovery_run_id, recovery_manifest = _run_live(
        console=console,
        source=source,
        workspace=repeated_workspace,
        case_root=repeated_root,
        scenario="empty",
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="repeated-recovery",
    )
    _require(recovered.returncode == 0, "case.repeated_signal.failed")
    _assert_terminal_manifest(
        recovery_manifest,
        outcome="succeeded",
        exit_code=0,
        reason_code="case.repeated_signal.failed",
    )
    cases["repeated_signal"] = _case_result(
        exit_codes=(130, 0),
        run_ids=(recovery_run_id,),
    )

    _start_case(cases, "postcommit_signal")
    current_before_postcommit = _tree_snapshot(clips_workspace / "delivery")
    (
        postcommit_exit,
        _postcommit_stdout,
        _postcommit_stderr,
        postcommit_run_id,
        postcommit_manifest,
    ) = _run_signalled_live(
        console=console,
        source=source,
        workspace=clips_workspace,
        case_root=clips_root,
        scenario="postcommit_signal",
        first_signal=signal.SIGINT,
        harness_root=harness_root,
        environment_prefix=environment_prefix,
        source_root=source_root,
        audit_name="postcommit-signal",
        overwrite=True,
    )
    assert postcommit_manifest is not None
    _require(
        postcommit_exit == 0
        and _tree_snapshot(clips_workspace / "delivery.previous")
        == current_before_postcommit,
        "case.postcommit_signal.failed",
    )
    _assert_terminal_manifest(
        postcommit_manifest,
        outcome="succeeded",
        exit_code=0,
        reason_code="case.postcommit_signal.failed",
    )
    postcommit_validation = _validate_delivery(
        python=python,
        validator=validator,
        delivery=clips_workspace / "delivery",
        source=source,
        application_version=application_version,
        expected_transcript=expected_transcript,
        case_root=clips_root,
        label="postcommit",
    )
    _require(
        postcommit_validation.get("short_video_count") == 1,
        "case.postcommit_signal.failed",
    )
    cases["postcommit_signal"] = _case_result(
        exit_codes=(0,),
        run_ids=(postcommit_run_id,),
    )

    _require(set(cases) == set(_CASE_IDS), "cases.incomplete")
    return cases


def run_acceptance(
    *,
    wheel: Path,
    runtime_lock: Path,
    commit_sha: str,
    apt_snapshot_id: str,
    installation_prefix: Path,
    source_root: Path,
    harness_root: Path,
    work_root: Path,
    evidence_path: Path,
) -> bool:
    evidence: dict[str, Any] | None = None
    try:
        candidate = _candidate_identity(
            wheel=wheel,
            runtime_lock=runtime_lock,
            commit_sha=commit_sha,
            apt_snapshot_id=apt_snapshot_id,
        )
        network = _network_attestation()
        evidence = _empty_evidence(candidate, network)
        try:
            source = source_root.resolve(strict=True)
            work = work_root.resolve(strict=False)
        except (OSError, RuntimeError):
            raise AcceptanceFailure("workspace.invalid") from None
        if work == source or work.is_relative_to(source):
            raise AcceptanceFailure("workspace.inside_source")
        try:
            work.mkdir(mode=0o700)
        except FileExistsError:
            if (
                not work.is_dir()
                or work.is_symlink()
                or any(work.iterdir())
            ):
                raise AcceptanceFailure("workspace.invalid") from None
        except OSError:
            raise AcceptanceFailure("workspace.invalid") from None
        installation = _verify_installation(installation_prefix, candidate)
        evidence["installation"] = installation
        harness = _verify_harness(harness_root)
        console = Path(str(installation["console"])).resolve(strict=True)
        python = Path(str(installation["python"]))
        environment_prefix = Path(
            str(installation["environment_prefix"])
        ).resolve(strict=True)
        _probe_network_policy(
            python=python,
            harness_root=harness["root"],
            work_root=work,
        )
        evidence["network"] = {
            "external_blocked": True,
            "loopback_allowed": True,
            "mode": network["mode"],
        }
        source_media, expected_transcript = _write_synthetic_inputs(work)
        cases: dict[str, dict[str, Any]] = {}
        evidence["cases"] = cases
        _execute_matrix(
            console=console,
            python=python,
            environment_prefix=environment_prefix,
            application_version=str(installation["application_version"]),
            source_root=source,
            harness=harness,
            work_root=work,
            source=source_media,
            expected_transcript=expected_transcript,
            cases=cases,
        )
        evidence["statistics"] = {
            "failed": 0,
            "passed": len(cases),
            "total": len(cases),
        }
        evidence["success"] = True
        _write_evidence(evidence_path, evidence)
        return True
    except AcceptanceFailure as failure:
        if evidence is None:
            try:
                candidate = _candidate_identity(
                    wheel=wheel,
                    runtime_lock=runtime_lock,
                    commit_sha=commit_sha,
                    apt_snapshot_id=apt_snapshot_id,
                )
            except AcceptanceFailure:
                candidate = {}
            try:
                network = _network_attestation()
            except AcceptanceFailure:
                network = {
                    "external_blocked": False,
                    "loopback_allowed": False,
                    "mode": os.environ.get("KEYLESS_GATE_NETWORK_MODE", ""),
                }
            evidence = _empty_evidence(candidate, network)
        _record_failure(evidence, failure.reason_code)
        _write_evidence(evidence_path, evidence)
        return False
    except Exception:
        if evidence is None:
            evidence = _empty_evidence({}, {
                "external_blocked": False,
                "loopback_allowed": False,
                "mode": os.environ.get("KEYLESS_GATE_NETWORK_MODE", ""),
            })
        _record_failure(evidence, "harness.internal_error")
        _write_evidence(evidence_path, evidence)
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对已安装候选 CLI 执行仓库外独立验收",
    )
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--runtime-lock", required=True, type=Path)
    parser.add_argument("--apt-snapshot-id", required=True)
    parser.add_argument("--installation-prefix", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--harness-root", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    succeeded = run_acceptance(
        wheel=arguments.wheel,
        runtime_lock=arguments.runtime_lock,
        commit_sha=arguments.commit_sha,
        apt_snapshot_id=arguments.apt_snapshot_id,
        installation_prefix=arguments.installation_prefix,
        source_root=arguments.source_root,
        harness_root=arguments.harness_root,
        work_root=arguments.work_root,
        evidence_path=arguments.evidence,
    )
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
