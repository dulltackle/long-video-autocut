import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEPENDENT_VALIDATOR = (
    PROJECT_ROOT / "scripts" / "validate_installed_delivery.py"
)
INSTALLED_ACCEPTANCE_RUNNER = (
    PROJECT_ROOT / "scripts" / "run_installed_acceptance.py"
)
RUN_ID = "run_00000000-0000-4000-8000-000000000001"
TRANSCRIPT_ID = "transcript_00000000-0000-4000-8000-000000000002"
TRANSCRIPT_CHUNK_ID = (
    "transcript_chunk_00000000-0000-4000-8000-000000000003"
)
PLAN_ID = "plan_00000000-0000-4000-8000-000000000004"
CANDIDATE_ID = "candidate_00000000-0000-4000-8000-000000000005"
TRANSCRIPT_TEXT = "嗯，忠实原文必须保留语气词。"


def _json_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(contents):
    return "sha256:" + hashlib.sha256(contents).hexdigest()


def _write_empty_delivery(tmp_path):
    source = tmp_path / "source.mp4"
    _write_synthetic_source(source)
    delivery = tmp_path / "delivery"
    clips = delivery / "clips"
    clips.mkdir(parents=True)
    transcript = {
        "schema_version": "transcript.v1",
        "run_id": RUN_ID,
        "transcript_id": TRANSCRIPT_ID,
        "speech_presence": "present",
        "source_duration_ms": 6_000,
        "chunks": [
            {
                "transcript_chunk_id": TRANSCRIPT_CHUNK_ID,
                "start_ms": 200,
                "end_ms": 4_800,
                "text": TRANSCRIPT_TEXT,
            }
        ],
    }
    plan = {
        "schema_version": "clip_plan.v1",
        "run_id": RUN_ID,
        "plan_id": PLAN_ID,
        "transcript_id": TRANSCRIPT_ID,
        "result_kind": "empty",
        "candidate_count": 1,
        "published_count": 0,
        "candidates": [
            {
                "candidate_id": CANDIDATE_ID,
                "transcript_chunk_ids": [TRANSCRIPT_CHUNK_ID],
                "initial_range": {"start_ms": 200, "end_ms": 4_800},
                "final_range": {"start_ms": 200, "end_ms": 4_800},
                "boundary_remedy": {
                    "status": "not_needed",
                    "suggestion": "",
                    "requested_start_ms": None,
                    "requested_end_ms": None,
                },
                "review": {
                    "topic_name": "上下文不足",
                    "topic_complete": False,
                    "learning_value": 5,
                    "share_value": 4,
                    "publish_ready_score": 60,
                    "export_decision": "reject",
                    "title": "不发布的候选",
                    "summary": "完整评审后确认没有独立结论。",
                    "keywords": ["评审"],
                    "needs_human_review": False,
                    "reject_reason": "缺少独立结论",
                    "boundary_fix_suggestion": "",
                    "boundary_fix_start_ms": None,
                    "boundary_fix_end_ms": None,
                },
                "selection": {
                    "outcome": "rejected",
                    "reason_code": "review_rejected",
                    "needs_human_review": False,
                    "human_review_reason": "",
                },
            }
        ],
    }
    metadata = {
        "schema_version": "short_video_catalog.v1",
        "run_id": RUN_ID,
        "result_kind": "empty",
        "short_videos": [],
        "series": [],
    }
    report = (
        "# 直播拆条报告\n\n"
        f"- 运行标识：`{RUN_ID}`\n"
        "- 结果类型：`empty`\n"
        "- 候选数量：1\n"
        "- 发布数量：0\n\n"
        "## 结果说明\n\n"
        "本次运行成功完成，形成有效空结果；"
        "没有候选满足发布条件，短视频集合为空。\n"
    ).encode("utf-8")
    documents = {
        "metadata.json": _json_bytes(metadata),
        "plan.json": _json_bytes(plan),
        "report.md": report,
        "transcript.json": _json_bytes(transcript),
        "transcript.srt": (
            "1\n00:00:00,200 --> 00:00:04,800\n"
            f"{TRANSCRIPT_TEXT}\n\n"
        ).encode("utf-8"),
    }
    artifact_contract = {
        "metadata.json": ("short_video_catalog", "application/json"),
        "plan.json": ("clip_plan", "application/json"),
        "report.md": ("human_report", "text/markdown"),
        "transcript.json": ("faithful_transcript", "application/json"),
        "transcript.srt": (
            "faithful_transcript_rendering",
            "application/x-subrip",
        ),
    }
    for relative_path, contents in documents.items():
        (delivery / relative_path).write_bytes(contents)
    files = [
        {
            "path": relative_path,
            "role": artifact_contract[relative_path][0],
            "media_type": artifact_contract[relative_path][1],
            "byte_length": len(contents),
            "sha256": _sha256(contents),
        }
        for relative_path, contents in sorted(documents.items())
    ]
    manifest = {
        "schema_version": "delivery_manifest.v1",
        "run_id": RUN_ID,
        "terminal_state": "succeeded",
        "result_kind": "empty",
        "started_at": "2026-08-02T12:00:00.000Z",
        "published_at": "2026-08-02T12:01:00.000Z",
        "application_version": "4.7.0",
        "source": {
            "sha256": _sha256(source.read_bytes()),
            "byte_length": source.stat().st_size,
            "duration_ms": 6_000,
        },
        "documents": {
            "transcript": {
                "path": "transcript.json",
                "transcript_id": TRANSCRIPT_ID,
            },
            "transcript_rendering": {
                "path": "transcript.srt",
                "transcript_id": TRANSCRIPT_ID,
            },
            "plan": {"path": "plan.json", "plan_id": PLAN_ID},
            "metadata": {"path": "metadata.json"},
            "report": {"path": "report.md"},
        },
        "execution": {
            "subtitle_optimization": {
                "short_video_count": 0,
                "window_count": 0,
                "model_request_count": 0,
                "cache_hit_count": 0,
                "cache_miss_count": 0,
                "semantic_retry_count": 0,
                "transport_attempt_count": 0,
                "transport_retry_count": 0,
            }
        },
        "files": files,
    }
    (delivery / "manifest.json").write_bytes(_json_bytes(manifest))
    expected_transcript = tmp_path / "expected-transcript.json"
    expected_transcript.write_bytes(
        _json_bytes(
            {
                "schema_version": "installed_acceptance_transcript.v1",
                "speech_presence": "present",
                "source_duration_ms": 6_000,
                "chunks": [
                    {
                        "start_ms": 200,
                        "end_ms": 4_800,
                        "text": TRANSCRIPT_TEXT,
                    }
                ],
            }
        )
    )
    return delivery, expected_transcript, source


def _build_installed_candidate(tmp_path):
    wheel_directory = tmp_path / "candidate"
    wheel_directory.mkdir()
    wheel = wheel_directory / "video_auto_editor-4.7.0-py3-none-any.whl"
    dist_info = "video_auto_editor-4.7.0.dist-info"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        record_names = []
        for source_path in sorted(
            (PROJECT_ROOT / "video_auto_editor").rglob("*.py")
        ):
            archive_name = source_path.relative_to(PROJECT_ROOT).as_posix()
            archive.write(
                source_path,
                archive_name,
            )
            record_names.append(archive_name)
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: video-auto-editor\n"
            "Version: 4.7.0\n"
            "Requires-Python: >=3.12.3,<3.13\n",
        )
        record_names.append(f"{dist_info}/METADATA")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: installed-acceptance-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        record_names.append(f"{dist_info}/WHEEL")
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\n"
            "video-auto-editor = video_auto_editor.cli:main\n",
        )
        record_names.append(f"{dist_info}/entry_points.txt")
        record_path = f"{dist_info}/RECORD"
        archive.writestr(
            record_path,
            "".join(f"{name},,\n" for name in (*record_names, record_path)),
        )
    prefix = tmp_path / "installation"
    version_directory = prefix / "versions" / "4.7.0"
    environment = version_directory / "venv"
    venv.EnvBuilder(with_pip=True, symlinks=True).create(environment)
    install = subprocess.run(
        [
            str(environment / "bin" / "python"),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    package_versions = {
        "ca-certificates": "20240203",
        "ffmpeg": "7:6.1.1-3ubuntu5",
        "fontconfig": "2.15.0-1.1ubuntu2",
        "fonts-noto-cjk": "1:20230817+repack1-3",
        "python3.12": "3.12.3-1ubuntu0.8",
        "python3.12-venv": "3.12.3-1ubuntu0.8",
    }
    runtime_lock = wheel_directory / "requirements-runtime.lock"
    runtime_lock.write_bytes(b"# installed acceptance runtime lock\n")
    manifest = {
        "application": {
            "name": "video-auto-editor",
            "version": "4.7.0",
            "wheel": {
                "filename": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            },
        },
        "apt_snapshot_id": "20260725T000000Z",
        "environment": {
            "ffmpeg_version": "6.1.1-3ubuntu5",
            "ffprobe_version": "6.1.1-3ubuntu5",
            "font_family": "Noto Sans CJK SC",
            "font_file": (
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
            ),
        },
        "installation_prefix": str(prefix),
        "platform": {
            "architecture": "amd64",
            "operating_system": "ubuntu",
            "operating_system_version": "24.04",
        },
        "python": {"implementation": "CPython", "version": "3.12.3"},
        "runtime_lock": {
            "filename": runtime_lock.name,
            "sha256": hashlib.sha256(runtime_lock.read_bytes()).hexdigest(),
        },
        "schema_version": "production-installation-manifest.v1",
        "snapshot_packages": package_versions,
        "system_packages": package_versions,
        "wheelhouse": [],
    }
    manifest_path = version_directory / "installation-manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    (version_directory / "READY").write_bytes(
        _json_bytes(
            {
                "installation_manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "schema_version": "production-installation-ready.v1",
            }
        )
    )
    prefix.mkdir(exist_ok=True)
    (prefix / "current").symlink_to("versions/4.7.0")
    return (
        wheel,
        runtime_lock,
        prefix,
        environment / "bin" / "video-auto-editor",
    )


def _write_synthetic_source(path):
    completed = subprocess.run(
        [
            "ffmpeg",
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
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _write_acceptance_inputs(directory):
    source = directory / "course.mp4"
    _write_synthetic_source(source)
    (directory / "course.config.json").write_bytes(
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
    expected = directory / "expected-transcript.json"
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
                        "text": TRANSCRIPT_TEXT,
                    }
                ],
            }
        )
    )
    return source, expected


def _rewrite_json(path, mutate):
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_bytes(_json_bytes(value))


def _refresh_manifest_artifact(delivery, relative_path):
    artifact = delivery / relative_path

    def refresh(manifest):
        entry = next(
            item
            for item in manifest["files"]
            if item["path"] == relative_path
        )
        contents = artifact.read_bytes()
        entry["byte_length"] = len(contents)
        entry["sha256"] = _sha256(contents)

    _rewrite_json(delivery / "manifest.json", refresh)


def _assert_validator_rejects(
    *,
    delivery,
    expected_transcript,
    source,
    result,
    reason_code,
):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INDEPENDENT_VALIDATOR),
            "--delivery",
            str(delivery),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            "4.7.0",
            "--result",
            str(result),
        ],
        cwd=delivery.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "reason_code": reason_code,
        "schema_version": "independent_delivery_validation.v1",
        "success": False,
    }


def _load_acceptance_runner():
    spec = importlib.util.spec_from_file_location(
        "installed_acceptance_runner_under_test",
        INSTALLED_ACCEPTANCE_RUNNER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_independent_validator_accepts_a_complete_effective_empty_delivery(
    tmp_path,
):
    delivery, expected_transcript, source = _write_empty_delivery(tmp_path)
    result = tmp_path / "validation.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INDEPENDENT_VALIDATOR),
            "--delivery",
            str(delivery),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            "4.7.0",
            "--result",
            str(result),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "artifact_count": 5,
        "checks": {
            "digests": True,
            "exact_file_set": True,
            "faithful_transcript": True,
            "mp4": True,
            "path_safety": True,
            "references": True,
            "schema": True,
        },
        "result_kind": "empty",
        "run_id": RUN_ID,
        "schema_version": "independent_delivery_validation.v1",
        "short_video_count": 0,
        "success": True,
    }

    syntax = ast.parse(INDEPENDENT_VALIDATOR.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(syntax)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".", 1)[0]
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "video_auto_editor" not in imported_roots

    in_delivery_result = delivery / "validator-result.json"
    unsafe_result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INDEPENDENT_VALIDATOR),
            "--delivery",
            str(delivery),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            "4.7.0",
            "--result",
            str(in_delivery_result),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe_result.returncode == 1
    assert not in_delivery_result.exists()

    missing_result = tmp_path / "missing-delivery-validation.json"
    missing_delivery = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INDEPENDENT_VALIDATOR),
            "--delivery",
            str(tmp_path / "missing-delivery"),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            "4.7.0",
            "--result",
            str(missing_result),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_delivery.returncode == 1
    assert json.loads(missing_result.read_text(encoding="utf-8")) == {
        "reason_code": "delivery.invalid",
        "schema_version": "independent_delivery_validation.v1",
        "success": False,
    }

    def invalid_remedy(case_delivery):
        _rewrite_json(
            case_delivery / "plan.json",
            lambda plan: plan["candidates"][0]["boundary_remedy"].update(
                {"status": "bogus"}
            ),
        )
        _refresh_manifest_artifact(case_delivery, "plan.json")

    def overlapping_transcript(case_delivery):
        def mutate(transcript):
            transcript["chunks"].append(
                {
                    "transcript_chunk_id": (
                        "transcript_chunk_00000000-0000-4000-8000-000000000099"
                    ),
                    "start_ms": 4_700,
                    "end_ms": 5_000,
                    "text": "重叠文本",
                }
            )

        _rewrite_json(case_delivery / "transcript.json", mutate)
        _refresh_manifest_artifact(case_delivery, "transcript.json")

    def nonempty_empty_execution(case_delivery):
        def mutate(manifest):
            counts = manifest["execution"]["subtitle_optimization"]
            counts["cache_miss_count"] = 1
            counts["model_request_count"] = 1
            counts["window_count"] = 1

        _rewrite_json(case_delivery / "manifest.json", mutate)

    for name, mutate, reason_code in (
        ("remedy", invalid_remedy, "plan.schema_invalid"),
        ("overlap", overlapping_transcript, "transcript.schema_invalid"),
        ("empty-counts", nonempty_empty_execution, "result_kind.invalid"),
    ):
        case_root = tmp_path / f"invalid-empty-{name}"
        case_delivery = case_root / "delivery"
        shutil.copytree(delivery, case_delivery)
        case_expected = case_root / "expected-transcript.json"
        shutil.copy2(expected_transcript, case_expected)
        mutate(case_delivery)
        _assert_validator_rejects(
            delivery=case_delivery,
            expected_transcript=case_expected,
            source=source,
            result=case_root / "validation.json",
            reason_code=reason_code,
        )


def test_installed_acceptance_fails_closed_with_candidate_bound_evidence(
    tmp_path,
):
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate wheel identity")
    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_bytes(b"# locked runtime\n")
    evidence = tmp_path / "installed-acceptance.json"
    commit_sha = "a" * 40

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INSTALLED_ACCEPTANCE_RUNNER),
            "--wheel",
            str(wheel),
            "--commit-sha",
            commit_sha,
            "--runtime-lock",
            str(runtime_lock),
            "--apt-snapshot-id",
            "20260725T000000Z",
            "--installation-prefix",
            str(tmp_path / "missing-installation"),
            "--source-root",
            str(PROJECT_ROOT),
            "--harness-root",
            str(PROJECT_ROOT),
            "--work-root",
            str(tmp_path / "outside-repository"),
            "--evidence",
            str(evidence),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "KEYLESS_GATE_NETWORK_MODE": "python_guard",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert json.loads(evidence.read_text(encoding="utf-8")) == {
        "candidate": {
            "apt_snapshot_id": "20260725T000000Z",
            "commit_sha": commit_sha,
            "runtime_lock_filename": runtime_lock.name,
            "runtime_lock_sha256": hashlib.sha256(
                runtime_lock.read_bytes()
            ).hexdigest(),
            "wheel_filename": wheel.name,
            "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        },
        "cases": {},
        "failure_reason": "installation.identity_invalid",
        "installation": {"verified": False},
        "network": {
            "external_blocked": False,
            "loopback_allowed": False,
            "mode": "python_guard",
        },
        "schema_version": "installed_acceptance_evidence.v1",
        "statistics": {"failed": 0, "passed": 0, "total": 0},
        "success": False,
    }


def test_installed_acceptance_preserves_partial_case_statistics(
    tmp_path,
    monkeypatch,
):
    runner = _load_acceptance_runner()
    wheel = tmp_path / "video_auto_editor-4.7.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate wheel identity")
    runtime_lock = tmp_path / "requirements-runtime.lock"
    runtime_lock.write_bytes(b"# locked runtime\n")
    environment_prefix = tmp_path / "installation" / "venv"
    bin_directory = environment_prefix / "bin"
    bin_directory.mkdir(parents=True)
    console = bin_directory / "video-auto-editor"
    python = bin_directory / "python"
    console.write_bytes(b"#!/bin/sh\n")
    python.write_bytes(b"#!/bin/sh\n")
    evidence_path = tmp_path / "partial-evidence.json"

    monkeypatch.setattr(
        runner,
        "_network_attestation",
        lambda: {
            "external_blocked": False,
            "loopback_allowed": False,
            "mode": "python_guard",
        },
    )
    monkeypatch.setattr(
        runner,
        "_verify_installation",
        lambda *_args: {
            "application_version": "4.7.0",
            "console": str(console),
            "environment_prefix": str(environment_prefix),
            "python": str(python),
            "verified": True,
        },
    )
    monkeypatch.setattr(
        runner,
        "_verify_harness",
        lambda root: {
            "root": root,
            "validate_installed_delivery.py": root
            / "validate_installed_delivery.py",
        },
    )
    monkeypatch.setattr(runner, "_probe_network_policy", lambda **_kwargs: None)

    def write_inputs(work_root):
        source = work_root / "source.mp4"
        expected = work_root / "expected-transcript.json"
        source.write_bytes(b"source")
        expected.write_bytes(b"{}\n")
        return source, expected

    monkeypatch.setattr(runner, "_write_synthetic_inputs", write_inputs)

    def fail_after_one_case(**kwargs):
        cases = kwargs["cases"]
        cases["short_video_success"] = runner._case_result(
            exit_codes=(0,),
            run_ids=(RUN_ID,),
            short_video_count=1,
        )
        runner._start_case(cases, "effective_empty")
        raise runner.AcceptanceFailure("case.effective_empty.failed")

    monkeypatch.setattr(runner, "_execute_matrix", fail_after_one_case)

    succeeded = runner.run_acceptance(
        wheel=wheel,
        runtime_lock=runtime_lock,
        commit_sha="c" * 40,
        apt_snapshot_id="20260725T000000Z",
        installation_prefix=tmp_path / "installation",
        source_root=PROJECT_ROOT,
        harness_root=PROJECT_ROOT / "scripts",
        work_root=tmp_path / "partial-work",
        evidence_path=evidence_path,
    )

    assert succeeded is False
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["statistics"] == {"failed": 1, "passed": 1, "total": 2}
    assert evidence["cases"]["short_video_success"]["status"] == "passed"
    assert evidence["cases"]["effective_empty"] == {
        "exit_codes": [],
        "reason_code": "case.effective_empty.failed",
        "run_ids": [],
        "status": "failed",
    }


def test_installed_acceptance_runs_the_complete_black_box_matrix(tmp_path):
    wheel, runtime_lock, prefix, _console = _build_installed_candidate(tmp_path)
    outside = tmp_path / "complete-outside-repository"
    evidence = tmp_path / "installed-acceptance.json"
    commit_sha = "b" * 40
    environment = os.environ.copy()
    environment.pop("STEPFUN_API_KEY", None)
    environment.update(
        {
            "KEYLESS_GATE_NETWORK_MODE": "python_guard",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INSTALLED_ACCEPTANCE_RUNNER),
            "--wheel",
            str(wheel),
            "--commit-sha",
            commit_sha,
            "--runtime-lock",
            str(runtime_lock),
            "--apt-snapshot-id",
            "20260725T000000Z",
            "--installation-prefix",
            str(prefix),
            "--source-root",
            str(PROJECT_ROOT),
            "--harness-root",
            str(PROJECT_ROOT / "scripts"),
            "--work-root",
            str(outside),
            "--evidence",
            str(evidence),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(evidence.read_text(encoding="utf-8"))
    expected_cases = {
        "cache_maintenance",
        "effective_empty",
        "overwrite",
        "postcommit_signal",
        "repeated_signal",
        "rollback",
        "short_video_success",
        "sigint",
        "sigterm",
        "typed_failure",
    }
    assert result["schema_version"] == "installed_acceptance_evidence.v1"
    assert result["success"] is True
    assert "failure_reason" not in result
    assert set(result["cases"]) == expected_cases
    assert all(
        case["status"] == "passed" for case in result["cases"].values()
    )
    assert result["statistics"] == {
        "failed": 0,
        "passed": len(expected_cases),
        "total": len(expected_cases),
    }
    assert result["candidate"] == {
        "apt_snapshot_id": "20260725T000000Z",
        "commit_sha": commit_sha,
        "runtime_lock_filename": runtime_lock.name,
        "runtime_lock_sha256": hashlib.sha256(
            runtime_lock.read_bytes()
        ).hexdigest(),
        "wheel_filename": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    assert result["installation"]["verified"] is True
    assert result["network"] == {
        "external_blocked": True,
        "loopback_allowed": True,
        "mode": "python_guard",
    }
    assert result["cases"]["short_video_success"]["short_video_count"] == 1
    assert result["cases"]["effective_empty"]["short_video_count"] == 0
    assert result["cases"]["typed_failure"]["exit_codes"] == [30]
    assert result["cases"]["overwrite"]["exit_codes"] == [60, 0]
    assert result["cases"]["rollback"]["exit_codes"] == [143]
    assert result["cases"]["sigint"]["exit_codes"] == [130]
    assert result["cases"]["sigterm"]["exit_codes"] == [143]
    assert result["cases"]["repeated_signal"]["exit_codes"] == [130, 0]
    assert result["cases"]["postcommit_signal"]["exit_codes"] == [0]


def test_installed_console_runs_real_media_from_outside_the_repository(
    tmp_path,
):
    wheel, _runtime_lock, prefix, console = _build_installed_candidate(tmp_path)
    outside = tmp_path / "outside-repository"
    outside.mkdir()
    source, expected_transcript = _write_acceptance_inputs(outside)
    workspace = outside / "workspace"
    process_audit = outside / "process-audit.json"
    network_audit = outside / "network-audit.log"
    environment = os.environ.copy()
    environment.pop("STEPFUN_API_KEY", None)
    environment.update(
        {
            "INSTALLED_ACCEPTANCE_PROCESS_AUDIT": str(process_audit),
            "INSTALLED_ACCEPTANCE_SCENARIO": "clips",
            "KEYLESS_GATE_NETWORK_AUDIT": str(network_audit),
            "KEYLESS_GATE_NETWORK_MODE": "python_guard",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PROJECT_ROOT / "scripts"),
        }
    )

    completed = subprocess.run(
        [
            str(console),
            "live",
            str(source),
            "--workspace-dir",
            str(workspace),
        ],
        cwd=outside,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    validation = outside / "delivery-validation.json"
    verified = subprocess.run(
        [
            sys.executable,
            "-I",
            str(INDEPENDENT_VALIDATOR),
            "--delivery",
            str(workspace / "delivery"),
            "--expected-transcript",
            str(expected_transcript),
            "--source",
            str(source),
            "--expected-application-version",
            "4.7.0",
            "--result",
            str(validation),
        ],
        cwd=outside,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    result = json.loads(validation.read_text(encoding="utf-8"))
    assert result["result_kind"] == "clips"
    assert result["short_video_count"] == 1
    audit = json.loads(process_audit.read_text(encoding="utf-8"))
    assert Path(audit["candidate_package_file"]).is_relative_to(
        (prefix / "current" / "venv").resolve()
    )
    assert Path(audit["cwd"]) == outside
    assert Path(audit["console"]) == console
    assert audit["production_credentials_present"] == []
    assert not Path(audit["candidate_package_file"]).is_relative_to(PROJECT_ROOT)
    assert wheel.is_file()

    mutations = []

    def invalid_manifest_schema(delivery, _expected):
        _rewrite_json(
            delivery / "manifest.json",
            lambda manifest: manifest.update({"unknown": True}),
        )

    mutations.append(("schema", invalid_manifest_schema, "manifest.schema_invalid"))

    def wrong_source_identity(delivery, _expected):
        _rewrite_json(
            delivery / "manifest.json",
            lambda manifest: manifest["source"].update(
                {"sha256": "sha256:" + "0" * 64}
            ),
        )

    mutations.append(
        ("source-identity", wrong_source_identity, "source.identity_mismatch")
    )

    def wrong_application_version(delivery, _expected):
        _rewrite_json(
            delivery / "manifest.json",
            lambda manifest: manifest.update({"application_version": "9.9.9"}),
        )

    mutations.append(
        (
            "application-version",
            wrong_application_version,
            "application.version_mismatch",
        )
    )

    def extra_file(delivery, _expected):
        (delivery / "unexpected.txt").write_text("rogue", encoding="utf-8")

    mutations.append(("file-set", extra_file, "artifact.file_set_mismatch"))

    def bad_digest(delivery, _expected):
        (delivery / "report.md").write_text("tampered", encoding="utf-8")

    mutations.append(("digest", bad_digest, "artifact.digest_mismatch"))

    def unsafe_path(delivery, _expected):
        def mutate(manifest):
            manifest["files"][0]["path"] = "../metadata.json"

        _rewrite_json(delivery / "manifest.json", mutate)

    mutations.append(("path", unsafe_path, "path.invalid"))

    def symlinked_media(delivery, _expected):
        metadata = json.loads(
            (delivery / "metadata.json").read_text(encoding="utf-8")
        )
        media = delivery / metadata["short_videos"][0]["media"]["path"]
        media.unlink()
        media.symlink_to(source)

    mutations.append(("symlink", symlinked_media, "path.invalid"))

    def dangling_reference(delivery, _expected):
        _rewrite_json(
            delivery / "plan.json",
            lambda plan: plan.update(
                {"plan_id": "plan_00000000-0000-4000-8000-000000000099"}
            ),
        )
        _refresh_manifest_artifact(delivery, "plan.json")

    mutations.append(("reference", dangling_reference, "reference.invalid"))

    def nonscalar_chunk_reference(delivery, _expected):
        _rewrite_json(
            delivery / "plan.json",
            lambda plan: plan["candidates"][0].update(
                {"transcript_chunk_ids": [{}]}
            ),
        )
        _refresh_manifest_artifact(delivery, "plan.json")

    mutations.append(
        ("reference-type", nonscalar_chunk_reference, "plan.reference_invalid")
    )

    def unfaithful_transcript(_delivery, expected):
        _rewrite_json(
            expected,
            lambda transcript: transcript["chunks"][0].update(
                {"text": "被篡改的预期转写"}
            ),
        )

    mutations.append(
        ("faithful-transcript", unfaithful_transcript, "transcript.not_faithful")
    )

    def forged_metadata(delivery, _expected):
        _rewrite_json(
            delivery / "metadata.json",
            lambda metadata: metadata["short_videos"][0].update(
                {"summary": "与候选评审不一致的伪摘要"}
            ),
        )
        _refresh_manifest_artifact(delivery, "metadata.json")

    mutations.append(
        ("cross-document", forged_metadata, "metadata.reference_invalid")
    )

    def forged_report(delivery, _expected):
        (delivery / "report.md").write_text(
            "# 伪造报告\n",
            encoding="utf-8",
        )
        _refresh_manifest_artifact(delivery, "report.md")

    mutations.append(("report", forged_report, "report.invalid"))

    def invalid_media(delivery, _expected):
        metadata = json.loads(
            (delivery / "metadata.json").read_text(encoding="utf-8")
        )
        media = delivery / metadata["short_videos"][0]["media"]["path"]
        contents = media.read_bytes()
        media.write_bytes(contents[: len(contents) * 9 // 10])
        _refresh_manifest_artifact(
            delivery,
            metadata["short_videos"][0]["media"]["path"],
        )

    mutations.append(("mp4", invalid_media, "media.invalid"))

    for name, mutate, reason_code in mutations:
        case_root = outside / f"invalid-{name}"
        delivery = case_root / "delivery"
        shutil.copytree(workspace / "delivery", delivery)
        case_expected = case_root / "expected-transcript.json"
        shutil.copy2(expected_transcript, case_expected)
        mutate(delivery, case_expected)
        _assert_validator_rejects(
            delivery=delivery,
            expected_transcript=case_expected,
            source=source,
            result=case_root / "validation.json",
            reason_code=reason_code,
        )
