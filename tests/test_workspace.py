import errno
import json
import multiprocessing
import os
import signal
import stat
import threading
from pathlib import Path

import pytest

from video_auto_editor.delivery import capability as delivery_capability
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import (
    ErrorCode,
    ErrorModule,
    RunError,
    RunStage,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    ManagedPathCapability,
    SourceFileCapability,
    Workspace,
    WorkspaceFailure,
)
from video_auto_editor.workspace import _capability as capability_module
from video_auto_editor.workspace import _workspace as workspace_module


def _hold_run_lock(source, workspace_root, connection):
    workspace = Workspace.open(source, workspace_root)
    try:
        with workspace.acquire_run(RunId.new()):
            connection.send("locked")
            connection.recv()
    finally:
        connection.close()


def test_open_resolves_source_link_and_derives_default_workspace_from_target(
    tmp_path,
):
    media_directory = tmp_path / "media"
    media_directory.mkdir()
    source = media_directory / "course.mp4"
    source.write_bytes(b"source")
    source_link = tmp_path / "linked-course.mp4"
    source_link.symlink_to(source)

    workspace = Workspace.open(source_link)

    assert workspace.source.path == source.resolve()
    assert workspace.root == media_directory / "course.autocut"
    assert workspace.root.is_dir()
    assert workspace.root.is_absolute()
    assert ".." not in workspace.root.parts
    assert isinstance(workspace.root, Path)


def test_open_accepts_an_explicit_workspace_link_only_after_resolving_its_target(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace_target = tmp_path / "workspace-target"
    workspace_target.mkdir()
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(workspace_target, target_is_directory=True)

    workspace = Workspace.open(source, workspace_link)

    assert workspace.root == workspace_target.resolve()
    assert workspace.root.is_dir()


def test_open_rejects_a_source_link_whose_final_object_is_not_a_regular_file(
    tmp_path,
):
    source_directory = tmp_path / "not-a-file"
    source_directory.mkdir()
    source_link = tmp_path / "course.mp4"
    source_link.symlink_to(source_directory, target_is_directory=True)

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source_link)

    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {
        "reason_code": "input.not_regular_file"
    }


def test_open_rejects_a_missing_source_without_creating_a_workspace(tmp_path):
    missing_source = tmp_path / "course.mp4"

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(missing_source)

    assert captured.value.error_code is ErrorCode.INPUT_MISSING
    assert captured.value.diagnostics == {"reason_code": "input.not_found"}
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not (tmp_path / "course.autocut").exists()


def test_open_rejects_an_explicit_workspace_whose_final_object_is_not_a_directory(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace_file = tmp_path / "workspace"
    workspace_file.write_text("foreign", encoding="utf-8")

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source, workspace_file)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.verify",
        "reason_code": "filesystem.not_directory",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert workspace_file.read_text(encoding="utf-8") == "foreign"


def test_open_rejects_a_nonempty_unmarked_workspace_without_mutating_it(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    foreign_workspace = tmp_path / "workspace"
    foreign_workspace.mkdir(mode=0o750)
    foreign_file = foreign_workspace / "keep.txt"
    foreign_file.write_text("keep", encoding="utf-8")
    before_mode = foreign_workspace.stat().st_mode
    before_inode = foreign_workspace.stat().st_ino

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source, foreign_workspace)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.verify",
        "reason_code": "filesystem.marker_invalid",
    }
    assert foreign_workspace.stat().st_mode == before_mode
    assert foreign_workspace.stat().st_ino == before_inode
    assert list(foreign_workspace.iterdir()) == [foreign_file]
    assert foreign_file.read_text(encoding="utf-8") == "keep"


def test_open_translates_workspace_permission_failure_without_exposing_path(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    root.mkdir()
    root.chmod(0)
    try:
        with pytest.raises(WorkspaceFailure) as captured:
            Workspace.open(source, root)
    finally:
        root.chmod(0o700)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.verify",
        "reason_code": "filesystem.permission_denied",
    }
    assert str(root) not in str(captured.value)


def test_new_workspace_has_a_versioned_fixed_layout_and_secure_default_modes(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    previous_umask = os.umask(0)
    try:
        workspace = Workspace.open(source, root)
    finally:
        os.umask(previous_umask)

    marker = root / ".video-auto-editor-workspace.json"
    lock_file = root / "work" / ".workspace.lock"
    managed_directories = [
        root,
        root / "delivery",
        root / "delivery.previous",
        root / "work",
        root / "work" / "cache",
        root / "work" / "runs",
        root / "work" / "tmp",
    ]

    assert workspace.root == root.resolve()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema_version": "workspace.v1"
    }
    assert stat.S_ISREG(marker.lstat().st_mode)
    assert stat.S_ISREG(lock_file.lstat().st_mode)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    assert all(path.is_dir() for path in managed_directories)
    assert {
        stat.S_IMODE(path.stat().st_mode) for path in managed_directories
    } == {0o700}


@pytest.mark.parametrize(
    ("relative_path", "insecure_mode"),
    [
        (".", 0o755),
        ("work/cache", 0o755),
        (".video-auto-editor-workspace.json", 0o644),
        ("work/.workspace.lock", 0o644),
    ],
)
def test_existing_workspace_rejects_insecure_managed_permissions_without_repair(
    tmp_path,
    relative_path,
    insecure_mode,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    target = (
        workspace.root
        if relative_path == "."
        else workspace.root / relative_path
    )
    target.chmod(insecure_mode)

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open_existing(workspace.root)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.verify",
        "reason_code": "filesystem.permission_denied",
    }
    assert stat.S_IMODE(target.stat().st_mode) == insecure_mode


def test_open_reuses_a_valid_workspace_without_binding_its_marker_to_source(
    tmp_path,
):
    first_source = tmp_path / "first.mp4"
    first_source.write_bytes(b"first")
    second_source = tmp_path / "second.mp4"
    second_source.write_bytes(b"second")
    root = tmp_path / "workspace"
    Workspace.open(first_source, root)
    existing_delivery = root / "delivery" / "keep.txt"
    existing_delivery.write_text("keep", encoding="utf-8")
    marker = root / ".video-auto-editor-workspace.json"
    marker_before = marker.read_bytes()
    marker_inode = marker.stat().st_ino

    reopened = Workspace.open(second_source, root)

    assert reopened.source.path == second_source.resolve()
    assert reopened.root == root.resolve()
    assert marker.read_bytes() == marker_before
    assert marker.stat().st_ino == marker_inode
    assert existing_delivery.read_text(encoding="utf-8") == "keep"


def test_valid_workspace_rejects_a_foreign_root_entry_without_mutating_it(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    foreign = workspace.root / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open_existing(workspace.root)

    assert captured.value.diagnostics["reason_code"] == (
        "filesystem.marker_invalid"
    )
    assert foreign.read_text(encoding="utf-8") == "keep"


def test_open_reports_a_source_link_loop_as_an_unreadable_input(tmp_path):
    source_link = tmp_path / "course.mp4"
    source_link.symlink_to(source_link.name)

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source_link)

    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {"reason_code": "input.read_failed"}
    assert not (tmp_path / "course.autocut").exists()


def test_open_translates_source_permission_failure_without_exposing_path(
    tmp_path,
):
    media_directory = tmp_path / "media"
    media_directory.mkdir()
    source = media_directory / "course.mp4"
    source.write_bytes(b"source")
    media_directory.chmod(0)
    try:
        with pytest.raises(WorkspaceFailure) as captured:
            Workspace.open(source)
    finally:
        media_directory.chmod(0o700)

    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {
        "reason_code": "input.permission_denied"
    }
    assert str(source) not in str(captured.value)


def test_open_rejects_a_broken_explicit_workspace_link_without_creating_target(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    missing_target = tmp_path / "missing-workspace-target"
    workspace_link = tmp_path / "workspace"
    workspace_link.symlink_to(missing_target, target_is_directory=True)

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source, workspace_link)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.verify",
        "reason_code": "filesystem.not_directory",
    }
    assert workspace_link.is_symlink()
    assert not missing_target.exists()


def test_run_lease_issues_role_scoped_capabilities_that_reject_path_escape(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        cache = run_workspace.cache
        cache_entry = cache.location("entry.json")

        assert cache.role is ManagedDirectoryRole.CACHE
        assert isinstance(cache_entry, ManagedPathCapability)
        assert not isinstance(cache_entry, os.PathLike)
        cache_entry.write_bytes(b"cache")
        assert cache_entry.read_bytes() == b"cache"
        assert (
            workspace.root / "work" / "cache" / "entry.json"
        ).read_bytes() == b"cache"
        for unsafe_path in (
            "",
            ".",
            "..",
            "../outside",
            "/tmp/outside",
            "nested//file",
            "nested\\file",
            "nul\x00file",
            "line\nbreak",
        ):
            with pytest.raises(ValueError, match="受管相对路径"):
                cache.location(unsafe_path)

    with pytest.raises(TypeError, match="只能由 Workspace 签发"):
        ManagedDirectoryCapability()
    with pytest.raises(TypeError, match="只能由 Workspace 签发"):
        SourceFileCapability(Path("/tmp/forged.mp4"))


def test_managed_path_atomically_publishes_complete_private_bytes_without_replacing(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    payload = b"complete-diagnostic-manifest"
    original_write = os.write

    with workspace.acquire_run(RunId.new()) as run_workspace:
        target = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
            / "run.json"
        )

        def write_at_most_three_bytes(descriptor, contents):
            return original_write(descriptor, contents[:3])

        monkeypatch.setattr(os, "write", write_at_most_three_bytes)

        written = run_workspace.diagnostics.location(
            "run.json"
        ).publish_bytes_atomically(payload)
        published_inode = target.stat().st_ino

        with pytest.raises(FileExistsError, match="已经存在"):
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"replacement")

        assert written == len(payload)
        assert target.read_bytes() == payload
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.stat().st_ino == published_inode
        assert not list(target.parent.glob(".workspace-create-*"))


def test_atomic_publish_cleans_its_temporary_file_when_data_sync_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_fsync = os.fsync

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )
        synced_directories = []

        def fail_data_sync(_descriptor):
            raise OSError(errno.ENOSPC, "injected")

        def observe_cleanup_sync(descriptor):
            synced_directories.append(
                Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            )
            return original_fsync(descriptor)

        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            fail_data_sync,
        )
        monkeypatch.setattr(os, "fsync", observe_cleanup_sync)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"manifest")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.io_failed"
        )
        assert not (diagnostic_directory / "run.json").exists()
        assert not list(
            diagnostic_directory.glob(".workspace-create-*")
        )
        assert synced_directories == [diagnostic_directory]


def test_atomic_publish_cleans_a_created_temporary_after_initial_fstat_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_fstat = os.fstat
    injected = False

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )

        def fail_first_temporary_fstat(descriptor):
            nonlocal injected
            try:
                name = Path(
                    os.readlink(f"/proc/self/fd/{descriptor}")
                ).name
            except OSError:
                name = ""
            if not injected and name.startswith(".workspace-create-"):
                injected = True
                raise OSError(errno.EIO, "injected")
            return original_fstat(descriptor)

        monkeypatch.setattr(os, "fstat", fail_first_temporary_fstat)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"manifest")

        assert injected
        assert captured.value.diagnostics["reason_code"] == (
            "workspace.io_failed"
        )
        assert not (diagnostic_directory / "run.json").exists()
        assert not list(
            diagnostic_directory.glob(".workspace-create-*")
        )


def test_atomic_publish_does_not_roll_back_after_parent_sync_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        target = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
            / "run.json"
        )

        def fail_parent_sync(_descriptor):
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(os, "fsync", fail_parent_sync)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"committed")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.io_failed"
        )
        assert target.read_bytes() == b"committed"
        assert not list(target.parent.glob(".workspace-create-*"))


def test_atomic_publish_syncs_private_data_before_same_directory_no_replace(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_open = os.open
    original_data_sync = workspace_module._sync_file_data
    original_rename_no_replace = workspace_module._rename_no_replace
    original_fsync = os.fsync

    with workspace.acquire_run(RunId.new()) as run_workspace:
        target = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
            / "run.json"
        )
        temporary_open = {}
        durability_boundaries = []

        def observe_open(path, flags, *args, **kwargs):
            if (
                isinstance(path, str)
                and path.startswith(".workspace-create-")
            ):
                temporary_open.update(
                    {
                        "name": path,
                        "flags": flags,
                        "mode": args[0],
                        "parent": Path(
                            os.readlink(
                                f"/proc/self/fd/{kwargs['dir_fd']}"
                            )
                        ),
                    }
                )
            return original_open(path, flags, *args, **kwargs)

        def observe_data_sync(descriptor):
            durability_boundaries.append(
                (
                    "file_data",
                    Path(os.readlink(f"/proc/self/fd/{descriptor}")),
                )
            )
            return original_data_sync(descriptor)

        def observe_rename(
            source_parent,
            source_name,
            target_parent,
            target_name,
        ):
            durability_boundaries.append(
                (
                    "no_replace",
                    Path(
                        os.readlink(f"/proc/self/fd/{source_parent}")
                    ),
                )
            )
            assert source_parent == target_parent
            assert source_name == temporary_open["name"]
            assert target_name == "run.json"
            return original_rename_no_replace(
                source_parent,
                source_name,
                target_parent,
                target_name,
            )

        def observe_parent_sync(descriptor):
            durability_boundaries.append(
                (
                    "parent",
                    Path(os.readlink(f"/proc/self/fd/{descriptor}")),
                )
            )
            return original_fsync(descriptor)

        monkeypatch.setattr(os, "open", observe_open)
        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            observe_data_sync,
        )
        monkeypatch.setattr(
            workspace_module,
            "_rename_no_replace",
            observe_rename,
        )
        monkeypatch.setattr(os, "fsync", observe_parent_sync)

        run_workspace.diagnostics.location(
            "run.json"
        ).publish_bytes_atomically(b"manifest")

        required_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
        )
        assert temporary_open["flags"] & required_flags == required_flags
        assert temporary_open["mode"] == 0o600
        assert temporary_open["parent"] == target.parent
        assert durability_boundaries == [
            ("file_data", target.parent / temporary_open["name"]),
            ("no_replace", target.parent),
            ("parent", target.parent),
        ]


def test_atomic_publish_never_replaces_a_target_inserted_at_commit_boundary(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_rename_no_replace = workspace_module._rename_no_replace

    with workspace.acquire_run(RunId.new()) as run_workspace:
        target = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
            / "run.json"
        )

        def occupy_target_before_commit(
            source_parent,
            source_name,
            target_parent,
            target_name,
        ):
            target.write_bytes(b"concurrent-owner")
            return original_rename_no_replace(
                source_parent,
                source_name,
                target_parent,
                target_name,
            )

        monkeypatch.setattr(
            workspace_module,
            "_rename_no_replace",
            occupy_target_before_commit,
        )

        with pytest.raises(FileExistsError, match="已经存在"):
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"must-not-replace")

        assert target.read_bytes() == b"concurrent-owner"
        assert not list(target.parent.glob(".workspace-create-*"))


def test_atomic_publish_rejects_a_parent_inode_replaced_after_data_sync(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_data_sync = workspace_module._sync_file_data

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )
        displaced = diagnostic_directory.with_name("displaced-diagnostics")

        def replace_parent_after_sync(descriptor):
            original_data_sync(descriptor)
            diagnostic_directory.rename(displaced)
            diagnostic_directory.mkdir(mode=0o700)

        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            replace_parent_after_sync,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"must-stay-bound")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert not (diagnostic_directory / "run.json").exists()
        assert not list(displaced.glob(".workspace-create-*"))


def test_atomic_publish_rejects_parent_replacement_at_commit_boundary_without_removing_published_inode(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_rename_no_replace = workspace_module._rename_no_replace

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )
        displaced = diagnostic_directory.with_name("displaced-diagnostics")
        swapped = False

        def replace_parent_at_commit_boundary(
            source_parent,
            source_name,
            target_parent,
            target_name,
        ):
            nonlocal swapped
            if target_name == "run.json" and not swapped:
                swapped = True
                diagnostic_directory.rename(displaced)
                diagnostic_directory.mkdir(mode=0o700)
            return original_rename_no_replace(
                source_parent,
                source_name,
                target_parent,
                target_name,
            )

        monkeypatch.setattr(
            workspace_module,
            "_rename_no_replace",
            replace_parent_at_commit_boundary,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"must-stay-bound")

        assert swapped
        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert not (diagnostic_directory / "run.json").exists()
        assert (displaced / "run.json").read_bytes() == b"must-stay-bound"
        assert not list(displaced.glob(".workspace-create-*"))


def test_atomic_publish_rejects_a_symlink_target_without_touching_its_destination(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )
        target = diagnostic_directory / "run.json"
        target.symlink_to(outside)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"must-not-follow")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.symlink_encountered"
        )
        assert target.is_symlink()
        assert outside.read_bytes() == b"outside"
        assert not list(
            diagnostic_directory.glob(".workspace-create-*")
        )


def test_atomic_publish_never_unlinks_an_inode_swapped_into_its_temporary_name(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_data_sync = workspace_module._sync_file_data

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )
        swapped_name = None
        displaced = diagnostic_directory / "displaced-temporary"

        def swap_temporary_before_failure(descriptor):
            nonlocal swapped_name
            original_data_sync(descriptor)
            temporary = Path(
                os.readlink(f"/proc/self/fd/{descriptor}")
            )
            swapped_name = temporary
            temporary.rename(displaced)
            temporary.write_bytes(b"foreign")
            raise OSError(errno.ENOSPC, "injected")

        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            swap_temporary_before_failure,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"owned-temporary")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert swapped_name is not None
        assert swapped_name.read_bytes() == b"foreign"
        assert displaced.read_bytes() == b"owned-temporary"
        assert not (diagnostic_directory / "run.json").exists()


def test_atomic_publish_fails_closed_when_its_temporary_name_disappears(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_data_sync = workspace_module._sync_file_data

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_directory = (
            workspace.root
            / "work"
            / "runs"
            / str(run_workspace.run_id)
        )

        def remove_temporary_before_failure(descriptor):
            original_data_sync(descriptor)
            Path(os.readlink(f"/proc/self/fd/{descriptor}")).unlink()
            raise OSError(errno.ENOSPC, "injected")

        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            remove_temporary_before_failure,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.diagnostics.location(
                "run.json"
            ).publish_bytes_atomically(b"owned-temporary")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert not (diagnostic_directory / "run.json").exists()
        assert not list(
            diagnostic_directory.glob(".workspace-create-*")
        )


def test_run_lease_creates_run_scoped_directories_and_issues_only_fixed_roles(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        capabilities = {
            run_workspace.cache: (
                ManagedDirectoryRole.CACHE,
                workspace.root / "work" / "cache",
            ),
            run_workspace.diagnostics: (
                ManagedDirectoryRole.RUN_DIAGNOSTICS,
                workspace.root / "work" / "runs" / str(run_id),
            ),
            run_workspace.temporary: (
                ManagedDirectoryRole.RUN_TEMPORARY,
                workspace.root / "work" / "tmp" / str(run_id) / "scratch",
            ),
            run_workspace.delivery_staging: (
                ManagedDirectoryRole.DELIVERY_STAGING,
                workspace.root
                / "work"
                / "tmp"
                / str(run_id)
                / "delivery",
            ),
            run_workspace.published_delivery: (
                ManagedDirectoryRole.PUBLISHED_DELIVERY,
                workspace.root / "delivery",
            ),
            run_workspace.previous_delivery: (
                ManagedDirectoryRole.PREVIOUS_DELIVERY,
                workspace.root / "delivery.previous",
            ),
        }

        for capability, (role, expected_path) in capabilities.items():
            assert capability.role is role
            capability._assert_current_directory()
            assert expected_path.is_dir()
            assert stat.S_IMODE(expected_path.stat().st_mode) == 0o700


def test_temporary_capability_cannot_write_into_delivery_staging(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with (
        workspace.acquire_run(RunId.new()) as run_workspace,
        pytest.raises(FileNotFoundError),
    ):
        run_workspace.temporary.location("delivery/forged.bin").write_bytes(
            b"forged"
        )


def test_capability_rejects_a_symlink_component_without_following_it(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    cache_link = workspace.root / "work" / "cache" / "linked"
    cache_link.symlink_to(external, target_is_directory=True)

    with (
        workspace.acquire_run(RunId.new()) as run_workspace,
        pytest.raises(WorkspaceFailure) as captured,
    ):
        run_workspace.cache.location("linked/sentinel.txt").read_bytes()

    assert captured.value.diagnostics["reason_code"] == (
        "workspace.symlink_encountered"
    )
    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_delivery_uses_the_single_workspace_owned_directory_capability():
    assert (
        delivery_capability.ManagedDirectoryCapability
        is ManagedDirectoryCapability
    )
    assert delivery_capability.ManagedDirectoryRole is ManagedDirectoryRole


def test_run_and_maintenance_leases_are_mutually_exclusive_and_release_lock(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    try:
        with pytest.raises(WorkspaceFailure) as captured:
            workspace.acquire_maintenance()

        assert (
            captured.value.error_code
            is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
        )
        assert captured.value.diagnostics == {
            "component": "workspace",
            "operation": "workspace.lock",
            "reason_code": "filesystem.lock_failed",
        }
    finally:
        run_workspace.close()

    with workspace.acquire_maintenance() as maintenance:
        assert maintenance.cache.role is ManagedDirectoryRole.CACHE

    with workspace.acquire_run(RunId.new()) as reopened_run:
        retained_location = reopened_run.cache.location("entry.json")

    with pytest.raises(RuntimeError, match="lease 已关闭"):
        retained_location.read_bytes()


def test_cache_maintenance_never_reports_success_if_cache_changes_during_clear(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    (cache / "entry.json").write_bytes(b"cached")
    original_sync = workspace_module._sync_cleanup_directory
    inserted = False

    def insert_after_empty_cache_sync(descriptor):
        nonlocal inserted
        original_sync(descriptor)
        descriptor_path = Path(
            os.readlink(f"/proc/self/fd/{descriptor}")
        )
        if (
            not inserted
            and descriptor_path == cache
            and list(cache.iterdir()) == []
        ):
            (cache / "late.json").write_bytes(b"late")
            inserted = True

    monkeypatch.setattr(
        workspace_module,
        "_sync_cleanup_directory",
        insert_after_empty_cache_sync,
    )

    with (
        workspace.acquire_maintenance() as maintenance,
        pytest.raises(WorkspaceFailure) as captured,
    ):
        maintenance.clear_cache()

    assert inserted is True
    assert captured.value.diagnostics == {
        "operation": "workspace.cleanup",
        "reason_code": "workspace.ownership_changed",
    }
    assert (cache / "late.json").read_bytes() == b"late"


def test_cache_maintenance_excludes_cache_effects_until_clear_finishes(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    (cache / "entry.json").write_bytes(b"cached")
    maintenance = workspace.acquire_maintenance()
    original_is_empty = workspace_module._directory_is_empty
    empty_checked = threading.Event()
    release_empty_check = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    failures = []

    def pause_after_final_empty_check(descriptor):
        result = original_is_empty(descriptor)
        descriptor_path = Path(
            os.readlink(f"/proc/self/fd/{descriptor}")
        )
        if (
            result
            and descriptor_path == cache
            and not empty_checked.is_set()
        ):
            empty_checked.set()
            if not release_empty_check.wait(5):
                raise AssertionError("缓存空目录检查没有获准继续")
        return result

    def clear_cache():
        try:
            maintenance.clear_cache()
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            failures.append(exc)

    def write_cache():
        writer_started.set()
        try:
            maintenance.cache.location("late.json").write_bytes(b"late")
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            failures.append(exc)
        finally:
            writer_finished.set()

    monkeypatch.setattr(
        workspace_module,
        "_directory_is_empty",
        pause_after_final_empty_check,
    )
    clear_thread = threading.Thread(target=clear_cache)
    writer_thread = threading.Thread(target=write_cache)
    try:
        clear_thread.start()
        assert empty_checked.wait(5)
        writer_thread.start()
        assert writer_started.wait(5)
        writer_thread.join(0.5)
        writer_was_excluded = writer_thread.is_alive()
    finally:
        release_empty_check.set()
        clear_thread.join(5)
        writer_thread.join(5)
        maintenance.close()

    assert writer_was_excluded is True
    assert not clear_thread.is_alive()
    assert not writer_thread.is_alive()
    assert failures == []
    assert writer_finished.is_set()
    assert (cache / "late.json").read_bytes() == b"late"


def test_cache_maintenance_allows_active_effects_to_finish_cross_thread_work(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    maintenance = workspace.acquire_maintenance()
    outer_started = threading.Event()
    nested_finished = threading.Event()
    nested_completed_during_outer = []
    failures = []

    def outer_effect(stream):
        outer_started.set()
        nested_completed_during_outer.append(
            nested_finished.wait(5)
        )
        stream.write(b"outer")

    def use_outer_effect():
        try:
            maintenance.cache.location("outer.bin").use_binary(
                "wb",
                outer_effect,
            )
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            failures.append(exc)

    def write_nested_effect():
        try:
            maintenance.cache.location("nested.bin").write_bytes(
                b"nested"
            )
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            failures.append(exc)
        finally:
            nested_finished.set()

    def clear_cache():
        try:
            maintenance.clear_cache()
        except BaseException as exc:  # noqa: BLE001 - 跨线程回传
            failures.append(exc)

    outer_thread = threading.Thread(target=use_outer_effect)
    clear_thread = threading.Thread(target=clear_cache)
    nested_thread = threading.Thread(target=write_nested_effect)
    try:
        outer_thread.start()
        assert outer_started.wait(5)
        clear_thread.start()
        guard = maintenance._lease_guard
        with guard._condition:
            assert guard._condition.wait_for(
                lambda: guard._exclusive_waiters == 1,
                timeout=1,
            )
        nested_thread.start()
        outer_thread.join(6)
        nested_thread.join(6)
        clear_thread.join(6)
    finally:
        maintenance.close()

    assert nested_completed_during_outer == [True]
    assert not outer_thread.is_alive()
    assert not nested_thread.is_alive()
    assert not clear_thread.is_alive()
    assert failures == []
    assert list(cache.iterdir()) == []


def test_exclusive_maintenance_rejects_reentrant_cache_effects(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"

    with workspace.acquire_maintenance() as maintenance:
        with pytest.raises(RuntimeError, match="独占维护"):
            maintenance._lease_guard.execute_exclusive(
                lambda _descriptor: maintenance.cache.location(
                    "nested.bin"
                ).write_bytes(b"nested")
            )

        maintenance.cache.location("after.bin").write_bytes(b"after")

    assert not (cache / "nested.bin").exists()
    assert (cache / "after.bin").read_bytes() == b"after"


def test_cache_maintenance_revalidates_marker_before_deleting_cache(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache_entry = workspace.root / "work" / "cache" / "keep.json"
    cache_entry.write_bytes(b"keep")

    with workspace.acquire_maintenance() as maintenance:
        (
            workspace.root / ".video-auto-editor-workspace.json"
        ).write_text(
            '{"schema_version":"workspace.v999"}\n',
            encoding="utf-8",
        )

        with pytest.raises(WorkspaceFailure) as captured:
            maintenance.clear_cache()

    assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
    assert captured.value.diagnostics == {
        "operation": "workspace.cleanup",
        "reason_code": "workspace.ownership_changed",
    }
    assert cache_entry.read_bytes() == b"keep"


def test_cache_maintenance_rejects_a_replaced_cache_root_without_following_it(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    cache_entry = cache / "keep.json"
    cache_entry.write_bytes(b"keep")
    displaced = tmp_path / "displaced-cache"
    external = tmp_path / "external"
    external.mkdir()
    external_sentinel = external / "sentinel.txt"
    external_sentinel.write_text("outside", encoding="utf-8")

    with workspace.acquire_maintenance() as maintenance:
        cache.rename(displaced)
        cache.symlink_to(external, target_is_directory=True)

        with pytest.raises(WorkspaceFailure) as captured:
            maintenance.clear_cache()

    assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
    assert captured.value.diagnostics == {
        "operation": "workspace.cleanup",
        "reason_code": "workspace.ownership_changed",
    }
    assert (displaced / "keep.json").read_bytes() == b"keep"
    assert external_sentinel.read_text(encoding="utf-8") == "outside"
    assert cache.is_symlink()


def test_cache_maintenance_classifies_replacement_after_initial_validation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    (cache / "keep.json").write_bytes(b"keep")
    displaced = tmp_path / "displaced-cache"
    external = tmp_path / "external"
    external.mkdir()
    external_sentinel = external / "sentinel.txt"
    external_sentinel.write_text("outside", encoding="utf-8")
    original_validate = Workspace._validate_lease_root
    armed = False
    swapped = False

    def swap_after_validation(candidate, root_descriptor):
        nonlocal swapped
        result = original_validate(candidate, root_descriptor)
        if armed and candidate is workspace and not swapped:
            cache.rename(displaced)
            cache.symlink_to(external, target_is_directory=True)
            swapped = True
        return result

    with workspace.acquire_maintenance() as maintenance:
        monkeypatch.setattr(
            Workspace,
            "_validate_lease_root",
            swap_after_validation,
        )
        armed = True

        with pytest.raises(WorkspaceFailure) as captured:
            maintenance.clear_cache()

    assert swapped is True
    assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
    assert captured.value.diagnostics == {
        "operation": "workspace.cleanup",
        "reason_code": "workspace.ownership_changed",
    }
    assert (displaced / "keep.json").read_bytes() == b"keep"
    assert external_sentinel.read_text(encoding="utf-8") == "outside"


def test_cache_maintenance_releases_lock_after_sync_failure_and_can_retry(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache = workspace.root / "work" / "cache"
    (cache / "entry.json").write_bytes(b"cached")
    original_fsync = os.fsync
    injected = False

    def fail_first_cache_sync(descriptor):
        nonlocal injected
        descriptor_path = Path(
            os.readlink(f"/proc/self/fd/{descriptor}")
        )
        if not injected and descriptor_path == cache:
            injected = True
            raise OSError(errno.EIO, "injected")
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_first_cache_sync)

    with (
        workspace.acquire_maintenance() as maintenance,
        pytest.raises(WorkspaceFailure) as captured,
    ):
        maintenance.clear_cache()

    assert injected is True
    assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
    assert captured.value.diagnostics == {
        "operation": "workspace.cleanup",
        "reason_code": "workspace.directory_sync_failed",
    }

    with workspace.acquire_maintenance() as maintenance:
        maintenance.clear_cache()

    assert list(cache.iterdir()) == []


def test_existing_workspace_object_rejects_replaced_lock_file(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lock_file = workspace.root / "work" / ".workspace.lock"
    lock_file.rename(tmp_path / "displaced-lock")
    lock_file.touch(mode=0o600)

    with pytest.raises(WorkspaceFailure) as captured:
        workspace.acquire_run(RunId.new())

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.lock",
        "reason_code": "filesystem.lock_failed",
    }
    assert not any((workspace.root / "work" / "runs").iterdir())


def test_open_existing_supports_maintenance_without_source_or_creation(
    tmp_path,
):
    missing_workspace = tmp_path / "missing-workspace"
    with pytest.raises(WorkspaceFailure):
        Workspace.open_existing(missing_workspace)
    assert not missing_workspace.exists()

    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    created = Workspace.open(source, tmp_path / "workspace")

    reopened = Workspace.open_existing(created.root)

    assert reopened.source is None
    assert reopened.root == created.root
    with reopened.acquire_maintenance():
        pass
    with pytest.raises(RuntimeError, match="素材"):
        reopened.acquire_run(RunId.new())


def test_run_lock_excludes_maintenance_in_another_process(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        target=_hold_run_lock,
        args=(source, workspace.root, child_connection),
    )
    process.start()
    child_connection.close()
    try:
        assert parent_connection.poll(10)
        assert parent_connection.recv() == "locked"

        with pytest.raises(WorkspaceFailure) as captured:
            workspace.acquire_maintenance()

        assert captured.value.diagnostics["reason_code"] == (
            "filesystem.lock_failed"
        )
        parent_connection.send("release")
        process.join(10)
        assert process.exitcode == 0
    finally:
        parent_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(10)


def test_run_acquisition_rolls_back_directories_after_partial_create_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    diagnostics = workspace.root / "work" / "runs" / str(run_id)
    temporary = workspace.root / "work" / "tmp" / str(run_id)
    original_mkdir = os.mkdir
    run_directory_creates = 0

    def fail_temporary_create(path, *args, **kwargs):
        nonlocal run_directory_creates
        if isinstance(path, str) and path.startswith(
            workspace_module._CREATE_PREFIX
        ):
            run_directory_creates += 1
            if run_directory_creates == 2:
                raise PermissionError("injected")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", fail_temporary_create)

    with pytest.raises(WorkspaceFailure) as captured:
        workspace.acquire_run(run_id)

    assert (
        captured.value.error_code
        is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
    )
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.create",
        "reason_code": "filesystem.permission_denied",
    }
    assert not diagnostics.exists()
    assert not temporary.exists()

    with workspace.acquire_maintenance():
        pass


def test_run_acquisition_journals_a_directory_before_identity_check_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    diagnostics = workspace.root / "work" / "runs" / str(run_id)
    temporary = workspace.root / "work" / "tmp" / str(run_id)
    original_mkdir = os.mkdir
    original_stat = os.stat
    created_name = None

    def observe_create(path, *args, **kwargs):
        nonlocal created_name
        result = original_mkdir(path, *args, **kwargs)
        if (
            isinstance(path, str)
            and path.startswith(workspace_module._CREATE_PREFIX)
            and created_name is None
        ):
            created_name = path
        return result

    def fail_identity_check(path, *args, **kwargs):
        nonlocal created_name
        if created_name is not None and path == created_name:
            created_name = None
            raise OSError("injected")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", observe_create)
    monkeypatch.setattr(os, "stat", fail_identity_check)

    with pytest.raises(WorkspaceFailure):
        workspace.acquire_run(run_id)

    assert not diagnostics.exists()
    assert not temporary.exists()
    with workspace.acquire_maintenance():
        pass


def test_run_creation_never_rolls_back_a_directory_swapped_in_before_open(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    displaced = tmp_path / "displaced-created-directory"
    external = tmp_path / "external-directory"
    external.mkdir(mode=0o700)
    original_open = os.open
    external_descriptor = original_open(
        external,
        os.O_RDONLY | os.O_DIRECTORY,
    )
    swapped = False
    staging = None

    def swap_before_created_directory_open(path, flags, *args, **kwargs):
        nonlocal staging, swapped
        if (
            not swapped
            and isinstance(path, str)
            and path.startswith(workspace_module._CREATE_PREFIX)
            and flags & os.O_DIRECTORY
            and kwargs.get("dir_fd") is not None
        ):
            parent = Path(
                os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")
            )
            staging = parent / path
            staging.rename(displaced)
            external.rename(staging)
            swapped = True
            raise OSError(errno.EIO, "injected")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_created_directory_open)
    try:
        with pytest.raises(WorkspaceFailure):
            workspace.acquire_run(run_id)

        assert os.fstat(external_descriptor).st_nlink > 0
        assert staging is not None and staging.is_dir()
        assert displaced.is_dir()
    finally:
        os.close(external_descriptor)


def test_run_creation_rejects_a_directory_swap_even_if_open_succeeds(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    displaced = tmp_path / "displaced-created-directory"
    external = tmp_path / "external-directory"
    external.mkdir(mode=0o700)
    original_open = os.open
    external_descriptor = original_open(
        external,
        os.O_RDONLY | os.O_DIRECTORY,
    )
    swapped = False
    staging = None

    def swap_before_created_directory_open(path, flags, *args, **kwargs):
        nonlocal staging, swapped
        if (
            not swapped
            and isinstance(path, str)
            and path.startswith(workspace_module._CREATE_PREFIX)
            and flags & os.O_DIRECTORY
            and kwargs.get("dir_fd") is not None
        ):
            parent = Path(
                os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")
            )
            staging = parent / path
            staging.rename(displaced)
            external.rename(staging)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_created_directory_open)
    try:
        with pytest.raises(WorkspaceFailure) as captured:
            workspace.acquire_run(run_id)

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert os.fstat(external_descriptor).st_nlink > 0
        assert staging is not None and staging.is_dir()
        assert displaced.is_dir()
    finally:
        os.close(external_descriptor)


def test_run_start_cleans_only_stale_temporary_content_without_following_links(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    stale = workspace.root / "work" / "tmp" / "stale-run"
    stale.mkdir()
    (stale / "sensitive.txt").write_text("temporary", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    (stale / "external-link").symlink_to(
        external,
        target_is_directory=True,
    )
    cache_entry = workspace.root / "work" / "cache" / "keep.json"
    cache_entry.write_text("cache", encoding="utf-8")
    diagnostic = workspace.root / "work" / "runs" / "keep.json"
    diagnostic.write_text("diagnostic", encoding="utf-8")
    delivery = workspace.root / "delivery" / "keep.txt"
    delivery.write_text("delivery", encoding="utf-8")
    run_id = RunId.new()

    with workspace.acquire_run(run_id):
        assert {path.name for path in (workspace.root / "work" / "tmp").iterdir()} == {
            str(run_id)
        }

    assert sentinel.read_text(encoding="utf-8") == "outside"
    assert cache_entry.read_text(encoding="utf-8") == "cache"
    assert diagnostic.read_text(encoding="utf-8") == "diagnostic"
    assert delivery.read_text(encoding="utf-8") == "delivery"


def test_run_cleanup_classifies_directory_sync_failures(tmp_path, monkeypatch):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        run_workspace.temporary.location("sensitive.txt").write_bytes(
            b"sensitive"
        )
        monkeypatch.setattr(
            os,
            "fsync",
            lambda _descriptor: (_ for _ in ()).throw(
                OSError(errno.EIO, "injected")
            ),
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

    assert captured.value.diagnostics["reason_code"] == (
        "workspace.directory_sync_failed"
    )


def test_run_cleanup_revalidates_marker_before_touching_sensitive_content(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    marker = workspace.root / ".video-auto-editor-workspace.json"

    with workspace.acquire_run(RunId.new()) as run_workspace:
        sensitive_location = run_workspace.temporary.location("sensitive.txt")
        sensitive_location.write_bytes(b"temporary")
        sensitive = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_workspace.run_id)
            / "scratch"
            / "sensitive.txt"
        )
        marker.write_text(
            '{"schema_version":"workspace.v999"}\n',
            encoding="utf-8",
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

        assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
        assert captured.value.diagnostics == {
            "operation": "workspace.cleanup",
            "reason_code": "workspace.ownership_changed",
        }
        assert sensitive.read_text(encoding="utf-8") == "temporary"


def test_publication_transition_revalidates_workspace_and_target_capabilities(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        unverified = delivery_capability.UnverifiedDelivery._from_build(
            run_id,
            run_workspace.delivery_staging,
        )
        verified = delivery_capability.VerifiedDelivery._from_verification(
            unverified,
            verification_snapshot="snapshot-001",
        )
        (
            workspace.root / ".video-auto-editor-workspace.json"
        ).write_text(
            '{"schema_version":"workspace.v999"}\n',
            encoding="utf-8",
        )

        with pytest.raises(WorkspaceFailure) as captured:
            delivery_capability.PublishedDelivery._from_publication(
                verified,
                published_directory=run_workspace.published_delivery,
            )

        assert (
            captured.value.error_code
            is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
        )
        assert captured.value.diagnostics["reason_code"] == (
            "filesystem.marker_invalid"
        )


def test_run_cleanup_rejects_a_replaced_target_without_following_it(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        temporary_root = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_workspace.run_id)
        )
        displaced = temporary_root.with_name("displaced-run")
        temporary_root.rename(displaced)
        temporary_root.symlink_to(external, target_is_directory=True)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

        assert captured.value.error_code is ErrorCode.WORKSPACE_CLEANUP_FAILED
        assert captured.value.diagnostics["reason_code"] == (
            "workspace.symlink_encountered"
        )

    assert sentinel.read_text(encoding="utf-8") == "outside"


def test_capability_does_not_expose_a_forgeable_issuer_or_raw_path(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    assert not hasattr(ManagedDirectoryCapability, "_issue")
    assert not hasattr(ManagedPathCapability, "_issue")
    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("entry.json")

        assert not hasattr(run_workspace.cache, "_base_parts")
        assert not hasattr(location, "path")
        with pytest.raises(TypeError):
            os.fspath(location)


def test_workspace_private_issuer_cannot_be_claimed_or_rebuilt_by_business_code():
    assert not hasattr(capability_module, "_issue_managed_directory")
    with pytest.raises(TypeError, match="只能由 Workspace"):
        capability_module._ManagedOperations()
    with pytest.raises(RuntimeError, match="已被领取"):
        capability_module._claim_workspace_issuer()


def test_atomic_publish_rejects_a_tampered_workspace_effect_binding(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.diagnostics.location("run.json")
        operations = location._operations
        authentic_publish = operations._publish_file
        object.__setattr__(
            operations,
            "_publish_file",
            lambda _parts, contents: len(contents),
        )
        try:
            with pytest.raises(TypeError, match="由 Workspace 签发"):
                location.publish_bytes_atomically(b"forged")
        finally:
            object.__setattr__(
                operations,
                "_publish_file",
                authentic_publish,
            )

        with pytest.raises(FileNotFoundError):
            location.read_bytes()


def test_cache_lock_and_quarantine_effects_reject_non_cache_roles(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        diagnostic_source = run_workspace.diagnostics.location("source.bin")
        diagnostic_source.write_bytes(b"diagnostic", exclusive=True)

        with pytest.raises(TypeError, match="只允许处理缓存"):
            diagnostic_source.with_exclusive_cache_lock(
                CancellationSource().token,
                lambda: None,
            )
        with pytest.raises(TypeError, match="只允许处理缓存"):
            diagnostic_source.quarantine_to(
                run_workspace.diagnostics.location("quarantined.bin")
            )

        assert diagnostic_source.read_bytes() == b"diagnostic"


def test_cache_lock_rejects_a_tampered_workspace_effect_binding(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("claim.lock")
        location.publish_bytes_atomically(b"")
        operations = location._operations
        authentic_lock = operations._use_exclusive_lock
        object.__setattr__(
            operations,
            "_use_exclusive_lock",
            lambda _parts, _cancellation, effect: effect(),
        )
        try:
            with pytest.raises(TypeError, match="由 Workspace 签发"):
                location.with_exclusive_cache_lock(
                    CancellationSource().token,
                    lambda: None,
                )
        finally:
            object.__setattr__(
                operations,
                "_use_exclusive_lock",
                authentic_lock,
            )


def test_cache_lock_maps_interrupted_wait_with_requested_signal_to_cancellation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cancellation = CancellationSource()
    original_flock = workspace_module.fcntl.flock
    interrupted = False

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("claim.lock")
        location.publish_bytes_atomically(b"")

        def interrupt_once(descriptor, operation):
            nonlocal interrupted
            if (
                not interrupted
                and operation
                == (
                    workspace_module.fcntl.LOCK_EX
                    | workspace_module.fcntl.LOCK_NB
                )
            ):
                interrupted = True
                cancellation.request(signal.SIGTERM)
                raise InterruptedError(errno.EINTR, "injected")
            return original_flock(descriptor, operation)

        monkeypatch.setattr(
            workspace_module.fcntl,
            "flock",
            interrupt_once,
        )

        with pytest.raises(CancellationRequested):
            location.with_exclusive_cache_lock(
                cancellation.token,
                lambda: pytest.fail("取消后不得执行持锁效果"),
            )

    assert interrupted


def test_cache_lock_retries_an_interrupted_unlock(tmp_path, monkeypatch):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_flock = workspace_module.fcntl.flock
    unlock_attempts = 0
    cache_lock_descriptor = None

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("claim.lock")
        location.publish_bytes_atomically(b"")

        def interrupt_first_unlock(descriptor, operation):
            nonlocal cache_lock_descriptor, unlock_attempts
            if operation == workspace_module.fcntl.LOCK_UN:
                if cache_lock_descriptor is None:
                    cache_lock_descriptor = descriptor
                if descriptor == cache_lock_descriptor:
                    unlock_attempts += 1
                    if unlock_attempts == 1:
                        raise InterruptedError(errno.EINTR, "injected")
            return original_flock(descriptor, operation)

        monkeypatch.setattr(
            workspace_module.fcntl,
            "flock",
            interrupt_first_unlock,
        )

        result = location.with_exclusive_cache_lock(
            CancellationSource().token,
            lambda: "completed",
        )

    assert result == "completed"
    assert unlock_attempts == 2


def test_private_capability_effects_remain_bound_and_reject_raw_escape_parts(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    escaped = workspace.root.parent / "escaped.bin"

    with workspace.acquire_run(RunId.new()) as run_workspace:
        with pytest.raises(ValueError, match="路径段"):
            run_workspace.cache._operations.use_file(
                ("..", "escaped.bin"),
                "wb",
                lambda _stream: None,
            )
        with pytest.raises(ValueError, match="路径段"):
            run_workspace.cache._operations.publish_file(
                ("..", "escaped.bin"),
                b"escape",
            )

    assert not escaped.exists()


def test_capability_effect_rejects_a_link_inserted_after_location_is_issued(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external"
    external.mkdir()

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("late/result.bin")
        (workspace.root / "work" / "cache" / "late").symlink_to(
            external,
            target_is_directory=True,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            location.write_bytes(b"must-stay-managed")

    assert captured.value.diagnostics["reason_code"] == (
        "workspace.symlink_encountered"
    )
    assert not (external / "result.bin").exists()


def test_capability_never_adopts_a_file_inserted_between_stat_and_create(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external.bin"
    external.write_bytes(b"SECRET")
    external_handle = external.open("rb")
    managed = workspace.root / "work" / "cache" / "entry.bin"
    original_open = os.open
    swapped = False

    def insert_before_create(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "entry.bin" and not swapped:
            external.rename(managed)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", insert_before_create)
    try:
        with (
            workspace.acquire_run(RunId.new()) as run_workspace,
            pytest.raises(WorkspaceFailure) as captured,
        ):
            run_workspace.cache.location("entry.bin").write_bytes(b"PWN")

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        external_handle.seek(0)
        assert external_handle.read() == b"SECRET"
    finally:
        external_handle.close()


def test_capability_translates_infrastructure_io_failure_without_calling_it_tamper(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_open = os.open

    with workspace.acquire_run(RunId.new()) as run_workspace:

        def fail_managed_file_open(path, flags, *args, **kwargs):
            if path == "entry.bin":
                raise OSError(errno.ENOSPC, "injected")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", fail_managed_file_open)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cache.location("entry.bin").write_bytes(b"value")

    assert captured.value.diagnostics["reason_code"] == "workspace.io_failed"


def test_capability_rejects_hard_links_without_reading_or_truncating_external_data(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external.bin"
    external.write_bytes(b"SECRET")
    alias = workspace.root / "work" / "cache" / "alias.bin"
    os.link(external, alias)

    with workspace.acquire_run(RunId.new()) as run_workspace:
        with pytest.raises(WorkspaceFailure) as read_failure:
            run_workspace.cache.location("alias.bin").read_bytes()
        with pytest.raises(WorkspaceFailure) as write_failure:
            run_workspace.cache.location("alias.bin").write_bytes(b"PWN")

    assert read_failure.value.diagnostics["reason_code"] == (
        "workspace.ownership_changed"
    )
    assert write_failure.value.diagnostics["reason_code"] == (
        "workspace.ownership_changed"
    )
    assert external.read_bytes() == b"SECRET"


def test_capability_revalidates_link_count_after_a_scoped_write_effect(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    outside = tmp_path / "outside.bin"

    with workspace.acquire_run(RunId.new()) as run_workspace:
        managed = workspace.root / "work" / "cache" / "entry.bin"

        def link_during_write(stream):
            os.link(managed, outside)
            stream.write(b"sensitive")

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cache.location("entry.bin").use_binary(
                "wb",
                link_during_write,
            )

    assert captured.value.diagnostics["reason_code"] == (
        "workspace.ownership_changed"
    )


def test_capability_does_not_return_a_stream_that_can_outlive_its_lease(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        location = run_workspace.cache.location("held.bin")

        assert not hasattr(location, "open_binary")


def test_scoped_binary_effect_does_not_expose_a_duplicable_file_descriptor(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    retained_streams = []

    with workspace.acquire_run(RunId.new()) as run_workspace:

        def write_without_descriptor(stream):
            retained_streams.append(stream)
            assert not hasattr(stream, "fileno")
            with pytest.raises(TypeError):
                os.dup(stream)
            stream.write(b"inside")

        run_workspace.cache.location("entry.bin").use_binary(
            "wb",
            write_without_descriptor,
        )

    assert retained_streams[0].closed
    with pytest.raises(ValueError, match="作用域"):
        retained_streams[0].write(b"outside")


def test_lease_close_waits_for_scoped_file_effect_and_closes_its_stream(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lease = workspace.acquire_run(RunId.new())
    location = lease.cache.location("held.bin")
    effect_started = threading.Event()
    allow_effect_to_finish = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    retained_streams = []
    thread_failures = []

    def use_file():
        try:

            def effect(stream):
                retained_streams.append(stream)
                effect_started.set()
                if not allow_effect_to_finish.wait(5):
                    raise TimeoutError("test effect timed out")
                stream.write(b"inside-lease")

            location.use_binary("wb", effect)
        except (OSError, RuntimeError) as exc:
            thread_failures.append(exc)

    def close_lease():
        close_started.set()
        lease.close()
        close_finished.set()

    effect_thread = threading.Thread(target=use_file)
    close_thread = threading.Thread(target=close_lease)
    effect_thread.start()
    assert effect_started.wait(5)
    close_thread.start()
    assert close_started.wait(5)
    assert not close_finished.wait(0.05)

    with pytest.raises(WorkspaceFailure):
        workspace.acquire_maintenance()

    allow_effect_to_finish.set()
    effect_thread.join(5)
    close_thread.join(5)

    assert not effect_thread.is_alive()
    assert not close_thread.is_alive()
    assert not thread_failures
    assert close_finished.is_set()
    assert retained_streams[0].closed
    with pytest.raises(ValueError):
        retained_streams[0].write(b"after-close")
    with workspace.acquire_maintenance():
        pass
    assert (
        workspace.root / "work" / "cache" / "held.bin"
    ).read_bytes() == b"inside-lease"


def test_lease_cannot_close_itself_from_inside_a_scoped_file_effect(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lease = workspace.acquire_run(RunId.new())
    try:
        with pytest.raises(RuntimeError, match="效果内关闭"):
            lease.cache.location("entry.bin").use_binary(
                "wb",
                lambda _stream: lease.close(),
            )
    finally:
        lease.close()

    with workspace.acquire_maintenance():
        pass


def test_effect_close_failure_does_not_leave_the_lease_permanently_active(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lease = workspace.acquire_run(RunId.new())
    original_dup = os.dup
    original_close = os.close
    effect_descriptors = []
    injected = False

    def capture_first_effect_descriptor(descriptor):
        duplicated = original_dup(descriptor)
        if not effect_descriptors:
            effect_descriptors.append(duplicated)
        return duplicated

    def fail_effect_descriptor_close(descriptor):
        nonlocal injected
        if (
            effect_descriptors
            and descriptor == effect_descriptors[0]
            and not injected
        ):
            injected = True
            original_close(descriptor)
            raise OSError(errno.EIO, "injected")
        return original_close(descriptor)

    monkeypatch.setattr(os, "dup", capture_first_effect_descriptor)
    monkeypatch.setattr(os, "close", fail_effect_descriptor_close)
    with pytest.raises(OSError, match="injected"):
        lease.cache.location("entry.bin").write_bytes(b"value")
    monkeypatch.setattr(os, "close", original_close)

    close_finished = threading.Event()
    close_thread = threading.Thread(
        target=lambda: (lease.close(), close_finished.set())
    )
    close_thread.start()
    try:
        assert close_finished.wait(2)
    finally:
        if not close_finished.is_set():
            lease._lease_guard._finish_effect()
        close_thread.join(2)

    with workspace.acquire_maintenance():
        pass


def test_interrupted_close_can_retry_and_release_the_workspace_lock(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lease = workspace.acquire_run(RunId.new())
    effect_started = threading.Event()
    finish_effect = threading.Event()

    def hold_effect():
        lease.cache.location("held.bin").use_binary(
            "wb",
            lambda stream: (
                effect_started.set(),
                finish_effect.wait(5),
                stream.write(b"done"),
            ),
        )

    effect_thread = threading.Thread(target=hold_effect)
    effect_thread.start()
    assert effect_started.wait(2)
    original_wait = lease._lease_guard._condition.wait
    monkeypatch.setattr(
        lease._lease_guard._condition,
        "wait",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        lease.close()

    monkeypatch.setattr(
        lease._lease_guard._condition,
        "wait",
        original_wait,
    )
    finish_effect.set()
    effect_thread.join(2)
    assert not effect_thread.is_alive()

    lease.close()
    with workspace.acquire_maintenance():
        pass


def test_maintenance_releases_its_lock_if_capability_construction_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            Workspace,
            "_capability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected")
            ),
        )
        with pytest.raises(RuntimeError, match="injected"):
            workspace.acquire_maintenance()

    with workspace.acquire_run(RunId.new()):
        pass


def test_run_rolls_back_directories_if_capability_construction_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    diagnostics = workspace.root / "work" / "runs" / str(run_id)
    temporary = workspace.root / "work" / "tmp" / str(run_id)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            Workspace,
            "_capability",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("injected")
            ),
        )
        with pytest.raises(RuntimeError, match="injected"):
            workspace.acquire_run(run_id)

    assert not diagnostics.exists()
    assert not temporary.exists()
    with workspace.acquire_maintenance():
        pass


def test_run_rolls_back_and_releases_lock_if_descriptor_close_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId.new()
    diagnostics = workspace.root / "work" / "runs" / str(run_id)
    temporary = workspace.root / "work" / "tmp" / str(run_id)
    original_capability = Workspace._capability
    original_close = os.close
    capability_count = 0
    close_armed = False
    injected = False

    def observe_capability(self, *args, **kwargs):
        nonlocal capability_count, close_armed
        capability = original_capability(self, *args, **kwargs)
        capability_count += 1
        if capability_count == 6:
            close_armed = True
        return capability

    def fail_one_run_descriptor_close(descriptor):
        nonlocal injected
        if close_armed and not injected:
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                target = ""
            if str(run_id) in target:
                injected = True
                original_close(descriptor)
                raise OSError(errno.EIO, "injected")
        return original_close(descriptor)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(Workspace, "_capability", observe_capability)
        scoped_patch.setattr(os, "close", fail_one_run_descriptor_close)
        with pytest.raises(WorkspaceFailure) as captured:
            workspace.acquire_run(run_id)

    assert injected
    assert captured.value.diagnostics == {
        "component": "workspace",
        "operation": "workspace.create",
        "reason_code": "filesystem.create_failed",
    }
    assert not diagnostics.exists()
    assert not temporary.exists()
    with workspace.acquire_maintenance():
        pass


def test_lock_release_attempts_both_descriptors_after_first_unlock_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    lease = workspace.acquire_maintenance()
    original_flock = workspace_module.fcntl.flock
    injected = False

    def fail_first_unlock(descriptor, operation):
        nonlocal injected
        if operation == workspace_module.fcntl.LOCK_UN and not injected:
            injected = True
            raise OSError(errno.EIO, "injected")
        return original_flock(descriptor, operation)

    monkeypatch.setattr(workspace_module.fcntl, "flock", fail_first_unlock)
    try:
        with pytest.raises(OSError, match="injected"):
            lease.close()

        monkeypatch.setattr(workspace_module.fcntl, "flock", original_flock)
        with workspace.acquire_run(RunId.new()):
            pass
    finally:
        for descriptor in (
            lease._lock_handle.lock_descriptor,
            lease._lock_handle.root_descriptor,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_capability_creates_sensitive_files_and_directories_with_secure_modes(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    previous_umask = os.umask(0)
    try:
        with workspace.acquire_run(RunId.new()) as run_workspace:
            run_workspace.cache.location("namespace").mkdir()
            run_workspace.cache.location("namespace/value.bin").write_bytes(
                b"value"
            )
    finally:
        os.umask(previous_umask)

    namespace = workspace.root / "work" / "cache" / "namespace"
    value = namespace / "value.bin"
    assert stat.S_IMODE(namespace.stat().st_mode) == 0o700
    assert stat.S_IMODE(value.stat().st_mode) == 0o600


def test_run_cleanup_rejects_a_plain_directory_replacement_by_inode(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    sentinel = replacement / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        temporary_root = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_workspace.run_id)
        )
        displaced = temporary_root.with_name("displaced-run")
        temporary_root.rename(displaced)
        replacement.rename(temporary_root)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )

    assert (
        temporary_root / "sentinel.txt"
    ).read_text(encoding="utf-8") == "outside"


def test_recursive_cleanup_rejects_a_directory_swapped_between_stat_and_open(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    sentinel = replacement / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        temporary_root = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_workspace.run_id)
        )
        nested = temporary_root / "nested"
        nested.mkdir(mode=0o700)
        (nested / "owned.txt").write_text("owned", encoding="utf-8")
        displaced = temporary_root / "displaced-nested"
        original_open = os.open
        swapped = False

        def swap_before_directory_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if (
                not swapped
                and path == "nested"
                and flags & os.O_DIRECTORY
                and kwargs.get("dir_fd") is not None
            ):
                nested.rename(displaced)
                replacement.rename(nested)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", swap_before_directory_open)

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )

    assert (nested / "sentinel.txt").read_text(encoding="utf-8") == "outside"


def test_recursive_cleanup_quarantines_an_inode_swapped_at_delete_boundary(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    external = tmp_path / "external.txt"
    external.write_text("outside", encoding="utf-8")
    external_handle = external.open("rb")
    synced_directories = []
    try:
        with workspace.acquire_run(RunId.new()) as run_workspace:
            temporary_root = (
                workspace.root
                / "work"
                / "tmp"
                / str(run_workspace.run_id)
            )
            owned = temporary_root / "owned.txt"
            owned.write_text("owned", encoding="utf-8")
            displaced = temporary_root / "displaced-owned.txt"
            original_rename_no_replace = workspace_module._rename_no_replace
            original_sync = workspace_module._sync_cleanup_directory
            swapped = False

            def swap_before_quarantine(
                source_parent,
                source_name,
                target_parent,
                target_name,
            ):
                nonlocal swapped
                if (
                    not swapped
                    and source_name == "owned.txt"
                    and target_name == "entry"
                ):
                    owned.rename(displaced)
                    external.rename(owned)
                    swapped = True
                return original_rename_no_replace(
                    source_parent,
                    source_name,
                    target_parent,
                    target_name,
                )

            def observe_sync(descriptor):
                synced_directories.append(
                    Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                )
                return original_sync(descriptor)

            monkeypatch.setattr(
                workspace_module,
                "_rename_no_replace",
                swap_before_quarantine,
            )
            monkeypatch.setattr(
                workspace_module,
                "_sync_cleanup_directory",
                observe_sync,
            )

            with pytest.raises(WorkspaceFailure) as captured:
                run_workspace.cleanup()

            assert captured.value.diagnostics["reason_code"] == (
                "workspace.ownership_changed"
            )
            assert owned.read_text(encoding="utf-8") == "outside"
            assert temporary_root in synced_directories
            assert any(
                directory.parent == temporary_root
                and directory.name.startswith(".workspace-quarantine-")
                for directory in synced_directories
            )

        external_handle.seek(0)
        assert external_handle.read() == b"outside"
    finally:
        external_handle.close()


def test_recursive_cleanup_never_overwrites_an_unknown_quarantine_entry(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    original_rename_no_replace = workspace_module._rename_no_replace
    injected = False

    with workspace.acquire_run(RunId.new()) as run_workspace:
        temporary_root = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_workspace.run_id)
        )
        owned = temporary_root / "owned.txt"
        owned.write_text("owned", encoding="utf-8")

        def occupy_quarantine_entry(
            source_parent,
            source_name,
            target_parent,
            target_name,
        ):
            nonlocal injected
            if (
                not injected
                and source_name == "owned.txt"
                and target_name == "entry"
            ):
                quarantine = Path(
                    os.readlink(f"/proc/self/fd/{target_parent}")
                )
                (quarantine / "entry").write_text(
                    "foreign",
                    encoding="utf-8",
                )
                injected = True
            return original_rename_no_replace(
                source_parent,
                source_name,
                target_parent,
                target_name,
            )

        monkeypatch.setattr(
            workspace_module,
            "_rename_no_replace",
            occupy_quarantine_entry,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cleanup()

        assert injected
        assert captured.value.diagnostics["reason_code"] == (
            "workspace.ownership_changed"
        )
        assert owned.read_text(encoding="utf-8") == "owned"
        quarantine_entries = list(
            temporary_root.glob(".workspace-quarantine-*/entry")
        )
        assert len(quarantine_entries) == 1
        assert quarantine_entries[0].read_text(encoding="utf-8") == "foreign"


def test_mkdir_translates_parent_mount_lookup_failure(tmp_path, monkeypatch):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        run_workspace.cache.location("parent").mkdir()
        parent = workspace.root / "work" / "cache" / "parent"
        original_mount_id = workspace_module._descriptor_mount_id

        def fail_parent_mount_lookup(descriptor):
            try:
                target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                return original_mount_id(descriptor)
            if target == parent:
                raise OSError(errno.EIO, "injected")
            return original_mount_id(descriptor)

        monkeypatch.setattr(
            workspace_module,
            "_descriptor_mount_id",
            fail_parent_mount_lookup,
        )

        with pytest.raises(WorkspaceFailure) as captured:
            run_workspace.cache.location("parent/child").mkdir()

        assert captured.value.diagnostics == {
            "component": "workspace",
            "operation": "workspace.access",
            "reason_code": "workspace.io_failed",
        }
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None


def test_workspace_rejects_a_replaced_fixed_layout_directory_by_inode(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    temporary_root = workspace.root / "work" / "tmp"
    displaced = temporary_root.with_name("displaced-tmp")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    temporary_root.rename(displaced)
    replacement.rename(temporary_root)

    with pytest.raises(WorkspaceFailure) as captured:
        workspace.acquire_run(RunId.new())

    assert captured.value.diagnostics["reason_code"] == (
        "filesystem.marker_invalid"
    )
    assert not any((workspace.root / "work" / "runs").iterdir())


def test_workspace_rejects_a_fixed_directory_on_another_mount_identity(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache_root = workspace.root / "work" / "cache"
    original_mount_id = workspace_module._descriptor_mount_id

    def substitute_cache_mount(descriptor):
        mount_id = original_mount_id(descriptor)
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return mount_id
        return mount_id + 1 if target == cache_root else mount_id

    monkeypatch.setattr(
        workspace_module,
        "_descriptor_mount_id",
        substitute_cache_mount,
    )

    with pytest.raises(WorkspaceFailure) as captured:
        workspace.acquire_run(RunId.new())

    assert captured.value.diagnostics["reason_code"] == (
        "filesystem.marker_invalid"
    )
    assert not any((workspace.root / "work" / "runs").iterdir())


def test_root_inode_lock_survives_lock_file_replacement(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()):
        lock_file = workspace.root / "work" / ".workspace.lock"
        lock_file.rename(tmp_path / "displaced-lock")
        lock_file.touch(mode=0o600)
        reopened = Workspace.open_existing(workspace.root)

        with pytest.raises(WorkspaceFailure) as captured:
            reopened.acquire_maintenance()

        assert captured.value.diagnostics["reason_code"] == (
            "filesystem.lock_failed"
        )


def test_new_workspace_initialization_rolls_back_after_partial_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    original_rename_no_replace = workspace_module._rename_no_replace

    def fail_cache_create(
        source_parent,
        source_name,
        target_parent,
        target_name,
    ):
        if target_name == "cache":
            raise PermissionError("injected")
        return original_rename_no_replace(
            source_parent,
            source_name,
            target_parent,
            target_name,
        )

    monkeypatch.setattr(
        workspace_module,
        "_rename_no_replace",
        fail_cache_create,
    )

    with pytest.raises(WorkspaceFailure) as captured:
        Workspace.open(source, root)

    assert captured.value.diagnostics["operation"] == "workspace.create"
    assert not root.exists()


def test_empty_workspace_is_not_claimed_if_foreign_content_appears_after_check(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    foreign = root / "foreign.txt"
    original_is_empty = workspace_module._directory_is_empty

    def inject_after_empty_check(descriptor):
        result = original_is_empty(descriptor)
        if result and not foreign.exists():
            foreign.write_text("keep", encoding="utf-8")
        return result

    monkeypatch.setattr(
        workspace_module,
        "_directory_is_empty",
        inject_after_empty_check,
    )

    with pytest.raises(WorkspaceFailure):
        Workspace.open(source, root)

    assert foreign.read_text(encoding="utf-8") == "keep"
    assert {path.name for path in root.iterdir()} == {"foreign.txt"}
    assert stat.S_IMODE(root.stat().st_mode) == 0o750


def test_new_workspace_rolls_back_if_identity_capture_fails_after_root_create(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    original_mkdir = os.mkdir
    original_stat = os.stat
    root_staging_name = None

    def observe_root_create(path, *args, **kwargs):
        nonlocal root_staging_name
        result = original_mkdir(path, *args, **kwargs)
        if (
            isinstance(path, str)
            and path.startswith(workspace_module._CREATE_PREFIX)
            and kwargs.get("dir_fd") is not None
            and root_staging_name is None
        ):
            root_staging_name = path
        return result

    def fail_first_root_identity(path, *args, **kwargs):
        nonlocal root_staging_name
        if (
            root_staging_name is not None
            and path == root_staging_name
            and kwargs.get("dir_fd") is not None
        ):
            root_staging_name = None
            raise OSError("injected")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", observe_root_create)
    monkeypatch.setattr(os, "stat", fail_first_root_identity)

    with pytest.raises(WorkspaceFailure):
        Workspace.open(source, root)

    assert not root.exists()


def test_workspace_creation_never_rolls_back_a_root_swapped_in_before_open(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    displaced = tmp_path / "displaced-created-root"
    external = tmp_path / "external-root"
    external.mkdir(mode=0o700)
    original_open = os.open
    external_descriptor = original_open(
        external,
        os.O_RDONLY | os.O_DIRECTORY,
    )
    swapped = False
    staging = None

    def swap_before_created_root_open(path, flags, *args, **kwargs):
        nonlocal staging, swapped
        if (
            not swapped
            and isinstance(path, str)
            and path.startswith(workspace_module._CREATE_PREFIX)
            and flags & os.O_DIRECTORY
            and kwargs.get("dir_fd") is not None
        ):
            parent = Path(
                os.readlink(f"/proc/self/fd/{kwargs['dir_fd']}")
            )
            if parent != root.parent:
                return original_open(path, flags, *args, **kwargs)
            staging = parent / path
            staging.rename(displaced)
            external.rename(staging)
            swapped = True
            raise OSError(errno.EIO, "injected")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_created_root_open)
    try:
        with pytest.raises(WorkspaceFailure):
            Workspace.open(source, root)

        assert os.fstat(external_descriptor).st_nlink > 0
        assert staging is not None and staging.is_dir()
        assert displaced.is_dir()
    finally:
        os.close(external_descriptor)


def test_new_workspace_rolls_back_if_final_layout_inspection_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    root = tmp_path / "workspace"
    original_inspect = workspace_module._inspect_layout_descriptor
    injected = False

    def fail_first_inspection(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("injected")
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(
        workspace_module,
        "_inspect_layout_descriptor",
        fail_first_inspection,
    )

    with pytest.raises(WorkspaceFailure):
        Workspace.open(source, root)

    assert not root.exists()


def test_capability_rejects_a_component_over_255_utf8_bytes_before_effect(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with (
        workspace.acquire_run(RunId.new()) as run_workspace,
        pytest.raises(ValueError, match="相对路径"),
    ):
        run_workspace.cache.location("你" * 100)


def test_run_workspace_public_bindings_are_read_only(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        with pytest.raises(AttributeError):
            run_workspace.run_id = RunId.new()
        with pytest.raises(AttributeError):
            run_workspace.cache = run_workspace.published_delivery


@pytest.mark.parametrize(
    "reason_code",
    [
        "workspace.ownership_changed",
        "workspace.symlink_encountered",
        "workspace.permission_denied",
        "workspace.io_failed",
    ],
)
def test_workspace_access_failures_fit_the_runtime_error_contract(reason_code):
    failure = workspace_module._workspace_access_failure(reason_code)

    run_error = RunError.create(
        code=failure.error_code,
        stage=RunStage.DELIVERY_BUILD,
        module=ErrorModule.WORKSPACE,
        event_sequence=1,
        diagnostics=failure.diagnostics,
    )

    assert run_error.diagnostics == failure.diagnostics
