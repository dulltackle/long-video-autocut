"""标准交付物按构建、验证、发布顺序推进的不可变能力。"""

from dataclasses import dataclass
from typing import Literal
from weakref import WeakKeyDictionary

from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    ManagedTreeEntry,
)

_DeliveryState = Literal[
    "unverified",
    "verified",
    "publishing",
    "published",
]


@dataclass(frozen=True, slots=True)
class _DeliveryAuthority:
    state: _DeliveryState
    run_id: RunId
    managed_directory: ManagedDirectoryCapability
    verification_snapshot: str | None
    verification_tree: tuple[ManagedTreeEntry, ...] | None


@dataclass(frozen=True, init=False, eq=False)
class _DeliveryBinding:
    run_id: RunId
    managed_directory: ManagedDirectoryCapability

    def __new__(cls) -> "_DeliveryBinding":
        raise TypeError("交付绑定只能由交付状态转换创建")


_DELIVERY_AUTHORITIES: WeakKeyDictionary[
    _DeliveryBinding,
    _DeliveryAuthority,
] = WeakKeyDictionary()


def _binding(
    run_id: RunId,
    managed_directory: ManagedDirectoryCapability,
    expected_role: ManagedDirectoryRole,
    *,
    state: _DeliveryState,
    verification_snapshot: str | None = None,
    verification_tree: tuple[ManagedTreeEntry, ...] | None = None,
) -> _DeliveryBinding:
    if not isinstance(run_id, RunId):
        raise TypeError("交付 capability 必须绑定 RunId")
    if not isinstance(managed_directory, ManagedDirectoryCapability):
        raise TypeError("交付 capability 必须绑定 Workspace 签发的受管目录 capability")
    managed_directory._assert_authentic()
    if managed_directory.role is not expected_role:
        expected_name = {
            ManagedDirectoryRole.DELIVERY_STAGING: "未发布交付暂存目录",
            ManagedDirectoryRole.PUBLISHED_DELIVERY: "已发布交付目录",
        }[expected_role]
        raise ValueError(f"交付 capability 必须绑定{expected_name}")
    managed_directory._assert_bound_to_run(run_id)
    managed_directory._assert_current_directory()
    instance = object.__new__(_DeliveryBinding)
    object.__setattr__(instance, "run_id", run_id)
    object.__setattr__(
        instance,
        "managed_directory",
        managed_directory,
    )
    _DELIVERY_AUTHORITIES[instance] = _DeliveryAuthority(
        state=state,
        run_id=run_id,
        managed_directory=managed_directory,
        verification_snapshot=verification_snapshot,
        verification_tree=verification_tree,
    )
    return instance


def _delivery_authority(
    binding: _DeliveryBinding,
    *,
    expected_state: _DeliveryState,
    message: str,
) -> _DeliveryAuthority:
    try:
        authority = _DELIVERY_AUTHORITIES[binding]
    except (KeyError, TypeError) as exc:
        raise TypeError(message) from exc
    if (
        authority.state != expected_state
        or binding.run_id != authority.run_id
        or binding.managed_directory is not authority.managed_directory
    ):
        raise TypeError(message)
    return authority


def _snapshot(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("验证快照标识必须是字符串")
    if not value or len(value) > 256:
        raise ValueError("验证快照标识必须是 1 到 256 个字符")
    if not value.isprintable():
        raise ValueError("验证快照标识只能包含可打印字符")
    return value


def _tree_snapshot(
    value: tuple[ManagedTreeEntry, ...] | None,
) -> tuple[ManagedTreeEntry, ...] | None:
    if value is None:
        return None
    if not isinstance(value, tuple) or any(
        not isinstance(item, ManagedTreeEntry) for item in value
    ):
        raise TypeError("验证目录快照必须是受管目录树条目元组")
    return value


def _verification_evidence(
    delivery: "VerifiedDelivery",
) -> tuple[str, tuple[ManagedTreeEntry, ...] | None]:
    """供 Publication 重新确认验证时绑定的内容与目录树。"""
    if not isinstance(delivery, VerifiedDelivery):
        raise TypeError("发布只能读取 VerifiedDelivery 的验证证据")
    authority = _delivery_authority(
        delivery._binding,
        expected_state="verified",
        message="VerifiedDelivery 只能由 DeliveryVerification 创建",
    )
    snapshot = authority.verification_snapshot
    if snapshot is None:
        raise TypeError("VerifiedDelivery 只能由 DeliveryVerification 创建")
    return snapshot, authority.verification_tree


def validate_publication_commit_proof(
    delivery: "PublishedDelivery",
    *,
    expected_run_id: RunId,
    expected_directory: ManagedDirectoryCapability,
) -> None:
    """验证发布提交回调的临时证明绑定到预期运行与最终目录。"""
    if not isinstance(delivery, PublishedDelivery):
        raise TypeError("发布提交回调只接受 PublishedDelivery")
    authority = _delivery_authority(
        delivery._binding,
        expected_state="publishing",
        message="PublishedDelivery 只能由 Publication 创建",
    )
    if authority.run_id != expected_run_id:
        raise ValueError("发布提交证明必须属于当前运行")
    if authority.managed_directory is not expected_directory:
        raise ValueError("发布提交证明必须绑定当前最终交付目录")


def _activate_publication(delivery: "PublishedDelivery") -> None:
    """只在目录事务越过提交点后激活最终交付能力。"""
    if not isinstance(delivery, PublishedDelivery):
        raise TypeError("只能激活 Publication 创建的 PublishedDelivery")
    authority = _delivery_authority(
        delivery._binding,
        expected_state="publishing",
        message="PublishedDelivery 只能由 Publication 创建",
    )
    _DELIVERY_AUTHORITIES[delivery._binding] = _DeliveryAuthority(
        state="published",
        run_id=authority.run_id,
        managed_directory=authority.managed_directory,
        verification_snapshot=authority.verification_snapshot,
        verification_tree=authority.verification_tree,
    )


def _revoke_publication(delivery: "PublishedDelivery") -> None:
    """撤销没有越过提交点却被回调暂时观察到的发布证明。"""
    if not isinstance(delivery, PublishedDelivery):
        raise TypeError("只能撤销 Publication 创建的 PublishedDelivery")
    _delivery_authority(
        delivery._binding,
        expected_state="publishing",
        message="PublishedDelivery 只能由 Publication 创建",
    )
    del _DELIVERY_AUTHORITIES[delivery._binding]


@dataclass(frozen=True, slots=True, init=False)
class UnverifiedDelivery:
    """构建完成、尚未通过独立完整性验证的标准交付物。"""

    _binding: _DeliveryBinding

    def __new__(cls) -> "UnverifiedDelivery":
        raise TypeError("UnverifiedDelivery 只能由 DeliveryBuild 创建")

    @classmethod
    def _from_build(
        cls,
        run_id: RunId,
        managed_directory: ManagedDirectoryCapability,
    ) -> "UnverifiedDelivery":
        """只表达构建完成，不授予发布能力。"""
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_binding",
            _binding(
                run_id,
                managed_directory,
                ManagedDirectoryRole.DELIVERY_STAGING,
                state="unverified",
            ),
        )
        return instance

    @property
    def run_id(self) -> RunId:
        _delivery_authority(
            self._binding,
            expected_state="unverified",
            message="UnverifiedDelivery 只能由 DeliveryBuild 创建",
        )
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
        _delivery_authority(
            self._binding,
            expected_state="unverified",
            message="UnverifiedDelivery 只能由 DeliveryBuild 创建",
        )
        return self._binding.managed_directory


@dataclass(frozen=True, slots=True, init=False)
class VerifiedDelivery:
    """独立完整性验证通过、可以进入发布模块的标准交付物。"""

    _binding: _DeliveryBinding
    verification_snapshot: str

    def __new__(cls) -> "VerifiedDelivery":
        raise TypeError("VerifiedDelivery 只能由 DeliveryVerification 创建")

    @classmethod
    def _from_verification(
        cls,
        delivery: UnverifiedDelivery,
        *,
        verification_snapshot: str,
        verification_tree: tuple[ManagedTreeEntry, ...] | None = None,
    ) -> "VerifiedDelivery":
        """只接受未验证交付并绑定不可变验证快照。"""
        if not isinstance(delivery, UnverifiedDelivery):
            raise TypeError("完整性验证只能推进 UnverifiedDelivery")
        _delivery_authority(
            delivery._binding,
            expected_state="unverified",
            message="UnverifiedDelivery 只能由 DeliveryBuild 创建",
        )
        delivery.managed_directory._assert_current_directory()
        snapshot = _snapshot(verification_snapshot)
        tree = _tree_snapshot(verification_tree)
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_binding",
            _binding(
                delivery.run_id,
                delivery.managed_directory,
                ManagedDirectoryRole.DELIVERY_STAGING,
                state="verified",
                verification_snapshot=snapshot,
                verification_tree=tree,
            ),
        )
        object.__setattr__(instance, "verification_snapshot", snapshot)
        return instance

    @property
    def run_id(self) -> RunId:
        _delivery_authority(
            self._binding,
            expected_state="verified",
            message="VerifiedDelivery 只能由 DeliveryVerification 创建",
        )
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
        _delivery_authority(
            self._binding,
            expected_state="verified",
            message="VerifiedDelivery 只能由 DeliveryVerification 创建",
        )
        return self._binding.managed_directory


@dataclass(frozen=True, slots=True, init=False)
class PublishedDelivery:
    """已经越过发布提交点、对操作员完整可见的标准交付物。"""

    _binding: _DeliveryBinding
    verification_snapshot: str

    def __new__(cls) -> "PublishedDelivery":
        raise TypeError("PublishedDelivery 只能由 Publication 创建")

    @classmethod
    def _from_publication(
        cls,
        delivery: VerifiedDelivery,
        *,
        published_directory: ManagedDirectoryCapability,
    ) -> "PublishedDelivery":
        """只接受已验证交付，并绑定提交点后的最终受管目录。"""
        if not isinstance(delivery, VerifiedDelivery):
            raise TypeError("发布只能推进 VerifiedDelivery")
        authority = _delivery_authority(
            delivery._binding,
            expected_state="verified",
            message="VerifiedDelivery 只能由 DeliveryVerification 创建",
        )
        if not isinstance(published_directory, ManagedDirectoryCapability):
            raise TypeError("发布必须绑定 Workspace 签发的受管目录 capability")
        published_directory._assert_current_directory()
        if not delivery.managed_directory._belongs_to_same_workspace(
            published_directory
        ):
            raise ValueError("发布前后目录必须属于同一个 Workspace")
        verification_snapshot = authority.verification_snapshot
        if verification_snapshot is None:
            raise TypeError("VerifiedDelivery 只能由 DeliveryVerification 创建")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_binding",
            _binding(
                delivery.run_id,
                published_directory,
                ManagedDirectoryRole.PUBLISHED_DELIVERY,
                state="published",
                verification_snapshot=verification_snapshot,
                verification_tree=authority.verification_tree,
            ),
        )
        object.__setattr__(
            instance,
            "verification_snapshot",
            verification_snapshot,
        )
        return instance

    @classmethod
    def _prepare_publication(
        cls,
        delivery: VerifiedDelivery,
        *,
        published_directory: ManagedDirectoryCapability,
    ) -> "PublishedDelivery":
        """形成提交回调可验证、但尚不能使用最终目录的临时证明。"""
        instance = cls._from_publication(
            delivery,
            published_directory=published_directory,
        )
        authority = _delivery_authority(
            instance._binding,
            expected_state="published",
            message="PublishedDelivery 只能由 Publication 创建",
        )
        _DELIVERY_AUTHORITIES[instance._binding] = _DeliveryAuthority(
            state="publishing",
            run_id=authority.run_id,
            managed_directory=authority.managed_directory,
            verification_snapshot=authority.verification_snapshot,
            verification_tree=authority.verification_tree,
        )
        return instance

    @property
    def run_id(self) -> RunId:
        _delivery_authority(
            self._binding,
            expected_state="published",
            message="PublishedDelivery 只能由 Publication 创建",
        )
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
        _delivery_authority(
            self._binding,
            expected_state="published",
            message="PublishedDelivery 只能由 Publication 创建",
        )
        return self._binding.managed_directory
