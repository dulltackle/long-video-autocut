import os
from dataclasses import FrozenInstanceError

import pytest

from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import (
    ManagedTreeEntryKind,
    Workspace,
    WorkspaceFailure,
)


def _staging_path(workspace, run_workspace):
    return workspace.root / "work" / "tmp" / str(run_workspace.run_id) / "delivery"


def test_managed_directory_inspection_returns_an_immutable_sorted_tree(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = run_workspace.delivery_staging
        staging.location("clips").mkdir()
        staging.location("clips/series").mkdir()
        staging.location("manifest.json").write_bytes(b"manifest")
        staging.location("clips/series/part.mp4").write_bytes(b"video")

        entries = staging.inspect_tree()

        assert tuple(
            (entry.relative_path, entry.kind, entry.byte_length) for entry in entries
        ) == (
            ("clips", ManagedTreeEntryKind.DIRECTORY, None),
            ("clips/series", ManagedTreeEntryKind.DIRECTORY, None),
            (
                "clips/series/part.mp4",
                ManagedTreeEntryKind.REGULAR_FILE,
                5,
            ),
            (
                "manifest.json",
                ManagedTreeEntryKind.REGULAR_FILE,
                8,
            ),
        )
        assert all(
            len(entry.revision) == 64 and set(entry.revision) <= set("0123456789abcdef")
            for entry in entries
        )
        assert str(workspace.root) not in repr(entries)
        with pytest.raises(FrozenInstanceError):
            entries[0].byte_length = 1


def test_managed_directory_inspection_rejects_a_symbolic_link_without_following(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = _staging_path(workspace, run_workspace)
        (staging / "linked").symlink_to(
            outside,
            target_is_directory=True,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.delivery_staging.inspect_tree()

    assert captured.value.error_code is (ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE)
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.access",
        "reason_code": "workspace.symlink_encountered",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert str(outside) not in str(captured.value)
    assert sentinel.read_bytes() == b"outside"


def test_managed_directory_inspection_preserves_the_symbolic_link_reason_for_an_unsafe_name(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = _staging_path(workspace, run_workspace)
        (staging / "linked\\outside").symlink_to(outside)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.delivery_staging.inspect_tree()

    assert captured.value.diagnostics["reason_code"] == (
        "workspace.symlink_encountered"
    )
    assert outside.read_bytes() == b"outside"


def test_managed_directory_inspection_rejects_a_fifo_without_opening_it(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = _staging_path(workspace, run_workspace)
        os.mkfifo(staging / "foreign.pipe")

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.delivery_staging.inspect_tree()

    assert captured.value.diagnostics["reason_code"] == ("workspace.ownership_changed")


def test_managed_directory_inspection_rejects_a_hard_link_without_reading_it(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"SECRET")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = _staging_path(workspace, run_workspace)
        os.link(outside, staging / "alias.bin")

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.delivery_staging.inspect_tree()

    assert captured.value.diagnostics["reason_code"] == ("workspace.ownership_changed")
    assert outside.read_bytes() == b"SECRET"


def test_managed_directory_inspection_fails_if_a_file_identity_changes_mid_scan(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_stat = os.stat

    with workspace.acquire_run(RunId.new()) as run_workspace:
        staging = _staging_path(workspace, run_workspace)
        target = staging / "manifest.json"
        displaced = staging / "displaced.json"
        target.write_bytes(b"original")
        replaced = False

        def replace_after_snapshot(path, *args, **kwargs):
            nonlocal replaced
            status = original_stat(path, *args, **kwargs)
            if (
                path == "manifest.json"
                and kwargs.get("follow_symlinks") is False
                and not replaced
            ):
                replaced = True
                target.rename(displaced)
                target.write_bytes(b"replacement")
            return status

        monkeypatch.setattr(os, "stat", replace_after_snapshot)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.delivery_staging.inspect_tree()

        assert target.read_bytes() == b"replacement"
        assert displaced.read_bytes() == b"original"

    assert replaced
    assert captured.value.diagnostics["reason_code"] == ("workspace.ownership_changed")


def test_managed_directory_inspection_cannot_outlive_its_workspace_lease(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        retained = run_workspace.delivery_staging

    with pytest.raises(RuntimeError, match="lease 已关闭"):
        retained.inspect_tree()
