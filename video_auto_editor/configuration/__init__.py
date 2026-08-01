"""严格形成不可变生效配置与课程上下文。"""

from ._diagnostics import assert_diagnostic_projection_authentic
from ._failure import ConfigurationFailure
from ._loader import Configuration
from ._model import (
    ClipPolicy,
    ConfigurationDiagnosticProjection,
    CourseContext,
    LoadedConfiguration,
    SubtitleStyle,
    TextGenerationSettings,
)

__all__ = [
    "ClipPolicy",
    "Configuration",
    "ConfigurationDiagnosticProjection",
    "ConfigurationFailure",
    "CourseContext",
    "LoadedConfiguration",
    "SubtitleStyle",
    "TextGenerationSettings",
    "assert_diagnostic_projection_authentic",
]
