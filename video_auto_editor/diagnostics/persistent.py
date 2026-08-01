"""可由生产组合根装配的持久化运行诊断 Adapter。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    WorkspaceFailure,
)

from ._failure import _startup_failure
from ._model import DiagnosticPackageSnapshot
from ._session import RunDiagnostics


class _PersistentDiagnosticStore:
    __slots__ = ("_directory", "_has_events", "run_id")

    def __init__(
        self,
        directory: ManagedDirectoryCapability,
        run_id: RunId,
    ) -> None:
        self._directory = directory
        self.run_id = run_id
        self._has_events = False

    def append(self, payload: bytes) -> None:
        location = self._directory.location("events.jsonl")
        if not self._has_events:
            location.publish_bytes_atomically(payload)
            self._has_events = True
            return

        def append_all(stream):
            offset = 0
            while offset < len(payload):
                written = stream.write(payload[offset:])
                if written <= 0:
                    raise OSError("诊断事件短写")
                offset += written

        location.use_binary("ab", append_all)

    def snapshot(self) -> DiagnosticPackageSnapshot:
        events = self._directory.location("events.jsonl").read_bytes()
        try:
            manifest = self._directory.location("run.json").read_bytes()
        except FileNotFoundError:
            manifest = None
        return DiagnosticPackageSnapshot(events=events, manifest=manifest)

    def publish_manifest(self, payload: bytes) -> None:
        self._directory.location("run.json").publish_bytes_atomically(
            payload
        )


def initialize(
    directory: ManagedDirectoryCapability,
    *,
    application_version: str,
    wall_clock: Callable[[], datetime],
    monotonic_clock: Callable[[], float],
) -> RunDiagnostics:
    """初始化绑定既有运行标识的持久化诊断包。"""
    if not isinstance(directory, ManagedDirectoryCapability):
        raise TypeError("运行诊断必须使用 Workspace 签发的诊断目录")
    directory_unavailable = False
    try:
        directory._assert_current_directory()
    except (OSError, WorkspaceFailure):
        directory_unavailable = True
    if directory_unavailable:
        raise _startup_failure(
            operation="diagnostics.initialize",
            reason_code="diagnostics.open_failed",
        )
    if directory.role is not ManagedDirectoryRole.RUN_DIAGNOSTICS:
        raise ValueError("运行诊断目录角色不合法")
    run_id = directory.bound_run_id
    if not isinstance(run_id, RunId):
        raise ValueError("运行诊断目录必须绑定一次直播拆条运行")
    return RunDiagnostics._start(
        _PersistentDiagnosticStore(directory, run_id),
        application_version=application_version,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )


__all__ = ["initialize"]
