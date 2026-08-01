"""把已验证交付以全有或全无语义切换为当前交付。"""

import hashlib
from collections.abc import Callable, Mapping
from threading import get_ident
from typing import Any

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.workspace import (
    ManagedBinaryFile,
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    ManagedTreeEntry,
    ManagedTreeEntryKind,
    WorkspaceFailure,
)

from .capability import (
    PublishedDelivery,
    VerifiedDelivery,
    _activate_publication,
    _revoke_publication,
    _verification_evidence,
)

_PUBLICATION_FAILURE_CODES = frozenset(
    {
        ErrorCode.PUBLICATION_BACKUP_FAILED,
        ErrorCode.PUBLICATION_COMMIT_FAILED,
        ErrorCode.PUBLICATION_ROLLBACK_FAILED,
    }
)
_Commit = Callable[
    [PublishedDelivery, Callable[[], None]],
    None,
]


class PublicationFailure(RuntimeError):
    """不包含物理路径或交付正文的稳定发布失败。"""

    __slots__ = (
        "category",
        "diagnostics",
        "error_code",
        "retryable_in_new_run",
        "safe_message",
    )

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, Any],
    ) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("发布失败必须使用稳定 ErrorCode")
        if error_code not in _PUBLICATION_FAILURE_CODES:
            raise ValueError("错误码不属于发布模块允许的稳定失败")
        definition = get_error_definition(error_code)
        self.error_code = error_code
        self.category: ErrorCategory = definition.category
        self.safe_message = definition.safe_message
        self.retryable_in_new_run = definition.retryable_in_new_run
        self.diagnostics = freeze_error_diagnostics(
            error_code,
            diagnostics,
        )
        super().__init__(self.safe_message)


class Publication:
    """隐藏验证快照复核、目录交换、耐久同步与失败回滚。"""

    __slots__ = ()

    @classmethod
    def check_destination(
        cls,
        published_directory: ManagedDirectoryCapability,
        *,
        overwrite: bool,
        cancellation: CancellationToken,
    ) -> None:
        """在业务工作前拒绝未授权的非空目标。"""
        _validate_destination_request(
            published_directory,
            overwrite=overwrite,
            cancellation=cancellation,
        )
        try:
            cancellation.raise_if_cancelled()
            published_directory._assert_current_directory()
            _assert_destination_available(
                published_directory,
                overwrite=overwrite,
            )
        except (CancellationRequested, PublicationFailure):
            raise
        except (OSError, WorkspaceFailure):
            raise _binding_failure() from None

    @classmethod
    def publish(
        cls,
        delivery: VerifiedDelivery,
        *,
        published_directory: ManagedDirectoryCapability,
        previous_directory: ManagedDirectoryCapability,
        overwrite: bool,
        cancellation: CancellationToken,
        commit: _Commit,
    ) -> PublishedDelivery:
        """复核验证事实，并只通过调用方提交临界区切换完整目录。"""
        _validate_publish_request(
            delivery,
            published_directory=published_directory,
            previous_directory=previous_directory,
            overwrite=overwrite,
            cancellation=cancellation,
            commit=commit,
        )
        try:
            return cls._publish(
                delivery,
                published_directory=published_directory,
                previous_directory=previous_directory,
                overwrite=overwrite,
                cancellation=cancellation,
                commit=commit,
            )
        except (CancellationRequested, PublicationFailure):
            raise
        except WorkspaceFailure as failure:
            if failure.error_code in _PUBLICATION_FAILURE_CODES:
                raise PublicationFailure(
                    failure.error_code,
                    failure.diagnostics,
                ) from None
            raise _binding_failure() from None
        except OSError:
            raise _binding_failure() from None

    @classmethod
    def _publish(
        cls,
        delivery: VerifiedDelivery,
        *,
        published_directory: ManagedDirectoryCapability,
        previous_directory: ManagedDirectoryCapability,
        overwrite: bool,
        cancellation: CancellationToken,
        commit: _Commit,
    ) -> PublishedDelivery:
        cancellation.raise_if_cancelled()
        _assert_directory_bindings(
            delivery,
            published_directory=published_directory,
            previous_directory=previous_directory,
        )
        _assert_destination_available(
            published_directory,
            overwrite=overwrite,
        )
        expected_snapshot, expected_tree = _verification_evidence(delivery)
        if expected_tree is None:
            raise _snapshot_failure()
        _assert_verification_snapshot(
            delivery.managed_directory,
            expected_snapshot=expected_snapshot,
            expected_tree=expected_tree,
            cancellation=cancellation,
        )
        cancellation.raise_if_cancelled()
        _assert_directory_bindings(
            delivery,
            published_directory=published_directory,
            previous_directory=previous_directory,
        )
        _assert_destination_available(
            published_directory,
            overwrite=overwrite,
        )

        published = PublishedDelivery._prepare_publication(
            delivery,
            published_directory=published_directory,
        )
        commit_reached = False
        callback_active = True
        callback_thread = get_ident()

        def effect() -> None:
            nonlocal commit_reached
            if get_ident() != callback_thread:
                raise RuntimeError("发布提交效果必须在提交回调线程执行")
            if not callback_active:
                raise RuntimeError("发布提交效果只能在提交回调作用域内执行")
            if commit_reached:
                raise RuntimeError("发布提交效果只能执行一次")
            cancellation.raise_if_cancelled()
            _assert_directory_bindings(
                delivery,
                published_directory=published_directory,
                previous_directory=previous_directory,
            )
            _assert_destination_available(
                published_directory,
                overwrite=overwrite,
            )
            _assert_verification_tree(
                delivery.managed_directory,
                expected_tree=expected_tree,
            )
            delivery.managed_directory._publish_as_current_delivery(
                published_directory,
                previous_directory,
                overwrite=overwrite,
                verification_tree=expected_tree,
                cancellation=cancellation,
            )
            commit_reached = True

        try:
            try:
                commit(published, effect)
            finally:
                callback_active = False
        except BaseException:
            if commit_reached:
                _activate_publication(published)
                return published
            _revoke_publication(published)
            raise
        if not commit_reached:
            _revoke_publication(published)
            raise RuntimeError("发布提交回调没有执行提交效果")
        _activate_publication(published)
        return published


def _validate_destination_request(
    published_directory: ManagedDirectoryCapability,
    *,
    overwrite: bool,
    cancellation: CancellationToken,
) -> None:
    if not isinstance(published_directory, ManagedDirectoryCapability):
        raise TypeError("发布目标必须是 Workspace 签发的受管目录")
    published_directory._assert_authentic()
    if (
        published_directory.role
        is not ManagedDirectoryRole.PUBLISHED_DELIVERY
    ):
        raise ValueError("发布目标必须是当前标准交付目录")
    if not isinstance(overwrite, bool):
        raise TypeError("覆盖发布选项必须是布尔值")
    if not isinstance(cancellation, CancellationToken):
        raise TypeError("发布必须绑定根取消令牌")


def _validate_publish_request(
    delivery: VerifiedDelivery,
    *,
    published_directory: ManagedDirectoryCapability,
    previous_directory: ManagedDirectoryCapability,
    overwrite: bool,
    cancellation: CancellationToken,
    commit: _Commit,
) -> None:
    if not isinstance(delivery, VerifiedDelivery):
        raise TypeError("发布只接受 VerifiedDelivery")
    _validate_destination_request(
        published_directory,
        overwrite=overwrite,
        cancellation=cancellation,
    )
    if not isinstance(previous_directory, ManagedDirectoryCapability):
        raise TypeError("上一版必须是 Workspace 签发的受管目录")
    previous_directory._assert_authentic()
    if previous_directory.role is not ManagedDirectoryRole.PREVIOUS_DELIVERY:
        raise ValueError("上一版必须绑定固定上一版交付目录")
    if not callable(commit):
        raise TypeError("发布提交临界区必须可调用")


def _assert_directory_bindings(
    delivery: VerifiedDelivery,
    *,
    published_directory: ManagedDirectoryCapability,
    previous_directory: ManagedDirectoryCapability,
) -> None:
    staging_directory = delivery.managed_directory
    for directory in (
        staging_directory,
        published_directory,
        previous_directory,
    ):
        directory._assert_current_directory()
        directory._assert_bound_to_run(delivery.run_id)
    if not (
        staging_directory._belongs_to_same_workspace(published_directory)
        and staging_directory._belongs_to_same_workspace(previous_directory)
    ):
        raise _binding_failure()


def _assert_destination_available(
    published_directory: ManagedDirectoryCapability,
    *,
    overwrite: bool,
) -> None:
    if not overwrite and published_directory.inspect_tree():
        raise PublicationFailure(
            ErrorCode.PUBLICATION_COMMIT_FAILED,
            {
                "operation": "publication.verify_binding",
                "reason_code": "publication.destination_not_empty",
            },
        )


def _assert_verification_snapshot(
    directory: ManagedDirectoryCapability,
    *,
    expected_snapshot: str,
    expected_tree: tuple[ManagedTreeEntry, ...],
    cancellation: CancellationToken,
) -> None:
    before_tree = directory.inspect_tree()
    if before_tree != expected_tree:
        raise _snapshot_failure()
    actual_snapshot = _delivery_snapshot(
        directory,
        before_tree,
        cancellation=cancellation,
    )
    after_tree = directory.inspect_tree()
    if (
        after_tree != before_tree
        or after_tree != expected_tree
        or actual_snapshot != expected_snapshot
    ):
        raise _snapshot_failure()


def _assert_verification_tree(
    directory: ManagedDirectoryCapability,
    *,
    expected_tree: tuple[ManagedTreeEntry, ...],
) -> None:
    if directory.inspect_tree() != expected_tree:
        raise _snapshot_failure()


def _delivery_snapshot(
    directory: ManagedDirectoryCapability,
    tree: tuple[ManagedTreeEntry, ...],
    *,
    cancellation: CancellationToken,
) -> str:
    snapshot = hashlib.sha256(b"delivery_snapshot.v1\0")
    files = sorted(
        (
            entry
            for entry in tree
            if entry.kind is ManagedTreeEntryKind.REGULAR_FILE
        ),
        key=lambda entry: entry.relative_path,
    )
    for entry in files:
        cancellation.raise_if_cancelled()
        byte_length, digest = directory.location(
            entry.relative_path
        ).use_binary(
            "rb",
            lambda stream: _hash_file(stream, cancellation),
        )
        if entry.byte_length != byte_length:
            raise _snapshot_failure()
        path_bytes = entry.relative_path.encode("utf-8")
        snapshot.update(len(path_bytes).to_bytes(8, "big"))
        snapshot.update(path_bytes)
        snapshot.update(byte_length.to_bytes(8, "big"))
        snapshot.update(digest)
    cancellation.raise_if_cancelled()
    return "sha256:" + snapshot.hexdigest()


def _hash_file(
    stream: ManagedBinaryFile,
    cancellation: CancellationToken,
) -> tuple[int, bytes]:
    digest = hashlib.sha256()
    byte_length = 0
    while True:
        cancellation.raise_if_cancelled()
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return byte_length, digest.digest()
        digest.update(chunk)
        byte_length += len(chunk)


def _binding_failure() -> PublicationFailure:
    return PublicationFailure(
        ErrorCode.PUBLICATION_COMMIT_FAILED,
        {
            "operation": "publication.verify_binding",
            "reason_code": "publication.binding_changed",
        },
    )


def _snapshot_failure() -> PublicationFailure:
    return PublicationFailure(
        ErrorCode.PUBLICATION_COMMIT_FAILED,
        {
            "operation": "publication.verify_snapshot",
            "reason_code": "publication.snapshot_changed",
        },
    )
