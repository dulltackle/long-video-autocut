import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = PROJECT_ROOT / "scripts" / "validate_release_evidence.py"
COMMIT_SHA = "a" * 40
COLD_RUN_ID = "run_11111111-1111-4111-8111-111111111111"
WARM_RUN_ID = "run_22222222-2222-4222-8222-222222222222"
SNAPSHOT_ID = "20260725T000000Z"
RELEASE_TOOL_NAMES = (
    "install-production.sh",
    "run_keyless_gate_network.sh",
    "run_release_gate.py",
    "systemd_credential_bridge.py",
    "validate_installed_delivery.py",
    "validate_release_evidence.py",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _passing_layer(collected: int) -> dict[str, int]:
    return {
        "collected": collected,
        "passed": collected,
        "failed": 0,
        "errors": 0,
        "deselected": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
        "retries": 0,
        "exit_code": 0,
    }


def _cache_stats(*, warm: bool, count: int = 1) -> dict[str, int]:
    return {
        "queries": count,
        "hits": count if warm else 0,
        "misses": 0 if warm else count,
        "corrupt_quarantined": 0,
        "writes_published": 0 if warm else count,
        "writes_already_present": 0,
        "infrastructure_failures": 0,
        "singleflight_wait_count": 0,
        "singleflight_wait_ms_total": 0,
    }


def _providers(*, warm: bool) -> dict[str, object]:
    count = 0 if warm else 1
    return {
        "transcription": {
            "provider_id": "stepaudio",
            "model_id": "stepaudio-2.5-asr",
            "requests": {
                "count": count,
                "succeeded": count,
                "failed": 0,
                "attempt_count_total": count,
            },
        },
        "topic_review": {
            "provider_id": "stepfun",
            "model_id": "step-2-mini",
            "requests": {
                "count": count,
                "succeeded": count,
                "failed": 0,
                "attempt_count_total": count,
            },
        },
        "subtitle_optimization": {
            "provider_id": "stepfun",
            "model_id": "step-2-mini",
            "requests": {
                "count": count,
                "succeeded": count,
                "failed": 0,
                "attempt_count_total": count,
            },
        },
    }


def _run(run_id: str, *, warm: bool) -> dict[str, object]:
    return {
        "run_id": run_id,
        "terminal": {
            "outcome": "succeeded",
            "exit_code": 0,
            "result_kind": "clips",
        },
        "diagnostic_manifest_sha256": ("c" if warm else "b") * 64,
        "delivery": {
            "manifest_sha256": ("e" if warm else "d") * 64,
            "result_kind": "clips",
            "artifact_count": 6,
            "short_video_count": 1,
        },
        "environment": {
            "certified_platform": "ubuntu_24_04_amd64",
            "python_version": "3.12.3",
            "application_version": "4.7.0",
            "ffmpeg_version": "6.1.1",
            "ffprobe_version": "6.1.1",
            "font_family": "Noto Sans CJK SC",
            "preflight_outcome": "succeeded",
            "installation_fingerprint": "sha256:" + "a" * 64,
        },
        "configuration": {
            "configuration_fingerprint": "sha256:" + "c" * 64,
            "course_context": {
                "provided": True,
                "attribution_provided": False,
                "priority_topic_count": 1,
                "excluded_content_count": 1,
            },
        },
        "providers": _providers(warm=warm),
        "cache": {
            "transcript": _cache_stats(warm=warm),
            "transcription_shard": _cache_stats(
                warm=warm,
                count=0 if warm else 2,
            ),
            "topic_review": _cache_stats(warm=warm),
            "subtitle_optimization": _cache_stats(warm=warm),
        },
    }


def _independent_validation(run_id: str) -> dict[str, object]:
    return {
        "schema_version": "independent_delivery_validation.v1",
        "success": True,
        "run_id": run_id,
        "result_kind": "clips",
        "short_video_count": 1,
        "artifact_count": 6,
        "checks": {
            "digests": True,
            "exact_file_set": True,
            "faithful_transcript": True,
            "mp4": True,
            "path_safety": True,
            "references": True,
            "schema": True,
        },
    }


def _acceptance_run(index: int) -> str:
    return f"run_{index:08x}-3333-4333-8333-{index:012x}"


def _installed_cases() -> dict[str, dict[str, object]]:
    return {
        "short_video_success": {
            "exit_codes": [0],
            "run_ids": [_acceptance_run(1)],
            "short_video_count": 1,
            "status": "passed",
        },
        "effective_empty": {
            "exit_codes": [0],
            "run_ids": [_acceptance_run(2)],
            "short_video_count": 0,
            "status": "passed",
        },
        "typed_failure": {
            "exit_codes": [30],
            "run_ids": [_acceptance_run(3)],
            "status": "passed",
        },
        "overwrite": {
            "exit_codes": [60, 0],
            "run_ids": [_acceptance_run(4), _acceptance_run(5)],
            "status": "passed",
        },
        "rollback": {
            "exit_codes": [143],
            "run_ids": [_acceptance_run(6)],
            "status": "passed",
        },
        "cache_maintenance": {
            "exit_codes": [0, 0, 10],
            "run_ids": [],
            "status": "passed",
        },
        "sigint": {
            "exit_codes": [130],
            "run_ids": [_acceptance_run(7)],
            "status": "passed",
        },
        "sigterm": {
            "exit_codes": [143],
            "run_ids": [_acceptance_run(8)],
            "status": "passed",
        },
        "repeated_signal": {
            "exit_codes": [130, 0],
            "run_ids": [_acceptance_run(9)],
            "status": "passed",
        },
        "postcommit_signal": {
            "exit_codes": [0],
            "run_ids": [_acceptance_run(10)],
            "status": "passed",
        },
    }


def _file_fact(path: Path) -> dict[str, str]:
    return {"filename": path.name, "path": str(path), "sha256": _sha256(path)}


def _course_context_binding(path: Path) -> tuple[str, dict[str, object]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    normalized = {
        "schema_version": value["schema_version"],
        "course_topic": value["course_topic"],
        "attribution": value.get("attribution"),
        "priority_topics": list(value.get("priority_topics", [])),
        "excluded_content": list(value.get("excluded_content", [])),
    }
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _business_projection_sha256(delivery: Path) -> str:
    def normalize(value):
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in sorted(value.items())
                if key != "run_id"
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    projection = {
        name: normalize(
            json.loads((delivery / name).read_text(encoding="utf-8"))
        )
        for name in ("transcript.json", "plan.json", "metadata.json")
    }
    payload = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_delivery_fixture(
    delivery: Path,
    *,
    run_id: str,
    source_sha256: str,
    source_byte_length: int,
) -> None:
    delivery.mkdir(parents=True, mode=0o700)
    for name, schema in (
        ("transcript.json", "transcript.v1"),
        ("plan.json", "clip_plan.v1"),
        ("metadata.json", "short_video_catalog.v1"),
    ):
        _write_json(
            delivery / name,
            {
                "schema_version": schema,
                "run_id": run_id,
                "business_value": "同一业务结果",
            },
        )
    files = [{"path": "clips/clip.mp4", "role": "short_video_media"}]
    _write_json(
        delivery / "manifest.json",
        {
            "schema_version": "delivery_manifest.v1",
            "run_id": run_id,
            "terminal_state": "succeeded",
            "result_kind": "clips",
            "started_at": "2025-08-03T10:00:00.000Z",
            "published_at": "2025-08-03T10:10:00.000Z",
            "application_version": "4.7.0",
            "source": {
                "sha256": "sha256:" + source_sha256,
                "byte_length": source_byte_length,
                "duration_ms": 600000,
            },
            "documents": {},
            "execution": {"subtitle_optimization": {"short_video_count": 1}},
            "files": files,
        },
    )


def _raw_run_manifest(
    run: dict[str, object],
    *,
    warm: bool,
    source_fact: dict[str, object],
    context_sha256: str,
    context_observation: dict[str, object],
    installation_sha256: str,
) -> dict[str, object]:
    services = []
    for capability, provider in run["providers"].items():
        requests = provider["requests"]
        count = requests["count"]
        categories = {
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
        }[capability]
        services.append(
            {
                "capability": capability,
                "adapter_id": provider["provider_id"],
                "provider_id": provider["provider_id"],
                "model_id": provider["model_id"],
                "configuration_fingerprint": "sha256:" + "d" * 64,
                "endpoint": {
                    "status": "available",
                    "origin": "https://api.stepfun.com",
                },
                "transport": "remote",
                "purpose": capability,
                "allowed_data_categories": categories,
                "contact": (
                    {"status": "not_contacted", "reason": "cache_hit"}
                    if warm
                    else {"status": "contacted"}
                ),
                "requests": {
                    **requests,
                    "duration_ms_total": count,
                    "duration_ms_max": count,
                    "token_usage": (
                        {"status": "not_applicable"}
                        if warm
                        else {"status": "not_reported"}
                    ),
                },
            }
        )
    return {
        "schema_version": "run_manifest.v1",
        "identity": {
            "run_id": run["run_id"],
            "application_version": "4.7.0",
            "release": {"status": "unknown"},
        },
        "lifecycle": {
            "started_at": "2025-08-03T10:00:00.000Z",
            "ended_at": "2025-08-03T10:10:00.000Z",
            "duration_ms": 600000,
            "outcome": "succeeded",
            "exit_code": 0,
            "result_kind": {"status": "available", "value": "clips"},
            "interruption": {"status": "not_applicable"},
        },
        "source": {
            "status": "available",
            "sha256": "sha256:" + str(source_fact["sha256"]),
            "byte_length": source_fact["byte_length"],
            "duration_ms": source_fact["duration_ms"],
            "course_context": {
                "provided": True,
                "sha256": {"status": "available", "value": context_sha256},
            },
        },
        "environment": {
            "status": "available",
            "certified_platform": "ubuntu_24_04_amd64",
            "python_version": "3.12.3",
            "ffmpeg_version": "6.1.1",
            "ffprobe_version": "6.1.1",
            "font": {"family": "Noto Sans CJK SC", "available": True},
            "installation_fingerprint": "sha256:" + installation_sha256,
            "preflight_outcome": "succeeded",
            "application_version": "4.7.0",
        },
        "external_services": {"status": "observed", "services": services},
        "cache": {"status": "observed", "namespaces": run["cache"]},
        "configuration": {
            "status": "available",
            "configuration_fingerprint": "sha256:" + "c" * 64,
            "result_configuration": {
                "transcription_provider": "stepaudio",
                "transcription_model": "stepaudio-2.5-asr",
                "text_model_provider": "stepfun",
                "topic_review": {"model": "step-2-mini"},
                "subtitle_optimization": {"model": "step-2-mini"},
            },
            "course_context": context_observation,
        },
        "stages": {},
        "operations": {},
        "retries_and_recovery": {},
        "delivery": {
            "build_state": "completed",
            "verification_state": "passed",
            "publication_state": "committed",
            "artifacts": {
                "status": "observed",
                "created_by_role": {"short_video": 1},
                "verified_by_role": {"short_video": 1},
            },
        },
        "notices": [],
        "errors": {
            "primary_error": {"status": "not_applicable"},
            "associated_errors": [],
            "recovery_incomplete": False,
        },
        "event_log": {},
    }


def _validation_document(run_id: str) -> dict[str, object]:
    return _independent_validation(run_id)


def _write_release_gate_provenance(
    tmp_path: Path,
    inputs: dict[str, Path],
    *,
    source_contents: bytes = b"real chinese course input\n",
) -> dict[str, Path]:
    private = tmp_path / "release-gate-private"
    private.mkdir(mode=0o700)
    private_inputs = private / "inputs"
    private_inputs.mkdir(mode=0o700)
    workspace_parent = private / "workspaces"
    workspace_parent.mkdir(mode=0o700)
    source_media = private_inputs / "course.mp4"
    source_media.write_bytes(source_contents)
    configuration = private_inputs / "course.config.json"
    _write_json(configuration, {"schema_version": "configuration.v1"})
    course_context = private_inputs / "course.context.json"
    _write_json(
        course_context,
        {
            "schema_version": "course_context.v1",
            "course_topic": "真实中文课程",
            "priority_topics": ["核心主题"],
            "excluded_content": ["课间闲聊"],
        },
    )
    expected_transcript = private_inputs / "expected-transcript.json"
    _write_json(
        expected_transcript,
        {"schema_version": "installed_acceptance_transcript.v1"},
    )
    for path in (source_media, configuration, course_context, expected_transcript):
        path.chmod(0o600)

    source = json.loads(inputs["source"].read_text(encoding="utf-8"))
    source["inputs"] = {
        "source": {
            "asset_id": "chinese-live-course",
            "version": "2026-08-03",
            "language": "zh-CN",
            "content_summary": "真实中文课程素材，覆盖核心主题与完整上下文。",
            "sha256": _sha256(source_media),
            "byte_length": source_media.stat().st_size,
            "duration_ms": 600000,
        },
        "configuration": {
            "schema_version": "configuration.v1",
            "sha256": _sha256(configuration),
        },
        "course_context": {
            "schema_version": "course_context.v1",
            "sha256": _sha256(course_context),
        },
        "expected_transcript": {
            "schema_version": "installed_acceptance_transcript.v1",
            "sha256": _sha256(expected_transcript),
        },
    }
    context_sha256, context_observation = _course_context_binding(course_context)

    attempt_id = "attempt-0001"
    workspace = workspace_parent / f"{attempt_id}.workspace"
    gate_private = workspace_parent / f"{attempt_id}.private"
    workspace.mkdir(mode=0o700)
    gate_private.mkdir(mode=0o700)
    cold_delivery = workspace / "delivery.previous"
    warm_delivery = workspace / "delivery"
    _write_delivery_fixture(
        cold_delivery,
        run_id=COLD_RUN_ID,
        source_sha256=source["inputs"]["source"]["sha256"],
        source_byte_length=source["inputs"]["source"]["byte_length"],
    )
    _write_delivery_fixture(
        warm_delivery,
        run_id=WARM_RUN_ID,
        source_sha256=source["inputs"]["source"]["sha256"],
        source_byte_length=source["inputs"]["source"]["byte_length"],
    )
    projection_sha256 = _business_projection_sha256(cold_delivery)
    assert projection_sha256 == _business_projection_sha256(warm_delivery)
    source["semantic_equivalence"] = {
        "equivalent": True,
        "cold_projection_sha256": projection_sha256,
        "warm_projection_sha256": projection_sha256,
    }
    host_prefix = tmp_path / "certified-installation"
    host_version = host_prefix / "versions" / "4.7.0"
    host_console = host_version / "venv" / "bin" / "video-auto-editor"
    host_console.parent.mkdir(parents=True)
    host_console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    host_console.chmod(0o755)
    host_manifest = host_version / "installation-manifest.json"
    host_manifest_document = json.loads(
        inputs["installation_manifest"].read_text(encoding="utf-8")
    )
    host_manifest_document["installation_prefix"] = str(host_prefix)
    host_manifest_document["system_packages"]["certified-host-agent"] = (
        "1.0.0"
    )
    _write_json(host_manifest, host_manifest_document)
    host_ready = host_version / "READY"
    _write_json(
        host_ready,
        {
            "schema_version": "production-installation-ready.v1",
            "installation_manifest_sha256": _sha256(host_manifest),
        },
    )

    for name, run_id, warm, delivery in (
        ("cold", COLD_RUN_ID, False, cold_delivery),
        ("warm", WARM_RUN_ID, True, warm_delivery),
    ):
        source["runs"][name]["environment"][
            "installation_fingerprint"
        ] = "sha256:" + _sha256(host_manifest)
        source["runs"][name]["configuration"][
            "course_context"
        ] = context_observation
        run_root = workspace / "work" / "runs" / run_id
        run_root.mkdir(parents=True, mode=0o700)
        run_manifest = run_root / "run.json"
        _write_json(
            run_manifest,
            _raw_run_manifest(
                source["runs"][name],
                warm=warm,
                source_fact=source["inputs"]["source"],
                context_sha256=context_sha256,
                context_observation=context_observation,
                installation_sha256=_sha256(host_manifest),
            ),
        )
        source["runs"][name]["diagnostic_manifest_sha256"] = _sha256(run_manifest)
        source["runs"][name]["delivery"]["manifest_sha256"] = _sha256(
            delivery / "manifest.json"
        )

    validation_paths = {
        "cold": gate_private / "cold-validation.json",
        "previous": gate_private / "previous-validation.json",
        "warm": gate_private / "warm-validation.json",
    }
    _write_json(validation_paths["cold"], _validation_document(COLD_RUN_ID))
    _write_json(validation_paths["previous"], _validation_document(COLD_RUN_ID))
    _write_json(validation_paths["warm"], _validation_document(WARM_RUN_ID))
    for path in validation_paths.values():
        path.chmod(0o600)
    for name, path in (("cold", validation_paths["cold"]), ("warm", validation_paths["warm"])):
        source["independent_validations"][name]["evidence_sha256"] = _sha256(path)

    release_tools = json.loads(
        inputs["keyless"].read_text(encoding="utf-8")
    )["release_tools"]
    independent_validator = PROJECT_ROOT / "scripts" / "validate_installed_delivery.py"
    credential_bridge = PROJECT_ROOT / "scripts" / "systemd_credential_bridge.py"
    network_guard = PROJECT_ROOT / "scripts" / "run_keyless_gate_network.sh"
    plan = private / "plan.json"
    plan_document = {
        "schema_version": "release_gate_plan.v1",
        "candidate": {
            "commit_sha": COMMIT_SHA,
            "version": "4.7.0",
            "wheel": _file_fact(inputs["wheel"]),
            "build_lock": _file_fact(inputs["build_lock"]),
            "runtime_lock": _file_fact(inputs["runtime_lock"]),
        },
        "certified_host": {
            "attestation_id": "certified-host-01",
            "apt_snapshot_id": SNAPSHOT_ID,
            "installation": {
                "manifest": _file_fact(host_manifest),
                "ready": _file_fact(host_ready),
                "console": _file_fact(host_console),
            },
        },
        "automation": {
            "run_url": source["automatic_gate_runs"]["keyless"]["url"],
            "keyless_gate_evidence": _file_fact(inputs["keyless"]),
            "installed_acceptance_evidence": _file_fact(inputs["installed"]),
            "release_tools": release_tools,
        },
        "inputs": {
            "source": {**_file_fact(source_media), **source["inputs"]["source"]},
            "configuration": _file_fact(configuration),
            "course_context": _file_fact(course_context),
            "expected_transcript": _file_fact(expected_transcript),
        },
        "execution": {
            "console": _file_fact(host_console),
            "independent_validator": _file_fact(independent_validator),
            "credential_bridge": _file_fact(credential_bridge),
            "network_guard": _file_fact(network_guard),
            "workspace_parent": str(workspace_parent),
            "initial_workspace_state": "new_with_empty_processing_cache",
            "credential_source": "systemd_credentials",
            "credential_id": "stepfun_api_key",
            "cold_then_overwrite": True,
        },
        "release": {"version": "4.7.0", "tag": "v4.7.0"},
    }
    _write_json(plan, plan_document)
    plan.chmod(0o600)
    plan_sha256 = _sha256(plan)
    fingerprint_payload = {
        "source": source["inputs"]["source"]["sha256"],
        "configuration": source["inputs"]["configuration"]["sha256"],
        "course_context": source["inputs"]["course_context"]["sha256"],
        "expected_transcript": source["inputs"]["expected_transcript"]["sha256"],
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    _, context_observation = _course_context_binding(
        Path(plan_document["inputs"]["course_context"]["path"])
    )

    def run_summary(name: str) -> dict[str, object]:
        run = source["runs"][name]
        return {
            "run_id": run["run_id"],
            "terminal_state": "succeeded",
            "result_kind": "clips",
            "run_manifest_sha256": run["diagnostic_manifest_sha256"],
            "delivery": {
                **run["delivery"],
                "source": {
                    "sha256": "sha256:" + source["inputs"]["source"]["sha256"],
                    "byte_length": source["inputs"]["source"]["byte_length"],
                    "duration_ms": source["inputs"]["source"]["duration_ms"],
                },
            },
            "environment": {
                "certified_platform": "ubuntu_24_04_amd64",
                "python_version": "3.12.3",
                "application_version": "4.7.0",
                "ffmpeg_version": "6.1.1",
                "ffprobe_version": "6.1.1",
                "font_family": "Noto Sans CJK SC",
                "preflight_outcome": "succeeded",
                "installation_fingerprint": (
                    "sha256:" + _sha256(host_manifest)
                ),
            },
            "configuration": {
                "configuration_fingerprint": "sha256:" + "c" * 64,
                "course_context": context_observation,
            },
            "external_services": [
                {"capability": capability, **provider}
                for capability, provider in run["providers"].items()
            ],
            "remote_request_count": sum(
                provider["requests"]["count"]
                for provider in run["providers"].values()
            ),
            "processing_cache": dict(run["cache"]),
        }

    def validation_summary(name: str, path: Path) -> dict[str, object]:
        validation = source["independent_validations"][name]
        return {
            **{key: value for key, value in validation.items() if key != "success"},
            "passed": True,
            "evidence_sha256": _sha256(path),
        }

    common = {
        "attempt_id": attempt_id,
        "candidate": {"commit_sha": COMMIT_SHA, "wheel_sha256": _sha256(inputs["wheel"])},
        "input_fingerprint": input_fingerprint,
        "plan_sha256": plan_sha256,
    }
    cold_record = workspace_parent / f"{attempt_id}.cold.json"
    cold_document = {
        "schema_version": "release_gate_cold_run.v1",
        **common,
        "status": "awaiting_manual_review",
        "started_at": "2025-08-03T10:00:00.000Z",
        "ended_at": "2025-08-03T10:10:00.000Z",
        "workspace": {
            "initial_processing_cache_empty": True,
            "same_workspace_reserved_for_rerun": True,
        },
        "cold_run": run_summary("cold"),
        "business_projection_sha256": projection_sha256,
        "independent_validation": validation_summary("cold", validation_paths["cold"]),
        "credential_handling": {"source": "systemd_credentials", "leak_scan_passed": True},
    }
    _write_json(cold_record, cold_document)
    cold_record.chmod(0o600)

    review_record = workspace_parent / f"{attempt_id}.review.json"
    manual = source["manual_review"]
    review_document = {
        "schema_version": "release_gate_review_record.v1",
        **common,
        "status": "passed",
        "cold_record_sha256": _sha256(cold_record),
        "review_source_sha256": "f" * 64,
        "recorded_at": "2025-08-03T12:35:00.000Z",
        "operator_id": manual["operator_id"],
        "reviewed_at": manual["reviewed_at"],
        "run_id": manual["run_id"],
        "source_and_transcript_compared": True,
        "clips": manual["clips"],
        "reviewed_clip_count": len(manual["clips"]),
        "all_checks_passed": True,
        "conclusion": "passed",
    }
    _write_json(review_record, review_document)
    review_record.chmod(0o600)

    attempt = workspace_parent / f"{attempt_id}.json"
    attempt_document = {
        "schema_version": "release_gate_attempt.v1",
        **common,
        "status": "passed",
        "started_at": cold_document["started_at"],
        "ended_at": "2025-08-03T13:00:00.000Z",
        "phase_records": {
            "cold_record_sha256": _sha256(cold_record),
            "review_record_sha256": _sha256(review_record),
        },
        "workspace": {
            "initial_processing_cache_empty": True,
            "same_workspace_for_rerun": True,
        },
        "cold_run": run_summary("cold"),
        "manual_review": {
            "operator_id": manual["operator_id"],
            "reviewed_at": manual["reviewed_at"],
            "run_id": manual["run_id"],
            "source_and_transcript_compared": True,
            "reviewed_clip_count": len(manual["clips"]),
            "all_checks_passed": True,
            "conclusion": "passed",
        },
        "cache_rerun": {
            **run_summary("warm"),
            "required_cache_hits": {
                namespace: source["runs"]["warm"]["cache"][namespace]["hits"]
                for namespace in ("transcript", "topic_review", "subtitle_optimization")
            },
            "previous_delivery_retained": True,
            "network_isolation": {
                "mode": "linux_network_namespace",
                "external_blocked": True,
                "loopback_allowed": True,
                "attestation_verified": True,
                "guard_sha256": release_tools[
                    "run_keyless_gate_network.sh"
                ],
            },
        },
        "semantic_equivalence": {
            "passed": True,
            "business_projection_sha256": projection_sha256,
        },
        "independent_validation": {
            "cold_run": validation_summary("cold", validation_paths["cold"]),
            "previous_delivery": validation_summary("cold", validation_paths["previous"]),
            "cache_rerun": validation_summary("warm", validation_paths["warm"]),
        },
        "credential_handling": {"source": "systemd_credentials", "leak_scan_passed": True},
    }
    _write_json(attempt, attempt_document)
    attempt.chmod(0o600)
    _write_json(inputs["source"], source)
    inputs["source"].chmod(0o600)
    return {"plan": plan, "attempt": attempt}


def _create_valid_inputs(
    tmp_path: Path,
    *,
    source_contents: bytes = b"real chinese course input\n",
) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp_path.chmod(0o700)
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "video_auto_editor-4.7.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: video-auto-editor\nVersion: 4.7.0\n",
        )
    build_lock = tmp_path / "requirements-build.lock"
    build_lock.write_bytes(
        b"build==1.3.0 --hash=sha256:" + b"1" * 64 + b"\n"
    )
    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_bytes(b"# no third-party runtime dependencies\n")

    installation_manifest = tmp_path / "installation-manifest.json"
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
                "font_file": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            },
            "installation_prefix": "/opt/video-auto-editor",
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

    keyless = tmp_path / "keyless-gate-evidence.json"
    _write_json(
        keyless,
        {
            "schema_version": "keyless_gate_evidence.v1",
            "candidate": {
                "commit_sha": COMMIT_SHA,
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel),
            },
            "credential_mode": "absent",
            "network": {
                "external_blocked": True,
                "loopback_allowed": True,
                "mode": "network_namespace",
            },
            "layers": {
                "unit_schema": _passing_layer(100),
                "module_interfaces": _passing_layer(200),
                "adapter_contracts": _passing_layer(300),
                "fault_injection": _passing_layer(400),
                "installation_contract": _passing_layer(500),
            },
            "release_tools": {
                name: _sha256(PROJECT_ROOT / "scripts" / name)
                for name in RELEASE_TOOL_NAMES
            },
            "success": True,
        },
    )

    installed = tmp_path / "installed-acceptance-evidence.json"
    _write_json(
        installed,
        {
            "schema_version": "installed_acceptance_evidence.v1",
            "candidate": {
                "apt_snapshot_id": SNAPSHOT_ID,
                "commit_sha": COMMIT_SHA,
                "runtime_lock_filename": runtime_lock.name,
                "runtime_lock_sha256": _sha256(runtime_lock),
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel),
            },
            "installation": {
                "application_version": "4.7.0",
                "console": (
                    "/opt/video-auto-editor/versions/4.7.0/venv/bin/"
                    "video-auto-editor"
                ),
                "environment_prefix": (
                    "/opt/video-auto-editor/versions/4.7.0/venv"
                ),
                "manifest_sha256": _sha256(installation_manifest),
                "prefix": "/opt/video-auto-editor",
                "python": (
                    "/opt/video-auto-editor/versions/4.7.0/venv/bin/python"
                ),
                "verified": True,
            },
            "network": {
                "external_blocked": True,
                "loopback_allowed": True,
                "mode": "network_namespace",
            },
            "cases": _installed_cases(),
            "statistics": {"failed": 0, "passed": 10, "total": 10},
            "success": True,
        },
    )

    source = tmp_path / "release-evidence-source.json"
    _write_json(
        source,
        {
            "schema_version": "release_evidence_source.v1",
            "candidate": {
                "application_version": "4.7.0",
                "commit_sha": COMMIT_SHA,
                "wheel_filename": wheel.name,
                "wheel_sha256": _sha256(wheel),
            },
            "locks": {
                "build": {
                    "filename": build_lock.name,
                    "sha256": _sha256(build_lock),
                },
                "runtime": {
                    "filename": runtime_lock.name,
                    "sha256": _sha256(runtime_lock),
                },
            },
            "apt_snapshot_id": SNAPSHOT_ID,
            "automatic_gate_runs": {
                "keyless": {
                    "url": "https://github.com/dulltackle/long-video-autocut/actions/runs/101"
                },
                "installed_acceptance": {
                    "url": "https://github.com/dulltackle/long-video-autocut/actions/runs/101"
                },
            },
            "inputs": {
                "source": {
                    "asset_id": "chinese-live-course",
                    "version": "2026-08-03",
                    "language": "zh-CN",
                    "content_summary": "真实中文课程素材，覆盖核心主题与完整上下文。",
                    "sha256": "1" * 64,
                    "byte_length": 123456,
                    "duration_ms": 600000,
                },
                "configuration": {
                    "schema_version": "configuration.v1",
                    "sha256": "2" * 64,
                },
                "course_context": {
                    "schema_version": "course_context.v1",
                    "sha256": "3" * 64,
                },
                "expected_transcript": {
                    "schema_version": "installed_acceptance_transcript.v1",
                    "sha256": "5" * 64,
                },
            },
            "runs": {
                "cold": _run(COLD_RUN_ID, warm=False),
                "warm": _run(WARM_RUN_ID, warm=True),
            },
            "independent_validations": {
                "cold": _independent_validation(COLD_RUN_ID),
                "warm": _independent_validation(WARM_RUN_ID),
            },
            "semantic_equivalence": {
                "equivalent": True,
                "cold_projection_sha256": "4" * 64,
                "warm_projection_sha256": "4" * 64,
            },
            "manual_review": {
                "schema_version": "release_gate_manual_review.v1",
                "operator_id": "release-operator-01",
                "reviewed_at": "2025-08-03T12:34:56Z",
                "run_id": COLD_RUN_ID,
                "source_and_transcript_compared": True,
                "clips": [
                    {
                        "ordinal": 1,
                        "checks": {
                            "topic_complete": True,
                            "boundaries_natural": True,
                            "audio_video_normal": True,
                            "subtitles_faithful_readable": True,
                            "title_summary_grounded": True,
                            "excluded_content_absent": True,
                        },
                    }
                ],
                "conclusion": "passed",
            },
            "retry_attempts": [],
            "known_limitations": [
                {
                    "code": "certified_platform_scope",
                    "statement": "首次生产版本只认证 Ubuntu 24.04 amd64。",
                }
            ],
        },
    )
    source.chmod(0o600)
    inputs = {
        "source": source,
        "wheel": wheel,
        "build_lock": build_lock,
        "runtime_lock": runtime_lock,
        "installation_manifest": installation_manifest,
        "keyless": keyless,
        "installed": installed,
    }
    inputs.update(
        _write_release_gate_provenance(
            tmp_path,
            inputs,
            source_contents=source_contents,
        )
    )
    return inputs


def _validator_arguments(inputs: dict[str, Path], output: Path) -> list[str]:
    return [
        "--source",
        str(inputs["source"]),
        "--plan",
        str(inputs["plan"]),
        "--attempt",
        str(inputs["attempt"]),
        "--wheel",
        str(inputs["wheel"]),
        "--build-lock",
        str(inputs["build_lock"]),
        "--runtime-lock",
        str(inputs["runtime_lock"]),
        "--installation-manifest",
        str(inputs["installation_manifest"]),
        "--keyless-evidence",
        str(inputs["keyless"]),
        "--installed-evidence",
        str(inputs["installed"]),
        "--output",
        str(output),
    ]


def _run_validator(
    inputs: dict[str, Path],
    output: Path,
    *,
    mapped_root_installation: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-I",
        str(VALIDATOR),
        *_validator_arguments(inputs, output),
    ]
    if mapped_root_installation and os.geteuid() != 0:
        command = [
            "unshare",
            "--user",
            "--map-root-user",
            "--",
            *command,
        ]
    return subprocess.run(
        command,
        cwd=output.parent,
        env={"LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)},
        text=True,
        capture_output=True,
        check=False,
    )


def _mutate_json(path: Path, mutate) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    _write_json(path, value)


def test_valid_candidate_is_sealed_once_with_a_public_sha256(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "release_evidence.v1"
    assert evidence["success"] is True
    assert evidence["candidate"] == {
        "application_version": "4.7.0",
        "commit_sha": COMMIT_SHA,
        "wheel_filename": inputs["wheel"].name,
        "wheel_sha256": _sha256(inputs["wheel"]),
    }
    assert evidence["automatic_gates"]["keyless"]["statistics"] == {
        "collected": 1500,
        "passed": 1500,
    }
    assert evidence["automatic_gates"]["keyless"]["network"] == {
        "external_blocked": True,
        "loopback_allowed": True,
        "mode": "network_namespace",
    }
    assert evidence["automatic_gates"]["installed_acceptance"][
        "statistics"
    ] == {"failed": 0, "passed": 10, "total": 10}
    assert evidence["apt_snapshot_id"] == SNAPSHOT_ID
    assert evidence["dependencies"]["build_lock"]["sha256"] == _sha256(
        inputs["build_lock"]
    )
    assert evidence["dependencies"]["runtime_lock"]["sha256"] == _sha256(
        inputs["runtime_lock"]
    )
    assert evidence["dependencies"]["ci_installed_acceptance"][
        "system_packages"
    ]["ffmpeg"] == (
        "7:6.1.1-3ubuntu5"
    )
    assert "certified-host-agent" not in evidence["dependencies"][
        "ci_installed_acceptance"
    ]["system_packages"]
    assert evidence["dependencies"]["certified_host"]["system_packages"][
        "certified-host-agent"
    ] == "1.0.0"
    assert evidence["installation"]["ci_installed_acceptance"][
        "platform"
    ] == {
        "architecture": "amd64",
        "operating_system": "ubuntu",
        "operating_system_version": "24.04",
    }
    assert evidence["installation"]["certified_host"]["platform"] == {
        "architecture": "amd64",
        "operating_system": "ubuntu",
        "operating_system_version": "24.04",
    }
    source_media = Path(
        json.loads(inputs["plan"].read_text(encoding="utf-8"))["inputs"]["source"][
            "path"
        ]
    )
    assert evidence["inputs"]["source"] == {
        "asset_id": "chinese-live-course",
        "version": "2026-08-03",
        "language": "zh-CN",
        "content_summary": "真实中文课程素材，覆盖核心主题与完整上下文。",
        "sha256": _sha256(source_media),
        "byte_length": source_media.stat().st_size,
        "duration_ms": 600000,
    }
    assert evidence["inputs"]["expected_transcript"] == {
        "schema_version": "installed_acceptance_transcript.v1",
        "sha256": json.loads(inputs["plan"].read_text(encoding="utf-8"))[
            "inputs"
        ]["expected_transcript"]["sha256"],
    }
    assert all(
        service["requests"]["count"] > 0
        for service in evidence["runs"]["cold"]["providers"].values()
    )
    assert all(
        service["requests"]["count"] == 0
        for service in evidence["runs"]["warm"]["providers"].values()
    )
    host_manifest = Path(
        json.loads(inputs["plan"].read_text(encoding="utf-8"))[
            "certified_host"
        ]["installation"]["manifest"]["path"]
    )
    expected_environment = {
        "application_version": "4.7.0",
        "certified_platform": "ubuntu_24_04_amd64",
        "ffmpeg_version": "6.1.1",
        "ffprobe_version": "6.1.1",
        "font_family": "Noto Sans CJK SC",
        "installation_fingerprint": "sha256:" + _sha256(host_manifest),
        "preflight_outcome": "succeeded",
        "python_version": "3.12.3",
    }
    expected_configuration = {
        "configuration_fingerprint": "sha256:" + "c" * 64,
        "course_context": {
            "provided": True,
            "attribution_provided": False,
            "priority_topic_count": 1,
            "excluded_content_count": 1,
        },
    }
    for name in ("cold", "warm"):
        assert evidence["runs"][name]["environment"] == expected_environment
        assert evidence["runs"][name]["configuration"] == (
            expected_configuration
        )
    assert set(evidence["runs"]["warm"]["cache"]) == {
        "transcript",
        "transcription_shard",
        "topic_review",
        "subtitle_optimization",
    }
    assert evidence["runs"]["warm"]["cache"]["transcription_shard"] == {
        field: 0 for field in _cache_stats(warm=True)
    }
    assert all(
        evidence["runs"]["warm"]["cache"][namespace]["hits"]
        == evidence["runs"]["warm"]["cache"][namespace]["queries"]
        > 0
        for namespace in ("transcript", "topic_review", "subtitle_optimization")
    )
    assert all(
        all(validation["checks"].values())
        for validation in evidence["independent_validations"].values()
    )
    assert evidence["semantic_equivalence"]["equivalent"] is True
    assert evidence["manual_review"]["reviewed_short_video_count"] == 1
    assert evidence["manual_review"]["run_id"] == COLD_RUN_ID
    assert evidence["manual_review"]["operator_id_sha256"] == hashlib.sha256(
        b"release-operator-01"
    ).hexdigest()
    assert "release-operator-01" not in output.read_text(encoding="utf-8")
    assert evidence["retry_attempts"] == []
    assert evidence["known_limitations"] == [
        {
            "code": "certified_platform_scope",
            "statement": "首次生产版本只认证 Ubuntu 24.04 amd64。",
        }
    ]
    assert evidence["artifacts"]["release_evidence_source"] == {
        "filename": "release-evidence-source.json",
        "sha256": _sha256(inputs["source"]),
    }
    workspace_parent = inputs["attempt"].parent
    assert evidence["artifacts"]["release_gate_plan"] == {
        "filename": "plan.json",
        "sha256": _sha256(inputs["plan"]),
    }
    assert evidence["artifacts"]["release_gate_attempt"] == {
        "filename": inputs["attempt"].name,
        "sha256": _sha256(inputs["attempt"]),
    }
    assert evidence["artifacts"]["release_gate_cold_record"] == {
        "filename": "attempt-0001.cold.json",
        "sha256": _sha256(workspace_parent / "attempt-0001.cold.json"),
    }
    assert evidence["artifacts"]["release_gate_review_record"] == {
        "filename": "attempt-0001.review.json",
        "sha256": _sha256(workspace_parent / "attempt-0001.review.json"),
    }
    host_manifest = Path(
        json.loads(inputs["plan"].read_text(encoding="utf-8"))["certified_host"][
            "installation"
        ]["manifest"]["path"]
    )
    assert evidence["artifacts"]["certified_host_installation_manifest"] == {
        "filename": "installation-manifest.json",
        "sha256": _sha256(host_manifest),
    }
    assert _sha256(host_manifest) != _sha256(inputs["installation_manifest"])
    assert evidence["artifacts"]["release_tools"] == {
        name: {"filename": name, "sha256": _sha256(PROJECT_ROOT / "scripts" / name)}
        for name in RELEASE_TOOL_NAMES
    }
    assert evidence["artifacts"]["release_gate_runs"]["warm"][
        "network_isolation"
    ] == {
        "mode": "linux_network_namespace",
        "external_blocked": True,
        "loopback_allowed": True,
        "attestation_verified": True,
        "guard_sha256": _sha256(
            PROJECT_ROOT / "scripts" / "run_keyless_gate_network.sh"
        ),
    }
    digest = _sha256(output)
    assert completed.stdout == f"release-evidence.json SHA-256: {digest}\n"
    assert completed.stderr == ""
    assert stat.S_IMODE(output.stat().st_mode) == 0o444
    assert list(tmp_path.glob(".release-evidence.json.*.tmp")) == []
    assert "/opt/" not in output.read_text(encoding="utf-8")
    assert str(tmp_path) not in output.read_text(encoding="utf-8")


def test_sealer_accepts_the_root_sealed_plan_copy(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    sealed_parent = tmp_path / "sealed-plan"
    sealed_parent.mkdir(mode=0o710)
    sealed_parent.chmod(0o710)
    sealed_plan = sealed_parent / "plan.json"
    sealed_plan.write_bytes(inputs["plan"].read_bytes())
    sealed_plan.chmod(0o440)
    inputs["plan"] = sealed_plan
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()


def test_sealer_rejects_a_nonstandard_output_name(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    output = tmp_path / "looks-like-release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "发布证据封存失败：output.path_invalid\n"
    assert not output.exists()


def test_review_record_source_digest_is_part_of_the_immutable_chain(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    review = inputs["attempt"].with_name("attempt-0001.review.json")
    _mutate_json(
        review,
        lambda record: record.update({"review_source_sha256": "not-a-digest"}),
    )
    _mutate_json(
        inputs["attempt"],
        lambda record: record["phase_records"].update(
            {"review_record_sha256": _sha256(review)}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.attempt_invalid\n"
    assert not output.exists()


def test_cache_rerun_network_isolation_is_bound_to_the_trusted_guard(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["attempt"],
        lambda attempt: attempt["cache_rerun"]["network_isolation"].update(
            {"guard_sha256": "f" * 64}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.attempt_invalid\n"
    assert not output.exists()


def test_manual_review_time_must_follow_the_cold_run(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    reviewed_at = "2025-08-03T09:59:59.000Z"
    _mutate_json(
        inputs["source"],
        lambda source: source["manual_review"].update(
            {"reviewed_at": reviewed_at}
        ),
    )
    _mutate_json(
        inputs["attempt"].with_name(
            f"{inputs['attempt'].stem}.review.json"
        ),
        lambda review: review.update({"reviewed_at": reviewed_at}),
    )
    _mutate_json(
        inputs["attempt"],
        lambda attempt: attempt["manual_review"].update(
            {"reviewed_at": reviewed_at}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.attempt_invalid\n"
    assert not output.exists()


def test_successful_attempt_end_time_cannot_be_in_the_future(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    _mutate_json(
        inputs["attempt"],
        lambda attempt: attempt.update({"ended_at": future}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.attempt_invalid\n"
    assert not output.exists()


def test_source_media_stays_private_while_it_is_streamed(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    plan = json.loads(inputs["plan"].read_text(encoding="utf-8"))
    source_media = Path(plan["inputs"]["source"]["path"])
    source_media.chmod(0o644)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.plan_invalid\n"
    assert not output.exists()


def test_certified_host_installation_requires_root_ownership(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 环境无法构造非 root 安装所有者")
    inputs = _create_valid_inputs(tmp_path)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(
        inputs,
        output,
        mapped_root_installation=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.plan_invalid\n"
    assert not output.exists()


def test_source_media_larger_than_the_json_limit_is_streamed(tmp_path):
    inputs = _create_valid_inputs(
        tmp_path,
        source_contents="中".encode() + b"x" * (32 * 1024 * 1024),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["inputs"]["source"][
        "byte_length"
    ] == 32 * 1024 * 1024 + 3


def test_coordinated_release_tool_summary_rewrite_cannot_replace_the_tool(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["keyless"],
        lambda evidence: evidence["release_tools"].update(
            {"validate_release_evidence.py": "f" * 64}
        ),
    )
    _mutate_json(
        inputs["plan"],
        lambda plan: (
            plan["automation"]["keyless_gate_evidence"].update(
                {"sha256": _sha256(inputs["keyless"])}
            ),
            plan["automation"]["release_tools"].update(
                {"validate_release_evidence.py": "f" * 64}
            ),
        ),
    )
    plan_sha256 = _sha256(inputs["plan"])
    cold = inputs["attempt"].with_name("attempt-0001.cold.json")
    review = inputs["attempt"].with_name("attempt-0001.review.json")
    _mutate_json(cold, lambda record: record.update({"plan_sha256": plan_sha256}))
    _mutate_json(
        review,
        lambda record: record.update(
            {
                "plan_sha256": plan_sha256,
                "cold_record_sha256": _sha256(cold),
            }
        ),
    )
    _mutate_json(
        inputs["attempt"],
        lambda record: record.update(
            {
                "plan_sha256": plan_sha256,
                "phase_records": {
                    "cold_record_sha256": _sha256(cold),
                    "review_record_sha256": _sha256(review),
                },
            }
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.plan_invalid\n"
    assert not output.exists()


def test_release_tool_cannot_come_from_an_operator_writable_directory(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    writable_tools = tmp_path / "writable-tools"
    writable_tools.mkdir(mode=0o777)
    writable_tools.chmod(0o777)
    guard = writable_tools / "run_keyless_gate_network.sh"
    guard.write_bytes(
        (PROJECT_ROOT / "scripts" / "run_keyless_gate_network.sh").read_bytes()
    )
    guard.chmod(0o755)
    _mutate_json(
        inputs["plan"],
        lambda plan: plan["execution"]["network_guard"].update(
            {
                "filename": guard.name,
                "path": str(guard),
                "sha256": _sha256(guard),
            }
        ),
    )
    plan_sha256 = _sha256(inputs["plan"])
    cold = inputs["attempt"].with_name("attempt-0001.cold.json")
    review = inputs["attempt"].with_name("attempt-0001.review.json")
    _mutate_json(cold, lambda record: record.update({"plan_sha256": plan_sha256}))
    _mutate_json(
        review,
        lambda record: record.update(
            {
                "plan_sha256": plan_sha256,
                "cold_record_sha256": _sha256(cold),
            }
        ),
    )
    _mutate_json(
        inputs["attempt"],
        lambda record: record.update(
            {
                "plan_sha256": plan_sha256,
                "phase_records": {
                    "cold_record_sha256": _sha256(cold),
                    "review_record_sha256": _sha256(review),
                },
            }
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == (
        "发布证据封存失败：release_gate.plan_invalid\n"
    )
    assert not output.exists()


@pytest.mark.parametrize(
    ("target", "expected_reason"),
    [
        pytest.param("source-media", "release_gate.plan_invalid", id="source-media"),
        pytest.param("plan", "release_gate.plan_invalid", id="plan"),
        pytest.param("attempt", "release_gate.attempt_invalid", id="attempt"),
        pytest.param("cold-run", "release_gate.attempt_invalid", id="cold-run"),
        pytest.param(
            "delivery-manifest",
            "release_gate.attempt_invalid",
            id="delivery-manifest",
        ),
        pytest.param(
            "independent-validation",
            "release_gate.attempt_invalid",
            id="independent-validation",
        ),
        pytest.param("review", "release_gate.attempt_invalid", id="review"),
    ],
)
def test_release_gate_originals_cannot_drift_after_the_private_summary(
    tmp_path,
    target,
    expected_reason,
):
    inputs = _create_valid_inputs(tmp_path)
    plan = json.loads(inputs["plan"].read_text(encoding="utf-8"))
    attempt_id = inputs["attempt"].stem
    workspace_parent = inputs["attempt"].parent
    workspace = workspace_parent / f"{attempt_id}.workspace"
    private = workspace_parent / f"{attempt_id}.private"
    paths = {
        "source-media": Path(plan["inputs"]["source"]["path"]),
        "plan": inputs["plan"],
        "attempt": inputs["attempt"],
        "cold-run": workspace / "work" / "runs" / COLD_RUN_ID / "run.json",
        "delivery-manifest": workspace / "delivery.previous" / "manifest.json",
        "independent-validation": private / "cold-validation.json",
        "review": workspace_parent / f"{attempt_id}.review.json",
    }
    if target == "plan":
        _mutate_json(
            paths[target],
            lambda document: document["candidate"].update(
                {"commit_sha": "f" * 40}
            ),
        )
    elif target == "attempt":
        _mutate_json(
            paths[target],
            lambda document: document.update({"status": "waived"}),
        )
    else:
        paths[target].write_bytes(paths[target].read_bytes() + b" \n")
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == f"发布证据封存失败：{expected_reason}\n"
    assert not output.exists()


def test_sealer_rejects_an_uncertified_installation_environment(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["installation_manifest"],
        lambda manifest: manifest["platform"].update(
            {"operating_system_version": "22.04"}
        ),
    )
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installation_manifest.invalid\n"
    assert not output.exists()


def test_certified_environment_facts_cannot_be_replaced_with_public_free_text(
    tmp_path,
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["installation_manifest"],
        lambda manifest: manifest["environment"].update(
            {"font_family": "LEAKME"}
        ),
    )
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installation_manifest.invalid\n"
    assert not output.exists()


def test_sealer_rejects_an_uncertified_python_runtime(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["installation_manifest"],
        lambda manifest: manifest["python"].update({"version": "3.11.9"}),
    )
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installation_manifest.invalid\n"
    assert not output.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda manifest: manifest["python"].update(
                {"implementation": "PyPy"}
            ),
            id="python-implementation",
        ),
        pytest.param(
            lambda manifest: manifest["python"].update({"version": "3.13.0"}),
            id="python-upper-bound",
        ),
        pytest.param(
            lambda manifest: manifest.update(
                {"installation_prefix": "opt/video-auto-editor"}
            ),
            id="relative-prefix",
        ),
        pytest.param(
            lambda manifest: manifest["environment"].update(
                {"font_file": "NotoSansCJK-Regular.ttc"}
            ),
            id="relative-font-file",
        ),
        pytest.param(
            lambda manifest: manifest["runtime_lock"].update(
                {"filename": "../requirements-runtime.lock"}
            ),
            id="unsafe-lock-filename",
        ),
        pytest.param(
            lambda manifest: manifest.update(
                {
                    "wheelhouse": [
                        {"filename": "../LEAKME.whl", "sha256": "8" * 64}
                    ]
                }
            ),
            id="unsafe-wheelhouse-filename",
        ),
        pytest.param(
            lambda manifest: manifest.update(
                {
                    "wheelhouse": [
                        {"filename": "z.whl", "sha256": "8" * 64},
                        {"filename": "a.whl", "sha256": "9" * 64},
                    ]
                }
            ),
            id="unsorted-wheelhouse",
        ),
        pytest.param(
            lambda manifest: manifest["system_packages"].update(
                {"INVALID PACKAGE": "1.0"}
            ),
            id="invalid-package-name",
        ),
        pytest.param(
            lambda manifest: manifest["system_packages"].update(
                {"sk-secret123": "1.0"}
            ),
            id="credential-shaped-package-name",
        ),
        pytest.param(
            lambda manifest: manifest.update(
                {
                    "wheelhouse": [
                        {
                            "filename": (
                                "dependency-ghp_LEAKME-1.0-py3-none-any.whl"
                            ),
                            "sha256": "8" * 64,
                        }
                    ]
                }
            ),
            id="credential-shaped-wheelhouse-filename",
        ),
        pytest.param(
            lambda manifest: manifest["system_packages"].update(
                {"ffmpeg": "release-ghp_LEAKME123"}
            ),
            id="credential-shaped-package-version",
        ),
        pytest.param(
            lambda manifest: manifest["environment"].update(
                {
                    "ffmpeg_version": "6.1.release-ghp_LEAKME",
                    "ffprobe_version": "6.1.release-ghp_LEAKME",
                }
            ),
            id="credential-shaped-media-version",
        ),
    ],
)
def test_installation_manifest_mirrors_the_trusted_producer_schema(
    tmp_path, mutate
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(inputs["installation_manifest"], mutate)
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installation_manifest.invalid\n"
    assert not output.exists()


def test_installation_dependency_inventory_cannot_omit_a_certified_package(
    tmp_path,
):
    inputs = _create_valid_inputs(tmp_path)

    def remove_font_package(manifest):
        manifest["snapshot_packages"].pop("fonts-noto-cjk")
        manifest["system_packages"].pop("fonts-noto-cjk")

    _mutate_json(inputs["installation_manifest"], remove_font_package)
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installation_manifest.invalid\n"
    assert not output.exists()


def test_sealer_never_overwrites_an_existing_final_evidence(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    output = tmp_path / "release-evidence.json"
    output.write_bytes(b"existing immutable evidence\n")

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：output.exists\n"
    assert output.read_bytes() == b"existing immutable evidence\n"


def test_sealer_failure_never_leaves_a_final_file_when_output_is_not_durable(
    tmp_path,
):
    inputs = _create_valid_inputs(tmp_path)
    output = Path("/proc/release-evidence.json")

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：output.write_failed\n"
    assert not output.exists()


def test_sealer_rejects_an_output_directory_inode_swap_before_commit(tmp_path):
    inputs = _create_valid_inputs(tmp_path / "private-inputs")
    output_directory = tmp_path / "output"
    output_directory.mkdir(mode=0o700)
    moved_directory = tmp_path / "moved-output"
    output = output_directory / "release-evidence.json"
    wrapper = """
import importlib.util
import os
import stat
import sys

validator_path, original_directory, moved_directory, *arguments = sys.argv[1:]
spec = importlib.util.spec_from_file_location("release_validator", validator_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original_fsync = os.fsync
swapped = False

def swapping_fsync(descriptor):
    global swapped
    result = original_fsync(descriptor)
    if not swapped and stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.rename(original_directory, moved_directory)
        os.mkdir(original_directory, mode=0o700)
        swapped = True
    return result

module.os.fsync = swapping_fsync
raise SystemExit(module.main(arguments))
"""

    command = [
        sys.executable,
        "-I",
        "-c",
        wrapper,
        str(VALIDATOR),
        str(output_directory),
        str(moved_directory),
        *_validator_arguments(inputs, output),
    ]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-root-user", "--", *command]
    completed = subprocess.run(
        command,
        cwd=output_directory,
        env={"LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "发布证据封存失败：output.write_failed\n"
    assert not output.exists()
    assert not (moved_directory / "release-evidence.json").exists()


def test_sealer_rejects_a_symlinked_output_parent(tmp_path):
    inputs = _create_valid_inputs(tmp_path / "private-inputs")
    actual_output = tmp_path / "actual-output"
    actual_output.mkdir(mode=0o700)
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(actual_output, target_is_directory=True)
    output = output_alias / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "发布证据封存失败：output.write_failed\n"
    assert not output.exists()
    assert not (actual_output / "release-evidence.json").exists()


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(
            lambda path: path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '  "schema_version": "release_evidence_source.v1",',
                    '  "schema_version": "release_evidence_source.v1",\n'
                    '  "schema_version": "release_evidence_source.v1",',
                    1,
                ),
                encoding="utf-8",
            ),
            id="duplicate-field",
        ),
        pytest.param(
            lambda path: _mutate_json(
                path, lambda value: value.update({"waiver": True})
            ),
            id="unknown-waiver",
        ),
        pytest.param(
            lambda path: _mutate_json(
                path, lambda value: value.pop("manual_review")
            ),
            id="missing-hard-gate",
        ),
        pytest.param(
            lambda path: path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '"duration_ms": 600000', '"duration_ms": NaN', 1
                ),
                encoding="utf-8",
            ),
            id="non-finite-number",
        ),
    ],
)
def test_source_schema_rejects_non_strict_or_bypass_fields(tmp_path, corrupt):
    inputs = _create_valid_inputs(tmp_path)
    corrupt(inputs["source"])
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr in {
        "发布证据封存失败：source.read_invalid\n",
        "发布证据封存失败：source.schema_invalid\n",
    }
    assert not output.exists()


def test_source_rejects_a_unicode_surrogate_without_a_traceback(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    inputs["source"].write_text(
        inputs["source"].read_text(encoding="utf-8").replace(
            '"version": "2026-08-03"', '"version": "\\ud800"', 1
        ),
        encoding="utf-8",
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert "Traceback" not in completed.stderr
    assert not output.exists()


@pytest.mark.parametrize("target", ["source", "directory"])
def test_private_source_permissions_are_fail_closed(tmp_path, target):
    inputs = _create_valid_inputs(tmp_path)
    if target == "source":
        inputs["source"].chmod(0o644)
    else:
        tmp_path.chmod(0o755)
    output = tmp_path / "release-evidence.json"

    try:
        completed = _run_validator(inputs, output)
    finally:
        tmp_path.chmod(0o700)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.read_invalid\n"
    assert not output.exists()


def test_json_inputs_are_bounded_and_leaf_symlinks_are_rejected(tmp_path):
    oversized = _create_valid_inputs(tmp_path / "oversized")
    oversized["installation_manifest"].write_bytes(b" " * (2 * 1024 * 1024 + 1))
    oversized_output = oversized["source"].parent / "release-evidence.json"

    oversized_result = _run_validator(oversized, oversized_output)

    assert oversized_result.returncode == 1
    assert oversized_result.stderr == (
        "发布证据封存失败：installation_manifest.read_invalid\n"
    )
    assert not oversized_output.exists()

    symlinked = _create_valid_inputs(tmp_path / "symlinked")
    source_target = symlinked["source"].with_name("private-source-target.json")
    symlinked["source"].rename(source_target)
    symlinked["source"].symlink_to(source_target)
    symlinked_output = symlinked["source"].parent / "release-evidence.json"

    symlinked_result = _run_validator(symlinked, symlinked_output)

    assert symlinked_result.returncode == 1
    assert symlinked_result.stderr == "发布证据封存失败：source.read_invalid\n"
    assert not symlinked_output.exists()


def test_lockfile_inputs_are_bounded(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    inputs["build_lock"].write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    _mutate_json(
        inputs["source"],
        lambda source: source["locks"]["build"].update(
            {"sha256": _sha256(inputs["build_lock"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：candidate.build_lock_invalid\n"
    assert not output.exists()


@pytest.mark.parametrize(
    "drift",
    [
        pytest.param(
            lambda inputs: inputs["wheel"].write_bytes(b"rebuilt candidate wheel\n"),
            id="wheel",
        ),
        pytest.param(
            lambda inputs: inputs["build_lock"].write_bytes(b"changed build lock\n"),
            id="build-lock",
        ),
        pytest.param(
            lambda inputs: inputs["runtime_lock"].write_bytes(b"changed runtime lock\n"),
            id="runtime-lock",
        ),
        pytest.param(
            lambda inputs: _mutate_json(
                inputs["source"],
                lambda value: value["candidate"].update({"commit_sha": "f" * 40}),
            ),
            id="commit",
        ),
        pytest.param(
            lambda inputs: _mutate_json(
                inputs["source"],
                lambda value: value.update(
                    {"apt_snapshot_id": "20260726T000000Z"}
                ),
            ),
            id="snapshot",
        ),
    ],
)
def test_any_candidate_or_locked_artifact_drift_invalidates_evidence(
    tmp_path, drift
):
    inputs = _create_valid_inputs(tmp_path)
    drift(inputs)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr.startswith("发布证据封存失败：")
    assert not output.exists()


def test_candidate_wheel_metadata_must_bind_the_declared_version(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    with zipfile.ZipFile(inputs["wheel"], "w") as archive:
        archive.writestr(
            "video_auto_editor-9.9.9.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: video-auto-editor\nVersion: 9.9.9\n",
        )
    wheel_sha256 = _sha256(inputs["wheel"])
    for input_name in ("source", "installation_manifest", "keyless", "installed"):
        def replace_digest(value, *, input_name=input_name):
            if input_name == "source":
                value["candidate"]["wheel_sha256"] = wheel_sha256
            elif input_name == "installation_manifest":
                value["application"]["wheel"]["sha256"] = wheel_sha256
            else:
                value["candidate"]["wheel_sha256"] = wheel_sha256

        _mutate_json(inputs[input_name], replace_digest)
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["installation"].update(
            {"manifest_sha256": _sha256(inputs["installation_manifest"])}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：candidate.wheel_invalid\n"
    assert not output.exists()


def test_release_version_is_strictly_major_minor_patch(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["candidate"].update(
            {"application_version": "4.7"}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("input_name", "canonical_name", "reason"),
    [
        ("build_lock", "renamed-build.lock", "candidate.build_lock_invalid"),
        ("runtime_lock", "renamed-runtime.lock", "candidate.runtime_lock_invalid"),
        (
            "installation_manifest",
            "renamed-installation.json",
            "installation_manifest.invalid",
        ),
    ],
)
def test_release_attachments_use_canonical_filenames(
    tmp_path, input_name, canonical_name, reason
):
    inputs = _create_valid_inputs(tmp_path)
    renamed = tmp_path / canonical_name
    inputs[input_name].rename(renamed)
    inputs[input_name] = renamed
    if input_name in {"build_lock", "runtime_lock"}:
        lock_name = "build" if input_name == "build_lock" else "runtime"
        _mutate_json(
            inputs["source"],
            lambda source: source["locks"][lock_name].update(
                {"filename": canonical_name}
            ),
        )
        if input_name == "runtime_lock":
            _mutate_json(
                inputs["installation_manifest"],
                lambda manifest: manifest["runtime_lock"].update(
                    {"filename": canonical_name}
                ),
            )
            _mutate_json(
                inputs["installed"],
                lambda evidence: evidence["candidate"].update(
                    {"runtime_lock_filename": canonical_name}
                ),
            )
    if input_name in {"runtime_lock", "installation_manifest"}:
        _mutate_json(
            inputs["installed"],
            lambda evidence: evidence["installation"].update(
                {"manifest_sha256": _sha256(inputs["installation_manifest"])}
            ),
        )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == f"发布证据封存失败：{reason}\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("input_name", "corrupt", "reason"),
    [
        pytest.param(
            "installation_manifest",
            lambda path: path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '  "schema_version": "production-installation-manifest.v1",',
                    '  "schema_version": "production-installation-manifest.v1",\n'
                    '  "schema_version": "production-installation-manifest.v1",',
                    1,
                ),
                encoding="utf-8",
            ),
            "installation_manifest.read_invalid",
            id="installation-duplicate",
        ),
        pytest.param(
            "keyless",
            lambda path: _mutate_json(
                path, lambda value: value.update({"allow_failure": True})
            ),
            "keyless_evidence.invalid",
            id="keyless-unknown",
        ),
        pytest.param(
            "installed",
            lambda path: _mutate_json(
                path, lambda value: value.pop("network")
            ),
            "installed_evidence.invalid",
            id="installed-missing",
        ),
    ],
)
def test_every_machine_evidence_input_uses_a_strict_schema(
    tmp_path, input_name, corrupt, reason
):
    inputs = _create_valid_inputs(tmp_path)
    corrupt(inputs[input_name])
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == f"发布证据封存失败：{reason}\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("case_id", "replacement"),
    [
        pytest.param(
            "short_video_success",
            {"exit_codes": [], "run_ids": [], "status": "passed"},
            id="missing-short-video-result",
        ),
        pytest.param(
            "overwrite",
            {
                "exit_codes": [0],
                "run_ids": [_acceptance_run(4)],
                "status": "passed",
            },
            id="wrong-overwrite-matrix",
        ),
        pytest.param(
            "cache_maintenance",
            {
                "exit_codes": [0, 0, 10],
                "run_ids": [_acceptance_run(11)],
                "status": "passed",
            },
            id="cache-command-claims-run",
        ),
    ],
)
def test_installed_cases_match_the_fixed_acceptance_matrix(
    tmp_path, case_id, replacement
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["installed"],
        lambda evidence: evidence["cases"].update({case_id: replacement}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：installed_evidence.invalid\n"
    assert not output.exists()


def test_installed_evidence_must_describe_the_same_installation_prefix(tmp_path):
    inputs = _create_valid_inputs(tmp_path)

    def replace_prefix(evidence):
        prefix = "/opt/other-installation"
        environment = f"{prefix}/versions/4.7.0/venv"
        evidence["installation"].update(
            {
                "prefix": prefix,
                "environment_prefix": environment,
                "console": f"{environment}/bin/video-auto-editor",
                "python": f"{environment}/bin/python",
            }
        )

    _mutate_json(inputs["installed"], replace_prefix)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：candidate.identity_mismatch\n"
    assert not output.exists()


def test_release_evidence_requires_kernel_network_namespace_attestation(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["keyless"],
        lambda evidence: evidence["network"].update({"mode": "python_guard"}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：keyless_evidence.invalid\n"
    assert not output.exists()


def test_real_runs_must_use_the_certified_providers(tmp_path):
    inputs = _create_valid_inputs(tmp_path)

    def replace_provider(source):
        for run in source["runs"].values():
            run["providers"]["transcription"]["provider_id"] = "other-provider"

    _mutate_json(inputs["source"], replace_provider)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.provider_invalid\n"
    assert not output.exists()


@pytest.mark.parametrize(
    ("input_name", "mutate", "reason"),
    [
        pytest.param(
            "keyless",
            lambda evidence: evidence["layers"]["unit_schema"].update(
                {"skipped": 1}
            ),
            "keyless_evidence.invalid",
            id="automatic-gate-anomaly",
        ),
        pytest.param(
            "installed",
            lambda evidence: evidence["statistics"].update(
                {"failed": 1, "passed": 9}
            ),
            "installed_evidence.invalid",
            id="installed-gate-failure",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["cold"]["providers"][
                "transcription"
            ]["requests"].update(
                {"count": 0, "succeeded": 0, "attempt_count_total": 0}
            ),
            "source.provider_invalid",
            id="cold-run-does-not-contact-provider",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["warm"]["providers"][
                "topic_review"
            ]["requests"].update(
                {"count": 1, "succeeded": 1, "attempt_count_total": 1}
            ),
            "source.provider_invalid",
            id="warm-run-contacts-provider",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["warm"]["providers"][
                "topic_review"
            ]["requests"].update({"attempt_count_total": 1}),
            "source.provider_invalid",
            id="warm-run-attempts-provider",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["warm"]["cache"][
                "subtitle_optimization"
            ].update(
                {"hits": 0, "misses": 1, "writes_published": 1}
            ),
            "source.cache_invalid",
            id="warm-run-cache-miss",
        ),
        pytest.param(
            "source",
            lambda source: source["independent_validations"]["cold"][
                "checks"
            ].update({"mp4": False}),
            "source.independent_validation_invalid",
            id="independent-validation-failure",
        ),
        pytest.param(
            "source",
            lambda source: source["semantic_equivalence"].update(
                {"warm_projection_sha256": "9" * 64}
            ),
            "source.schema_invalid",
            id="semantic-drift",
        ),
        pytest.param(
            "source",
            lambda source: source["manual_review"]["clips"][0][
                "checks"
            ].update({"title_summary_grounded": False}),
            "source.schema_invalid",
            id="manual-review-failure",
        ),
        pytest.param(
            "source",
            lambda source: source["manual_review"]["clips"][0].update(
                {"ordinal": True}
            ),
            "source.schema_invalid",
            id="boolean-manual-ordinal",
        ),
        pytest.param(
            "source",
            lambda source: source["manual_review"].update(
                {"run_id": WARM_RUN_ID}
            ),
            "source.schema_invalid",
            id="manual-review-wrong-run",
        ),
        pytest.param(
            "source",
            lambda source: source["manual_review"].update(
                {"source_and_transcript_compared": False}
            ),
            "source.schema_invalid",
            id="manual-review-did-not-compare-source",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["cold"]["terminal"].update(
                {"exit_code": False}
            ),
            "source.run_invalid",
            id="boolean-terminal-exit-code",
        ),
        pytest.param(
            "source",
            lambda source: source["runs"]["cold"]["environment"].update(
                {"certified_platform": "unverified-platform"}
            ),
            "source.run_invalid",
            id="uncertified-real-run-environment",
        ),
        pytest.param(
            "source",
            lambda source: source["inputs"]["source"].update(
                {"language": "en-US"}
            ),
            "source.schema_invalid",
            id="source-is-not-declared-as-real-chinese",
        ),
    ],
)
def test_no_hard_gate_can_be_sealed_as_a_success(
    tmp_path, input_name, mutate, reason
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(inputs[input_name], mutate)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == f"发布证据封存失败：{reason}\n"
    assert not output.exists()


def _retry_attempt(
    run_id: str,
    classification: str,
    *,
    commit_sha: str = COMMIT_SHA,
    wheel_sha256: str,
    stable_error_code: str = "transcription.service_unavailable",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "occurred_at": "2026-08-03T10:00:00Z",
        "classification": classification,
        "stable_error_code": stable_error_code,
        "candidate": {
            "commit_sha": commit_sha,
            "wheel_sha256": wheel_sha256,
        },
    }


def _install_retry_history(
    inputs: dict[str, Path],
    retries: list[dict[str, object]],
) -> None:
    workspace_parent = inputs["attempt"].parent
    successful_id = f"attempt-{len(retries) + 1:04d}"
    for suffix in ("workspace", "private", "cold.json", "review.json", "json"):
        source_path = workspace_parent / f"attempt-0001.{suffix}"
        source_path.rename(workspace_parent / f"{successful_id}.{suffix}")
    successful_cold = workspace_parent / f"{successful_id}.cold.json"
    _mutate_json(
        successful_cold,
        lambda record: record.update({"attempt_id": successful_id}),
    )
    successful_review = workspace_parent / f"{successful_id}.review.json"
    _mutate_json(
        successful_review,
        lambda record: record.update(
            {
                "attempt_id": successful_id,
                "cold_record_sha256": _sha256(successful_cold),
            }
        ),
    )
    successful_attempt = workspace_parent / f"{successful_id}.json"
    _mutate_json(
        successful_attempt,
        lambda record: record.update(
            {
                "attempt_id": successful_id,
                "phase_records": {
                    "cold_record_sha256": _sha256(successful_cold),
                    "review_record_sha256": _sha256(successful_review),
                },
            }
        ),
    )
    inputs["attempt"] = successful_attempt

    candidate = {
        "commit_sha": COMMIT_SHA,
        "wheel_sha256": _sha256(inputs["wheel"]),
    }
    plan_sha256 = _sha256(inputs["plan"])
    plan_document = json.loads(inputs["plan"].read_text(encoding="utf-8"))
    fingerprint_payload = {
        name: plan_document["inputs"][name]["sha256"]
        for name in (
            "source",
            "configuration",
            "course_context",
            "expected_transcript",
        )
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    context_sha256, context_observation = _course_context_binding(
        Path(plan_document["inputs"]["course_context"]["path"])
    )
    source_retries = []
    for number, retry in enumerate(retries, start=1):
        attempt_id = f"attempt-{number:04d}"
        workspace = workspace_parent / f"{attempt_id}.workspace"
        private = workspace_parent / f"{attempt_id}.private"
        workspace.mkdir(mode=0o700)
        private.mkdir(mode=0o700)
        run_id = str(retry["run_id"])
        classification = str(retry["classification"])
        stable_error_code = str(retry["stable_error_code"])
        run_root = workspace / "work" / "runs" / run_id
        run_root.mkdir(parents=True, mode=0o700)
        run_document = _raw_run_manifest(
            _run(run_id, warm=False),
            warm=False,
            source_fact=plan_document["inputs"]["source"],
            context_sha256=context_sha256,
            context_observation=context_observation,
            installation_sha256=plan_document["certified_host"]["installation"][
                "manifest"
            ]["sha256"],
        )
        if classification == "provider_transient":
            run_document["lifecycle"].update(
                {
                    "outcome": "failed",
                    "exit_code": 20,
                    "result_kind": {"status": "not_applicable"},
                }
            )
            run_document["errors"] = {
                "primary_error": {
                    "status": "present",
                    "error_code": stable_error_code,
                },
                "associated_errors": [],
                "recovery_incomplete": False,
            }
            failure = {
                "reason_code": "run.failed",
                "stable_error_code": stable_error_code,
                "classification": "unclassified",
                "permitted_same_candidate_rerun": ["provider_transient"],
                "same_candidate_rerun_allowed": False,
            }
        else:
            failure = {
                "reason_code": "independent_validation.launch_failed",
                "stable_error_code": "independent_validation.launch_failed",
                "classification": "unclassified",
                "permitted_same_candidate_rerun": [
                    "certified_host_infrastructure"
                ],
                "same_candidate_rerun_allowed": False,
            }
        _write_json(run_root / "run.json", run_document)
        ended_at = f"2026-08-03T09:{number:02d}:00.000Z"
        record_path = workspace_parent / f"{attempt_id}.json"
        _write_json(
            record_path,
            {
                "schema_version": "release_gate_attempt.v1",
                "attempt_id": attempt_id,
                "status": "failed",
                "started_at": f"2026-08-03T09:{number - 1:02d}:00.000Z",
                "ended_at": ended_at,
                "candidate": candidate,
                "input_fingerprint": input_fingerprint,
                "plan_sha256": plan_sha256,
                "phase_records": {},
                "failure_phase": "cold_run",
                "run_ids": [run_id],
                "failed_run_id": run_id,
                "failure": failure,
            },
        )
        record_path.chmod(0o600)
        classification_path = workspace_parent / f"{attempt_id}.classification.json"
        _write_json(
            classification_path,
            {
                "schema_version": "release_gate_failure_classification.v1",
                "attempt_id": attempt_id,
                "candidate": candidate,
                "input_fingerprint": input_fingerprint,
                "plan_sha256": plan_sha256,
                "failure_record_sha256": _sha256(record_path),
                "classification": classification,
                "operator_id": f"release-operator-{number:02d}",
                "classified_at": f"2026-08-03T09:{number:02d}:30.000Z",
                "same_candidate_rerun_allowed": True,
            },
        )
        classification_path.chmod(0o600)
        source_retries.append(
            _retry_attempt(
                run_id,
                classification,
                wheel_sha256=candidate["wheel_sha256"],
                stable_error_code=stable_error_code,
            )
        )
        source_retries[-1]["occurred_at"] = ended_at
    _mutate_json(
        inputs["source"],
        lambda source: source.update({"retry_attempts": source_retries}),
    )


def test_retry_summaries_without_their_failed_attempt_originals_are_rejected(
    tmp_path,
):
    inputs = _create_valid_inputs(tmp_path)
    attempts = [
        _retry_attempt(
            "run_33333333-3333-4333-8333-333333333333",
            "provider_transient",
            wheel_sha256=_sha256(inputs["wheel"]),
        ),
        _retry_attempt(
            "run_44444444-4444-4444-8444-444444444444",
            "certified_host_infrastructure",
            wheel_sha256=_sha256(inputs["wheel"]),
            stable_error_code="independent_validation.launch_failed",
        ),
    ]
    _mutate_json(
        inputs["source"],
        lambda source: source.update({"retry_attempts": attempts}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：release_gate.attempt_invalid\n"
    assert not output.exists()


def test_classified_retry_history_is_bound_one_to_one_to_failed_attempts(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    retries = [
        _retry_attempt(
            "run_33333333-3333-4333-8333-333333333333",
            "provider_transient",
            wheel_sha256=_sha256(inputs["wheel"]),
        ),
        _retry_attempt(
            "run_44444444-4444-4444-8444-444444444444",
            "certified_host_infrastructure",
            wheel_sha256=_sha256(inputs["wheel"]),
            stable_error_code="independent_validation.launch_failed",
        ),
    ]
    _install_retry_history(inputs, retries)
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert [item["run_id"] for item in evidence["retry_attempts"]] == [
        retry["run_id"] for retry in retries
    ]
    history = evidence["artifacts"]["release_gate_retry_history"]
    assert [item["failure_record"]["filename"] for item in history] == [
        "attempt-0001.json",
        "attempt-0002.json",
    ]
    assert all("operator_id" not in item for item in history)


def test_provider_transient_retry_must_originate_in_the_cold_phase(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    retries = [
        _retry_attempt(
            "run_33333333-3333-4333-8333-333333333333",
            "provider_transient",
            wheel_sha256=_sha256(inputs["wheel"]),
        )
    ]
    _install_retry_history(inputs, retries)
    workspace_parent = inputs["attempt"].parent
    forged_cold = workspace_parent / "attempt-0001.cold.json"
    _write_json(forged_cold, {})
    forged_cold.chmod(0o600)
    failed_record = workspace_parent / "attempt-0001.json"
    _mutate_json(
        failed_record,
        lambda record: record.update(
            {
                "failure_phase": "manual_review",
                "phase_records": {
                    "cold_record_sha256": _sha256(forged_cold)
                },
            }
        ),
    )
    classification = workspace_parent / "attempt-0001.classification.json"
    _mutate_json(
        classification,
        lambda record: record.update(
            {"failure_record_sha256": _sha256(failed_record)}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == (
        "发布证据封存失败：release_gate.attempt_invalid\n"
    )
    assert not output.exists()


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param(
            lambda wheel_sha256: _retry_attempt(
                "run_33333333-3333-4333-8333-333333333333",
                "manual_waiver",
                wheel_sha256=wheel_sha256,
            ),
            id="waiver-classification",
        ),
        pytest.param(
            lambda wheel_sha256: _retry_attempt(
                "run_33333333-3333-4333-8333-333333333333",
                "provider_transient",
                commit_sha="f" * 40,
                wheel_sha256=wheel_sha256,
            ),
            id="different-candidate",
        ),
        pytest.param(
            lambda wheel_sha256: _retry_attempt(
                "run_33333333-3333-4333-8333-333333333333",
                "certified_host_infrastructure",
                wheel_sha256=wheel_sha256,
                stable_error_code="transcription.service_unavailable",
            ),
            id="classification-error-code-mismatch",
        ),
        pytest.param(
            lambda wheel_sha256: _retry_attempt(
                "run_33333333-3333-4333-8333-333333333333",
                "certified_host_infrastructure",
                wheel_sha256=wheel_sha256,
                stable_error_code="run.launch_failed",
            ),
            id="uncertified-host-error-code",
        ),
    ],
)
def test_retry_records_cannot_waive_failures_or_switch_candidates(tmp_path, attempt):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source.update(
            {"retry_attempts": [attempt(_sha256(inputs["wheel"]))]}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_public_free_text_rejects_credential_shaped_content(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source.update(
            {
                "known_limitations": [
                    {
                        "code": "certified_platform_scope",
                        "statement": "STEPFUN_API_KEY=sk-must-not-escape",
                    }
                ]
            }
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


@pytest.mark.parametrize(
    "content_summary",
    (
        "plain English only",
        "/home/release/private/course.mp4 中的中文课程",
        "中文课程来自 https://private.example/source",
    ),
)
def test_public_source_summary_rejects_non_chinese_or_path_content(
    tmp_path,
    content_summary,
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["inputs"]["source"].update(
            {"content_summary": content_summary}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_required_known_limitations_cannot_be_omitted(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source.update({"known_limitations": []}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_public_input_identifiers_reject_credential_shaped_values(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["inputs"]["source"].update(
            {"version": "release-ghp_LEAKME1234567890"}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_automatic_gate_links_cannot_carry_query_credentials(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["automatic_gate_runs"]["keyless"].update(
            {
                "url": (
                    "https://github.com/dulltackle/long-video-autocut/"
                    "actions/runs/101?token=must-not-escape"
                )
            }
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_automatic_gate_links_must_target_this_repositorys_actions_run(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["automatic_gate_runs"][
            "installed_acceptance"
        ].update({"url": "https://example.invalid/actions/runs/101"}),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_both_automatic_evidence_files_must_come_from_the_same_workflow_run(
    tmp_path,
):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["automatic_gate_runs"][
            "installed_acceptance"
        ].update(
            {
                "url": (
                    "https://github.com/dulltackle/long-video-autocut/"
                    "actions/runs/102"
                )
            }
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert not output.exists()


def test_malformed_automatic_gate_url_has_a_stable_failure(tmp_path):
    inputs = _create_valid_inputs(tmp_path)
    _mutate_json(
        inputs["source"],
        lambda source: source["automatic_gate_runs"]["keyless"].update(
            {"url": "https://["}
        ),
    )
    output = tmp_path / "release-evidence.json"

    completed = _run_validator(inputs, output)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "发布证据封存失败：source.schema_invalid\n"
    assert "Traceback" not in completed.stderr
    assert not output.exists()
