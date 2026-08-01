import json
from importlib.metadata import version
from io import StringIO

import pytest

from video_auto_editor import cli
from video_auto_editor.application import LiveRunRequest
from video_auto_editor.composition import compose_live_application
from video_auto_editor.workspace import Workspace


def test_root_help_only_exposes_live_and_cache_commands(capsys):
    with pytest.raises(SystemExit) as captured:
        cli.main(["--help"])

    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "{live,cache}" in help_text
    for retired_surface in (
        "single",
        "batch",
        "--output-dir",
        "--work-dir",
        "--config-file",
        "--context-file",
        "--max-clips",
        "--dry-run",
        "--allow-unreviewed-export",
        "--quiet",
    ):
        assert retired_surface not in help_text


@pytest.mark.parametrize("retired_command", ["single", "batch"])
def test_retired_business_command_is_rejected_without_creating_a_run(
    retired_command,
    tmp_path,
):
    source = tmp_path / "course.mp4"

    with pytest.raises(SystemExit) as captured:
        cli.main([retired_command, str(source)])

    assert captured.value.code == 2
    assert not source.with_suffix(".autocut").exists()
    assert not any(tmp_path.rglob("work/runs"))


def test_version_query_returns_installed_application_version(capsys):
    with pytest.raises(SystemExit) as captured:
        cli.main(["--version"])

    assert captured.value.code == 0
    assert capsys.readouterr().out == (
        f"video-auto-editor {version('video-auto-editor')}\n"
    )


def test_live_help_only_exposes_the_formal_run_arguments(capsys):
    with pytest.raises(SystemExit) as captured:
        cli.main(["live", "--help"])

    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "SOURCE" in help_text.upper()
    assert "--workspace-dir" in help_text
    assert "--overwrite" in help_text
    for retired_argument in (
        "--output-dir",
        "--work-dir",
        "--config-file",
        "--context-file",
        "--max-clips",
        "--dry-run",
        "--allow-unreviewed-export",
        "--quiet",
    ):
        assert retired_argument not in help_text


def test_live_semantic_error_is_an_auditable_failed_run(tmp_path, capsys):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"

    exit_code = cli.main(
        ["live", str(source), "--workspace-dir", str(workspace)]
    )

    assert exit_code == 2
    run_directories = list((workspace / "work" / "runs").iterdir())
    assert len(run_directories) == 1
    manifest = json.loads(
        (run_directories[0] / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["lifecycle"]["outcome"] == "failed"
    assert (
        manifest["errors"]["primary_error"]["error_code"]
        == "config.schema_invalid"
    )
    terminal = capsys.readouterr().err
    assert f"run_id: {run_directories[0].name}" in terminal
    assert f"诊断位置: {run_directories[0]}" in terminal


@pytest.mark.parametrize(
    "invalid_tail",
    [
        (),
        ("--quiet",),
        ("--workspace-dir",),
        ("extra.mp4",),
    ],
)
def test_live_structure_errors_exit_two_without_creating_a_run(
    invalid_tail,
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"argparse must not create a live run")
    arguments = ["live"]
    if invalid_tail:
        arguments.append(str(source))
        arguments.extend(invalid_tail)

    with pytest.raises(SystemExit) as captured:
        cli.main(arguments)

    assert captured.value.code == 2
    assert not source.with_suffix(".autocut").exists()
    assert not any(tmp_path.rglob("work/runs"))


def test_cache_clear_is_scoped_idempotent_and_does_not_create_a_run(
    tmp_path,
    capsys,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace").root
    cache_entry = workspace / "work" / "cache" / "entry.json"
    cache_entry.write_text("cached", encoding="utf-8")
    historical_run = workspace / "work" / "runs" / "historical"
    historical_run.mkdir()
    (historical_run / "events.jsonl").write_text("history", encoding="utf-8")
    delivery = workspace / "delivery" / "current.txt"
    delivery.write_text("current", encoding="utf-8")

    first_exit = cli.main(["cache", "clear", str(workspace)])
    second_exit = cli.main(["cache", "clear", str(workspace)])

    assert first_exit == second_exit == 0
    assert list((workspace / "work" / "cache").iterdir()) == []
    assert list((workspace / "work" / "runs").iterdir()) == [historical_run]
    assert delivery.read_text(encoding="utf-8") == "current"
    assert capsys.readouterr().out == "处理缓存已清空\n处理缓存已清空\n"


def test_cache_clear_rejects_an_unmanaged_workspace_without_mutation(
    tmp_path,
    capsys,
):
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    sentinel = unmanaged / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    exit_code = cli.main(["cache", "clear", str(unmanaged)])

    assert exit_code == 10
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert "受管 workspace 不可写。" in capsys.readouterr().err


def test_quiet_terminal_renderer_keeps_terminal_run_and_diagnostic_facts(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration fails before media analysis")
    source.with_suffix(".config.json").write_text(
        '{"schema_version":"configuration.v999"}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    outcome = compose_live_application().execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )
    stdout = StringIO()
    stderr = StringIO()
    renderer = cli.TerminalRenderer(
        quiet=True,
        stdout=stdout,
        stderr=stderr,
    )
    diagnostics_directory = (
        workspace / "work" / "runs" / str(outcome.run_id)
    )

    renderer.disclose_providers((object(),))
    renderer.render_live_outcome(outcome, diagnostics_directory)

    assert stdout.getvalue() == ""
    terminal = stderr.getvalue()
    assert "终态: failed" in terminal
    assert f"run_id: {outcome.run_id}" in terminal
    assert f"诊断位置: {diagnostics_directory}" in terminal
