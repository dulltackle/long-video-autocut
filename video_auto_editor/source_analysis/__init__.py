"""验证输入素材并形成不可变素材描述。"""

from ._analysis import SourceAnalysis
from ._failure import SourceAnalysisFailure
from ._model import SourceDescription

__all__ = [
    "SourceAnalysis",
    "SourceAnalysisFailure",
    "SourceDescription",
]
