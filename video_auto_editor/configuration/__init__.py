"""严格形成不可变生效配置与课程上下文。"""

from ._failure import ConfigurationFailure
from ._loader import Configuration
from ._model import LoadedConfiguration

__all__ = [
    "Configuration",
    "ConfigurationFailure",
    "LoadedConfiguration",
]
