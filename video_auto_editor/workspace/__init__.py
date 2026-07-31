"""建立并守卫受管 workspace。"""

from ._capability import (
    ManagedBinaryFile,
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
    ManagedPathCapability,
    ManagedTreeEntry,
    ManagedTreeEntryKind,
)
from ._failure import WorkspaceFailure
from ._workspace import (
    DiagnosticRunWorkspace,
    MaintenanceWorkspace,
    RunWorkspace,
    SourceFileCapability,
    Workspace,
)

__all__ = [
    "DiagnosticRunWorkspace",
    "MaintenanceWorkspace",
    "ManagedBinaryFile",
    "ManagedDirectoryCapability",
    "ManagedDirectoryRole",
    "ManagedPathCapability",
    "ManagedTreeEntry",
    "ManagedTreeEntryKind",
    "RunWorkspace",
    "SourceFileCapability",
    "Workspace",
    "WorkspaceFailure",
]
