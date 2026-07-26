"""只能由 Workspace 签发的受管位置 capability。"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import BinaryIO, Literal, TypeVar, cast
from weakref import WeakKeyDictionary

from video_auto_editor.runtime.identity import RunId

from ._failure import _without_sensitive_exception_context


class ManagedDirectoryRole(str, Enum):
    """受管目录的封闭业务角色。"""

    CACHE = "cache"
    RUN_DIAGNOSTICS = "run_diagnostics"
    RUN_TEMPORARY = "run_temporary"
    DELIVERY_STAGING = "delivery_staging"
    PUBLISHED_DELIVERY = "published_delivery"
    PREVIOUS_DELIVERY = "previous_delivery"


_FileMode = Literal["rb", "wb", "xb", "ab"]
_ResultT = TypeVar("_ResultT")
_FileEffect = Callable[[BinaryIO], object]
_UseFile = Callable[[tuple[str, ...], _FileMode, _FileEffect], object]
_PublishFile = Callable[[tuple[str, ...], bytes], int]
_ValidateDirectory = Callable[[tuple[str, ...]], None]
_MakeDirectory = Callable[[tuple[str, ...]], None]
_CAPABILITY_SEAL = object()


class ManagedBinaryFile:
    """只在一次受管效果调用内有效、且不暴露文件描述符的二进制流。"""

    __slots__ = ("__stream",)

    __stream: BinaryIO | None

    def __new__(cls) -> "ManagedBinaryFile":
        raise TypeError("ManagedBinaryFile 只能由受管文件效果创建")

    @property
    def closed(self) -> bool:
        stream = self.__stream
        return stream is None or stream.closed

    def read(self, size: int = -1) -> bytes:
        return self.__require_stream().read(size)

    def write(self, contents: bytes | bytearray | memoryview) -> int:
        return self.__require_stream().write(contents)

    def flush(self) -> None:
        self.__require_stream().flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.__require_stream().seek(offset, whence)

    def tell(self) -> int:
        return self.__require_stream().tell()

    def __require_stream(self) -> BinaryIO:
        stream = self.__stream
        if stream is None or stream.closed:
            raise ValueError("受管二进制流作用域已经结束")
        return stream


def _scoped_binary_effect(
    stream: BinaryIO,
    effect: Callable[[ManagedBinaryFile], _ResultT],
) -> _ResultT:
    scoped = object.__new__(ManagedBinaryFile)
    object.__setattr__(scoped, "_ManagedBinaryFile__stream", stream)
    try:
        return effect(scoped)
    finally:
        object.__setattr__(scoped, "_ManagedBinaryFile__stream", None)


@dataclass(frozen=True, slots=True)
class _ManagedAuthority:
    use_file: _UseFile
    publish_file: _PublishFile
    validate_directory: _ValidateDirectory
    make_directory: _MakeDirectory
    workspace_identity: object
    run_id: RunId | None
    role: ManagedDirectoryRole


@dataclass(frozen=True, init=False, eq=False)
class _ManagedOperations:
    _use_file: _UseFile = field(repr=False)
    _publish_file: _PublishFile = field(repr=False)
    _validate_directory: _ValidateDirectory = field(repr=False)
    _make_directory: _MakeDirectory = field(repr=False)
    _seal: object = field(repr=False)

    def __new__(cls) -> "_ManagedOperations":
        raise TypeError("受管效果只能由 Workspace 绑定")

    def use_file(
        self,
        relative_parts: tuple[str, ...],
        mode: _FileMode,
        effect: _FileEffect,
    ) -> object:
        self._assert_authentic()
        return _without_sensitive_exception_context(
            lambda: self._use_file(relative_parts, mode, effect)
        )

    def validate_directory(
        self,
        relative_parts: tuple[str, ...],
    ) -> None:
        self._assert_authentic()
        _without_sensitive_exception_context(
            lambda: self._validate_directory(relative_parts)
        )

    def publish_file(
        self,
        relative_parts: tuple[str, ...],
        contents: bytes,
    ) -> int:
        self._assert_authentic()
        return _without_sensitive_exception_context(
            lambda: self._publish_file(relative_parts, contents)
        )

    def make_directory(
        self,
        relative_parts: tuple[str, ...],
    ) -> None:
        self._assert_authentic()
        _without_sensitive_exception_context(
            lambda: self._make_directory(relative_parts)
        )

    def _assert_authentic(self) -> None:
        authority = _OPERATION_AUTHORITIES.get(self)
        if authority is None or (
            self._seal is not _CAPABILITY_SEAL
            or self._use_file is not authority.use_file
            or self._publish_file is not authority.publish_file
            or self._validate_directory is not authority.validate_directory
            or self._make_directory is not authority.make_directory
        ):
            raise TypeError("受管效果只能由 Workspace 绑定")

    def _authority(self) -> _ManagedAuthority:
        self._assert_authentic()
        return _OPERATION_AUTHORITIES[self]


_OPERATION_AUTHORITIES: WeakKeyDictionary[
    _ManagedOperations,
    _ManagedAuthority,
] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True, init=False)
class ManagedPathCapability:
    """一个只能通过受管、no-follow 效果操作使用的位置。"""

    _operations: _ManagedOperations = field(repr=False)
    _relative_parts: tuple[str, ...] = field(repr=False)
    _seal: object = field(repr=False)

    def __new__(cls) -> "ManagedPathCapability":
        raise TypeError("ManagedPathCapability 只能由 Workspace 签发")

    def use_binary(
        self,
        mode: _FileMode,
        effect: Callable[[ManagedBinaryFile], _ResultT],
    ) -> _ResultT:
        """在 lease 与锁覆盖的作用域内使用二进制文件。"""
        self._assert_authentic()
        if mode not in {"rb", "wb", "xb", "ab"}:
            raise ValueError("受管文件只支持 rb、wb、xb 或 ab 二进制模式")
        if not callable(effect):
            raise TypeError("受管文件效果必须可调用")
        return cast(
            _ResultT,
            self._operations.use_file(
                self._relative_parts,
                mode,
                lambda stream: _scoped_binary_effect(stream, effect),
            ),
        )

    def read_bytes(self) -> bytes:
        """从受管常规文件读取字节。"""

        def read(stream: ManagedBinaryFile) -> bytes:
            return stream.read()

        return self.use_binary("rb", read)

    def write_bytes(
        self,
        contents: bytes | bytearray | memoryview,
        *,
        exclusive: bool = False,
    ) -> int:
        """向受管常规文件写入字节，并返回写入长度。"""
        if not isinstance(contents, (bytes, bytearray, memoryview)):
            raise TypeError("受管文件内容必须是字节")
        payload = bytes(contents)

        def write(stream: ManagedBinaryFile) -> int:
            written = stream.write(payload)
            stream.flush()
            return written

        return self.use_binary("xb" if exclusive else "wb", write)

    def publish_bytes_atomically(
        self,
        contents: bytes | bytearray | memoryview,
    ) -> int:
        """同步完整字节后，以 no-replace 语义原子发布到受管位置。"""
        self._assert_authentic()
        if not isinstance(contents, (bytes, bytearray, memoryview)):
            raise TypeError("受管文件内容必须是字节")
        if not self._relative_parts:
            raise ValueError("原子发布位置必须包含相对文件名")
        return self._operations.publish_file(
            self._relative_parts,
            bytes(contents),
        )

    def mkdir(self) -> None:
        """在已存在的受管父目录下新建一个 ``0700`` 目录。"""
        self._assert_authentic()
        if not self._relative_parts:
            raise ValueError("不能重复创建 capability 根目录")
        self._operations.make_directory(self._relative_parts)

    def _assert_authentic(self) -> None:
        try:
            seal = self._seal
            self._operations._assert_authentic()
        except (AttributeError, TypeError) as exc:
            raise TypeError("受管位置必须由 Workspace 签发") from exc
        if seal is not _CAPABILITY_SEAL:
            raise TypeError("受管位置必须由 Workspace 签发")


@dataclass(frozen=True, slots=True, init=False)
class ManagedDirectoryCapability:
    """只允许在一个指定受管子树内执行受约束文件系统效果。"""

    _operations: _ManagedOperations = field(repr=False)
    _workspace_identity: object = field(repr=False)
    _run_id: RunId | None = field(repr=False)
    _seal: object = field(repr=False)
    role: ManagedDirectoryRole

    def __new__(cls) -> "ManagedDirectoryCapability":
        raise TypeError("ManagedDirectoryCapability 只能由 Workspace 签发")

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ManagedDirectoryCapability 不能由 Workspace 之外的模块实现")

    def location(self, relative_path: str) -> ManagedPathCapability:
        """取得所属子树中的受约束相对位置。"""
        self._assert_authentic()
        relative_parts = _validate_relative_path(relative_path)
        instance = object.__new__(ManagedPathCapability)
        object.__setattr__(instance, "_operations", self._operations)
        object.__setattr__(instance, "_relative_parts", relative_parts)
        object.__setattr__(instance, "_seal", _CAPABILITY_SEAL)
        return instance

    @property
    def root(self) -> ManagedPathCapability:
        """取得本角色受管子树根，不泄漏可拼接的物理路径。"""
        self._assert_authentic()
        instance = object.__new__(ManagedPathCapability)
        object.__setattr__(instance, "_operations", self._operations)
        object.__setattr__(instance, "_relative_parts", ())
        object.__setattr__(instance, "_seal", _CAPABILITY_SEAL)
        return instance

    @property
    def bound_run_id(self) -> RunId | None:
        """返回签发时绑定的运行标识；维护 capability 返回 ``None``。"""
        self._assert_authentic()
        return self._run_id

    def _belongs_to_same_workspace(
        self,
        other: "ManagedDirectoryCapability",
    ) -> bool:
        self._assert_authentic()
        other._assert_authentic()
        return self._workspace_identity is other._workspace_identity

    def _assert_bound_to_run(self, run_id: RunId) -> None:
        self._assert_authentic()
        if self._run_id != run_id:
            raise ValueError("交付目录 capability 必须绑定同一次运行")

    def _assert_current_directory(self) -> None:
        """供授权效果模块在提交前重新验证 capability。"""
        self._assert_authentic()
        self._operations.validate_directory(())

    def _assert_authentic(self) -> None:
        try:
            seal = self._seal
            authority = self._operations._authority()
            metadata_matches = (
                self._workspace_identity is authority.workspace_identity
                and self._run_id == authority.run_id
                and self.role is authority.role
            )
        except (AttributeError, TypeError) as exc:
            raise TypeError("受管目录必须由 Workspace 签发") from exc
        if seal is not _CAPABILITY_SEAL or not metadata_matches:
            raise TypeError("受管目录必须由 Workspace 签发")


class _WorkspaceCapabilityIssuer:
    __slots__ = ("_seal",)
    _seal: object

    def __new__(cls) -> "_WorkspaceCapabilityIssuer":
        raise TypeError("Workspace capability issuer 不能由业务模块构造")

    def operations(
        self,
        *,
        use_file: _UseFile,
        publish_file: _PublishFile,
        validate_directory: _ValidateDirectory,
        make_directory: _MakeDirectory,
        workspace_identity: object,
        run_id: RunId | None,
        role: ManagedDirectoryRole,
    ) -> _ManagedOperations:
        self._assert_authentic()
        if not isinstance(role, ManagedDirectoryRole):
            raise TypeError("受管目录 capability 必须使用 ManagedDirectoryRole")
        operations = object.__new__(_ManagedOperations)
        object.__setattr__(operations, "_use_file", use_file)
        object.__setattr__(operations, "_publish_file", publish_file)
        object.__setattr__(
            operations,
            "_validate_directory",
            validate_directory,
        )
        object.__setattr__(operations, "_make_directory", make_directory)
        object.__setattr__(operations, "_seal", _CAPABILITY_SEAL)
        _OPERATION_AUTHORITIES[operations] = _ManagedAuthority(
            use_file=use_file,
            publish_file=publish_file,
            validate_directory=validate_directory,
            make_directory=make_directory,
            workspace_identity=workspace_identity,
            run_id=run_id,
            role=role,
        )
        return operations

    def directory(
        self,
        *,
        operations: _ManagedOperations,
        workspace_identity: object,
        run_id: RunId | None,
        role: ManagedDirectoryRole,
    ) -> ManagedDirectoryCapability:
        self._assert_authentic()
        authority = operations._authority()
        if not isinstance(role, ManagedDirectoryRole):
            raise TypeError("受管目录 capability 必须使用 ManagedDirectoryRole")
        if (
            workspace_identity is not authority.workspace_identity
            or run_id != authority.run_id
            or role is not authority.role
        ):
            raise TypeError("受管目录 capability 的授权元数据不匹配")
        instance = object.__new__(ManagedDirectoryCapability)
        object.__setattr__(instance, "_operations", operations)
        object.__setattr__(instance, "_workspace_identity", workspace_identity)
        object.__setattr__(instance, "_run_id", run_id)
        object.__setattr__(instance, "_seal", _CAPABILITY_SEAL)
        object.__setattr__(instance, "role", role)
        return instance

    def _assert_authentic(self) -> None:
        if self._seal is not _CAPABILITY_SEAL:
            raise TypeError("Workspace capability issuer 不合法")


_issuer = object.__new__(_WorkspaceCapabilityIssuer)
object.__setattr__(_issuer, "_seal", _CAPABILITY_SEAL)
_unclaimed_issuer: list[_WorkspaceCapabilityIssuer] = [_issuer]
del _issuer


def _claim_workspace_issuer() -> _WorkspaceCapabilityIssuer:
    """只允许 Workspace 实现模块在导入时领取一次 issuer。"""
    if not _unclaimed_issuer:
        raise RuntimeError("Workspace capability issuer 已被领取")
    return _unclaimed_issuer.pop()


def _validate_relative_path(relative_path: str) -> tuple[str, ...]:
    if not isinstance(relative_path, str):
        raise TypeError("受管相对路径必须是字符串")
    encoded_path = relative_path.encode("utf-8")
    if (
        not relative_path
        or len(encoded_path) > 4096
        or relative_path.startswith("/")
        or relative_path.endswith("/")
        or "//" in relative_path
        or "\\" in relative_path
        or "\x00" in relative_path
        or unicodedata.normalize("NFC", relative_path) != relative_path
    ):
        raise ValueError("受管相对路径格式不合法")
    parts = tuple(relative_path.split("/"))
    if any(
        part in {"", ".", ".."}
        or part.startswith(
            (".workspace-quarantine-", ".workspace-create-")
        )
        or len(part.encode("utf-8")) > 255
        or not part.isprintable()
        for part in parts
    ):
        raise ValueError("受管相对路径格式不合法")
    return parts
