from pathlib import Path
from typing import List, Optional, Union

HANDLE = Optional[int]

class BindError(Exception):
    """BindError Exception."""

    ...

def AddIcon(target_path: Union[str, Path], exe_icon: Union[str, Path]):
    """Add the icon as a resource to the specified file."""
    ...

def BeginUpdateResource(
    path: Union[str, Path], delete_existing_resources: bool = True
) -> HANDLE:
    """Wrapper for BeginUpdateResource()."""
    ...

def UpdateResource(
    handle: HANDLE, resource_type: int, resource_id: int, resource_data: bytes
) -> None:
    """Wrapper for UpdateResource()."""
    ...

def EndUpdateResource(handle: HANDLE, discard_changes: bool):
    """Wrapper for EndUpdateResource()."""
    ...

def UpdateCheckSum(target_path: Union[str, Path]):
    """Update the CheckSum into the specified executable."""
    ...

def GetSystemDir() -> str:
    """Return the Windows system directory (C:\Windows\system for example)."""
    ...

def GetWindowsDir() -> str:
    """Return the Windows directory (C:\Windows for example)."""
    ...

def GetDependentFiles(path: Union[str, Path]) -> List[str]:
    """Return a list of files that this file depends on."""
    ...
