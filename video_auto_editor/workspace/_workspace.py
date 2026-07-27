"""Workspace 深模块的公共入口。

该边界防止路径替换、符号链接与归属漂移造成误写误删；共享同一 UID
且主动篡改目录的进程不属于其隔离对象，协作进程必须遵循同一锁协议。
"""

import ctypes
import errno
import fcntl
import os
import secrets
import stat
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from threading import Condition, get_ident
from typing import BinaryIO, Literal, TypeVar

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId

from ._capability import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    _claim_workspace_issuer,
    _ManagedOperations,
)
from ._failure import WorkspaceFailure, _without_sensitive_exception_context

_MARKER_NAME = ".video-auto-editor-workspace.json"
_MARKER_BYTES = b'{"schema_version":"workspace.v1"}\n'
_CREATE_PREFIX = ".workspace-create-"
_DIRECTORY_MODE = 0o700
_SENSITIVE_FILE_MODE = 0o600
_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_REGULAR_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_FileMode = Literal["rb", "wb", "xb", "ab"]
_ResultT = TypeVar("_ResultT")
_MANAGED_FILE_FLAGS: dict[_FileMode, int] = {
    "rb": os.O_RDONLY,
    "wb": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "xb": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    # 追加只允许既有受管文件，防止事件日志丢失后静默创建新时间线。
    "ab": os.O_WRONLY | os.O_APPEND,
}
_CACHE_LOCK_FLAGS = os.O_RDWR
_ALLOWED_MANAGED_FILE_FLAGS = frozenset(
    (*_MANAGED_FILE_FLAGS.values(), _CACHE_LOCK_FLAGS)
)
_Identity = tuple[int, int]
_CAPABILITY_ISSUER = _claim_workspace_issuer()
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_NOREPLACE = 1


@dataclass(frozen=True, slots=True, init=False)
class SourceFileCapability:
    """启动时已经解析并验证的素材普通文件。"""

    path: Path

    def __new__(
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "SourceFileCapability":
        raise TypeError("SourceFileCapability 只能由 Workspace 签发")


@dataclass(frozen=True, slots=True)
class _LayoutSnapshot:
    root: _Identity
    mount_id: int
    marker: _Identity
    lock: _Identity
    directories: tuple[tuple[tuple[str, ...], _Identity], ...]

    def directory(self, parts: tuple[str, ...]) -> _Identity:
        for candidate, identity in self.directories:
            if candidate == parts:
                return identity
        raise KeyError(parts)


@dataclass(frozen=True, slots=True)
class _LockHandle:
    root_descriptor: int
    lock_descriptor: int


@dataclass(slots=True)
class _CreatedEntry:
    parent_descriptor: int
    name: str
    identity: _Identity
    is_directory: bool


@dataclass(frozen=True, slots=True)
class Workspace:
    """一个绑定规范根目录和固定磁盘身份的受管 workspace。"""

    source: SourceFileCapability | None
    root: Path
    _identity: object = field(repr=False, compare=False)
    _layout: _LayoutSnapshot = field(repr=False, compare=False)

    @classmethod
    def open(
        cls,
        source: PathLike[str] | str,
        workspace_dir: PathLike[str] | str | None = None,
    ) -> "Workspace":
        """解析素材并打开默认或显式 workspace。"""
        return _without_sensitive_exception_context(
            lambda: cls._open(source, workspace_dir)
        )

    @classmethod
    def _open(
        cls,
        source: PathLike[str] | str,
        workspace_dir: PathLike[str] | str | None,
    ) -> "Workspace":
        source_path = _resolve_source_file(Path(source))
        root = (
            source_path.with_suffix(".autocut")
            if workspace_dir is None
            else _resolve_workspace_path(Path(workspace_dir))
        )
        layout = _open_or_initialize_workspace(root)
        source_capability = object.__new__(SourceFileCapability)
        object.__setattr__(source_capability, "path", source_path)
        return cls(
            source=source_capability,
            root=root,
            _identity=object(),
            _layout=layout,
        )

    @classmethod
    def open_existing(
        cls,
        workspace_dir: PathLike[str] | str,
    ) -> "Workspace":
        """只打开已有受管 workspace，不创建直播拆条运行或目录。"""
        return _without_sensitive_exception_context(
            lambda: cls._open_existing(workspace_dir)
        )

    @classmethod
    def _open_existing(
        cls,
        workspace_dir: PathLike[str] | str,
    ) -> "Workspace":
        root = _resolve_workspace_path(Path(workspace_dir))
        layout = _open_existing_workspace(root)
        return cls(
            source=None,
            root=root,
            _identity=object(),
            _layout=layout,
        )

    def acquire_run(self, run_id: RunId) -> "RunWorkspace":
        """为一次直播拆条运行签发受管 capability。"""
        return _without_sensitive_exception_context(
            lambda: self._acquire_run(run_id)
        )

    def _acquire_run(self, run_id: RunId) -> "RunWorkspace":
        if not isinstance(run_id, RunId):
            raise TypeError("直播拆条运行 workspace 必须绑定 RunId")
        if self.source is None:
            raise RuntimeError("维护模式 workspace 没有素材，不能创建直播拆条运行")

        lock_handle = self._acquire_lock()
        journal: list[_CreatedEntry] = []
        opened_descriptors: list[int] = []
        try:
            self._cleanup_stale_temporary(lock_handle.root_descriptor)
            runs_descriptor = self._open_layout_directory(
                lock_handle.root_descriptor,
                ("work", "runs"),
            )
            opened_descriptors.append(runs_descriptor)
            temporary_parent_descriptor = self._open_layout_directory(
                lock_handle.root_descriptor,
                ("work", "tmp"),
            )
            opened_descriptors.append(temporary_parent_descriptor)

            run_name = str(run_id)
            run_descriptor, run_identity = _create_directory_at(
                runs_descriptor,
                run_name,
                journal,
            )
            opened_descriptors.append(run_descriptor)
            temporary_descriptor, temporary_identity = _create_directory_at(
                temporary_parent_descriptor,
                run_name,
                journal,
            )
            opened_descriptors.append(temporary_descriptor)
            scratch_descriptor, scratch_identity = _create_directory_at(
                temporary_descriptor,
                "scratch",
                journal,
            )
            opened_descriptors.append(scratch_descriptor)
            staging_descriptor, staging_identity = _create_directory_at(
                temporary_descriptor,
                "delivery",
                journal,
            )
            opened_descriptors.append(staging_descriptor)
            self._validate_lease_root(lock_handle.root_descriptor)

            lease_guard = _LeaseGuard(
                workspace=self,
                root_descriptor=lock_handle.root_descriptor,
            )
            run_parts = ("work", "runs", run_name)
            temporary_run_parts = ("work", "tmp", run_name)
            temporary_parts = (*temporary_run_parts, "scratch")
            staging_parts = (*temporary_run_parts, "delivery")
            run_chain = (
                self._layout.directory(("work",)),
                self._layout.directory(("work", "runs")),
                run_identity,
            )
            temporary_run_chain = (
                self._layout.directory(("work",)),
                self._layout.directory(("work", "tmp")),
                temporary_identity,
            )
            temporary_chain = (*temporary_run_chain, scratch_identity)
            staging_chain = (*temporary_run_chain, staging_identity)
            run_workspace = RunWorkspace(
                lock_handle=lock_handle,
                lease_guard=lease_guard,
                cleanup_callback=lambda root_descriptor: self._cleanup_run_temporary(
                    root_descriptor,
                    run_name,
                    temporary_identity,
                ),
                run_id=run_id,
                cache=self._capability(
                    ManagedDirectoryRole.CACHE,
                    ("work", "cache"),
                    self._layout_chain(("work", "cache")),
                    lease_guard,
                    run_id,
                ),
                diagnostics=self._capability(
                    ManagedDirectoryRole.RUN_DIAGNOSTICS,
                    run_parts,
                    run_chain,
                    lease_guard,
                    run_id,
                ),
                temporary=self._capability(
                    ManagedDirectoryRole.RUN_TEMPORARY,
                    temporary_parts,
                    temporary_chain,
                    lease_guard,
                    run_id,
                ),
                delivery_staging=self._capability(
                    ManagedDirectoryRole.DELIVERY_STAGING,
                    staging_parts,
                    staging_chain,
                    lease_guard,
                    run_id,
                ),
                published_delivery=self._capability(
                    ManagedDirectoryRole.PUBLISHED_DELIVERY,
                    ("delivery",),
                    self._layout_chain(("delivery",)),
                    lease_guard,
                    run_id,
                ),
                previous_delivery=self._capability(
                    ManagedDirectoryRole.PREVIOUS_DELIVERY,
                    ("delivery.previous",),
                    self._layout_chain(("delivery.previous",)),
                    lease_guard,
                    run_id,
                ),
            )
            close_failure = _close_descriptors(
                reversed(opened_descriptors)
            )
            opened_descriptors.clear()
            if close_failure is not None:
                raise close_failure
            _discard_created_entries(journal)
            return run_workspace
        except BaseException as exc:
            rollback_failure = _rollback_created_entries(journal)
            _release_lock(lock_handle)
            if rollback_failure is not None:
                raise rollback_failure from exc
            if isinstance(exc, WorkspaceFailure):
                raise
            if isinstance(exc, PermissionError):
                raise _workspace_filesystem_failure(
                    operation="workspace.create",
                    reason_code="filesystem.permission_denied",
                ) from exc
            if isinstance(exc, OSError):
                raise _workspace_filesystem_failure(
                    operation="workspace.create",
                    reason_code="filesystem.create_failed",
                ) from exc
            raise
        finally:
            _close_descriptors(reversed(opened_descriptors))

    def acquire_maintenance(self) -> "MaintenanceWorkspace":
        """取得不创建直播拆条运行的受管维护 lease。"""
        return _without_sensitive_exception_context(
            self._acquire_maintenance
        )

    def _acquire_maintenance(self) -> "MaintenanceWorkspace":
        lock_handle = self._acquire_lock()
        try:
            lease_guard = _LeaseGuard(
                workspace=self,
                root_descriptor=lock_handle.root_descriptor,
            )
            return MaintenanceWorkspace(
                lock_handle=lock_handle,
                lease_guard=lease_guard,
                cache=self._capability(
                    ManagedDirectoryRole.CACHE,
                    ("work", "cache"),
                    self._layout_chain(("work", "cache")),
                    lease_guard,
                    None,
                ),
            )
        except BaseException as exc:
            try:
                _release_lock(lock_handle)
            except BaseException as release_failure:
                raise release_failure from exc
            raise

    def _layout_chain(
        self,
        parts: tuple[str, ...],
    ) -> tuple[_Identity, ...]:
        return tuple(
            self._layout.directory(parts[:index])
            for index in range(1, len(parts) + 1)
        )

    def _capability(
        self,
        role: ManagedDirectoryRole,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        lease_guard: "_LeaseGuard",
        run_id: RunId | None,
    ) -> ManagedDirectoryCapability:
        return _CAPABILITY_ISSUER.directory(
            operations=lease_guard.bind(
                base_parts,
                base_identities,
                workspace_identity=self._identity,
                run_id=run_id,
                role=role,
            ),
            workspace_identity=self._identity,
            run_id=run_id,
            role=role,
        )

    def _acquire_lock(self) -> _LockHandle:
        root_descriptor = -1
        lock_descriptor = -1
        work_descriptor = -1
        root_locked = False
        lock_locked = False
        try:
            root_descriptor = self._open_verified_root()
            fcntl.flock(
                root_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            root_locked = True
            self._validate_lease_root(root_descriptor)
            work_descriptor = self._open_layout_directory(
                root_descriptor,
                ("work",),
            )
            lock_descriptor, lock_status = _open_regular_at(
                work_descriptor,
                ".workspace.lock",
                expected=self._layout.lock,
            )
            if stat.S_IMODE(lock_status.st_mode) != _SENSITIVE_FILE_MODE:
                raise _insecure_workspace_permissions()
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            lock_locked = True
            self._validate_lease_root(root_descriptor)
            return _LockHandle(
                root_descriptor=root_descriptor,
                lock_descriptor=lock_descriptor,
            )
        except (BlockingIOError, OSError, WorkspaceFailure) as exc:
            _unlock_and_close(
                lock_descriptor,
                locked=lock_locked,
            )
            _unlock_and_close(
                root_descriptor,
                locked=root_locked,
            )
            if isinstance(exc, WorkspaceFailure) and (
                exc.diagnostics.get("operation") == "workspace.verify"
            ):
                raise
            raise _workspace_lock_failure() from exc
        finally:
            if work_descriptor >= 0:
                os.close(work_descriptor)

    def _open_verified_root(self) -> int:
        try:
            descriptor = _open_absolute_directory_no_follow(self.root)
            _inspect_layout_descriptor(
                descriptor,
                expected=self._layout,
            )
            return descriptor
        except WorkspaceFailure:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        except OSError as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise _invalid_workspace_marker() from exc

    def _validate_lease_root(self, root_descriptor: int) -> None:
        try:
            root_status = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_status.st_mode)
                or _identity(root_status) != self._layout.root
            ):
                raise _invalid_workspace_marker()
            _inspect_layout_descriptor(
                root_descriptor,
                expected=self._layout,
            )
            current_descriptor = _open_absolute_directory_no_follow(self.root)
            try:
                if _identity(os.fstat(current_descriptor)) != self._layout.root:
                    raise _invalid_workspace_marker()
            finally:
                os.close(current_descriptor)
        except WorkspaceFailure:
            raise
        except OSError as exc:
            raise _invalid_workspace_marker() from exc

    def _open_layout_directory(
        self,
        root_descriptor: int,
        parts: tuple[str, ...],
    ) -> int:
        return _open_bound_directory(
            root_descriptor,
            parts,
            self._layout_chain(parts),
            expected_mount_id=self._layout.mount_id,
        )

    def _validate_managed_directory(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
    ) -> None:
        _validate_effect_parts(base_parts, allow_empty=False)
        _validate_effect_parts(relative_parts, allow_empty=True)
        self._validate_lease_root(root_descriptor)
        descriptor = -1
        try:
            descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parts:
                next_descriptor = _open_managed_directory_at(
                    descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validate_lease_root(root_descriptor)

    def _open_managed_file(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
        flags: int,
    ) -> int:
        if not relative_parts:
            raise ValueError("受管文件位置必须包含相对文件名")
        _validate_effect_parts(base_parts, allow_empty=False)
        _validate_effect_parts(relative_parts, allow_empty=False)
        if flags not in _ALLOWED_MANAGED_FILE_FLAGS:
            raise ValueError("受管文件打开标志不合法")
        self._validate_lease_root(root_descriptor)
        parent_descriptor = -1
        file_descriptor = -1
        try:
            parent_descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    parent_descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            name = relative_parts[-1]
            before_status: os.stat_result | None
            try:
                before_status = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                before_status = None
            if before_status is not None and (
                not stat.S_ISREG(before_status.st_mode)
                or before_status.st_nlink != 1
            ):
                if stat.S_ISLNK(before_status.st_mode):
                    raise _workspace_access_failure(
                        "workspace.symlink_encountered"
                    )
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            safe_flags = flags & ~os.O_TRUNC
            require_new_identity = (
                before_status is None and bool(flags & os.O_CREAT)
            )
            if require_new_identity:
                safe_flags |= os.O_EXCL
            try:
                file_descriptor = os.open(
                    name,
                    safe_flags
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC
                    | os.O_NONBLOCK,
                    _SENSITIVE_FILE_MODE,
                    dir_fd=parent_descriptor,
                )
            except OSError as exc:
                if (
                    isinstance(exc, FileExistsError)
                    and require_new_identity
                    and not flags & os.O_EXCL
                ):
                    raise _workspace_access_failure(
                        "workspace.ownership_changed"
                    ) from exc
                raise _managed_component_failure(
                    parent_descriptor,
                    name,
                    exc,
                ) from exc
            opened_status = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened_status.st_mode):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            if opened_status.st_nlink != 1:
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            if (
                _descriptor_mount_id(file_descriptor)
                != self._layout.mount_id
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            if (
                before_status is not None
                and _identity(opened_status) != _identity(before_status)
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            if flags & os.O_CREAT:
                os.fchmod(file_descriptor, _SENSITIVE_FILE_MODE)
            _assert_name_identity(
                parent_descriptor,
                name,
                _identity(opened_status),
                access_failure=True,
            )
            if flags & os.O_TRUNC:
                current_status = os.fstat(file_descriptor)
                if current_status.st_nlink != 1:
                    raise _workspace_access_failure(
                        "workspace.ownership_changed"
                    )
                _assert_name_identity(
                    parent_descriptor,
                    name,
                    _identity(current_status),
                    access_failure=True,
                )
                os.ftruncate(file_descriptor, 0)
            return file_descriptor
        except BaseException:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            raise
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)

    def _use_managed_file(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
        mode: _FileMode,
        effect: Callable[[BinaryIO], object],
    ) -> object:
        try:
            flags = _MANAGED_FILE_FLAGS[mode]
        except KeyError as exc:
            raise ValueError("受管文件打开模式不合法") from exc
        descriptor = -1
        try:
            descriptor = self._open_managed_file(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts,
                flags,
            )
            expected_identity = _identity(os.fstat(descriptor))
            stream = os.fdopen(descriptor, mode)
            descriptor = -1
            with stream:
                result = effect(stream)
                if stream.closed:
                    raise RuntimeError("受管文件效果不能关闭文件流")
                if mode != "rb":
                    stream.flush()
                    os.fsync(stream.fileno())
                self._revalidate_managed_open_file(
                    root_descriptor,
                    base_parts,
                    base_identities,
                    relative_parts,
                    stream.fileno(),
                    expected_identity,
                )
                return result
        except (WorkspaceFailure, FileNotFoundError, FileExistsError):
            raise
        except PermissionError as exc:
            raise _workspace_access_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_access_failure("workspace.io_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _use_managed_exclusive_lock(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
        cancellation: CancellationToken,
        effect: Callable[[], object],
    ) -> object:
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("处理缓存独占锁必须绑定 CancellationToken")
        if not callable(effect):
            raise TypeError("处理缓存独占锁效果必须可调用")
        descriptor = -1
        locked = False
        try:
            descriptor = self._open_managed_file(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts,
                _CACHE_LOCK_FLAGS,
            )
            expected_identity = _identity(os.fstat(descriptor))
            while True:
                cancellation.raise_if_cancelled()
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    locked = True
                    break
                except InterruptedError:
                    cancellation.raise_if_cancelled()
                    continue
                except BlockingIOError:
                    cancellation.wait(0.01)
            cancellation.raise_if_cancelled()
            result = effect()
            self._revalidate_managed_open_file(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts,
                descriptor,
                expected_identity,
            )
            return result
        except WorkspaceFailure:
            raise
        except PermissionError as exc:
            raise _workspace_access_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_access_failure("workspace.io_failed") from exc
        finally:
            if descriptor >= 0:
                try:
                    if locked:
                        while True:
                            try:
                                fcntl.flock(descriptor, fcntl.LOCK_UN)
                                break
                            except InterruptedError:
                                continue
                finally:
                    os.close(descriptor)

    def _publish_managed_file_atomically(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
        contents: bytes,
    ) -> int:
        if not relative_parts:
            raise ValueError("原子发布位置必须包含相对文件名")
        _validate_effect_parts(base_parts, allow_empty=False)
        _validate_effect_parts(relative_parts, allow_empty=False)
        self._validate_lease_root(root_descriptor)
        parent_descriptor = -1
        file_descriptor = -1
        temporary_name = ""
        temporary_identity: _Identity | None = None
        published = False
        try:
            parent_descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    parent_descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            target_name = relative_parts[-1]
            try:
                target_status = os.stat(
                    target_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                target_status = None
            if target_status is not None:
                if stat.S_ISLNK(target_status.st_mode):
                    raise _workspace_access_failure(
                        "workspace.symlink_encountered"
                    )
                if (
                    not stat.S_ISREG(target_status.st_mode)
                    or target_status.st_nlink != 1
                ):
                    raise _workspace_access_failure(
                        "workspace.ownership_changed"
                    )
                raise FileExistsError(
                    errno.EEXIST,
                    "受管位置已经存在",
                )

            for _ in range(16):
                candidate = f"{_CREATE_PREFIX}{secrets.token_hex(16)}"
                try:
                    file_descriptor = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC
                        | os.O_NONBLOCK,
                        _SENSITIVE_FILE_MODE,
                        dir_fd=parent_descriptor,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if file_descriptor < 0:
                raise OSError(
                    errno.EBUSY,
                    "无法分配受管原子发布文件",
                )

            temporary_status = os.fstat(file_descriptor)
            temporary_identity = _identity(temporary_status)
            if (
                not stat.S_ISREG(temporary_status.st_mode)
                or temporary_status.st_nlink != 1
                or _descriptor_mount_id(file_descriptor)
                != self._layout.mount_id
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            os.fchmod(file_descriptor, _SENSITIVE_FILE_MODE)
            _assert_name_identity(
                parent_descriptor,
                temporary_name,
                temporary_identity,
                access_failure=True,
            )

            remaining = memoryview(contents)
            while remaining:
                written = os.write(file_descriptor, remaining)
                if written == 0:
                    raise OSError(errno.EIO, "无法完整写入受管文件")
                remaining = remaining[written:]
            _sync_file_data(file_descriptor)

            self._validate_lease_root(root_descriptor)
            synced_status = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(synced_status.st_mode)
                or synced_status.st_nlink != 1
                or _identity(synced_status) != temporary_identity
                or _descriptor_mount_id(file_descriptor)
                != self._layout.mount_id
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            _assert_name_identity(
                parent_descriptor,
                temporary_name,
                temporary_identity,
                access_failure=True,
            )
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts[:-1],
                parent_descriptor,
            )
            try:
                _rename_no_replace(
                    parent_descriptor,
                    temporary_name,
                    parent_descriptor,
                    target_name,
                )
            except OSError as exc:
                raise _managed_component_failure(
                    parent_descriptor,
                    target_name,
                    exc,
                ) from exc
            published = True
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts[:-1],
                parent_descriptor,
            )
            _assert_name_identity(
                parent_descriptor,
                target_name,
                temporary_identity,
                access_failure=True,
            )
            os.fsync(parent_descriptor)
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                relative_parts[:-1],
                parent_descriptor,
            )
            _assert_name_identity(
                parent_descriptor,
                target_name,
                temporary_identity,
                access_failure=True,
            )
            return len(contents)
        except (WorkspaceFailure, FileExistsError):
            raise
        except PermissionError as exc:
            raise _workspace_access_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_access_failure("workspace.io_failed") from exc
        finally:
            if (
                temporary_name
                and temporary_identity is None
                and file_descriptor >= 0
            ):
                try:
                    fallback_status = os.fstat(file_descriptor)
                    if (
                        stat.S_ISREG(fallback_status.st_mode)
                        and fallback_status.st_nlink == 1
                    ):
                        temporary_identity = _identity(fallback_status)
                except OSError:
                    pass
            try:
                if (
                    temporary_name
                    and temporary_identity is not None
                    and not published
                    and parent_descriptor >= 0
                ):
                    # 清理无法证明仍拥有临时名称时，所有权/耐久性失败
                    # 必须优先于原始 I/O 错误，不能把未知残留静默当成功。
                    _remove_atomic_publish_temporary(
                        parent_descriptor,
                        temporary_name,
                        temporary_identity,
                    )
            finally:
                try:
                    if file_descriptor >= 0:
                        os.close(file_descriptor)
                finally:
                    if parent_descriptor >= 0:
                        os.close(parent_descriptor)

    def _quarantine_managed_file(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        source_parts: tuple[str, ...],
        destination_parts: tuple[str, ...],
    ) -> None:
        if not source_parts or not destination_parts:
            raise ValueError("缓存隔离源和目标都必须包含相对文件名")
        if source_parts == destination_parts:
            raise ValueError("缓存隔离源和目标不能相同")
        _validate_effect_parts(base_parts, allow_empty=False)
        _validate_effect_parts(source_parts, allow_empty=False)
        _validate_effect_parts(destination_parts, allow_empty=False)
        self._validate_lease_root(root_descriptor)
        source_parent = -1
        destination_parent = -1
        try:
            source_parent = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in source_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    source_parent,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(source_parent)
                source_parent = next_descriptor

            destination_parent = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in destination_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    destination_parent,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(destination_parent)
                destination_parent = next_descriptor

            source_name = source_parts[-1]
            destination_name = destination_parts[-1]
            source_status = os.stat(
                source_name,
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(source_status.st_mode):
                raise _workspace_access_failure(
                    "workspace.symlink_encountered"
                )
            if (
                not stat.S_ISREG(source_status.st_mode)
                or source_status.st_nlink != 1
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
            source_identity = _identity(source_status)
            _assert_name_identity(
                source_parent,
                source_name,
                source_identity,
                access_failure=True,
            )
            try:
                destination_status = os.stat(
                    destination_name,
                    dir_fd=destination_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                destination_status = None
            if destination_status is not None:
                if stat.S_ISLNK(destination_status.st_mode):
                    raise _workspace_access_failure(
                        "workspace.symlink_encountered"
                    )
                raise FileExistsError(
                    errno.EEXIST,
                    "缓存隔离目标已经存在",
                )

            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                source_parts[:-1],
                source_parent,
            )
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                destination_parts[:-1],
                destination_parent,
            )
            _rename_no_replace(
                source_parent,
                source_name,
                destination_parent,
                destination_name,
            )
            _assert_name_identity(
                destination_parent,
                destination_name,
                source_identity,
                access_failure=True,
            )
            try:
                os.stat(
                    source_name,
                    dir_fd=source_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )

            os.fsync(source_parent)
            if _identity(os.fstat(destination_parent)) != _identity(
                os.fstat(source_parent)
            ):
                os.fsync(destination_parent)
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                source_parts[:-1],
                source_parent,
            )
            self._revalidate_managed_parent(
                root_descriptor,
                base_parts,
                base_identities,
                destination_parts[:-1],
                destination_parent,
            )
            _assert_name_identity(
                destination_parent,
                destination_name,
                source_identity,
                access_failure=True,
            )
        except (WorkspaceFailure, FileExistsError):
            raise
        except PermissionError as exc:
            raise _workspace_access_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_access_failure("workspace.io_failed") from exc
        finally:
            if destination_parent >= 0:
                os.close(destination_parent)
            if source_parent >= 0:
                os.close(source_parent)

    def _revalidate_managed_parent(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parent_parts: tuple[str, ...],
        parent_descriptor: int,
    ) -> None:
        self._validate_lease_root(root_descriptor)
        held_status = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(held_status.st_mode)
            or _descriptor_mount_id(parent_descriptor)
            != self._layout.mount_id
        ):
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            )
        current_descriptor = -1
        try:
            current_descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parent_parts:
                next_descriptor = _open_managed_directory_at(
                    current_descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(current_descriptor)
                current_descriptor = next_descriptor
            if _identity(os.fstat(current_descriptor)) != _identity(
                held_status
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
        finally:
            if current_descriptor >= 0:
                os.close(current_descriptor)
        self._validate_lease_root(root_descriptor)

    def _revalidate_managed_open_file(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
        file_descriptor: int,
        expected_identity: _Identity,
    ) -> None:
        self._validate_lease_root(root_descriptor)
        opened_status = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or opened_status.st_nlink != 1
            or _identity(opened_status) != expected_identity
            or _descriptor_mount_id(file_descriptor)
            != self._layout.mount_id
        ):
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            )
        parent_descriptor = -1
        try:
            parent_descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    parent_descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            name = relative_parts[-1]
            try:
                current_status = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                ) from exc
            if stat.S_ISLNK(current_status.st_mode):
                raise _workspace_access_failure(
                    "workspace.symlink_encountered"
                )
            if (
                not stat.S_ISREG(current_status.st_mode)
                or current_status.st_nlink != 1
                or _identity(current_status) != expected_identity
            ):
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                )
        finally:
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
        self._validate_lease_root(root_descriptor)

    def _make_managed_directory(
        self,
        root_descriptor: int,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        relative_parts: tuple[str, ...],
    ) -> None:
        if not relative_parts:
            raise ValueError("不能重复创建 capability 根目录")
        _validate_effect_parts(base_parts, allow_empty=False)
        _validate_effect_parts(relative_parts, allow_empty=False)
        self._validate_lease_root(root_descriptor)
        parent_descriptor = -1
        created_descriptor = -1
        journal: list[_CreatedEntry] = []
        try:
            parent_descriptor = _open_bound_directory(
                root_descriptor,
                base_parts,
                base_identities,
                expected_mount_id=self._layout.mount_id,
            )
            for part in relative_parts[:-1]:
                next_descriptor = _open_managed_directory_at(
                    parent_descriptor,
                    part,
                    expected_mount_id=self._layout.mount_id,
                )
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            name = relative_parts[-1]
            try:
                created_descriptor, _ = _create_directory_at(
                    parent_descriptor,
                    name,
                    journal,
                )
                _discard_created_entries(journal)
            except OSError as exc:
                rollback_failure = _rollback_created_entries(journal)
                if rollback_failure is not None:
                    raise rollback_failure from exc
                raise _managed_component_failure(
                    parent_descriptor,
                    name,
                    exc,
                ) from exc
        finally:
            if created_descriptor >= 0:
                os.close(created_descriptor)
            if parent_descriptor >= 0:
                os.close(parent_descriptor)
        self._validate_lease_root(root_descriptor)

    def _cleanup_stale_temporary(self, root_descriptor: int) -> None:
        self._validate_lease_root(root_descriptor)
        descriptor = -1
        try:
            descriptor = self._open_layout_directory(
                root_descriptor,
                ("work", "tmp"),
            )
            _remove_directory_contents(
                descriptor,
                expected_device=self._layout.root[0],
                expected_mount_id=self._layout.mount_id,
            )
            _sync_cleanup_directory(descriptor)
        except WorkspaceFailure:
            raise
        except PermissionError as exc:
            raise _workspace_cleanup_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_cleanup_failure(
                "workspace.remove_failed"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validate_lease_root(root_descriptor)

    def _cleanup_run_temporary(
        self,
        root_descriptor: int,
        run_directory_name: str,
        expected_identity: _Identity,
    ) -> None:
        try:
            self._validate_lease_root(root_descriptor)
        except WorkspaceFailure as exc:
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            ) from exc

        parent_descriptor = -1
        child_descriptor = -1
        try:
            parent_descriptor = self._open_layout_directory(
                root_descriptor,
                ("work", "tmp"),
            )
            try:
                target_status = os.stat(
                    run_directory_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISLNK(target_status.st_mode):
                raise _workspace_cleanup_failure(
                    "workspace.symlink_encountered"
                )
            if (
                not stat.S_ISDIR(target_status.st_mode)
                or _identity(target_status) != expected_identity
            ):
                raise _workspace_cleanup_failure(
                    "workspace.ownership_changed"
                )
            child_descriptor = _open_directory_at(
                parent_descriptor,
                run_directory_name,
                expected=expected_identity,
            )
            if (
                _descriptor_mount_id(child_descriptor)
                != self._layout.mount_id
            ):
                raise _workspace_cleanup_failure(
                    "workspace.ownership_changed"
                )
            _remove_directory_contents(
                child_descriptor,
                expected_device=self._layout.root[0],
                expected_mount_id=self._layout.mount_id,
            )
            _sync_cleanup_directory(child_descriptor)
            _quarantine_and_remove(
                parent_descriptor,
                run_directory_name,
                expected_identity,
                is_directory=True,
            )
            _sync_cleanup_directory(parent_descriptor)
        except WorkspaceFailure:
            raise
        except PermissionError as exc:
            raise _workspace_cleanup_failure(
                "workspace.permission_denied"
            ) from exc
        except OSError as exc:
            raise _workspace_cleanup_failure(
                "workspace.remove_failed"
            ) from exc
        finally:
            if child_descriptor >= 0:
                os.close(child_descriptor)
            if parent_descriptor >= 0:
                os.close(parent_descriptor)

        try:
            self._validate_lease_root(root_descriptor)
        except WorkspaceFailure as exc:
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            ) from exc


class _LeaseGuard:
    __slots__ = (
        "_active",
        "_active_effects",
        "_condition",
        "_effect_threads",
        "_release_pending",
        "_root_descriptor",
        "_workspace",
    )

    def __init__(
        self,
        *,
        workspace: Workspace,
        root_descriptor: int,
    ) -> None:
        self._workspace = workspace
        self._root_descriptor = root_descriptor
        self._active = True
        self._active_effects = 0
        self._condition = Condition()
        self._effect_threads: dict[int, int] = {}
        self._release_pending = True

    def bind(
        self,
        base_parts: tuple[str, ...],
        base_identities: tuple[_Identity, ...],
        *,
        workspace_identity: object,
        run_id: RunId | None,
        role: ManagedDirectoryRole,
    ) -> _ManagedOperations:
        """把效果永久绑定到一个已验证子树，调用方不能替换基址。"""
        _validate_effect_parts(base_parts, allow_empty=False)
        if len(base_parts) != len(base_identities):
            raise ValueError("受管目录路径与身份链不一致")

        def validate_directory(relative_parts: tuple[str, ...]) -> None:
            try:
                self._execute(
                    lambda root_descriptor: self._workspace._validate_managed_directory(
                        root_descriptor,
                        base_parts,
                        base_identities,
                        relative_parts,
                    )
                )
            except WorkspaceFailure:
                raise
            except PermissionError as exc:
                raise _workspace_access_failure(
                    "workspace.permission_denied"
                ) from exc
            except OSError as exc:
                raise _workspace_access_failure(
                    "workspace.io_failed"
                ) from exc

        def use_file(
            relative_parts: tuple[str, ...],
            mode: _FileMode,
            effect: Callable[[BinaryIO], object],
        ) -> object:
            return self._execute(
                lambda root_descriptor: self._workspace._use_managed_file(
                    root_descriptor,
                    base_parts,
                    base_identities,
                    relative_parts,
                    mode,
                    effect,
                )
            )

        def publish_file(
            relative_parts: tuple[str, ...],
            contents: bytes,
        ) -> int:
            return self._execute(
                lambda root_descriptor: self._workspace._publish_managed_file_atomically(
                    root_descriptor,
                    base_parts,
                    base_identities,
                    relative_parts,
                    contents,
                )
            )

        def make_directory(relative_parts: tuple[str, ...]) -> None:
            try:
                self._execute(
                    lambda root_descriptor: self._workspace._make_managed_directory(
                        root_descriptor,
                        base_parts,
                        base_identities,
                        relative_parts,
                    )
                )
            except (WorkspaceFailure, FileExistsError):
                raise
            except PermissionError as exc:
                raise _workspace_access_failure(
                    "workspace.permission_denied"
                ) from exc
            except OSError as exc:
                raise _workspace_access_failure(
                    "workspace.io_failed"
                ) from exc

        def use_exclusive_lock(
            relative_parts: tuple[str, ...],
            cancellation: CancellationToken,
            effect: Callable[[], object],
        ) -> object:
            return self._execute(
                lambda root_descriptor: (
                    self._workspace._use_managed_exclusive_lock(
                        root_descriptor,
                        base_parts,
                        base_identities,
                        relative_parts,
                        cancellation,
                        effect,
                    )
                )
            )

        def quarantine_file(
            source_parts: tuple[str, ...],
            destination_parts: tuple[str, ...],
        ) -> None:
            self._execute(
                lambda root_descriptor: (
                    self._workspace._quarantine_managed_file(
                        root_descriptor,
                        base_parts,
                        base_identities,
                        source_parts,
                        destination_parts,
                    )
                )
            )

        return _CAPABILITY_ISSUER.operations(
            use_file=use_file,
            publish_file=publish_file,
            validate_directory=validate_directory,
            make_directory=make_directory,
            use_exclusive_lock=use_exclusive_lock,
            quarantine_file=quarantine_file,
            workspace_identity=workspace_identity,
            run_id=run_id,
            role=role,
        )

    def execute(
        self,
        effect: Callable[[int], _ResultT],
    ) -> _ResultT:
        return self._execute(effect)

    def _execute(
        self,
        effect: Callable[[int], _ResultT],
    ) -> _ResultT:
        descriptor = self._begin_effect()
        try:
            return effect(descriptor)
        finally:
            try:
                os.close(descriptor)
            finally:
                self._finish_effect()

    def _begin_effect(self) -> int:
        with self._condition:
            if not self._active:
                raise RuntimeError("workspace lease 已关闭")
            descriptor = os.dup(self._root_descriptor)
            self._active_effects += 1
            thread_id = get_ident()
            self._effect_threads[thread_id] = (
                self._effect_threads.get(thread_id, 0) + 1
            )
            return descriptor

    def _finish_effect(self) -> None:
        with self._condition:
            self._active_effects -= 1
            thread_id = get_ident()
            remaining = self._effect_threads[thread_id] - 1
            if remaining:
                self._effect_threads[thread_id] = remaining
            else:
                del self._effect_threads[thread_id]
            if self._active_effects == 0:
                self._condition.notify_all()

    def assert_active(self) -> None:
        with self._condition:
            if not self._active:
                raise RuntimeError("workspace lease 已关闭")

    def close(self) -> bool:
        with self._condition:
            if self._effect_threads.get(get_ident(), 0):
                raise RuntimeError("不能在受管文件效果内关闭 workspace lease")
            if self._active:
                self._active = False
            while self._active_effects:
                self._condition.wait()
            if not self._release_pending:
                return False
            self._release_pending = False
            return True


class _WorkspaceLease:
    __slots__ = (
        "_close_condition",
        "_closed",
        "_closing",
        "_lease_guard",
        "_lock_handle",
    )

    def __init__(
        self,
        lock_handle: _LockHandle,
        lease_guard: _LeaseGuard,
    ) -> None:
        self._lock_handle = lock_handle
        self._lease_guard = lease_guard
        self._close_condition = Condition()
        self._closed = False
        self._closing = False

    def close(self) -> None:
        """释放当前 workspace lease；重复调用仍安全。"""
        with self._close_condition:
            while self._closing and not self._closed:
                self._close_condition.wait()
            if self._closed:
                return
            self._closing = True
        owns_release = False
        try:
            owns_release = self._lease_guard.close()
            if owns_release:
                _release_lock(self._lock_handle)
        except BaseException:
            with self._close_condition:
                if owns_release:
                    self._closed = True
                self._closing = False
                self._close_condition.notify_all()
            raise
        else:
            with self._close_condition:
                self._closed = True
                self._closing = False
                self._close_condition.notify_all()

    def __enter__(self) -> "_WorkspaceLease":
        self._lease_guard.assert_active()
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.close()


class RunWorkspace(_WorkspaceLease):
    """持有一次直播拆条运行锁与最小受管 capability 的 lease。"""

    __slots__ = (
        "_cache",
        "_cleanup_callback",
        "_delivery_staging",
        "_diagnostics",
        "_previous_delivery",
        "_published_delivery",
        "_run_id",
        "_temporary",
    )

    def __init__(
        self,
        *,
        lock_handle: _LockHandle,
        lease_guard: _LeaseGuard,
        cleanup_callback: Callable[[int], None],
        run_id: RunId,
        cache: ManagedDirectoryCapability,
        diagnostics: ManagedDirectoryCapability,
        temporary: ManagedDirectoryCapability,
        delivery_staging: ManagedDirectoryCapability,
        published_delivery: ManagedDirectoryCapability,
        previous_delivery: ManagedDirectoryCapability,
    ) -> None:
        super().__init__(lock_handle, lease_guard)
        self._cleanup_callback = cleanup_callback
        self._run_id = run_id
        self._cache = cache
        self._diagnostics = diagnostics
        self._temporary = temporary
        self._delivery_staging = delivery_staging
        self._published_delivery = published_delivery
        self._previous_delivery = previous_delivery

    @property
    def run_id(self) -> RunId:
        return self._run_id

    @property
    def cache(self) -> ManagedDirectoryCapability:
        return self._cache

    @property
    def diagnostics(self) -> ManagedDirectoryCapability:
        return self._diagnostics

    @property
    def temporary(self) -> ManagedDirectoryCapability:
        return self._temporary

    @property
    def delivery_staging(self) -> ManagedDirectoryCapability:
        return self._delivery_staging

    @property
    def published_delivery(self) -> ManagedDirectoryCapability:
        return self._published_delivery

    @property
    def previous_delivery(self) -> ManagedDirectoryCapability:
        return self._previous_delivery

    def cleanup(self) -> None:
        """清理本次运行敏感临时内容，不触碰缓存、诊断或交付。"""
        _without_sensitive_exception_context(
            lambda: self._lease_guard.execute(self._cleanup_callback)
        )

    def __enter__(self) -> "RunWorkspace":
        super().__enter__()
        return self


class MaintenanceWorkspace(_WorkspaceLease):
    """只授予处理缓存维护能力且不创建运行事实的 lease。"""

    __slots__ = ("_cache",)

    def __init__(
        self,
        *,
        lock_handle: _LockHandle,
        lease_guard: _LeaseGuard,
        cache: ManagedDirectoryCapability,
    ) -> None:
        super().__init__(lock_handle, lease_guard)
        self._cache = cache

    @property
    def cache(self) -> ManagedDirectoryCapability:
        return self._cache

    def __enter__(self) -> "MaintenanceWorkspace":
        super().__enter__()
        return self


def _resolve_source_file(candidate: Path) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        parent_descriptor = _open_absolute_directory_no_follow(resolved.parent)
        try:
            descriptor = os.open(
                resolved.name,
                _REGULAR_FILE_FLAGS,
                dir_fd=parent_descriptor,
            )
            try:
                source_status = os.fstat(descriptor)
                _assert_name_identity(
                    parent_descriptor,
                    resolved.name,
                    _identity(source_status),
                )
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)
    except PermissionError as exc:
        raise WorkspaceFailure(
            ErrorCode.INPUT_UNREADABLE,
            {"reason_code": "input.permission_denied"},
        ) from exc
    except FileNotFoundError as exc:
        raise WorkspaceFailure(
            ErrorCode.INPUT_MISSING,
            {"reason_code": "input.not_found"},
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise WorkspaceFailure(
            ErrorCode.INPUT_UNREADABLE,
            {"reason_code": "input.read_failed"},
        ) from exc
    if not stat.S_ISREG(source_status.st_mode):
        raise WorkspaceFailure(
            ErrorCode.INPUT_UNREADABLE,
            {"reason_code": "input.not_regular_file"},
        )
    return resolved


def _resolve_workspace_path(candidate: Path) -> Path:
    try:
        if os.path.lexists(candidate):
            return candidate.resolve(strict=True)
        parent = candidate.parent.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise _workspace_not_directory() from exc
    return parent / candidate.name


def _open_or_initialize_workspace(root: Path) -> _LayoutSnapshot:
    if os.path.lexists(root):
        descriptor = -1
        try:
            descriptor = _open_absolute_directory_no_follow(root)
            if _directory_is_empty(descriptor):
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    if _directory_is_empty(descriptor):
                        original_mode = stat.S_IMODE(
                            os.fstat(descriptor).st_mode
                        )
                        try:
                            os.fchmod(descriptor, _DIRECTORY_MODE)
                            return _initialize_layout_descriptor(descriptor)
                        except BaseException:
                            os.fchmod(descriptor, original_mode)
                            raise
                    return _inspect_layout_descriptor(descriptor)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            return _inspect_after_possible_initialization(descriptor)
        except WorkspaceFailure:
            raise
        except PermissionError as exc:
            raise _workspace_filesystem_failure(
                operation="workspace.verify",
                reason_code="filesystem.permission_denied",
            ) from exc
        except NotADirectoryError as exc:
            raise _workspace_not_directory() from exc
        except OSError as exc:
            raise _workspace_filesystem_failure(
                operation="workspace.create",
                reason_code="filesystem.create_failed",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return _create_workspace(root)


def _open_existing_workspace(root: Path) -> _LayoutSnapshot:
    if not os.path.lexists(root):
        raise _invalid_workspace_marker()
    descriptor = -1
    try:
        descriptor = _open_absolute_directory_no_follow(root)
        return _inspect_after_possible_initialization(descriptor)
    except WorkspaceFailure:
        raise
    except PermissionError as exc:
        raise _workspace_filesystem_failure(
            operation="workspace.verify",
            reason_code="filesystem.permission_denied",
        ) from exc
    except NotADirectoryError as exc:
        raise _workspace_not_directory() from exc
    except OSError as exc:
        raise _invalid_workspace_marker() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_workspace(root: Path) -> _LayoutSnapshot:
    parent_descriptor = -1
    root_descriptor = -1
    root_locked = False
    creation_journal: list[_CreatedEntry] = []
    try:
        parent_descriptor = _open_absolute_directory_no_follow(root.parent)
        try:
            root_descriptor, _ = _create_directory_at(
                parent_descriptor,
                root.name,
                creation_journal,
            )
        except FileExistsError as exc:
            rollback_failure = _rollback_created_entries(creation_journal)
            if rollback_failure is not None:
                raise rollback_failure from exc
            if os.path.lexists(root):
                return _open_or_initialize_workspace(root)
            raise
        fcntl.flock(root_descriptor, fcntl.LOCK_EX)
        root_locked = True
        snapshot = (
            _initialize_layout_descriptor(root_descriptor)
            if _directory_is_empty(root_descriptor)
            else _inspect_layout_descriptor(root_descriptor)
        )
        _assert_name_identity(
            parent_descriptor,
            root.name,
            snapshot.root,
        )
        _discard_created_entries(creation_journal)
        return snapshot
    except WorkspaceFailure as exc:
        rollback_failure = _rollback_created_entries(creation_journal)
        if rollback_failure is not None:
            raise rollback_failure from exc
        raise
    except PermissionError as exc:
        rollback_failure = _rollback_created_entries(creation_journal)
        if rollback_failure is not None:
            raise rollback_failure from exc
        raise _workspace_filesystem_failure(
            operation="workspace.create",
            reason_code="filesystem.permission_denied",
        ) from exc
    except OSError as exc:
        rollback_failure = _rollback_created_entries(creation_journal)
        if rollback_failure is not None:
            raise rollback_failure from exc
        raise _workspace_filesystem_failure(
            operation="workspace.create",
            reason_code="filesystem.create_failed",
        ) from exc
    finally:
        root_release_failure = _unlock_and_close(
            root_descriptor,
            locked=root_locked,
        )
        parent_release_failure = _unlock_and_close(
            parent_descriptor,
            locked=False,
        )
        if root_release_failure is not None:
            raise root_release_failure
        if parent_release_failure is not None:
            raise parent_release_failure

def _inspect_after_possible_initialization(
    root_descriptor: int,
) -> _LayoutSnapshot:
    try:
        return _inspect_layout_descriptor(root_descriptor)
    except WorkspaceFailure as initial_failure:
        if initial_failure.diagnostics.get("reason_code") != (
            "filesystem.marker_invalid"
        ):
            raise
        fcntl.flock(root_descriptor, fcntl.LOCK_SH)
        try:
            return _inspect_layout_descriptor(root_descriptor)
        finally:
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)


def _initialize_layout_descriptor(
    root_descriptor: int,
) -> _LayoutSnapshot:
    journal: list[_CreatedEntry] = []
    work_descriptor = -1
    opened_descriptors: list[int] = []
    try:
        _create_sensitive_file_at(
            root_descriptor,
            _MARKER_NAME,
            _MARKER_BYTES,
            journal,
        )
        for name in ("delivery", "delivery.previous"):
            descriptor, _ = _create_directory_at(
                root_descriptor,
                name,
                journal,
            )
            opened_descriptors.append(descriptor)
        work_descriptor, _ = _create_directory_at(
            root_descriptor,
            "work",
            journal,
        )
        opened_descriptors.append(work_descriptor)
        for name in ("cache", "runs", "tmp"):
            descriptor, _ = _create_directory_at(
                work_descriptor,
                name,
                journal,
            )
            opened_descriptors.append(descriptor)
        _create_sensitive_file_at(
            work_descriptor,
            ".workspace.lock",
            b"",
            journal,
        )
        os.fsync(work_descriptor)
        os.fsync(root_descriptor)
        snapshot = _inspect_layout_descriptor(root_descriptor)
        _discard_created_entries(journal)
        return snapshot
    except BaseException as exc:
        rollback_failure = _rollback_created_entries(journal)
        if rollback_failure is not None:
            raise rollback_failure from exc
        raise
    finally:
        for descriptor in reversed(opened_descriptors):
            os.close(descriptor)


def _inspect_layout_descriptor(
    root_descriptor: int,
    *,
    expected: _LayoutSnapshot | None = None,
) -> _LayoutSnapshot:
    marker_descriptor = -1
    lock_descriptor = -1
    directory_descriptors: list[int] = []
    try:
        marker_descriptor, marker_status = _open_regular_at(
            root_descriptor,
            _MARKER_NAME,
        )
        marker_bytes = os.read(marker_descriptor, len(_MARKER_BYTES) + 1)
        if marker_bytes != _MARKER_BYTES:
            raise _invalid_workspace_marker()
        if stat.S_IMODE(marker_status.st_mode) != _SENSITIVE_FILE_MODE:
            raise _insecure_workspace_permissions()

        root_status = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_status.st_mode):
            raise _invalid_workspace_marker()
        if stat.S_IMODE(root_status.st_mode) != _DIRECTORY_MODE:
            raise _insecure_workspace_permissions()
        root_mount_id = _descriptor_mount_id(root_descriptor)
        if marker_status.st_dev != root_status.st_dev:
            raise _invalid_workspace_marker()
        if _descriptor_mount_id(marker_descriptor) != root_mount_id:
            raise _invalid_workspace_marker()
        _assert_exact_directory_names(
            root_descriptor,
            {
                _MARKER_NAME,
                "delivery",
                "delivery.previous",
                "work",
            },
        )

        directory_identities: list[
            tuple[tuple[str, ...], _Identity]
        ] = []
        root_children: dict[str, int] = {}
        for name in ("delivery", "delivery.previous", "work"):
            descriptor = _open_directory_at(root_descriptor, name)
            directory_descriptors.append(descriptor)
            root_children[name] = descriptor
            status = os.fstat(descriptor)
            if status.st_dev != root_status.st_dev:
                raise _invalid_workspace_marker()
            if _descriptor_mount_id(descriptor) != root_mount_id:
                raise _invalid_workspace_marker()
            if stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE:
                raise _insecure_workspace_permissions()
            directory_identities.append(((name,), _identity(status)))

        work_descriptor = root_children["work"]
        _assert_exact_directory_names(
            work_descriptor,
            {".workspace.lock", "cache", "runs", "tmp"},
        )
        for name in ("cache", "runs", "tmp"):
            descriptor = _open_directory_at(work_descriptor, name)
            directory_descriptors.append(descriptor)
            status = os.fstat(descriptor)
            if status.st_dev != root_status.st_dev:
                raise _invalid_workspace_marker()
            if _descriptor_mount_id(descriptor) != root_mount_id:
                raise _invalid_workspace_marker()
            if stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE:
                raise _insecure_workspace_permissions()
            directory_identities.append(
                (("work", name), _identity(status))
            )
        lock_descriptor, lock_status = _open_regular_at(
            work_descriptor,
            ".workspace.lock",
        )
        if lock_status.st_dev != root_status.st_dev:
            raise _invalid_workspace_marker()
        if _descriptor_mount_id(lock_descriptor) != root_mount_id:
            raise _invalid_workspace_marker()
        if stat.S_IMODE(lock_status.st_mode) != _SENSITIVE_FILE_MODE:
            raise _insecure_workspace_permissions()

        snapshot = _LayoutSnapshot(
            root=_identity(root_status),
            mount_id=root_mount_id,
            marker=_identity(marker_status),
            lock=_identity(lock_status),
            directories=tuple(directory_identities),
        )
        if expected is not None:
            _assert_layout_identity(snapshot, expected)
        return snapshot
    except WorkspaceFailure:
        raise
    except PermissionError as exc:
        raise _insecure_workspace_permissions() from exc
    except OSError as exc:
        raise _invalid_workspace_marker() from exc
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if marker_descriptor >= 0:
            os.close(marker_descriptor)
        for descriptor in reversed(directory_descriptors):
            os.close(descriptor)


def _assert_layout_identity(
    actual: _LayoutSnapshot,
    expected: _LayoutSnapshot,
) -> None:
    if actual.lock != expected.lock:
        raise _workspace_lock_failure()
    if (
        actual.root != expected.root
        or actual.mount_id != expected.mount_id
        or actual.marker != expected.marker
        or actual.directories != expected.directories
    ):
        raise _invalid_workspace_marker()


def _open_absolute_directory_no_follow(path: Path) -> int:
    if not path.is_absolute():
        raise OSError("受管目录必须是绝对路径")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            next_descriptor = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _descriptor_mount_id(descriptor: int) -> int:
    """从 Linux fdinfo 读取描述符实际绑定的挂载点身份。"""
    info_descriptor = os.open(
        f"/proc/self/fdinfo/{descriptor}",
        os.O_RDONLY | os.O_CLOEXEC,
    )
    try:
        chunks: list[bytes] = []
        total_size = 0
        while True:
            chunk = os.read(info_descriptor, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            total_size += len(chunk)
            if total_size > 65536:
                raise OSError(errno.EOVERFLOW, "fdinfo 内容异常")
        for line in b"".join(chunks).splitlines():
            key, separator, value = line.partition(b":")
            if separator and key == b"mnt_id":
                try:
                    return int(value.strip())
                except ValueError as exc:
                    raise OSError(
                        errno.EIO,
                        "fdinfo 挂载点身份无效",
                    ) from exc
        raise OSError(errno.EIO, "fdinfo 缺少挂载点身份")
    finally:
        os.close(info_descriptor)


def _open_bound_directory(
    root_descriptor: int,
    parts: tuple[str, ...],
    identities: tuple[_Identity, ...],
    *,
    expected_mount_id: int,
) -> int:
    _validate_effect_parts(parts, allow_empty=False)
    if len(parts) != len(identities):
        raise _workspace_access_failure("workspace.ownership_changed")
    descriptor = os.dup(root_descriptor)
    try:
        for part, expected in zip(parts, identities, strict=True):
            try:
                next_descriptor = _open_directory_at(
                    descriptor,
                    part,
                    expected=expected,
                )
            except OSError as exc:
                raise _managed_component_failure(
                    descriptor,
                    part,
                    exc,
                ) from exc
            try:
                if _descriptor_mount_id(next_descriptor) != expected_mount_id:
                    raise _workspace_access_failure(
                        "workspace.ownership_changed"
                    )
            except WorkspaceFailure:
                os.close(next_descriptor)
                raise
            except OSError as exc:
                os.close(next_descriptor)
                raise _workspace_access_failure(
                    "workspace.ownership_changed"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except WorkspaceFailure:
        os.close(descriptor)
        raise
    except OSError:
        os.close(descriptor)
        raise


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: _Identity | None = None,
) -> int:
    descriptor = os.open(
        name,
        _DIRECTORY_FLAGS,
        dir_fd=parent_descriptor,
    )
    try:
        opened_status = os.fstat(descriptor)
        opened_identity = _identity(opened_status)
        if (
            not stat.S_ISDIR(opened_status.st_mode)
            or (expected is not None and opened_identity != expected)
        ):
            raise OSError(errno.ESTALE, "目录身份已变化")
        _assert_name_identity(
            parent_descriptor,
            name,
            opened_identity,
        )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_managed_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected_mount_id: int,
) -> int:
    try:
        descriptor = _open_directory_at(parent_descriptor, name)
    except OSError as exc:
        raise _managed_component_failure(
            parent_descriptor,
            name,
            exc,
        ) from exc
    try:
        if _descriptor_mount_id(descriptor) != expected_mount_id:
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: _Identity | None = None,
) -> tuple[int, os.stat_result]:
    before_status = os.stat(
        name,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISREG(before_status.st_mode)
        or before_status.st_nlink != 1
    ):
        raise OSError(errno.EINVAL, "受管文件不是常规文件")
    descriptor = os.open(
        name,
        _REGULAR_FILE_FLAGS,
        dir_fd=parent_descriptor,
    )
    try:
        opened_status = os.fstat(descriptor)
        opened_identity = _identity(opened_status)
        if (
            not stat.S_ISREG(opened_status.st_mode)
            or opened_status.st_nlink != 1
            or opened_identity != _identity(before_status)
            or (expected is not None and opened_identity != expected)
        ):
            raise OSError(errno.ESTALE, "文件身份已变化")
        _assert_name_identity(
            parent_descriptor,
            name,
            opened_identity,
        )
        return descriptor, opened_status
    except BaseException:
        os.close(descriptor)
        raise


def _assert_name_identity(
    parent_descriptor: int,
    name: str,
    expected: _Identity,
    *,
    access_failure: bool = False,
    cleanup_failure: bool = False,
) -> None:
    try:
        current_status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        if cleanup_failure:
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            ) from exc
        if access_failure:
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            ) from exc
        raise
    if _identity(current_status) != expected:
        if cleanup_failure:
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            )
        if access_failure:
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            )
        raise OSError(errno.ESTALE, "受管名称身份已变化")


def _create_directory_at(
    parent_descriptor: int,
    name: str,
    journal: list[_CreatedEntry],
) -> tuple[int, _Identity]:
    journal_parent = os.dup(parent_descriptor)
    descriptor = -1
    recorded = False
    staging_name = ""
    entry: _CreatedEntry | None = None
    try:
        for _ in range(16):
            candidate = f"{_CREATE_PREFIX}{secrets.token_hex(16)}"
            try:
                os.mkdir(
                    candidate,
                    mode=_DIRECTORY_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            staging_name = candidate
            break
        if not staging_name:
            raise OSError(errno.EEXIST, "无法分配受管创建目录")
        parent_status = os.fstat(parent_descriptor)
        created_status = os.lstat(
            staging_name,
            dir_fd=parent_descriptor,
        )
        if (
            not stat.S_ISDIR(created_status.st_mode)
            or created_status.st_dev != parent_status.st_dev
        ):
            raise OSError(errno.ENOTDIR, "新建受管目录类型不合法")
        identity = _identity(created_status)
        entry = _CreatedEntry(
            parent_descriptor=journal_parent,
            name=staging_name,
            identity=identity,
            is_directory=True,
        )
        journal.append(entry)
        recorded = True
        journal_parent = -1
        _assert_name_identity(
            parent_descriptor,
            staging_name,
            identity,
        )
        descriptor = os.open(
            staging_name,
            _DIRECTORY_FLAGS,
            dir_fd=parent_descriptor,
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or _identity(status) != identity
            or status.st_dev != parent_status.st_dev
            or _descriptor_mount_id(descriptor)
            != _descriptor_mount_id(parent_descriptor)
        ):
            raise OSError(errno.ENOTDIR, "新建受管目录类型不合法")
        _assert_name_identity(
            parent_descriptor,
            staging_name,
            identity,
        )
        os.fchmod(descriptor, _DIRECTORY_MODE)
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        _rename_no_replace(
            parent_descriptor,
            staging_name,
            parent_descriptor,
            name,
        )
        entry.name = name
        _assert_name_identity(
            parent_descriptor,
            name,
            identity,
        )
        os.fsync(parent_descriptor)
        return descriptor, identity
    except BaseException:
        if not recorded and journal_parent >= 0 and descriptor >= 0:
            try:
                fallback_status = os.fstat(descriptor)
                if stat.S_ISDIR(fallback_status.st_mode):
                    journal.append(
                        _CreatedEntry(
                            parent_descriptor=journal_parent,
                            name=staging_name,
                            identity=_identity(fallback_status),
                            is_directory=True,
                        )
                    )
                    journal_parent = -1
            except OSError:
                pass
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        if journal_parent >= 0:
            os.close(journal_parent)


def _create_sensitive_file_at(
    parent_descriptor: int,
    name: str,
    contents: bytes,
    journal: list[_CreatedEntry],
) -> None:
    journal_parent = os.dup(parent_descriptor)
    descriptor = -1
    created = False
    recorded = False
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            _SENSITIVE_FILE_MODE,
            dir_fd=parent_descriptor,
        )
        created = True
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise OSError(errno.EINVAL, "新建受管文件类型不合法")
        identity = _identity(status)
        journal.append(
            _CreatedEntry(
                parent_descriptor=journal_parent,
                name=name,
                identity=identity,
                is_directory=False,
            )
        )
        recorded = True
        journal_parent = -1
        os.fchmod(descriptor, _SENSITIVE_FILE_MODE)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("无法写入受管敏感文件")
            view = view[written:]
        os.fsync(descriptor)
        _assert_name_identity(parent_descriptor, name, identity)
        os.fsync(parent_descriptor)
    except BaseException:
        if created and not recorded and journal_parent >= 0:
            try:
                fallback_status = (
                    os.fstat(descriptor)
                    if descriptor >= 0
                    else os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                )
                if (
                    stat.S_ISREG(fallback_status.st_mode)
                    and fallback_status.st_nlink == 1
                ):
                    journal.append(
                        _CreatedEntry(
                            parent_descriptor=journal_parent,
                            name=name,
                            identity=_identity(fallback_status),
                            is_directory=False,
                        )
                    )
                    journal_parent = -1
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if journal_parent >= 0:
            os.close(journal_parent)


def _rollback_created_entries(
    journal: list[_CreatedEntry],
) -> WorkspaceFailure | None:
    failure: WorkspaceFailure | None = None
    for entry in reversed(journal):
        try:
            if failure is not None:
                continue
            _quarantine_and_remove(
                entry.parent_descriptor,
                entry.name,
                entry.identity,
                is_directory=entry.is_directory,
            )
            _sync_cleanup_directory(entry.parent_descriptor)
        except WorkspaceFailure as exc:
            failure = exc
        except PermissionError:
            failure = _workspace_cleanup_failure(
                "workspace.permission_denied"
            )
        except OSError:
            failure = _workspace_cleanup_failure(
                "workspace.remove_failed"
            )
        finally:
            close_failure = _unlock_and_close(
                entry.parent_descriptor,
                locked=False,
            )
            if close_failure is not None and failure is None:
                failure = _workspace_cleanup_failure(
                    "workspace.remove_failed"
                )
    journal.clear()
    return failure


def _discard_created_entries(journal: list[_CreatedEntry]) -> None:
    _close_descriptors(entry.parent_descriptor for entry in journal)
    journal.clear()


def _remove_directory_contents(
    descriptor: int,
    *,
    expected_device: int,
    expected_mount_id: int,
) -> None:
    try:
        descriptor_status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(descriptor_status.st_mode)
            or descriptor_status.st_dev != expected_device
            or _descriptor_mount_id(descriptor) != expected_mount_id
        ):
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            )
    except WorkspaceFailure:
        raise
    except OSError as exc:
        raise _workspace_cleanup_failure(
            "workspace.ownership_changed"
        ) from exc
    names = _directory_names(descriptor)
    for name in names:
        if name.startswith((".workspace-quarantine-", _CREATE_PREFIX)):
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            )
        try:
            before_status = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        before_identity = _identity(before_status)
        if stat.S_ISDIR(before_status.st_mode):
            if before_status.st_dev != expected_device:
                raise _workspace_cleanup_failure(
                    "workspace.ownership_changed"
                )
            try:
                child_descriptor = _open_directory_at(
                    descriptor,
                    name,
                    expected=before_identity,
                )
            except OSError as exc:
                raise _workspace_cleanup_failure(
                    "workspace.ownership_changed"
                ) from exc
            try:
                if (
                    _descriptor_mount_id(child_descriptor)
                    != expected_mount_id
                ):
                    raise _workspace_cleanup_failure(
                        "workspace.ownership_changed"
                    )
                _remove_directory_contents(
                    child_descriptor,
                    expected_device=expected_device,
                    expected_mount_id=expected_mount_id,
                )
                _sync_cleanup_directory(child_descriptor)
            finally:
                os.close(child_descriptor)
            _quarantine_and_remove(
                descriptor,
                name,
                before_identity,
                is_directory=True,
            )
        elif stat.S_ISREG(before_status.st_mode) or stat.S_ISLNK(
            before_status.st_mode
        ):
            _quarantine_and_remove(
                descriptor,
                name,
                before_identity,
                is_directory=False,
            )
        else:
            raise _workspace_cleanup_failure("workspace.ownership_changed")


def _quarantine_and_remove(
    parent_descriptor: int,
    name: str,
    expected_identity: _Identity,
    *,
    is_directory: bool,
) -> None:
    quarantine_name = ""
    quarantine_descriptor = -1
    quarantine_identity: _Identity | None = None
    entry_removed = False
    source_missing = False
    entry_moved = False
    try:
        for _ in range(16):
            candidate = f".workspace-quarantine-{secrets.token_hex(16)}"
            try:
                os.mkdir(
                    candidate,
                    mode=_DIRECTORY_MODE,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            quarantine_name = candidate
            break
        if not quarantine_name:
            raise OSError(errno.EEXIST, "无法分配受管隔离目录")
        quarantine_descriptor = _open_directory_at(
            parent_descriptor,
            quarantine_name,
        )
        quarantine_identity = _identity(os.fstat(quarantine_descriptor))
        _assert_name_identity(
            parent_descriptor,
            name,
            expected_identity,
            cleanup_failure=True,
        )
        try:
            _rename_no_replace(
                parent_descriptor,
                name,
                quarantine_descriptor,
                "entry",
            )
        except FileNotFoundError:
            source_missing = True
            return
        except OSError as exc:
            entry_moved = True
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            ) from exc
        entry_moved = True
        moved_status = os.stat(
            "entry",
            dir_fd=quarantine_descriptor,
            follow_symlinks=False,
        )
        if (
            _identity(moved_status) != expected_identity
            or stat.S_ISDIR(moved_status.st_mode) != is_directory
        ):
            try:
                _rename_no_replace(
                    quarantine_descriptor,
                    "entry",
                    parent_descriptor,
                    name,
                )
                _assert_name_identity(
                    parent_descriptor,
                    name,
                    _identity(moved_status),
                    cleanup_failure=True,
                )
                entry_moved = False
                _sync_cleanup_directory(quarantine_descriptor)
                _sync_cleanup_directory(parent_descriptor)
            except (OSError, WorkspaceFailure) as restore_failure:
                raise _workspace_cleanup_failure(
                    "workspace.ownership_changed"
                ) from restore_failure
            raise _workspace_cleanup_failure(
                "workspace.ownership_changed"
            )
        if is_directory:
            os.rmdir("entry", dir_fd=quarantine_descriptor)
        else:
            os.unlink("entry", dir_fd=quarantine_descriptor)
        entry_removed = True
        _sync_cleanup_directory(quarantine_descriptor)
    finally:
        if quarantine_descriptor >= 0:
            try:
                if entry_moved and not entry_removed:
                    _sync_cleanup_directory(quarantine_descriptor)
                    _sync_cleanup_directory(parent_descriptor)
            finally:
                os.close(quarantine_descriptor)
        if (
            quarantine_name
            and quarantine_identity is not None
            and (entry_removed or source_missing or not entry_moved)
        ):
            _assert_name_identity(
                parent_descriptor,
                quarantine_name,
                quarantine_identity,
                cleanup_failure=True,
            )
            os.rmdir(quarantine_name, dir_fd=parent_descriptor)
            _sync_cleanup_directory(parent_descriptor)


def _rename_no_replace(
    source_parent_descriptor: int,
    source_name: str,
    target_parent_descriptor: int,
    target_name: str,
) -> None:
    """以 Linux renameat2 的 no-replace 语义恢复隔离对象。"""
    try:
        renameat2 = _LIBC.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "系统缺少 renameat2") from exc
    result = renameat2(
        source_parent_descriptor,
        ctypes.c_char_p(os.fsencode(source_name)),
        target_parent_descriptor,
        ctypes.c_char_p(os.fsencode(target_name)),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            target_name,
        )


def _sync_file_data(descriptor: int) -> None:
    sync = getattr(os, "fdatasync", os.fsync)
    sync(descriptor)


def _remove_atomic_publish_temporary(
    parent_descriptor: int,
    name: str,
    expected_identity: _Identity,
) -> None:
    try:
        try:
            status = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            ) from None
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or _identity(status) != expected_identity
        ):
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            )
        _assert_name_identity(
            parent_descriptor,
            name,
            expected_identity,
            access_failure=True,
        )
        try:
            os.unlink(name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            raise _workspace_access_failure(
                "workspace.ownership_changed"
            ) from None
        os.fsync(parent_descriptor)
    except WorkspaceFailure:
        raise
    except PermissionError as exc:
        raise _workspace_access_failure(
            "workspace.permission_denied"
        ) from exc
    except OSError as exc:
        raise _workspace_access_failure("workspace.io_failed") from exc


def _directory_is_empty(descriptor: int) -> bool:
    return not _directory_names(descriptor)


def _assert_exact_directory_names(
    descriptor: int,
    expected_names: set[str],
) -> None:
    actual_names = set(_directory_names(descriptor))
    if actual_names != expected_names:
        raise _invalid_workspace_marker()


def _directory_names(descriptor: int) -> list[str]:
    """用独立 open-file description 扫描，避免并发副本共享目录偏移。"""
    scan_descriptor = os.open(
        ".",
        _DIRECTORY_FLAGS,
        dir_fd=descriptor,
    )
    try:
        with os.scandir(scan_descriptor) as entries:
            return [entry.name for entry in entries]
    finally:
        os.close(scan_descriptor)


def _validate_effect_parts(
    parts: tuple[str, ...],
    *,
    allow_empty: bool,
) -> None:
    if not isinstance(parts, tuple) or (not parts and not allow_empty):
        raise ValueError("受管路径段不合法")
    if any(
        not isinstance(part, str)
        or not part
        or part in {".", ".."}
        or part.startswith((".workspace-quarantine-", _CREATE_PREFIX))
        or len(part.encode("utf-8")) > 255
        or "/" in part
        or "\\" in part
        or "\x00" in part
        or not part.isprintable()
        or unicodedata.normalize("NFC", part) != part
        for part in parts
    ):
        raise ValueError("受管路径段不合法")


def _identity(status: os.stat_result) -> _Identity:
    return status.st_dev, status.st_ino


def _managed_component_failure(
    parent_descriptor: int,
    name: str,
    exception: OSError,
) -> BaseException:
    if isinstance(exception, FileNotFoundError):
        return FileNotFoundError(
            errno.ENOENT,
            "受管位置不存在",
        )
    if isinstance(exception, FileExistsError):
        return FileExistsError(
            errno.EEXIST,
            "受管位置已经存在",
        )
    if isinstance(exception, PermissionError):
        return _workspace_access_failure("workspace.permission_denied")
    return _workspace_access_failure(
        _managed_reason_code(parent_descriptor, name, exception)
    )


def _managed_reason_code(
    parent_descriptor: int,
    name: str,
    exception: OSError,
) -> str:
    if name:
        try:
            status = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            status = None
        if status is not None and stat.S_ISLNK(status.st_mode):
            return "workspace.symlink_encountered"
    if exception.errno == errno.ELOOP:
        return "workspace.symlink_encountered"
    if exception.errno in {
        errno.EINVAL,
        errno.EISDIR,
        errno.ENOTDIR,
        errno.ESTALE,
    }:
        return "workspace.ownership_changed"
    return "workspace.io_failed"


def _invalid_workspace_marker() -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        {
            "component": "workspace",
            "operation": "workspace.verify",
            "reason_code": "filesystem.marker_invalid",
        },
    )


def _workspace_not_directory() -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        {
            "component": "workspace",
            "operation": "workspace.verify",
            "reason_code": "filesystem.not_directory",
        },
    )


def _workspace_lock_failure() -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        {
            "component": "workspace",
            "operation": "workspace.lock",
            "reason_code": "filesystem.lock_failed",
        },
    )


def _workspace_access_failure(reason_code: str) -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        {
            "component": "workspace",
            "operation": "workspace.access",
            "reason_code": reason_code,
        },
    )


def _insecure_workspace_permissions() -> WorkspaceFailure:
    return _workspace_filesystem_failure(
        operation="workspace.verify",
        reason_code="filesystem.permission_denied",
    )


def _workspace_filesystem_failure(
    *,
    operation: str,
    reason_code: str,
) -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        {
            "component": "workspace",
            "operation": operation,
            "reason_code": reason_code,
        },
    )


def _workspace_cleanup_failure(reason_code: str) -> WorkspaceFailure:
    return WorkspaceFailure(
        ErrorCode.WORKSPACE_CLEANUP_FAILED,
        {
            "operation": "workspace.cleanup",
            "reason_code": reason_code,
        },
    )


def _sync_cleanup_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except PermissionError as exc:
        raise _workspace_cleanup_failure(
            "workspace.permission_denied"
        ) from exc
    except OSError as exc:
        raise _workspace_cleanup_failure(
            "workspace.directory_sync_failed"
        ) from exc


def _release_lock(handle: _LockHandle) -> None:
    failure = _unlock_and_close(handle.lock_descriptor, locked=True)
    root_failure = _unlock_and_close(handle.root_descriptor, locked=True)
    if failure is not None:
        raise failure
    if root_failure is not None:
        raise root_failure


def _unlock_and_close(
    descriptor: int,
    *,
    locked: bool,
) -> BaseException | None:
    if descriptor < 0:
        return None
    failure: BaseException | None = None
    if locked:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as exc:  # noqa: BLE001 - 仍须释放另一个锁
            failure = exc
    try:
        os.close(descriptor)
    except BaseException as exc:  # noqa: BLE001 - 仍须释放另一个锁
        if failure is None:
            failure = exc
    return failure


def _close_descriptors(
    descriptors: Iterable[int],
) -> BaseException | None:
    failure: BaseException | None = None
    for descriptor in descriptors:
        close_failure = _unlock_and_close(
            descriptor,
            locked=False,
        )
        if failure is None and close_failure is not None:
            failure = close_failure
    return failure
