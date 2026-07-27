"""SourceAnalysis 拥有的不可变业务事实。"""

import re
from dataclasses import dataclass, field

from video_auto_editor.workspace import SourceFileCapability

_FULL_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True, init=False)
class SourceDescription:
    """后续业务模块消费的已验证素材描述。"""

    source_file: SourceFileCapability = field(repr=False, compare=False)
    sha256: str = field(repr=False)
    byte_length: int
    duration_ms: int

    def __new__(
        cls,
        *_args: object,
        **_kwargs: object,
    ) -> "SourceDescription":
        raise TypeError("SourceDescription 只能由 SourceAnalysis 创建")

    @classmethod
    def _from_analysis(
        cls,
        *,
        source_file: SourceFileCapability,
        sha256: str,
        byte_length: int,
        duration_ms: int,
    ) -> "SourceDescription":
        if not isinstance(source_file, SourceFileCapability):
            raise TypeError("素材描述必须绑定 Workspace 签发的素材")
        if (
            not isinstance(sha256, str)
            or _FULL_SHA256.fullmatch(sha256) is None
        ):
            raise ValueError("素材描述必须包含完整规范 SHA-256")
        for value, field_name in (
            (byte_length, "素材字节长度"),
            (duration_ms, "素材时长毫秒数"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数")
            if value <= 0:
                raise ValueError(f"{field_name}必须是正整数")
        instance = object.__new__(cls)
        object.__setattr__(instance, "source_file", source_file)
        object.__setattr__(instance, "sha256", sha256)
        object.__setattr__(instance, "byte_length", byte_length)
        object.__setattr__(instance, "duration_ms", duration_ms)
        return instance
