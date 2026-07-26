"""Workspace 深模块返回给应用层的类型化安全失败。"""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TypeVar

from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode

_ResultT = TypeVar("_ResultT")


class WorkspaceFailure(RuntimeError):
    """不包含素材或 workspace 绝对路径的安全失败事实。"""

    __slots__ = ("diagnostics", "error_code")

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, object],
    ) -> None:
        self.error_code = error_code
        self.diagnostics = MappingProxyType(dict(diagnostics))
        super().__init__(ERROR_REGISTRY[error_code].safe_message)


def _without_sensitive_exception_context(
    effect: Callable[[], _ResultT],
) -> _ResultT:
    """在公共边界复制安全失败，丢弃可能含物理路径的异常链。"""
    try:
        return effect()
    except WorkspaceFailure as failure:
        error_code = failure.error_code
        diagnostics = failure.diagnostics
    raise WorkspaceFailure(
        error_code,
        diagnostics,
    ) from None
