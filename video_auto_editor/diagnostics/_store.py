"""运行诊断持久化与测试收集的内部 seam。"""

from __future__ import annotations

from typing import Protocol

from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import ManagedDirectoryCapability

from ._model import DiagnosticPackageSnapshot


class _DiagnosticStore(Protocol):
    run_id: RunId

    def append(self, payload: bytes) -> None:
        ...

    def snapshot(self) -> DiagnosticPackageSnapshot:
        ...

    def publish_manifest(self, payload: bytes) -> None:
        ...


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


class _MemoryDiagnosticStore:
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
