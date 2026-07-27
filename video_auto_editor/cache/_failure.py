"""处理缓存基础设施的类型化安全失败。"""

from collections.abc import Mapping
from types import MappingProxyType

from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode


class CacheFailure(RuntimeError):
    """权限、磁盘、同步、锁或原子文件效果失败。"""

    __slots__ = ("diagnostics", "error_code")

    def __init__(self, diagnostics: Mapping[str, object]) -> None:
        self.error_code = ErrorCode.CACHE_INFRASTRUCTURE_FAILED
        self.diagnostics = MappingProxyType(dict(diagnostics))
        super().__init__(ERROR_REGISTRY[self.error_code].safe_message)
