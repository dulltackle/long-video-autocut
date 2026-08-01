import json
from inspect import signature

import pytest

from tests.support.deterministic_composition import (
    compose_deterministic_live_application,
)
from video_auto_editor import cli
from video_auto_editor.application import LiveRunRequest, run_interpretation
from video_auto_editor.application.run_interpretation import interpret_run
from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.diagnostics import InterruptionSignal
from video_auto_editor.runtime.errors import RunStage


def _delivery_manifest(run_id, *, result_kind, short_video_count):
    if short_video_count not in {0, 1}:
        raise ValueError("测试清单只支持零或一个短视频")
    execution_counts = {
        "short_video_count": short_video_count,
        "window_count": short_video_count,
        "model_request_count": short_video_count,
        "cache_hit_count": 0,
        "cache_miss_count": short_video_count,
        "semantic_retry_count": 0,
        "transport_attempt_count": short_video_count,
        "transport_retry_count": 0,
    }
    files = [
        {
            "path": "metadata.json",
            "role": "short_video_catalog",
            "media_type": "application/json",
            "byte_length": 101,
            "sha256": "sha256:" + "1" * 64,
        },
        {
            "path": "plan.json",
            "role": "clip_plan",
            "media_type": "application/json",
            "byte_length": 102,
            "sha256": "sha256:" + "2" * 64,
        },
        {
            "path": "report.md",
            "role": "human_report",
            "media_type": "text/markdown",
            "byte_length": 103,
            "sha256": "sha256:" + "3" * 64,
        },
        {
            "path": "transcript.json",
            "role": "faithful_transcript",
            "media_type": "application/json",
            "byte_length": 104,
            "sha256": "sha256:" + "4" * 64,
        },
        {
            "path": "transcript.srt",
            "role": "faithful_transcript_rendering",
            "media_type": "application/x-subrip",
            "byte_length": 105,
            "sha256": "sha256:" + "5" * 64,
        },
    ]
    if short_video_count:
        files.insert(
            0,
            {
                "path": (
                    "clips/"
                    "short_video_11111111-1111-4111-8111-111111111111.mp4"
                ),
                "role": "short_video_media",
                "media_type": "video/mp4",
                "byte_length": 106,
                "sha256": "sha256:" + "6" * 64,
            },
        )
    return {
        "schema_version": "delivery_manifest.v1",
        "run_id": str(run_id),
        "terminal_state": "succeeded",
        "result_kind": result_kind,
        "started_at": "2026-07-31T12:00:00.000Z",
        "published_at": "2026-07-31T12:10:00.000Z",
        "application_version": "4.7.0",
        "source": {
            "sha256": "sha256:" + "a" * 64,
            "byte_length": 42,
            "duration_ms": 300_000,
        },
        "documents": {
            "transcript": {
                "path": "transcript.json",
                "transcript_id": (
                    "transcript_33333333-3333-4333-8333-333333333333"
                ),
            },
            "transcript_rendering": {
                "path": "transcript.srt",
                "transcript_id": (
                    "transcript_33333333-3333-4333-8333-333333333333"
                ),
            },
            "plan": {
                "path": "plan.json",
                "plan_id": "plan_44444444-4444-4444-8444-444444444444",
            },
            "metadata": {"path": "metadata.json"},
            "report": {"path": "report.md"},
        },
        "execution": {"subtitle_optimization": execution_counts},
        "files": files,
    }


def test_interpret_run_explains_failure_from_the_run_manifest(
    tmp_path,
    capsys,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    capsys.readouterr()
    run_directory = next((workspace / "work" / "runs").iterdir())

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation == {
        "run_id": run_directory.name,
        "diagnostics": {
            "state": "complete",
            "reason": "valid",
        },
        "terminal_state": "failed",
        "exit_code": 2,
        "result_kind": None,
        "interruption_signal": None,
        "primary_error": {
            "error_code": "config.schema_invalid",
            "safe_message": "配置 schema 不受支持或不合法。",
            "retryable_in_new_run": True,
            "operator_action": "fix_configuration",
        },
        "associated_errors": [],
        "recovery_incomplete": False,
        "delivery_manifest": {
            "state": "not_applicable",
            "reason": "terminal_not_succeeded",
        },
        "delivery": None,
    }


@pytest.mark.parametrize(
    "terminal_text",
    (
        "完全不同的终端格式\n",
        "success failed interrupted 都只是人类文本\n",
    ),
)
def test_interpret_run_ignores_terminal_text(
    tmp_path,
    monkeypatch,
    terminal_text,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    def render_arbitrary_text(renderer, _outcome, _diagnostics_directory):
        renderer._stderr.write(terminal_text)

    monkeypatch.setattr(
        cli.TerminalRenderer,
        "render_live_outcome",
        render_arbitrary_text,
    )
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    run_directory = next((workspace / "work" / "runs").iterdir())

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation["terminal_state"] == "failed"
    assert interpretation["primary_error"] == {
        "error_code": "config.schema_invalid",
        "safe_message": "配置 schema 不受支持或不合法。",
        "retryable_in_new_run": True,
        "operator_action": "fix_configuration",
    }


def test_interpret_run_reports_missing_terminal_manifest_as_incomplete(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        failure_stage=RunStage.SOURCE_ANALYSIS,
        terminal_diagnostics_failure=True,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))
    run_directory = workspace / "work" / "runs" / str(outcome.run_id)
    assert not (run_directory / "run.json").exists()

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation == {
        "run_id": str(outcome.run_id),
        "diagnostics": {
            "state": "incomplete",
            "reason": "terminal_event_missing",
        },
        "terminal_state": None,
        "exit_code": None,
        "result_kind": None,
        "interruption_signal": None,
        "primary_error": None,
        "associated_errors": [],
        "recovery_incomplete": None,
        "delivery_manifest": {
            "state": "incomplete",
            "reason": "manifest_missing",
        },
        "delivery": None,
    }


def test_interpret_run_reports_an_uninitialized_diagnostic_package(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    run_id = "run_11111111-1111-4111-8111-111111111111"
    (workspace / "work" / "runs" / run_id).mkdir(parents=True)

    interpretation = interpret_run(workspace, run_id)

    assert interpretation["diagnostics"] == {
        "state": "incomplete",
        "reason": "event_log_missing",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_missing",
    }
    assert interpretation["delivery"] is None


def test_interpret_run_explains_interruption_and_recovery_from_run_manifest(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        interruption_stage=RunStage.TRANSCRIPTION,
        interruption_signal=InterruptionSignal.SIGTERM,
        cleanup_failure=True,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))

    interpretation = interpret_run(workspace, outcome.run_id)

    assert {
        "terminal_state": interpretation["terminal_state"],
        "exit_code": interpretation["exit_code"],
        "interruption_signal": interpretation["interruption_signal"],
        "primary_error": interpretation["primary_error"],
        "associated_errors": interpretation["associated_errors"],
        "recovery_incomplete": interpretation["recovery_incomplete"],
    } == {
        "terminal_state": "interrupted",
        "exit_code": 143,
        "interruption_signal": "sigterm",
        "primary_error": None,
        "associated_errors": [
            {
                "error_code": "workspace.cleanup_failed",
                "safe_message": "受管 workspace 清理不完整。",
                "retryable_in_new_run": True,
                "operator_action": "report_internal_error",
            }
        ],
        "recovery_incomplete": True,
    }
    assert interpretation["delivery_manifest"] == {
        "state": "not_applicable",
        "reason": "terminal_not_succeeded",
    }


def test_interpret_run_explains_a_clean_interruption_from_run_manifest(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        interruption_stage=RunStage.TRANSCRIPTION,
        interruption_signal=InterruptionSignal.SIGINT,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["terminal_state"] == "interrupted"
    assert interpretation["exit_code"] == 130
    assert interpretation["interruption_signal"] == "sigint"
    assert interpretation["primary_error"] is None
    assert interpretation["associated_errors"] == []
    assert interpretation["recovery_incomplete"] is False


def test_interpret_run_summarizes_a_clips_delivery_manifest(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        result_kind=ResultKind.CLIPS,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                outcome.run_id,
                result_kind="clips",
                short_video_count=1,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert {
        "terminal_state": interpretation["terminal_state"],
        "result_kind": interpretation["result_kind"],
        "delivery": interpretation["delivery"],
    } == {
        "terminal_state": "succeeded",
        "result_kind": "clips",
        "delivery": {
            "manifest_path": "delivery/manifest.json",
            "schema_version": "delivery_manifest.v1",
            "run_id": str(outcome.run_id),
            "result_kind": "clips",
            "application_version": "4.7.0",
            "started_at": "2026-07-31T12:00:00.000Z",
            "published_at": "2026-07-31T12:10:00.000Z",
            "source": {
                "sha256": "sha256:" + "a" * 64,
                "byte_length": 42,
                "duration_ms": 300_000,
            },
            "short_video_count": 1,
            "file_count": 6,
        },
    }
    assert interpretation["delivery_manifest"] == {
        "state": "complete",
        "reason": "valid",
    }


def test_interpret_run_distinguishes_an_effective_empty_delivery(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        result_kind=ResultKind.EMPTY,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                outcome.run_id,
                result_kind="empty",
                short_video_count=0,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["terminal_state"] == "succeeded"
    assert interpretation["result_kind"] == "empty"
    assert interpretation["delivery"]["result_kind"] == "empty"
    assert interpretation["delivery"]["short_video_count"] == 0
    assert interpretation["delivery"]["file_count"] == 5
    assert interpretation["delivery_manifest"] == {
        "state": "complete",
        "reason": "valid",
    }


def test_interpret_run_cannot_receive_terminal_text_or_process_exit_code():
    assert tuple(signature(interpret_run).parameters) == (
        "workspace_dir",
        "run_id",
    )


def test_dispatcher_does_not_expose_retired_business_decision_helpers():
    assert {
        "diagnose_run",
        "interpret_artifacts",
        "interpret_output_dir",
        "load_artifacts",
    }.isdisjoint(vars(run_interpretation))


def test_interpret_run_reports_an_unsupported_delivery_manifest(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    manifest = _delivery_manifest(
        outcome.run_id,
        result_kind="clips",
        short_video_count=1,
    )
    manifest["schema_version"] = "delivery_manifest.v999"
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["delivery_manifest"] == {
        "state": "corrupt",
        "reason": "manifest_schema_invalid",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["result_kind"] is None
    assert interpretation["delivery"] is None


def test_interpret_run_rejects_a_delivery_result_that_disagrees_with_run(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        result_kind=ResultKind.CLIPS,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                outcome.run_id,
                result_kind="empty",
                short_video_count=0,
            )
        ),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["delivery_manifest"] == {
        "state": "corrupt",
        "reason": "manifest_result_kind_mismatch",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery"] is None


def test_interpret_run_rejects_a_package_moved_under_another_run_id(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    runs = workspace / "work" / "runs"
    actual_directory = next(runs.iterdir())
    requested_run_id = "run_99999999-9999-4999-8999-999999999999"
    actual_directory.rename(runs / requested_run_id)

    interpretation = interpret_run(workspace, requested_run_id)

    assert interpretation["diagnostics"] == {
        "state": "corrupt",
        "reason": "run_id_directory_mismatch",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_missing",
    }


@pytest.mark.parametrize(
    ("unreadable_name", "reason"),
    (
        ("events.jsonl", "event_log_unreadable"),
        ("run.json", "manifest_unreadable"),
    ),
)
def test_interpret_run_reports_unreadable_diagnostics_as_incomplete(
    tmp_path,
    monkeypatch,
    unreadable_name,
    reason,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    original_open = run_interpretation.os.open

    def fail_selected_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == unreadable_name:
            raise PermissionError("sensitive filesystem detail")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(run_interpretation.os, "open", fail_selected_open)

    interpretation = interpret_run(
        workspace,
        next((workspace / "work" / "runs").iterdir()).name,
    )

    assert interpretation["diagnostics"] == {
        "state": "incomplete",
        "reason": reason,
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["primary_error"] is None


def test_interpret_run_reports_an_unreadable_delivery_as_incomplete(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    original_open = run_interpretation.os.open

    def fail_delivery_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "manifest.json":
            raise PermissionError("sensitive filesystem detail")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(run_interpretation.os, "open", fail_delivery_open)

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["diagnostics"] == {
        "state": "complete",
        "reason": "valid",
    }
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_unreadable",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery"] is None


@pytest.mark.parametrize(
    ("diagnostic_name", "reason"),
    (
        ("events.jsonl", "event_log_symlink"),
        ("run.json", "manifest_symlink"),
    ),
)
def test_interpret_run_rejects_symlinked_diagnostic_manifests(
    tmp_path,
    diagnostic_name,
    reason,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    run_directory = next((workspace / "work" / "runs").iterdir())
    manifest_path = run_directory / diagnostic_name
    external_path = tmp_path / f"external-{diagnostic_name}"
    manifest_path.rename(external_path)
    manifest_path.symlink_to(external_path)

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation["diagnostics"] == {
        "state": "corrupt",
        "reason": reason,
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_missing",
    }


def test_interpret_run_rejects_a_symlinked_delivery_manifest(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    manifest_path = workspace / "delivery" / "manifest.json"
    external_path = tmp_path / "external-delivery-manifest.json"
    manifest_path.rename(external_path)
    manifest_path.symlink_to(external_path)

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["delivery_manifest"] == {
        "state": "corrupt",
        "reason": "manifest_symlink",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery"] is None


def test_interpret_run_uses_delivery_when_run_manifest_is_a_symlink(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                outcome.run_id,
                result_kind="clips",
                short_video_count=1,
            )
        ),
        encoding="utf-8",
    )
    run_manifest = (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "run.json"
    )
    external_manifest = tmp_path / "external-run.json"
    run_manifest.rename(external_manifest)
    run_manifest.symlink_to(external_manifest)

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["diagnostics"] == {
        "state": "corrupt",
        "reason": "manifest_symlink",
    }
    assert interpretation["terminal_state"] == "succeeded"
    assert interpretation["result_kind"] == "clips"
    assert interpretation["delivery_manifest"] == {
        "state": "complete",
        "reason": "valid",
    }


def test_interpret_run_rejects_a_symlinked_run_directory(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    runs = workspace / "work" / "runs"
    run_directory = next(runs.iterdir())
    external_directory = tmp_path / "external-run"
    run_directory.rename(external_directory)
    run_directory.symlink_to(external_directory, target_is_directory=True)

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation["diagnostics"] == {
        "state": "corrupt",
        "reason": "event_log_symlink",
    }
    assert interpretation["terminal_state"] is None


def test_interpret_run_rejects_a_symlinked_delivery_directory(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    delivery_directory = workspace / "delivery"
    external_directory = tmp_path / "external-delivery"
    delivery_directory.rename(external_directory)
    delivery_directory.symlink_to(
        external_directory,
        target_is_directory=True,
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["delivery_manifest"] == {
        "state": "corrupt",
        "reason": "manifest_symlink",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["delivery"] is None


def test_interpret_run_does_not_infer_success_without_delivery_manifest(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    (workspace / "delivery" / "manifest.json").unlink()

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["diagnostics"] == {
        "state": "complete",
        "reason": "valid",
    }
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_missing",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["result_kind"] is None
    assert interpretation["delivery"] is None


def test_interpret_run_accepts_valid_delivery_after_diagnostic_tail_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"deterministic source")
    workspace = tmp_path / "workspace"
    outcome = compose_deterministic_live_application(
        postcommit_effect_failure=True,
    ).execute(LiveRunRequest(source, workspace_dir=workspace))
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                outcome.run_id,
                result_kind="clips",
                short_video_count=1,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, outcome.run_id)

    assert interpretation["diagnostics"] == {
        "state": "incomplete",
        "reason": "terminal_event_missing",
    }
    assert interpretation["delivery_manifest"] == {
        "state": "complete",
        "reason": "valid",
    }
    assert interpretation["terminal_state"] == "succeeded"
    assert interpretation["exit_code"] is None
    assert interpretation["result_kind"] == "clips"
    assert interpretation["delivery"]["short_video_count"] == 1


def test_interpret_run_uses_delivery_to_resolve_a_corrupt_diagnostic_package(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    run_directory = next((workspace / "work" / "runs").iterdir())
    (run_directory / "run.json").write_bytes(b'{"broken":true}\n')

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation["diagnostics"] == {
        "state": "corrupt",
        "reason": "manifest_field_unknown",
    }
    assert interpretation["terminal_state"] is None
    assert interpretation["exit_code"] is None
    assert interpretation["primary_error"] is None
    assert interpretation["delivery_manifest"] == {
        "state": "incomplete",
        "reason": "manifest_missing",
    }
    assert interpretation["delivery"] is None

    (workspace / "delivery").mkdir(exist_ok=True)
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                run_directory.name,
                result_kind="clips",
                short_video_count=1,
            )
        ),
        encoding="utf-8",
    )

    delivered = interpret_run(workspace, run_directory.name)

    assert delivered["diagnostics"] == {
        "state": "corrupt",
        "reason": "manifest_field_unknown",
    }
    assert delivered["terminal_state"] == "succeeded"
    assert delivered["exit_code"] is None
    assert delivered["result_kind"] == "clips"
    assert delivered["delivery_manifest"] == {
        "state": "complete",
        "reason": "valid",
    }
    assert delivered["delivery"]["short_video_count"] == 1


def test_interpret_run_does_not_use_delivery_to_override_failure(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    assert cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    ) == 2
    run_directory = next((workspace / "work" / "runs").iterdir())
    (workspace / "delivery").mkdir(exist_ok=True)
    (workspace / "delivery" / "manifest.json").write_text(
        json.dumps(
            _delivery_manifest(
                run_directory.name,
                result_kind="clips",
                short_video_count=1,
            )
        ),
        encoding="utf-8",
    )

    interpretation = interpret_run(workspace, run_directory.name)

    assert interpretation["terminal_state"] == "failed"
    assert interpretation["primary_error"]["operator_action"] == (
        "fix_configuration"
    )
    assert interpretation["delivery_manifest"] == {
        "state": "not_applicable",
        "reason": "terminal_not_succeeded",
    }
    assert interpretation["delivery"] is None
