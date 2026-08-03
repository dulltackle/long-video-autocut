import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_GATE = PROJECT_ROOT / "scripts" / "run_release_gate.py"
COMMIT_SHA = "a" * 40
WHEEL_NAME = "video_auto_editor-4.7.0-py3-none-any.whl"
SNAPSHOT_ID = "20260725T000000Z"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_wheel(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "video_auto_editor-4.7.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: video-auto-editor\n"
            "Version: 4.7.0\n",
        )


def _release_gate_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    workspace_parent = private_root / "workspaces"
    workspace_parent.mkdir(mode=0o700)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    installation_prefix = artifacts / "installation"
    version_directory = installation_prefix / "versions" / "4.7.0"
    console_directory = version_directory / "venv" / "bin"
    console_directory.mkdir(parents=True)
    private_inputs = private_root / "inputs"
    private_inputs.mkdir(mode=0o700)

    wheel = artifacts / WHEEL_NAME
    _write_wheel(wheel)
    build_lock = artifacts / "requirements-build.lock"
    build_lock.write_text("build==1.3.0\n", encoding="utf-8")
    runtime_lock = artifacts / "requirements-runtime.lock"
    runtime_lock.write_text("# no runtime dependencies\n", encoding="utf-8")
    installation_manifest = version_directory / "installation-manifest.json"
    _write_json(
        installation_manifest,
        {
            "schema_version": "production-installation-manifest.v1",
            "application": {
                "name": "video-auto-editor",
                "version": "4.7.0",
                "wheel": {
                    "filename": wheel.name,
                    "sha256": _sha256(wheel),
                },
            },
            "apt_snapshot_id": SNAPSHOT_ID,
            "environment": {
                "ffmpeg_version": "6.1.1-3ubuntu5",
                "ffprobe_version": "6.1.1-3ubuntu5",
                "font_family": "Noto Sans CJK SC",
                "font_file": (
                    "/usr/share/fonts/opentype/noto/"
                    "NotoSansCJK-Regular.ttc"
                ),
            },
            "installation_prefix": str(installation_prefix),
            "platform": {
                "architecture": "amd64",
                "operating_system": "ubuntu",
                "operating_system_version": "24.04",
            },
            "python": {"implementation": "CPython", "version": "3.12.3"},
            "runtime_lock": {
                "filename": runtime_lock.name,
                "sha256": _sha256(runtime_lock),
            },
            "snapshot_packages": {
                "ca-certificates": "20240203",
                "ffmpeg": "7:6.1.1-3ubuntu5",
                "fontconfig": "2.15.0-1.1ubuntu2",
                "fonts-noto-cjk": "1:20230817+repack1-3",
                "python3.12": "3.12.3-1ubuntu0.8",
                "python3.12-venv": "3.12.3-1ubuntu0.8",
            },
            "system_packages": {
                "ca-certificates": "20240203",
                "ffmpeg": "7:6.1.1-3ubuntu5",
                "fontconfig": "2.15.0-1.1ubuntu2",
                "fonts-noto-cjk": "1:20230817+repack1-3",
                "python3.12": "3.12.3-1ubuntu0.8",
                "python3.12-venv": "3.12.3-1ubuntu0.8",
            },
            "wheelhouse": [],
        },
    )
    installation_ready = version_directory / "READY"
    _write_json(
        installation_ready,
        {
            "schema_version": "production-installation-ready.v1",
            "installation_manifest_sha256": _sha256(installation_manifest),
        },
    )
    keyless_evidence = artifacts / "keyless-gate-evidence.json"
    installed_evidence = artifacts / "installed-acceptance-evidence.json"
    _write_json(
        installed_evidence,
        {
            "schema_version": "installed_acceptance_evidence.v1",
            "candidate": {
                "commit_sha": COMMIT_SHA,
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel),
                "runtime_lock_filename": runtime_lock.name,
                "runtime_lock_sha256": _sha256(runtime_lock),
                "apt_snapshot_id": SNAPSHOT_ID,
            },
            "success": True,
        },
    )

    source = private_inputs / "course.mp4"
    source.write_bytes(b"real-chinese-course")
    source.chmod(0o600)
    configuration = private_inputs / "course.config.json"
    _write_json(configuration, {"schema_version": "configuration.v1"})
    configuration.chmod(0o600)
    context = private_inputs / "course.context.json"
    _write_json(
        context,
        {
            "schema_version": "course_context.v1",
            "course_topic": "真实中文课程",
            "priority_topics": ["核心主题"],
            "excluded_content": ["课间闲聊"],
        },
    )
    context.chmod(0o600)
    expected_transcript = private_inputs / "expected-transcript.json"
    _write_json(
        expected_transcript,
        {
            "schema_version": "installed_acceptance_transcript.v1",
            "speech_presence": "present",
            "source_duration_ms": 600_000,
            "chunks": [],
        },
    )
    expected_transcript.chmod(0o600)
    console = console_directory / "video-auto-editor"
    console.write_text(
        """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

if not os.environ.get("STEPFUN_API_KEY"):
    raise SystemExit(9)
arguments = sys.argv[1:]
if len(arguments) not in {4, 5} or arguments[0] != "live":
    raise SystemExit(8)
if os.environ.get("FAKE_RELEASE_GATE_MODE") == "leak_secret":
    print(os.environ["STEPFUN_API_KEY"])
    raise SystemExit(5)
workspace = Path(arguments[3])
overwrite = arguments[4:] == ["--overwrite"]
runs = workspace / "work" / "runs"
runs.mkdir(parents=True, exist_ok=True)
ordinal = len(tuple(runs.iterdir())) + 1
if ordinal == 1 and overwrite:
    raise SystemExit(7)
if ordinal == 2 and not overwrite:
    raise SystemExit(6)
run_id = (
    "run_11111111-1111-4111-8111-111111111111"
    if ordinal == 1
    else "run_22222222-2222-4222-8222-222222222222"
)
delivery = workspace / "delivery"
if overwrite:
    previous = workspace / "delivery.previous"
    if previous.exists():
        shutil.rmtree(previous)
    delivery.rename(previous)
    if os.environ.get("FAKE_RELEASE_GATE_MODE") == "mutate_previous":
        previous_manifest = json.loads((previous / "manifest.json").read_text())
        previous_manifest["published_at"] = "2026-08-03T23:59:59.000Z"
        (previous / "manifest.json").write_text(
            json.dumps(previous_manifest, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
delivery.mkdir()
transcript_id = f"transcript_{ordinal}3333333-3333-4333-8333-333333333333"
candidate_id = f"candidate_{ordinal}4444444-4444-4444-8444-444444444444"
short_video_id = f"short_video_{ordinal}5555555-5555-4555-8555-555555555555"
documents = {
    "transcript.json": {
        "schema_version": "transcript.v1",
        "run_id": run_id,
        "transcript_id": transcript_id,
        "source_duration_ms": 600000,
        "speech_presence": "present",
        "chunks": [{
            "transcript_chunk_id": f"transcript_chunk_{ordinal}6666666-6666-4666-8666-666666666666",
            "start_ms": 1000,
            "end_ms": 180000,
            "text": "真实中文课程内容。",
        }],
    },
    "plan.json": {
        "schema_version": "clip_plan.v1",
        "run_id": run_id,
        "plan_id": f"plan_{ordinal}7777777-7777-4777-8777-777777777777",
        "transcript_id": transcript_id,
        "result_kind": "clips",
        "candidate_count": 1,
        "published_count": 1,
        "candidates": [{
            "candidate_id": candidate_id,
            "transcript_chunk_ids": [f"transcript_chunk_{ordinal}6666666-6666-4666-8666-666666666666"],
            "initial_range": {"start_ms": 1000, "end_ms": 180000},
            "final_range": {"start_ms": 1000, "end_ms": 180000},
            "boundary_remedy": {"status": "not_needed", "suggestion": "", "requested_start_ms": None, "requested_end_ms": None},
            "review": {"topic_name": "课程主题", "topic_complete": True, "learning_value": 9, "share_value": 8, "publish_ready_score": 92, "export_decision": "publish", "title": "课程标题", "summary": "课程摘要", "keywords": ["课程"], "needs_human_review": False, "reject_reason": "", "boundary_fix_suggestion": "", "boundary_fix_start_ms": None, "boundary_fix_end_ms": None},
            "selection": {"outcome": "published", "short_video_id": short_video_id},
        }],
        "short_videos": [{"short_video_id": short_video_id, "source_candidate_id": candidate_id, "title": "课程标题", "summary": "课程摘要", "keywords": ["课程"], "final_range": {"start_ms": 1000, "end_ms": 180000}}],
        "series": [],
    },
    "metadata.json": {
        "schema_version": "short_video_catalog.v1",
        "run_id": run_id,
        "result_kind": "clips",
        "short_videos": [{"short_video_id": short_video_id, "source_candidate_id": candidate_id, "topic_name": "课程主题", "title": "课程标题", "summary": "课程摘要", "keywords": ["课程"], "start_ms": 1000, "end_ms": 180000, "duration_ms": 179000, "media": {"path": f"clips/{short_video_id}.mp4", "container": "mp4", "video_required": True, "audio_required": True}, "subtitles": {"kind": "burned_in"}}],
        "series": [],
    },
}
for name, document in documents.items():
    (delivery / name).write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\\n", encoding="utf-8")
(delivery / "transcript.srt").write_text("1\\n00:00:01,000 --> 00:03:00,000\\n真实中文课程内容。\\n", encoding="utf-8")
(delivery / "report.md").write_text(
    os.environ["STEPFUN_API_KEY"]
    if os.environ.get("FAKE_RELEASE_GATE_MODE") == "leak_file"
    else "# 直播拆条报告\\n",
    encoding="utf-8",
)
(delivery / "clips").mkdir()
(delivery / "clips" / f"{short_video_id}.mp4").write_bytes(b"mp4")
manifest = {
    "schema_version": "delivery_manifest.v1",
    "run_id": run_id,
    "terminal_state": "succeeded",
    "result_kind": "clips",
    "started_at": f"2026-08-03T0{ordinal}:00:00.000Z",
    "published_at": f"2026-08-03T0{ordinal}:10:00.000Z",
    "application_version": "4.7.0",
    "source": {"sha256": os.environ["FAKE_SOURCE_SHA256"], "byte_length": 19, "duration_ms": 600000},
    "documents": {},
    "execution": {"subtitle_optimization": {"short_video_count": 1}},
    "files": [{"path": f"clips/{short_video_id}.mp4", "role": "short_video_media", "media_type": "video/mp4", "byte_length": 3, "sha256": "sha256:" + "b" * 64}],
}
(delivery / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\\n", encoding="utf-8")

def cache_stats(hits, misses, writes):
    return {"queries": hits + misses, "hits": hits, "misses": misses, "corrupt_quarantined": 0, "writes_published": writes, "writes_already_present": 0, "infrastructure_failures": 0, "singleflight_wait_count": 0, "singleflight_wait_ms_total": 0}

namespaces = (
    {"transcript": cache_stats(0, 1, 1), "transcription_shard": cache_stats(0, 1, 1), "topic_review": cache_stats(0, 1, 1), "subtitle_optimization": cache_stats(0, 1, 1)}
    if ordinal == 1
    else {"transcript": cache_stats(1, 0, 0), "topic_review": cache_stats(1, 0, 0), "subtitle_optimization": cache_stats(1, 0, 0)}
)
services = []
for capability, provider, model in (("transcription", "stepaudio", "stepaudio-2.5-asr"), ("topic_review", "stepfun", "step-2-mini"), ("subtitle_optimization", "stepfun", "step-2-mini")):
    count = 1 if ordinal == 1 or os.environ.get("FAKE_RELEASE_GATE_MODE") == "warm_request" else 0
    if ordinal == 1 and capability == "transcription" and os.environ.get("FAKE_RELEASE_GATE_MODE") == "partial_token_usage":
        count = 2
    attempt_count = 1 if ordinal == 2 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "warm_attempt" else count
    token_usage = ({"status": "not_applicable"} if not count else {"status": "not_reported"})
    if count == 2:
        token_usage = {"status": "partially_reported", "input_tokens": 10, "output_tokens": 5, "reported_request_count": 1}
    categories = {
        "transcription": ["audio_shard"],
        "topic_review": ["business_constraints", "candidate_transcript", "course_context"],
        "subtitle_optimization": ["fixed_instructions", "subtitle_window"],
    }[capability]
    endpoint_origin = (
        "https://attacker.example"
        if os.environ.get("FAKE_RELEASE_GATE_MODE") == "wrong_endpoint_origin"
        else "https://api.stepfun.com"
    )
    services.append({"capability": capability, "adapter_id": provider, "provider_id": provider, "model_id": model, "configuration_fingerprint": "sha256:" + "d" * 64, "endpoint": {"status": "available", "origin": endpoint_origin}, "transport": "remote", "purpose": capability, "allowed_data_categories": categories, "contact": {"status": "contacted"} if count else {"status": "not_contacted", "reason": "cache_hit"}, "requests": {"count": count, "succeeded": count, "failed": 0, "attempt_count_total": attempt_count, "duration_ms_total": count, "duration_ms_max": count, "token_usage": token_usage}})
artifact_roles = {"manifest": 1, "transcript_json": 1, "transcript_srt": 1, "plan": 1, "metadata": 1, "report": 1, "short_video": 1}
run_manifest = {
    "schema_version": "run_manifest.v1",
    "identity": {
        "run_id": run_id,
        "application_version": "4.7.0",
        "release": {"status": "unknown"},
    },
    "lifecycle": {"started_at": f"2026-08-03T0{ordinal}:00:00.000Z", "ended_at": f"2026-08-03T0{ordinal}:10:00.000Z", "duration_ms": 600000, "outcome": "succeeded", "exit_code": 0, "result_kind": {"status": "available", "value": "clips"}, "interruption": {"status": "not_applicable"}},
    "source": {"status": "available", "sha256": os.environ["FAKE_SOURCE_SHA256"], "byte_length": 19, "duration_ms": 600000, "course_context": {"provided": True, "sha256": {"status": "available", "value": os.environ["FAKE_CONTEXT_SHA256"]}}},
    "environment": {"status": "available", "certified_platform": "ubuntu_24_04_amd64", "python_version": "3.12.3", "ffmpeg_version": "6.1.1", "ffprobe_version": "6.1.1", "font": {"family": "Noto Sans CJK SC", "available": True}, "installation_fingerprint": os.environ["FAKE_INSTALLATION_FINGERPRINT"], "preflight_outcome": "succeeded", "application_version": "4.7.0"},
    "configuration": {"status": "available", "configuration_fingerprint": "sha256:" + "c" * 64, "result_configuration": {"transcription_provider": "stepaudio", "transcription_model": "stepaudio-2.5-asr", "text_model_provider": "stepfun", "topic_review": {"model": "step-2-mini"}, "subtitle_optimization": {"model": "step-2-mini"}}, "course_context": {"provided": True, "attribution_provided": False, "priority_topic_count": 1, "excluded_content_count": 1}},
    "cache": {"status": "observed", "namespaces": namespaces},
    "external_services": {"status": "observed", "services": services},
    "delivery": {"build_state": "completed", "verification_state": "passed", "publication_state": "committed", "artifacts": {"status": "observed", "created_by_role": artifact_roles, "verified_by_role": artifact_roles}},
    "errors": {"primary_error": {"status": "not_applicable"}, "associated_errors": [], "recovery_incomplete": False},
}
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "bad_environment":
    run_manifest["environment"]["font"]["available"] = False
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "unbound_context":
    run_manifest["source"]["course_context"]["sha256"]["value"] = "sha256:" + "d" * 64
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "bad_configuration":
    run_manifest["configuration"]["course_context"]["provided"] = False
if (
    ordinal == 1
    and os.environ.get("FAKE_RELEASE_GATE_MODE") == "provider_transient"
) or (
    ordinal == 2
    and os.environ.get("FAKE_RELEASE_GATE_MODE") == "warm_provider_transient"
):
    run_manifest["lifecycle"].update({"outcome": "failed", "exit_code": 30, "result_kind": {"status": "not_applicable"}})
    run_manifest["errors"]["primary_error"] = {"status": "available", "error_code": "transcription.service_unavailable", "category": "external_service"}
run_root = runs / run_id
run_root.mkdir()
(run_root / "run.json").write_text(json.dumps(run_manifest, sort_keys=True) + "\\n", encoding="utf-8")
(run_root / "events.jsonl").write_text("{}\\n", encoding="utf-8")
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "leak_binary_failure":
    (workspace / "credential-leak.bin").write_bytes(os.environ["STEPFUN_API_KEY"].encode())
    raise SystemExit(31)
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "leak_filename_failure":
    (workspace / (os.environ["STEPFUN_API_KEY"] + "-filename")).write_text("public")
    raise SystemExit(32)
if ordinal == 1 and os.environ.get("FAKE_RELEASE_GATE_MODE") == "leak_symlink_failure":
    (workspace / "credential-target-link").symlink_to(
        os.environ["STEPFUN_API_KEY"] + "-target"
    )
    raise SystemExit(33)
print("fake live complete")
if (
    ordinal == 1
    and os.environ.get("FAKE_RELEASE_GATE_MODE") == "provider_transient"
) or (
    ordinal == 2
    and os.environ.get("FAKE_RELEASE_GATE_MODE") == "warm_provider_transient"
):
    raise SystemExit(30)
""",
        encoding="utf-8",
    )
    console.chmod(0o755)
    validator = artifacts / "validate_installed_delivery.py"
    validator.write_text(
        """import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--delivery", type=Path, required=True)
parser.add_argument("--expected-transcript", required=True)
parser.add_argument("--source", required=True)
parser.add_argument("--expected-application-version", required=True)
parser.add_argument("--result", type=Path, required=True)
arguments = parser.parse_args()
manifest = json.loads((arguments.delivery / "manifest.json").read_text())
arguments.result.write_text(json.dumps({"artifact_count": 6, "checks": {"digests": True, "exact_file_set": True, "faithful_transcript": True, "mp4": True, "path_safety": True, "references": True, "schema": True}, "result_kind": "clips", "run_id": manifest["run_id"], "schema_version": "independent_delivery_validation.v1", "short_video_count": 1, "success": True}, sort_keys=True) + "\\n")
""",
        encoding="utf-8",
    )
    network_guard = artifacts / "run_keyless_gate_network.sh"
    network_guard.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "test \"${KEYLESS_GATE_REQUIRE_NAMESPACE:-}\" = 1\n"
        "fd=${RELEASE_GATE_NETWORK_ATTESTATION_FD:?}\n"
        "parent=${RELEASE_GATE_SYSTEMD_HOST_NETNS:?}\n"
        "if [ \"${FAKE_RELEASE_GATE_MODE:-}\" != missing_network_attestation ]; then\n"
        "eval \"printf 'release_gate_network.v1\\t%s\\tnet:[999999]\\tlo\\n' "
        "\\\"\\$parent\\\" >&$fd\"\n"
        "fi\n"
        "eval \"exec $fd>&-\"\n"
        "unset RELEASE_GATE_NETWORK_ATTESTATION_FD\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    network_guard.chmod(0o755)
    release_tool_paths = {
        "install-production.sh": PROJECT_ROOT / "scripts" / "install-production.sh",
        "run_keyless_gate_network.sh": network_guard,
        "run_release_gate.py": RELEASE_GATE,
        "systemd_credential_bridge.py": (
            PROJECT_ROOT / "scripts" / "systemd_credential_bridge.py"
        ),
        "validate_installed_delivery.py": validator,
        "validate_release_evidence.py": (
            PROJECT_ROOT / "scripts" / "validate_release_evidence.py"
        ),
    }
    _write_json(
        keyless_evidence,
        {
            "schema_version": "keyless_gate_evidence.v1",
            "candidate": {
                "commit_sha": COMMIT_SHA,
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel),
            },
            "layers": {
                "unit_schema": {"collected": 1, "passed": 1},
            },
            "credential_mode": "absent",
            "network": {
                "external_blocked": True,
                "loopback_allowed": True,
                "mode": "linux_network_namespace",
            },
            "release_tools": {
                name: _sha256(path)
                for name, path in release_tool_paths.items()
            },
            "success": True,
        },
    )

    request = {
        "schema_version": "release_gate_request.v1",
        "candidate": {
            "commit_sha": COMMIT_SHA,
            "wheel": str(wheel),
            "build_lock": str(build_lock),
            "runtime_lock": str(runtime_lock),
            "installation_manifest": str(installation_manifest),
            "installation_ready": str(installation_ready),
            "keyless_gate_evidence": str(keyless_evidence),
            "installed_acceptance_evidence": str(installed_evidence),
        },
        "certified_host": {
            "attestation_id": "certified-host-01",
            "apt_snapshot_id": SNAPSHOT_ID,
        },
        "automation": {
            "run_url": (
                "https://github.com/dulltackle/long-video-autocut/"
                "actions/runs/123456"
            ),
        },
        "inputs": {
            "source": {
                "path": str(source),
                "asset_id": "real-zh-course-v1",
                "version": "1",
                "language": "zh-CN",
                "content_summary": "真实中文课程素材，覆盖核心主题与完整上下文。",
                "duration_ms": 600_000,
            },
            "configuration": str(configuration),
            "course_context": str(context),
            "expected_transcript": str(expected_transcript),
        },
        "execution": {
            "console": str(console),
            "independent_validator": str(validator),
            "credential_bridge": str(
                PROJECT_ROOT / "scripts" / "systemd_credential_bridge.py"
            ),
            "network_guard": str(
                network_guard
            ),
            "workspace_parent": str(workspace_parent),
        },
        "release": {"version": "4.7.0", "tag": "v4.7.0"},
    }
    request_path = private_root / "request.json"
    _write_json(request_path, request)
    request_path.chmod(0o600)
    return request_path, private_root / "plan.json", request


def _run_gate(
    *arguments: object,
    cwd: Path,
    credential: str | None = None,
    credential_attestation: bool = True,
    mapped_root_installation: bool = True,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    installation_manifest = (
        cwd
        / "artifacts"
        / "installation"
        / "versions"
        / "4.7.0"
        / "installation-manifest.json"
    )
    if installation_manifest.is_file():
        environment["FAKE_INSTALLATION_FINGERPRINT"] = (
            "sha256:" + _sha256(installation_manifest)
        )
    source = cwd / "private" / "inputs" / "course.mp4"
    if source.is_file():
        environment["FAKE_SOURCE_SHA256"] = "sha256:" + _sha256(source)
    context = cwd / "private" / "inputs" / "course.context.json"
    if context.is_file():
        values = json.loads(context.read_text(encoding="utf-8"))
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
        environment["FAKE_CONTEXT_SHA256"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
    environment.pop("STEPFUN_API_KEY", None)
    if extra_environment:
        environment.update(extra_environment)
    command = (
        sys.executable,
        str(RELEASE_GATE),
        *(str(item) for item in arguments),
    )
    passed_descriptors: tuple[int, ...] = ()
    credential_descriptor = -1
    if credential is not None and credential_attestation:
        credential_descriptor, write_descriptor = os.pipe()
        try:
            os.write(write_descriptor, credential.encode("utf-8"))
        finally:
            os.close(write_descriptor)
        environment["RELEASE_GATE_TEST_CREDENTIAL_FD"] = str(
            credential_descriptor
        )
        bootstrap = f"""
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location(
    "_release_gate_under_test",
    {str(RELEASE_GATE)!r},
)
if spec is None or spec.loader is None:
    raise SystemExit(97)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
descriptor = int(os.environ.pop("RELEASE_GATE_TEST_CREDENTIAL_FD"))
try:
    credential = os.read(descriptor, 4097).decode("utf-8")
finally:
    os.close(descriptor)
host_network_namespace = os.readlink("/proc/self/ns/net")
module._release_credential = lambda _phase: (
    credential,
    host_network_namespace,
)
if os.environ.pop(
    "RELEASE_GATE_TEST_INDEPENDENT_LAUNCH_FAILURE",
    None,
) == "1":
    def fail_independent_validation(**_arguments):
        raise module.ReleaseGateFailure(
            "independent_validation.launch_failed"
        )

    module._independent_validate = fail_independent_validation
raise SystemExit(module.main(sys.argv[1:]))
"""
        command = (
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            *(str(item) for item in arguments),
        )
        passed_descriptors = (credential_descriptor,)
        if mapped_root_installation and os.geteuid() != 0:
            command = (
                "unshare",
                "--user",
                "--map-root-user",
                "--",
                *command,
            )
    elif credential is not None:
        environment["STEPFUN_API_KEY"] = credential
        if mapped_root_installation and os.geteuid() != 0:
            command = (
                "unshare",
                "--user",
                "--map-root-user",
                "--",
                *command,
            )
    elif mapped_root_installation and os.geteuid() != 0:
        command = (
            "unshare",
            "--user",
            "--map-root-user",
            "--",
            *command,
        )
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            pass_fds=passed_descriptors,
        )
    finally:
        if credential_descriptor >= 0:
            os.close(credential_descriptor)


def _write_passing_review(plan: Path, *, attempt_number: int = 1) -> Path:
    workspace_parent = plan.parent / "workspaces"
    attempt_id = f"attempt-{attempt_number:04d}"
    cold = json.loads(
        (workspace_parent / f"{attempt_id}.cold.json").read_text(
            encoding="utf-8"
        )
    )
    review = plan.parent / f"{attempt_id}.manual-review.json"
    _write_json(
        review,
        {
            "schema_version": "release_gate_manual_review.v1",
            "operator_id": "release-operator-01",
            "reviewed_at": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "run_id": cold["cold_run"]["run_id"],
            "source_and_transcript_compared": True,
            "clips": [
                {
                    "ordinal": ordinal,
                    "checks": {
                        "topic_complete": True,
                        "boundaries_natural": True,
                        "audio_video_normal": True,
                        "subtitles_faithful_readable": True,
                        "title_summary_grounded": True,
                        "excluded_content_absent": True,
                    },
                }
                for ordinal in range(
                    1,
                    cold["cold_run"]["delivery"]["short_video_count"] + 1,
                )
            ],
            "conclusion": "passed",
        },
    )
    review.chmod(0o600)
    return review


def _record_passing_review(
    plan: Path,
    *,
    cwd: Path,
    attempt_number: int = 1,
) -> subprocess.CompletedProcess[str]:
    review = _write_passing_review(plan, attempt_number=attempt_number)
    return _run_gate(
        "record-review",
        "--plan",
        plan,
        "--review",
        review,
        cwd=cwd,
    )


def test_prepare_locks_the_candidate_and_private_cold_run_inputs(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(plan.read_text(encoding="utf-8"))
    assert document["schema_version"] == "release_gate_plan.v1"
    assert document["candidate"] == {
        "commit_sha": COMMIT_SHA,
        "version": "4.7.0",
        "wheel": {
            "filename": WHEEL_NAME,
            "path": request_document["candidate"]["wheel"],
            "sha256": _sha256(Path(request_document["candidate"]["wheel"])),
        },
        "build_lock": {
            "filename": "requirements-build.lock",
            "path": request_document["candidate"]["build_lock"],
            "sha256": _sha256(Path(request_document["candidate"]["build_lock"])),
        },
        "runtime_lock": {
            "filename": "requirements-runtime.lock",
            "path": request_document["candidate"]["runtime_lock"],
            "sha256": _sha256(Path(request_document["candidate"]["runtime_lock"])),
        },
    }
    assert document["inputs"]["source"]["sha256"] == _sha256(
        Path(request_document["inputs"]["source"]["path"])
    )
    assert document["inputs"]["source"]["language"] == "zh-CN"
    assert document["inputs"]["source"]["content_summary"] == (
        "真实中文课程素材，覆盖核心主题与完整上下文。"
    )
    assert document["execution"]["initial_workspace_state"] == (
        "new_with_empty_processing_cache"
    )
    assert document["execution"]["credential_source"] == "systemd_credentials"
    assert plan.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("field", "value"),
    (("language", "en-US"), ("content_summary", "")),
)
def test_prepare_rejects_source_without_a_real_chinese_declaration(
    tmp_path,
    field,
    value,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    request_document["inputs"]["source"][field] = value
    _write_json(request, request_document)
    request.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：request.source_invalid\n"
    assert not plan.exists()


def test_prepare_rejects_a_nonofficial_provider_endpoint(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    configuration = Path(request_document["inputs"]["configuration"])
    _write_json(
        configuration,
        {
            "schema_version": "configuration.v1",
            "text_model_provider_config": {
                "endpoint": "https://attacker.example/v1"
            },
        },
    )
    configuration.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：request.configuration_provider_invalid\n"
    )
    assert not plan.exists()


@pytest.mark.parametrize(
    "run_url",
    (
        "https://github.com/attacker/long-video-autocut/actions/runs/101",
        "https://github.com/dulltackle/other/actions/runs/101",
        "https://github.com/dulltackle/long-video-autocut/actions/runs/0",
        (
            "https://github.com/dulltackle/long-video-autocut/"
            "actions/runs/101/attempts/0"
        ),
        (
            "https://github.com/dulltackle/long-video-autocut/"
            "actions/runs/101?token=secret"
        ),
        (
            "https://github.com/dulltackle/long-video-autocut/"
            "actions/runs/101#credential"
        ),
    ),
)
def test_prepare_rejects_an_untrusted_automation_run_url(tmp_path, run_url):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    request_document["automation"]["run_url"] = run_url
    _write_json(request, request_document)
    request.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：request.automation_invalid\n"
    assert not plan.exists()


def test_prepare_accepts_a_specific_workflow_attempt_url(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    request_document["automation"]["run_url"] = (
        "https://github.com/dulltackle/long-video-autocut/"
        "actions/runs/101/attempts/2"
    )
    _write_json(request, request_document)
    request.chmod(0o600)

    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    verified = _run_gate("verify", "--plan", plan, cwd=tmp_path)

    assert prepared.returncode == 0, prepared.stderr
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize(
    "content_summary",
    (
        "plain English only",
        "/home/release/private/course.mp4 中的中文课程",
        "中文课程来自 https://private.example/source",
    ),
)
def test_prepare_rejects_a_nonpublic_chinese_summary(
    tmp_path,
    content_summary,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    request_document["inputs"]["source"]["content_summary"] = content_summary
    _write_json(request, request_document)
    request.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：request.source_invalid\n"
    assert not plan.exists()


@pytest.mark.parametrize(
    ("field", "filename"),
    (
        ("configuration", "unloaded.config.json"),
        ("course_context", "unloaded.context.json"),
    ),
)
def test_prepare_rejects_configuration_or_context_not_discovered_by_source(
    tmp_path,
    field,
    filename,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    unrelated = request.parent / "inputs" / filename
    _write_json(unrelated, {"schema_version": "configuration.v1"})
    unrelated.chmod(0o600)
    request_document["inputs"][field] = str(unrelated)
    _write_json(request, request_document)
    request.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert "request.inputs_not_source_sidecars" in completed.stderr
    assert not plan.exists()


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    (
        ("console", "request.console_invalid"),
        ("independent_validator", "request.release_tools_invalid"),
    ),
)
def test_prepare_rejects_unbound_console_or_validator(
    tmp_path,
    field,
    expected_reason,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    replacement = tmp_path / "artifacts" / f"replacement-{field}"
    replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    replacement.chmod(0o755)
    request_document["execution"][field] = str(replacement)
    _write_json(request, request_document)
    request.chmod(0o600)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == f"真实门禁失败：{expected_reason}\n"
    assert not plan.exists()


def test_prepare_rejects_a_release_tool_from_a_writable_directory(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    artifacts = tmp_path / "artifacts"
    artifacts.chmod(0o777)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：request.release_tools_invalid\n"
    assert not plan.exists()


def test_prepare_rejects_an_installation_not_owned_by_the_host_authority(
    tmp_path,
):
    if os.geteuid() == 0:
        pytest.skip("root 环境无法构造非 root 安装所有者")
    request, plan, _request_document = _release_gate_fixture(tmp_path)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
        mapped_root_installation=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：installation.binding_invalid\n"
    assert not plan.exists()


@pytest.mark.parametrize(
    "mutate",
    (
        pytest.param(
            lambda manifest: manifest.update({"unexpected": True}),
            id="extra-field",
        ),
        pytest.param(
            lambda manifest: manifest["platform"].update(
                {"architecture": "arm64"}
            ),
            id="uncertified-platform",
        ),
        pytest.param(
            lambda manifest: manifest["python"].update(
                {"version": "3.13.0"}
            ),
            id="uncertified-python",
        ),
        pytest.param(
            lambda manifest: manifest["snapshot_packages"].pop("ffmpeg"),
            id="missing-snapshot-package",
        ),
        pytest.param(
            lambda manifest: manifest["system_packages"].update(
                {"ffmpeg": "7:6.1.2-attacker"}
            ),
            id="snapshot-system-drift",
        ),
        pytest.param(
            lambda manifest: manifest.update(
                {
                    "wheelhouse": [
                        {"filename": "z.whl", "sha256": "1" * 64},
                        {"filename": "a.whl", "sha256": "2" * 64},
                    ]
                }
            ),
            id="unsorted-wheelhouse",
        ),
    ),
)
def test_prepare_rejects_an_incomplete_certified_host_manifest(
    tmp_path,
    mutate,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    manifest_path = Path(request_document["candidate"]["installation_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(manifest)
    _write_json(manifest_path, manifest)
    ready_path = Path(request_document["candidate"]["installation_ready"])
    _write_json(
        ready_path,
        {
            "schema_version": "production-installation-ready.v1",
            "installation_manifest_sha256": _sha256(manifest_path),
        },
    )

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：installation.binding_invalid\n"
    assert not plan.exists()


def test_locked_plan_is_invalid_after_the_candidate_wheel_changes(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    wheel = Path(request_document["candidate"]["wheel"])
    wheel.write_bytes(wheel.read_bytes() + b"candidate-drift")

    verified = _run_gate("verify", "--plan", plan, cwd=tmp_path)

    assert verified.returncode == 1
    assert verified.stderr == "真实门禁失败：candidate.drift\n"


def test_verify_rejects_plan_permission_drift(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    plan.chmod(0o644)

    verified = _run_gate("verify", "--plan", plan, cwd=tmp_path)

    assert verified.returncode == 1
    assert verified.stderr == "真实门禁失败：plan.input_invalid\n"


def test_verify_accepts_the_root_sealed_plan_copy(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    sealed_parent = tmp_path / "sealed-plan"
    sealed_parent.mkdir(mode=0o710)
    sealed_parent.chmod(0o710)
    sealed_plan = sealed_parent / "plan.json"
    sealed_plan.write_bytes(plan.read_bytes())
    sealed_plan.chmod(0o440)

    verified = _run_gate("verify", "--plan", sealed_plan, cwd=tmp_path)

    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize("mode", (0o400, 0o444, 0o640, 0o660))
def test_verify_rejects_an_incorrectly_sealed_plan_mode(tmp_path, mode):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    sealed_parent = tmp_path / "sealed-plan"
    sealed_parent.mkdir(mode=0o710)
    sealed_parent.chmod(0o710)
    sealed_plan = sealed_parent / "plan.json"
    sealed_plan.write_bytes(plan.read_bytes())
    sealed_plan.chmod(mode)

    verified = _run_gate("verify", "--plan", sealed_plan, cwd=tmp_path)

    assert verified.returncode == 1
    assert verified.stderr == "真实门禁失败：plan.input_invalid\n"


def test_verify_rejects_automation_run_url_drift(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    document = json.loads(plan.read_text(encoding="utf-8"))
    document["automation"]["run_url"] = (
        "https://github.com/attacker/long-video-autocut/actions/runs/101"
    )
    _write_json(plan, document)
    plan.chmod(0o600)

    verified = _run_gate("verify", "--plan", plan, cwd=tmp_path)

    assert verified.returncode == 1
    assert verified.stderr == "真实门禁失败：automation.drift\n"


def test_prepare_rejects_a_publicly_readable_sensitive_input(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    configuration = Path(request_document["inputs"]["configuration"])
    configuration.chmod(0o644)

    completed = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：request.configuration_invalid\n"
    )
    assert not plan.exists()


def test_verify_rejects_sensitive_input_permissions_that_drift(tmp_path):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    Path(request_document["inputs"]["course_context"]).chmod(0o644)

    completed = _run_gate("verify", "--plan", plan, cwd=tmp_path)

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：inputs.drift\n"


def test_cold_review_then_zero_request_overwrite_records_evidence(
    tmp_path,
):
    request, plan, request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"

    cold_completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )

    assert cold_completed.returncode == 0, cold_completed.stderr
    assert credential not in cold_completed.stdout + cold_completed.stderr
    workspace_parent = plan.parent / "workspaces"
    cold_path = workspace_parent / "attempt-0001.cold.json"
    cold = json.loads(cold_path.read_text(encoding="utf-8"))
    assert cold["schema_version"] == "release_gate_cold_run.v1"
    assert cold["status"] == "awaiting_manual_review"
    assert cold["cold_run"]["remote_request_count"] == 3
    assert not (workspace_parent / "attempt-0001.json").exists()
    workspace = workspace_parent / "attempt-0001.workspace"
    assert not (workspace / "delivery.previous").exists()
    assert len(tuple((workspace / "work" / "runs").iterdir())) == 1

    review_completed = _record_passing_review(plan, cwd=tmp_path)

    assert review_completed.returncode == 0, review_completed.stderr
    review_record = json.loads(
        (workspace_parent / "attempt-0001.review.json").read_text(
            encoding="utf-8"
        )
    )
    assert review_record["schema_version"] == "release_gate_review_record.v1"
    assert review_record["status"] == "passed"
    assert review_record["reviewed_clip_count"] == 1
    assert len(tuple((workspace / "work" / "runs").iterdir())) == 1

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )

    assert completed.returncode == 0, completed.stderr
    assert credential not in completed.stdout + completed.stderr
    attempt = json.loads(
        (workspace_parent / "attempt-0001.json").read_text(encoding="utf-8")
    )
    assert attempt["schema_version"] == "release_gate_attempt.v1"
    assert attempt["status"] == "passed"
    assert attempt["candidate"] == {
        "commit_sha": COMMIT_SHA,
        "wheel_sha256": _sha256(
            Path(request_document["candidate"]["wheel"])
        ),
    }
    assert attempt["cold_run"]["remote_request_count"] == 3
    assert attempt["cache_rerun"]["remote_request_count"] == 0
    assert attempt["cache_rerun"]["required_cache_hits"] == {
        "subtitle_optimization": 1,
        "topic_review": 1,
        "transcript": 1,
    }
    assert attempt["cache_rerun"]["previous_delivery_retained"] is True
    assert attempt["cache_rerun"]["network_isolation"] == {
        "mode": "linux_network_namespace",
        "external_blocked": True,
        "loopback_allowed": True,
        "attestation_verified": True,
        "guard_sha256": json.loads(plan.read_text(encoding="utf-8"))[
            "execution"
        ]["network_guard"]["sha256"],
    }
    assert attempt["manual_review"] == {
        "all_checks_passed": True,
        "conclusion": "passed",
        "operator_id": "release-operator-01",
        "reviewed_at": review_record["reviewed_at"],
        "reviewed_clip_count": 1,
        "run_id": cold["cold_run"]["run_id"],
        "source_and_transcript_compared": True,
    }
    assert attempt["semantic_equivalence"]["passed"] is True
    validations = attempt["independent_validation"]
    assert set(validations) == {
        "cache_rerun",
        "cold_run",
        "previous_delivery",
    }
    assert validations["cold_run"]["run_id"] == attempt["cold_run"]["run_id"]
    assert validations["previous_delivery"]["run_id"] == attempt["cold_run"][
        "run_id"
    ]
    assert validations["cache_rerun"]["run_id"] == attempt["cache_rerun"][
        "run_id"
    ]
    for validation in validations.values():
        assert validation["schema_version"] == (
            "independent_delivery_validation.v1"
        )
        assert validation["passed"] is True
        assert validation["result_kind"] == "clips"
        assert validation["short_video_count"] == 1
        assert validation["artifact_count"] == 6
        assert all(validation["checks"].values())
    assert attempt["credential_handling"] == {
        "leak_scan_passed": True,
        "source": "systemd_credentials",
    }
    assert credential not in json.dumps(attempt, ensure_ascii=False)


def test_cold_run_accepts_partially_reported_provider_token_usage(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={"FAKE_RELEASE_GATE_MODE": "partial_token_usage"},
    )

    assert completed.returncode == 0, completed.stderr
    cold = json.loads(
        (plan.parent / "workspaces" / "attempt-0001.cold.json").read_text(
            encoding="utf-8"
        )
    )
    assert cold["cold_run"]["remote_request_count"] == 4


def test_cache_rerun_requires_runtime_network_namespace_attestation(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"
    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )
    assert cold.returncode == 0, cold.stderr
    reviewed = _record_passing_review(plan, cwd=tmp_path)
    assert reviewed.returncode == 0, reviewed.stderr

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={
            "FAKE_RELEASE_GATE_MODE": "missing_network_attestation"
        },
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：cache_rerun.network_attestation_invalid\n"
    )
    attempt = json.loads(
        (plan.parent / "workspaces" / "attempt-0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["status"] == "failed"
    assert attempt["failure"]["reason_code"] == (
        "cache_rerun.network_attestation_invalid"
    )


@pytest.mark.parametrize("warm_request_fact", ("warm_request", "warm_attempt"))
def test_remote_request_during_cache_rerun_rejects_the_candidate_immutably(
    tmp_path,
    warm_request_fact,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert cold.returncode == 0, cold.stderr
    reviewed = _record_passing_review(plan, cwd=tmp_path)
    assert reviewed.returncode == 0, reviewed.stderr

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={"FAKE_RELEASE_GATE_MODE": warm_request_fact},
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：cache_rerun.remote_request_detected\n"
    )
    record_path = plan.parent / "workspaces" / "attempt-0001.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["failure"] == {
        "classification": "candidate_rejected",
        "reason_code": "cache_rerun.remote_request_detected",
        "same_candidate_rerun_allowed": False,
    }
    original = record_path.read_bytes()

    repeated = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )

    assert repeated.returncode == 1
    assert record_path.read_bytes() == original


def test_provider_transient_during_cache_rerun_rejects_the_candidate(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"
    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )
    assert cold.returncode == 0, cold.stderr
    reviewed = _record_passing_review(plan, cwd=tmp_path)
    assert reviewed.returncode == 0, reviewed.stderr

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={
            "FAKE_RELEASE_GATE_MODE": "warm_provider_transient"
        },
    )

    assert completed.returncode == 1
    record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert record["failure"] == {
        "classification": "candidate_rejected",
        "reason_code": "run.failed",
        "same_candidate_rerun_allowed": False,
    }

    record_path = plan.parent / "workspaces" / "attempt-0001.json"
    record["failure"] = {
        "classification": "unclassified",
        "permitted_same_candidate_rerun": ["provider_transient"],
        "reason_code": "run.failed",
        "same_candidate_rerun_allowed": False,
        "stable_error_code": "transcription.service_unavailable",
    }
    _write_json(record_path, record)
    record_path.chmod(0o600)

    classified = _run_gate(
        "classify",
        "--plan",
        plan,
        "--attempt",
        record_path,
        "--classification",
        "provider_transient",
        "--operator-id",
        "release-operator-01",
        cwd=tmp_path,
    )

    assert classified.returncode == 1
    assert classified.stderr == "真实门禁失败：attempt.state_invalid\n"
    assert not record_path.with_name(
        "attempt-0001.classification.json"
    ).exists()


def test_cache_rerun_rejects_a_changed_previous_delivery(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert cold.returncode == 0, cold.stderr
    reviewed = _record_passing_review(plan, cwd=tmp_path)
    assert reviewed.returncode == 0, reviewed.stderr

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={"FAKE_RELEASE_GATE_MODE": "mutate_previous"},
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：cache_rerun.previous_delivery_drift\n"
    )
    record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert record["failure"]["classification"] == "candidate_rejected"


def test_only_a_classified_provider_transient_failure_allows_same_candidate_retry(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"
    failed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={"FAKE_RELEASE_GATE_MODE": "provider_transient"},
    )
    assert failed.returncode == 1
    first_record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert first_record["failure"] == {
        "classification": "unclassified",
        "permitted_same_candidate_rerun": ["provider_transient"],
        "reason_code": "run.failed",
        "same_candidate_rerun_allowed": False,
        "stable_error_code": "transcription.service_unavailable",
    }

    unclassified_retry = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )
    assert unclassified_retry.returncode == 1
    assert not (
        plan.parent / "workspaces" / "attempt-0002.workspace"
    ).exists()

    classified = _run_gate(
        "classify",
        "--plan",
        plan,
        "--attempt",
        plan.parent / "workspaces" / "attempt-0001.json",
        "--classification",
        "provider_transient",
        "--operator-id",
        "release-operator-01",
        cwd=tmp_path,
    )
    assert classified.returncode == 0, classified.stderr
    classification_path = (
        plan.parent
        / "workspaces"
        / "attempt-0001.classification.json"
    )
    classification = json.loads(
        classification_path.read_text(encoding="utf-8")
    )
    assert classification["same_candidate_rerun_allowed"] is True

    retried = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )

    assert retried.returncode == 0, retried.stderr
    reviewed = _record_passing_review(
        plan,
        cwd=tmp_path,
        attempt_number=2,
    )
    assert reviewed.returncode == 0, reviewed.stderr
    rerun = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )
    assert rerun.returncode == 0, rerun.stderr
    second_record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0002.json"
        ).read_text(encoding="utf-8")
    )
    assert second_record["status"] == "passed"
    assert second_record["candidate"] == first_record["candidate"]


def test_only_a_classified_certified_host_failure_allows_same_candidate_retry(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"
    failed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={
            "RELEASE_GATE_TEST_INDEPENDENT_LAUNCH_FAILURE": "1"
        },
    )

    assert failed.returncode == 1
    assert failed.stderr == (
        "真实门禁失败：independent_validation.launch_failed\n"
    )
    attempt = plan.parent / "workspaces" / "attempt-0001.json"
    record = json.loads(attempt.read_text(encoding="utf-8"))
    assert record["failure"] == {
        "classification": "unclassified",
        "permitted_same_candidate_rerun": [
            "certified_host_infrastructure"
        ],
        "reason_code": "independent_validation.launch_failed",
        "same_candidate_rerun_allowed": False,
        "stable_error_code": "independent_validation.launch_failed",
    }

    classified = _run_gate(
        "classify",
        "--plan",
        plan,
        "--attempt",
        attempt,
        "--classification",
        "certified_host_infrastructure",
        "--operator-id",
        "release-operator-01",
        cwd=tmp_path,
    )
    assert classified.returncode == 0, classified.stderr

    retried = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
    )
    assert retried.returncode == 0, retried.stderr
    assert (
        plan.parent / "workspaces" / "attempt-0002.cold.json"
    ).is_file()


def test_cache_rerun_is_blocked_until_every_clip_review_check_passes(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert cold.returncode == 0, cold.stderr

    before_review = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert before_review.returncode == 1
    assert before_review.stderr == "真实门禁失败：review.required\n"
    review = _write_passing_review(plan)
    document = json.loads(review.read_text(encoding="utf-8"))
    document["clips"][0]["checks"]["boundaries_natural"] = False
    _write_json(review, document)
    review.chmod(0o600)
    rejected = _run_gate(
        "record-review",
        "--plan",
        plan,
        "--review",
        review,
        cwd=tmp_path,
    )
    assert rejected.returncode == 1
    assert rejected.stderr == "真实门禁失败：review.failed\n"
    assert not (
        plan.parent / "workspaces" / "attempt-0001.review.json"
    ).exists()
    failure = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert failure["failure_phase"] == "manual_review"
    assert failure["failure"] == {
        "classification": "candidate_rejected",
        "reason_code": "review.failed",
        "same_candidate_rerun_allowed": False,
    }


def test_missing_credential_does_not_turn_an_unstarted_rerun_into_a_failure(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    cold = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert cold.returncode == 0, cold.stderr
    reviewed = _record_passing_review(plan, cwd=tmp_path)
    assert reviewed.returncode == 0, reviewed.stderr
    attempt = plan.parent / "workspaces" / "attempt-0001.json"

    missing = _run_gate("rerun", "--plan", plan, cwd=tmp_path)

    assert missing.returncode == 1
    assert missing.stderr == "真实门禁失败：credential.missing_or_invalid\n"
    assert not attempt.exists()
    assert not (
        plan.parent
        / "workspaces"
        / "attempt-0001.workspace"
        / "delivery.previous"
    ).exists()

    completed = _run_gate(
        "rerun",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(attempt.read_text(encoding="utf-8"))["status"] == "passed"


def test_direct_credential_environment_without_bridge_attestation_is_rejected(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="direct-environment-credential",
        credential_attestation=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.source_unverified\n"
    assert not any((plan.parent / "workspaces").iterdir())


def test_readonly_fake_credential_descriptor_without_system_unit_is_rejected(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential_directory = tmp_path / "private" / "fake-credentials"
    credential_directory.mkdir(mode=0o700)
    credential_path = credential_directory / "stepfun_api_key"
    credential_path.write_text("readonly-fake-credential", encoding="utf-8")
    credential_path.chmod(0o400)

    completed = subprocess.run(
        (
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "sh",
            "-c",
            (
                'mount --bind "$1" "$1" && '
                'mount -o remount,bind,ro "$1" && '
                'exec 9<"$1/stepfun_api_key" && '
                'export RELEASE_GATE_SYSTEMD_CREDENTIAL_FD=9 && '
                'export RELEASE_GATE_SYSTEMD_HOST_NETNS="net:[100]" && '
                'shift && exec "$@"'
            ),
            "sh",
            str(credential_directory),
            sys.executable,
            str(RELEASE_GATE),
            "execute",
            "--plan",
            str(plan),
        ),
        cwd=tmp_path,
        env={
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", os.defpath),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.source_unverified\n"
    assert not any((plan.parent / "workspaces").iterdir())


def test_credential_canary_in_child_output_fails_without_echoing_the_value(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={"FAKE_RELEASE_GATE_MODE": "leak_secret"},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.output_leak\n"
    assert credential not in completed.stdout + completed.stderr
    record = (
        plan.parent / "workspaces" / "attempt-0001.json"
    ).read_text(encoding="utf-8")
    assert credential not in record


def test_credential_canary_in_generated_evidence_rejects_the_candidate(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={"FAKE_RELEASE_GATE_MODE": "leak_file"},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.evidence_leak\n"
    assert credential not in completed.stdout + completed.stderr
    record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert record["failure"]["classification"] == "candidate_rejected"
    assert credential not in json.dumps(record, ensure_ascii=False)
    assert not (
        plan.parent
        / "workspaces"
        / "attempt-0001.workspace"
        / "delivery"
        / "report.md"
    ).exists()


def test_failed_run_scans_and_removes_a_binary_credential_leak(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={"FAKE_RELEASE_GATE_MODE": "leak_binary_failure"},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.evidence_leak\n"
    leak = (
        plan.parent
        / "workspaces"
        / "attempt-0001.workspace"
        / "credential-leak.bin"
    )
    assert not leak.exists()
    record = json.loads(
        (plan.parent / "workspaces" / "attempt-0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["failure"]["reason_code"] == "credential.evidence_leak"


@pytest.mark.parametrize(
    ("mode", "leak_name"),
    (
        (
            "leak_filename_failure",
            "credential-canary-release-gate-filename",
        ),
        ("leak_symlink_failure", "credential-target-link"),
    ),
)
def test_failed_run_removes_credentials_from_names_and_symlink_targets(
    tmp_path,
    mode,
    leak_name,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr
    credential = "credential-canary-release-gate"

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential=credential,
        extra_environment={"FAKE_RELEASE_GATE_MODE": mode},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：credential.evidence_leak\n"
    workspace = plan.parent / "workspaces" / "attempt-0001.workspace"
    assert not (workspace / leak_name).exists()
    assert not (workspace / leak_name).is_symlink()
    record = json.loads(
        (plan.parent / "workspaces" / "attempt-0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["failure"]["reason_code"] == "credential.evidence_leak"
    assert credential not in json.dumps(record, ensure_ascii=False)


def test_cold_run_rejects_an_uncertified_observed_environment(tmp_path):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={"FAKE_RELEASE_GATE_MODE": "bad_environment"},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：run.contract_invalid\n"
    record = json.loads(
        (
            plan.parent / "workspaces" / "attempt-0001.json"
        ).read_text(encoding="utf-8")
    )
    assert record["failure"]["classification"] == "candidate_rejected"


def test_cold_run_rejects_a_nonofficial_provider_endpoint_disclosure(
    tmp_path,
):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={
            "FAKE_RELEASE_GATE_MODE": "wrong_endpoint_origin"
        },
    )

    assert completed.returncode == 1
    assert completed.stderr == (
        "真实门禁失败：run.external_services_invalid\n"
    )


@pytest.mark.parametrize("mode", ("unbound_context", "bad_configuration"))
def test_cold_run_rejects_inputs_not_observed_by_the_real_run(tmp_path, mode):
    request, plan, _request_document = _release_gate_fixture(tmp_path)
    prepared = _run_gate(
        "prepare",
        "--request",
        request,
        "--plan",
        plan,
        cwd=tmp_path,
    )
    assert prepared.returncode == 0, prepared.stderr

    completed = _run_gate(
        "execute",
        "--plan",
        plan,
        cwd=tmp_path,
        credential="credential-canary-release-gate",
        extra_environment={"FAKE_RELEASE_GATE_MODE": mode},
    )

    assert completed.returncode == 1
    assert completed.stderr == "真实门禁失败：run.contract_invalid\n"
    record = json.loads(
        (plan.parent / "workspaces" / "attempt-0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["failure"]["classification"] == "candidate_rejected"
