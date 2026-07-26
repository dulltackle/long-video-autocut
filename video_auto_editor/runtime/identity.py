"""运行与业务实体的类型化标识。"""

from types import MappingProxyType
from typing import Mapping, TypeVar
from uuid import RFC_4122, UUID, uuid4


_BusinessIdT = TypeVar("_BusinessIdT", bound="BusinessId")
_DiagnosticIdT = TypeVar("_DiagnosticIdT", bound="DiagnosticId")


class BusinessId(str):
    """只用于标准交付物关系的业务标识基类。"""

    __slots__ = ()

    def __new__(cls: type[_BusinessIdT], value: str) -> _BusinessIdT:
        expected_prefix = f"{_business_prefix(cls)}_"
        try:
            suffix = value.removeprefix(expected_prefix)
            parsed = UUID(suffix)
        except (AttributeError, ValueError) as exc:
            raise ValueError(
                f"{cls.__name__} 必须使用 {expected_prefix}<规范小写 UUIDv4>"
            ) from exc
        if (
            not value.startswith(expected_prefix)
            or parsed.version != 4
            or parsed.variant != RFC_4122
            or str(parsed) != suffix
        ):
            raise ValueError(
                f"{cls.__name__} 必须使用 {expected_prefix}<规范小写 UUIDv4>"
            )
        return str.__new__(cls, value)

    @classmethod
    def new(cls: type[_BusinessIdT]) -> _BusinessIdT:
        """创建不依赖业务内容的全新标识。"""
        return cls(f"{_business_prefix(cls)}_{uuid4()}")


class RunId(BusinessId):
    """一次直播拆条运行的不可变标识。"""

    __slots__ = ()


class TranscriptId(BusinessId):
    """一次运行内完整转写文本的标识。"""

    __slots__ = ()


class TranscriptChunkId(BusinessId):
    """一次运行内转写文本块的标识。"""

    __slots__ = ()


class PlanId(BusinessId):
    """一次运行内拆条方案的标识。"""

    __slots__ = ()


class CandidateId(BusinessId):
    """一次运行内候选片段的标识。"""

    __slots__ = ()


class ShortVideoId(BusinessId):
    """一次运行内短视频的标识。"""

    __slots__ = ()


class SeriesId(BusinessId):
    """一次运行内同主题系列的标识。"""

    __slots__ = ()


_BUSINESS_PREFIXES: Mapping[type[BusinessId], str] = MappingProxyType(
    {
        RunId: "run",
        TranscriptId: "transcript",
        TranscriptChunkId: "transcript_chunk",
        PlanId: "plan",
        CandidateId: "candidate",
        ShortVideoId: "short_video",
        SeriesId: "series",
    }
)


def _business_prefix(identity_type: type[BusinessId]) -> str:
    try:
        return _BUSINESS_PREFIXES[identity_type]
    except KeyError as exc:
        raise TypeError("业务标识类型必须来自封闭类型集合") from exc


class DiagnosticId(str):
    """只用于运行诊断内部关系的不透明标识基类。"""

    __slots__ = ()

    def __new__(cls: type[_DiagnosticIdT], value: str) -> _DiagnosticIdT:
        try:
            parsed = UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{cls.__name__} 必须是规范小写 UUIDv4") from exc
        if parsed.version != 4 or parsed.variant != RFC_4122 or str(parsed) != value:
            raise ValueError(f"{cls.__name__} 必须是规范小写 UUIDv4")
        return str.__new__(cls, value)

    @classmethod
    def new(cls: type[_DiagnosticIdT]) -> _DiagnosticIdT:
        """创建一个不透明且不参与业务关系的诊断标识。"""
        return cls(str(uuid4()))


class OperationId(DiagnosticId):
    """运行诊断中的操作标识。"""

    __slots__ = ()


class ErrorId(DiagnosticId):
    """运行诊断中的错误标识。"""

    __slots__ = ()
