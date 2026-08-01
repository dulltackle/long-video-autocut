"""可由测试组合根装配的内存运行诊断 Adapter。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from video_auto_editor.runtime.identity import RunId

from ._model import DiagnosticPackageSnapshot
from ._session import RunDiagnostics


class _CollectingDiagnosticStore:
    __slots__ = ("_events", "_manifest", "run_id")

    def __init__(self, run_id: RunId) -> None:
        self.run_id = run_id
        self._events = bytearray()
        self._manifest: bytes | None = None

    def append(self, payload: bytes) -> None:
        self._events.extend(payload)

    def snapshot(self) -> DiagnosticPackageSnapshot:
        return DiagnosticPackageSnapshot(
            events=bytes(self._events),
            manifest=self._manifest,
        )

    def publish_manifest(self, payload: bytes) -> None:
        if self._manifest is not None:
            raise FileExistsError("运行诊断清单已经存在")
        self._manifest = bytes(payload)


def initialize(
    run_id: RunId,
    *,
    application_version: str,
    wall_clock: Callable[[], datetime],
    monotonic_clock: Callable[[], float],
) -> RunDiagnostics:
    """使用内存收集 Adapter 初始化同一诊断契约。"""
    if not isinstance(run_id, RunId):
        raise TypeError("内存诊断 Adapter 必须绑定 RunId")
    return RunDiagnostics._start(
        _CollectingDiagnosticStore(run_id),
        application_version=application_version,
        wall_clock=wall_clock,
        monotonic_clock=monotonic_clock,
    )


__all__ = ["initialize"]
