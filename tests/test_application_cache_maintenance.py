import stat

import pytest

from video_auto_editor.application.cache_maintenance import (
    CacheMaintenanceApplication,
)
from video_auto_editor.cache import CacheNamespace, CacheRepository
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import Workspace, WorkspaceFailure


def test_cache_maintenance_clears_every_cache_artifact_and_preserves_other_data(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    config = tmp_path / "course.config.json"
    config.write_text('{"keep": true}', encoding="utf-8")
    context = tmp_path / "course.context.json"
    context.write_text('{"topic": "keep"}', encoding="utf-8")
    workspace = Workspace.open(source, tmp_path / "workspace").root

    cache = workspace / "work" / "cache"
    artifacts = {
        "transcript/aa/result.json": b"transcript",
        "transcription_shard/bb/result.json": b"shard",
        "topic_review/cc/result.json": b"review",
        "subtitle_optimization/dd/result.json": b"subtitle",
        ".quarantine/topic_review/ee/damaged.json": b"damaged",
        ".locks/transcript/ff/result.lock": b"",
        "transcript/aa/.workspace-create-interrupted": b"temporary",
        "legacy/.workspace-create-directory/partial.json": b"partial",
        ".workspace-quarantine-interrupted/entry": b"quarantined",
        "unknown-v0/cache.bin": b"legacy",
    }
    for relative_path, contents in artifacts.items():
        target = cache / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)

    external = tmp_path / "external"
    external.mkdir()
    external_sentinel = external / "sentinel.txt"
    external_sentinel.write_text("outside", encoding="utf-8")
    (cache / "external-link").symlink_to(
        external,
        target_is_directory=True,
    )

    preserved = {
        workspace / "work" / "runs" / "existing" / "events.jsonl": "diagnostic",
        workspace / "work" / "tmp" / "stale" / "sensitive.txt": "temporary",
        workspace / "delivery" / "manifest.json": "delivery",
        workspace / "delivery.previous" / "manifest.json": "previous",
    }
    for path, contents in preserved.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    cache_identity = (cache.stat().st_dev, cache.stat().st_ino)

    CacheMaintenanceApplication().clear(workspace)

    assert cache.is_dir()
    assert list(cache.iterdir()) == []
    assert (cache.stat().st_dev, cache.stat().st_ino) == cache_identity
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert source.read_bytes() == b"source"
    assert config.read_text(encoding="utf-8") == '{"keep": true}'
    assert context.read_text(encoding="utf-8") == '{"topic": "keep"}'
    assert {
        path: path.read_text(encoding="utf-8") for path in preserved
    } == preserved
    assert external_sentinel.read_text(encoding="utf-8") == "outside"


def test_cache_maintenance_is_idempotent_without_creating_run_facts(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace").root
    historical_run = workspace / "work" / "runs" / "historical"
    historical_run.mkdir()
    historical_events = historical_run / "events.jsonl"
    historical_events.write_text("history", encoding="utf-8")
    cache = workspace / "work" / "cache"
    cache_identity = (cache.stat().st_dev, cache.stat().st_ino)

    def reject_run_id_creation(cls):
        del cls
        raise AssertionError("缓存维护不得创建运行标识")

    monkeypatch.setattr(
        RunId,
        "new",
        classmethod(reject_run_id_creation),
    )
    application = CacheMaintenanceApplication()

    first_result = application.clear(workspace)
    second_result = application.clear(workspace)

    assert first_result is None
    assert second_result is None
    assert list(cache.iterdir()) == []
    assert (cache.stat().st_dev, cache.stat().st_ino) == cache_identity
    assert list((workspace / "work" / "runs").iterdir()) == [
        historical_run
    ]
    assert historical_events.read_text(encoding="utf-8") == "history"

    reopened = Workspace.open_existing(workspace)
    with reopened.acquire_maintenance() as maintenance:
        CacheRepository.initialize(
            maintenance.cache,
            application_version="4.7.0",
        )
    expected_cache_directories = {
        ".locks",
        ".quarantine",
        *(namespace.value for namespace in CacheNamespace),
    }
    assert expected_cache_directories <= {
        path.name for path in cache.iterdir()
    }

    application.clear(workspace)
    assert list(cache.iterdir()) == []


def test_cache_maintenance_rejects_an_active_live_run_without_mutating_cache(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    cache_entry = workspace.root / "work" / "cache" / "entry.json"
    cache_entry.write_bytes(b"keep")

    with workspace.acquire_run(RunId.new()):
        with pytest.raises(WorkspaceFailure) as captured:
            CacheMaintenanceApplication().clear(workspace.root)

        assert (
            captured.value.error_code
            is ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE
        )
        assert captured.value.diagnostics == {
            "component": "workspace",
            "operation": "workspace.lock",
            "reason_code": "filesystem.lock_failed",
        }
        assert cache_entry.read_bytes() == b"keep"

    CacheMaintenanceApplication().clear(workspace.root)
    assert list((workspace.root / "work" / "cache").iterdir()) == []


def test_cache_maintenance_rejects_unmanaged_or_damaged_workspaces_before_delete(
    tmp_path,
):
    application = CacheMaintenanceApplication()
    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    foreign = unmanaged / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")

    with pytest.raises(WorkspaceFailure):
        application.clear(unmanaged)

    assert foreign.read_text(encoding="utf-8") == "keep"
    assert {path.name for path in unmanaged.iterdir()} == {"foreign.txt"}

    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace").root
    cache_entry = workspace / "work" / "cache" / "keep.json"
    cache_entry.write_bytes(b"keep")
    marker = workspace / ".video-auto-editor-workspace.json"
    marker.write_text(
        '{"schema_version":"workspace.v999"}\n',
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceFailure):
        application.clear(workspace)

    assert cache_entry.read_bytes() == b"keep"
    assert list((workspace / "work" / "runs").iterdir()) == []
