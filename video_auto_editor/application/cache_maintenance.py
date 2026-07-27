"""不创建直播拆条运行的处理缓存维护应用。"""

from os import PathLike

from video_auto_editor.workspace import Workspace


class CacheMaintenanceApplication:
    """在受管 workspace 维护锁内清空全部处理缓存。"""

    __slots__ = ()

    def clear(self, workspace_dir: PathLike[str] | str) -> None:
        """清空受管处理缓存，并保留 workspace 中的其他数据。"""
        workspace = Workspace.open_existing(workspace_dir)
        with workspace.acquire_maintenance() as maintenance:
            maintenance.clear_cache()
