"""基于受管 Workspace capability 的文件系统缓存 Adapter。"""

import errno
import secrets
from collections.abc import Callable
from time import monotonic
from typing import TypeVar

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    WorkspaceFailure,
)

from ._failure import CacheFailure
from ._model import CacheIdentity, CacheNamespace

_ResultT = TypeVar("_ResultT")


class _ClaimEffectFailed(Exception):
    __slots__ = ("failure",)

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        super().__init__("处理缓存 claim 回调失败")


class _FileSystemStore:
    def __init__(self, directory: ManagedDirectoryCapability) -> None:
        if not isinstance(directory, ManagedDirectoryCapability):
            raise TypeError("文件系统处理缓存必须使用受管目录 capability")
        directory._assert_authentic()
        if directory.role is not ManagedDirectoryRole.CACHE:
            raise ValueError("文件系统处理缓存必须绑定缓存目录 capability")
        self._directory = directory
        try:
            for relative_path in (
                *(namespace.value for namespace in CacheNamespace),
                ".locks",
                ".quarantine",
            ):
                self._ensure_directory(relative_path)
        except Exception as exc:
            failure = _storage_failure("cache.publish", exc)
        else:
            return
        raise failure from None

    def read(self, identity: CacheIdentity) -> bytes | None:
        try:
            self._ensure_directory(
                f"{identity.namespace.value}/{identity.digest[:2]}"
            )
            location = self._directory.location(_entry_path(identity))
        except CacheFailure:
            raise
        except Exception as exc:
            failure = _storage_failure("cache.read", exc)
        else:
            try:
                return location.read_bytes()
            except FileNotFoundError:
                return None
            except CacheFailure:
                raise
            except Exception as exc:
                failure = _storage_failure("cache.read", exc)
        raise failure from None

    def publish(self, identity: CacheIdentity, envelope: bytes) -> bool:
        try:
            self._ensure_directory(
                f"{identity.namespace.value}/{identity.digest[:2]}"
            )
            target = self._directory.location(_entry_path(identity))
            try:
                target.publish_bytes_atomically(envelope)
            except FileExistsError:
                return False
            return True
        except CacheFailure:
            raise
        except Exception as exc:
            failure = _storage_failure("cache.publish", exc)
        raise failure from None

    def quarantine(
        self,
        identity: CacheIdentity,
        reason_code: str,
    ) -> None:
        try:
            parent = (
                f".quarantine/{identity.namespace.value}/"
                f"{identity.digest[:2]}"
            )
            self._ensure_directory(parent)
            source = self._directory.location(_entry_path(identity))
            for _ in range(16):
                nonce = secrets.token_hex(8)
                destination = self._directory.location(
                    f"{parent}/{identity.digest}.{reason_code}.{nonce}.json"
                )
                try:
                    source.quarantine_to(destination)
                except FileExistsError:
                    continue
                return
            raise OSError(
                errno.EBUSY,
                "无法分配缓存隔离名称",
            )
        except CacheFailure:
            raise
        except Exception as exc:
            failure = _storage_failure("cache.quarantine", exc)
        raise failure from None

    def with_claim(
        self,
        identity: CacheIdentity,
        cancellation: CancellationToken,
        effect: Callable[[int], _ResultT],
    ) -> _ResultT:
        effect_failure: BaseException | None = None
        try:
            parent = (
                f".locks/{identity.namespace.value}/{identity.digest[:2]}"
            )
            self._ensure_directory(parent)
            lock_location = self._directory.location(
                f"{parent}/{identity.digest}.lock"
            )
            try:
                lock_location.publish_bytes_atomically(b"")
            except FileExistsError:
                pass
            started = monotonic()

            def execute() -> _ResultT:
                waited = max(0, round((monotonic() - started) * 1000))
                try:
                    return effect(waited)
                except BaseException as exc:
                    raise _ClaimEffectFailed(exc) from None

            return lock_location.with_exclusive_cache_lock(
                cancellation,
                execute,
            )
        except CancellationRequested:
            raise
        except _ClaimEffectFailed as exc:
            effect_failure = exc.failure
        except CacheFailure:
            raise
        except Exception as exc:
            failure = _storage_failure("cache.claim", exc)
        if effect_failure is not None:
            raise effect_failure
        raise failure from None

    def _ensure_directory(self, relative_path: str) -> None:
        parts = relative_path.split("/")
        for index in range(1, len(parts) + 1):
            location = self._directory.location("/".join(parts[:index]))
            try:
                location.mkdir()
            except FileExistsError:
                pass


def _entry_path(identity: CacheIdentity) -> str:
    return (
        f"{identity.namespace.value}/{identity.digest[:2]}/"
        f"{identity.digest}.json"
    )


def _storage_failure(
    operation: str,
    exc: BaseException,
) -> CacheFailure:
    if isinstance(exc, CacheFailure):
        return exc
    reason_code = _reason_code(operation, exc)
    return CacheFailure(
        {
            "operation": operation,
            "reason_code": reason_code,
        }
    )


def _reason_code(operation: str, exc: BaseException) -> str:
    if isinstance(exc, WorkspaceFailure):
        workspace_reason = exc.diagnostics.get("reason_code")
        if workspace_reason in {
            "filesystem.permission_denied",
            "workspace.permission_denied",
        }:
            return "cache.permission_denied"
        if workspace_reason == "filesystem.file_sync_failed":
            return "cache.file_sync_failed"
        if workspace_reason == "filesystem.directory_sync_failed":
            return "cache.directory_sync_failed"
        if workspace_reason in {
            "filesystem.atomic_replace_failed",
            "filesystem.cross_device",
        }:
            return "cache.atomic_replace_failed"
    if isinstance(exc, PermissionError):
        return "cache.permission_denied"
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "cache.disk_full"
    return {
        "cache.claim": "cache.lock_failed",
        "cache.read": "cache.read_failed",
        "cache.quarantine": "cache.quarantine_failed",
        "cache.publish": "cache.write_failed",
    }[operation]
