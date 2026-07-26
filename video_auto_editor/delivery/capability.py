"""标准交付物按构建、验证、发布顺序推进的不可变能力。"""

from dataclasses import dataclass
from enum import Enum
from os import PathLike
from pathlib import Path

from video_auto_editor.runtime.identity import RunId


class ManagedDirectoryRole(str, Enum):
    """受管目录在交付状态机中的封闭角色。"""

    DELIVERY_STAGING = "delivery_staging"
    PUBLISHED_DELIVERY = "published_delivery"


@dataclass(frozen=True, slots=True, init=False)
class ManagedDirectoryCapability(PathLike[str]):
    """只能由 Workspace 内部签发的不可变受管目录能力。"""

    _path: Path
    _workspace_identity: object
    role: ManagedDirectoryRole

    def __new__(cls) -> "ManagedDirectoryCapability":
        raise TypeError("ManagedDirectoryCapability 只能由 Workspace 签发")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ManagedDirectoryCapability 不能由 Workspace 之外的模块实现")

    @classmethod
    def _from_workspace(
        cls,
        path: PathLike[str] | str,
        *,
        workspace_identity: object,
        role: ManagedDirectoryRole,
    ) -> "ManagedDirectoryCapability":
        """供 Workspace 模块在完成归属和路径校验后签发。"""
        if not isinstance(role, ManagedDirectoryRole):
            raise TypeError("受管目录 capability 必须使用 ManagedDirectoryRole")
        try:
            normalized_path = Path(path)
        except TypeError as exc:
            raise TypeError("受管目录 capability 必须绑定字符串路径") from exc
        if not normalized_path.is_absolute() or ".." in normalized_path.parts:
            raise ValueError("受管目录 capability 必须绑定规范绝对路径")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_path", normalized_path)
        object.__setattr__(instance, "_workspace_identity", workspace_identity)
        object.__setattr__(instance, "role", role)
        return instance

    def __fspath__(self) -> str:
        return str(self._path)

    @property
    def path(self) -> Path:
        """返回不可变的规范路径快照。"""
        return self._path

    def _belongs_to_same_workspace(
        self,
        other: "ManagedDirectoryCapability",
    ) -> bool:
        return self._workspace_identity is other._workspace_identity


@dataclass(frozen=True, slots=True)
class _DeliveryBinding:
    run_id: RunId
    managed_directory: ManagedDirectoryCapability


def _binding(
    run_id: RunId,
    managed_directory: ManagedDirectoryCapability,
    expected_role: ManagedDirectoryRole,
) -> _DeliveryBinding:
    if not isinstance(run_id, RunId):
        raise TypeError("交付 capability 必须绑定 RunId")
    if not isinstance(managed_directory, ManagedDirectoryCapability):
        raise TypeError("交付 capability 必须绑定 Workspace 签发的受管目录 capability")
    if managed_directory.role is not expected_role:
        expected_name = {
            ManagedDirectoryRole.DELIVERY_STAGING: "未发布交付暂存目录",
            ManagedDirectoryRole.PUBLISHED_DELIVERY: "已发布交付目录",
        }[expected_role]
        raise ValueError(f"交付 capability 必须绑定{expected_name}")
    return _DeliveryBinding(run_id=run_id, managed_directory=managed_directory)


def _snapshot(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("验证快照标识必须是字符串")
    if not value or len(value) > 256:
        raise ValueError("验证快照标识必须是 1 到 256 个字符")
    if not value.isprintable():
        raise ValueError("验证快照标识只能包含可打印字符")
    return value


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
            ),
        )
        return instance

    @property
    def run_id(self) -> RunId:
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
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
    ) -> "VerifiedDelivery":
        """只接受未验证交付并绑定不可变验证快照。"""
        if not isinstance(delivery, UnverifiedDelivery):
            raise TypeError("完整性验证只能推进 UnverifiedDelivery")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_binding", delivery._binding)
        object.__setattr__(
            instance,
            "verification_snapshot",
            _snapshot(verification_snapshot),
        )
        return instance

    @property
    def run_id(self) -> RunId:
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
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
        if not isinstance(published_directory, ManagedDirectoryCapability):
            raise TypeError("发布必须绑定 Workspace 签发的受管目录 capability")
        if not delivery.managed_directory._belongs_to_same_workspace(
            published_directory
        ):
            raise ValueError("发布前后目录必须属于同一个 Workspace")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_binding",
            _binding(
                delivery.run_id,
                published_directory,
                ManagedDirectoryRole.PUBLISHED_DELIVERY,
            ),
        )
        object.__setattr__(
            instance,
            "verification_snapshot",
            delivery.verification_snapshot,
        )
        return instance

    @property
    def run_id(self) -> RunId:
        return self._binding.run_id

    @property
    def managed_directory(self) -> ManagedDirectoryCapability:
        return self._binding.managed_directory
