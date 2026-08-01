"""基于受管 Workspace capability 的文件系统缓存生产 Adapter。"""

from collections.abc import Callable
from datetime import datetime

from video_auto_editor.workspace import ManagedDirectoryCapability

from ._filesystem import _FileSystemStore
from ._repository import CacheRepository, _create_repository

_Clock = Callable[[], datetime]


def initialize_cache_repository(
    cache_directory: ManagedDirectoryCapability,
    *,
    application_version: str,
    clock: _Clock | None = None,
) -> CacheRepository:
    """在受管缓存目录上初始化生产处理缓存仓库。"""

    return _create_repository(
        lambda: _FileSystemStore(cache_directory),
        application_version=application_version,
        clock=clock,
    )


__all__ = ["initialize_cache_repository"]
