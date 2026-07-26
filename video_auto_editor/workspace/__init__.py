"""建立并守卫受管 workspace。"""

from ._capability import (
    ManagedBinaryFile,
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    ManagedPathCapability,
)
from ._failure import WorkspaceFailure
from ._workspace import (
    MaintenanceWorkspace,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
)

__all__ = [
    "MaintenanceWorkspace",
    "ManagedBinaryFile",
    "ManagedDirectoryCapability",
    "ManagedDirectoryRole",
    "ManagedPathCapability",
    "RunWorkspace",
    "SourceFileCapability",
    "Workspace",
    "WorkspaceFailure",
]
