"""运行诊断深模块返回给应用层的类型化安全失败。"""

from collections.abc import Mapping
from types import MappingProxyType

from video_auto_editor.runtime.errors import (
    ERROR_REGISTRY,
    ErrorCode,
    ErrorModule,
    PreservedApplicationFailure,
    RunError,
    RunStage,
)


class DiagnosticsFailure(PreservedApplicationFailure):
    """不包含物理路径、异常文本或用户内容的诊断持久化失败。"""

    __slots__ = ("diagnostics", "error_code")

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, object],
    ) -> None:
        if error_code not in {
            ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
            ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        }:
            raise ValueError("DiagnosticsFailure 只能表达诊断持久化错误")
        self.error_code = error_code
        self.diagnostics = MappingProxyType(dict(diagnostics))
        # 借用公共错误注册表完成严格字段和值校验，同时不保留错误对象。
        RunError.create(
            code=error_code,
            stage=RunStage.PREFLIGHT,
            module=ErrorModule.RUN_DIAGNOSTICS,
            event_sequence=1,
            diagnostics=self.diagnostics,
        )
        super().__init__(ERROR_REGISTRY[error_code].safe_message)


def _startup_failure(
    *,
    operation: str,
    reason_code: str,
) -> DiagnosticsFailure:
    return DiagnosticsFailure(
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
        {
            "component": "run_diagnostics",
            "operation": operation,
            "reason_code": reason_code,
        },
    )


def _runtime_failure(
    *,
    operation: str,
    reason_code: str,
) -> DiagnosticsFailure:
    return DiagnosticsFailure(
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        {
            "operation": operation,
            "reason_code": reason_code,
        },
    )
