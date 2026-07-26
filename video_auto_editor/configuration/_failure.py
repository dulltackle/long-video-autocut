"""Configuration 深模块返回给应用层的类型化安全失败。"""

from collections.abc import Mapping
from types import MappingProxyType

from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode


class ConfigurationFailure(ValueError):
    """不包含原始配置值、路径或课程正文的配置失败事实。"""

    __slots__ = ("diagnostics", "error_code")

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, object],
    ) -> None:
        self.error_code = error_code
        self.diagnostics = MappingProxyType(dict(diagnostics))
        super().__init__(ERROR_REGISTRY[error_code].safe_message)
