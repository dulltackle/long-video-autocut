"""SourceAnalysis 返回给应用层的类型化安全失败。"""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TypeVar

from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode

_ResultT = TypeVar("_ResultT")


class SourceAnalysisFailure(RuntimeError):
    """只包含稳定错误码与白名单诊断的素材验证失败。"""

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
    """在公共边界复制安全事实，丢弃底层路径与工具异常链。"""
    try:
        return effect()
    except SourceAnalysisFailure as failure:
        error_code = failure.error_code
        diagnostics = failure.diagnostics
    raise SourceAnalysisFailure(error_code, diagnostics) from None
