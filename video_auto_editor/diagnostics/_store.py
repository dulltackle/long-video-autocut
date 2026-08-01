"""运行诊断接收端的内部 seam。"""

from __future__ import annotations

from typing import Protocol

from video_auto_editor.runtime.identity import RunId

from ._model import DiagnosticPackageSnapshot


class _DiagnosticStore(Protocol):
    run_id: RunId

    def append(self, payload: bytes) -> None:
        ...

    def snapshot(self) -> DiagnosticPackageSnapshot:
        ...

    def publish_manifest(self, payload: bytes) -> None:
        ...
