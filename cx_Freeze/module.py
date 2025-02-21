"""
Base class for module.
"""

import datetime
import socket
from contextlib import suppress
from keyword import iskeyword
from pathlib import Path
from types import CodeType
from typing import Dict, List, Optional, Set, Tuple, Union

from ._compat import importlib_metadata
from .common import TemporaryPath
from .exception import ConfigError

__all__ = ["ConstantsModule", "Module"]


class DistributionCache(importlib_metadata.PathDistribution):
    """Cache the distribution package."""

    _cachedir = TemporaryPath()

    @staticmethod
    def at(path: Union[str, Path]):
        return DistributionCache(Path(path))

    at.__doc__ = importlib_metadata.PathDistribution.at.__doc__

    @classmethod
    def from_name(cls, name: str):
        distribution = super().from_name(name)

        # Cache dist-info files in a temporary directory
        normalized_name = getattr(distribution, "_normalized_name", None)
        if normalized_name is None:
            normalized_name = importlib_metadata.Prepared.normalize(name)
        source_path = getattr(distribution, "_path", None)
        if source_path is None:
            mask = f"{normalized_name}-{distribution.version}*-info"
            source_path = list(distribution.locate_file("").glob(mask))[0]
        if not source_path.exists():
            raise importlib_metadata.PackageNotFoundError(name)

        target_name = f"{normalized_name}-{distribution.version}.dist-info"
        target_path = cls._cachedir.path / target_name
        target_path.mkdir(exist_ok=True)

        purelib = None
        if source_path.name.endswith(".dist-info"):
            for source in source_path.iterdir():  # type: Path
                target = target_path / source.name
                target.write_bytes(source.read_bytes())
        elif source_path.is_file():
            # old egg-info file is converted to dist-info
            target = target_path / "METADATA"
            target.write_bytes(source_path.read_bytes())
            purelib = (source_path.parent / (normalized_name + ".py")).exists()
        else:
            # Copy minimal data from egg-info directory into dist-info
            source = source_path / "PKG-INFO"
            if source.is_file():
                target = target_path / "METADATA"
                target.write_bytes(source.read_bytes())
            source = source_path / "top_level.txt"
            if source.is_file():
                target = target_path / "top_level.txt"
                target.write_bytes(source.read_bytes())
            purelib = not source_path.joinpath("not-zip-safe").is_file()

        cls._write_wheel_distinfo(target_path, purelib)
        cls._write_record_distinfo(target_path)

        return cls.at(target_path)

    from_name.__doc__ = importlib_metadata.PathDistribution.from_name.__doc__

    @staticmethod
    def _write_wheel_distinfo(target_path: Path, purelib: bool):
        """Create WHEEL if it doesn't exist"""
        target = target_path / "WHEEL"
        if not target.exists():
            project = Path(__file__).parent.name
            version = importlib_metadata.version(project)
            root_is_purelib = "true" if purelib else "false"
            text = [
                "Wheel-Version: 1.0",
                f"Generator: {project} ({version})",
                f"Root-Is-Purelib: {root_is_purelib}",
                "Tag: py3-none-any",
            ]
            target.write_text("\n".join(text))

    @staticmethod
    def _write_record_distinfo(target_path: Path):
        """Recreate minimal RECORD file"""
        target_name = target_path.name
        record = []
        for file in target_path.iterdir():
            record.append(f"{target_name}/{file.name},,")
        record.append(f"{target_name}/RECORD,,")
        target = target_path / "RECORD"
        target.write_text("\n".join(record), encoding="utf-8")


class Module:
    """
    The Module class.
    """

    def __init__(
        self,
        name: str,
        path: Optional[List[Union[Path, str]]] = None,
        file_name: Optional[Union[Path, str]] = None,
        parent: Optional["Module"] = None,
    ):
        self.name: str = name
        self.path: Optional[List[Path]] = (
            [Path(p) for p in path] if path else None
        )
        self.file = file_name
        self.parent: Optional["Module"] = parent
        self.code: Optional[CodeType] = None
        self.distribution: Optional[DistributionCache] = None
        self.exclude_names: Set[str] = set()
        self.global_names: Set[str] = set()
        self.ignore_names: Set[str] = set()
        self.in_import: bool = True
        self.source_is_string: bool = False
        self.source_is_zip_file: bool = False
        self._in_file_system: int = 1
        # cache the dist-info files (metadata)
        self.update_distribution(name)

    @property
    def file(self) -> Optional[Path]:
        """Module filename"""
        return self._file

    @file.setter
    def file(self, file_name: Optional[Union[Path, str]]):
        self._file: Optional[Path] = Path(file_name) if file_name else None

    def update_distribution(self, name: str) -> None:
        """Update the distribution cache based on its name.
        This method may be used to link an distribution's name to a module.

        Example: ModuleFinder cannot detects the distribution of _cffi_backend
        but in a hook we can link it to 'cffi'.
        """
        try:
            distribution = DistributionCache.from_name(name)
        except importlib_metadata.PackageNotFoundError:
            distribution = None
        if distribution is None:
            return
        try:
            requires = importlib_metadata.requires(distribution.name) or []
        except importlib_metadata.PackageNotFoundError:
            requires = []
        for req in requires:
            req_name = req.partition(" ")[0]
            with suppress(importlib_metadata.PackageNotFoundError):
                DistributionCache.from_name(req_name)
        self.distribution = distribution

    def __repr__(self) -> str:
        parts = [f"name={self.name!r}"]
        if self.file is not None:
            parts.append(f"file={self.file!r}")
        if self.path is not None:
            parts.append(f"path={self.path!r}")
        return "<Module {}>".format(", ".join(parts))

    @property
    def in_file_system(self) -> int:
        """Returns a value indicating where the module/package will be stored:
        0. in a zip file (not directly in the file system)
        1. in the file system, package with modules and data
        2. in the file system, only detected modules."""
        if self.parent is not None:
            return self.parent.in_file_system
        if self.path is None or self.file is None:
            return 0
        return self._in_file_system

    @in_file_system.setter
    def in_file_system(self, value: int) -> None:
        self._in_file_system = value


class ConstantsModule:
    """
    Base ConstantsModule class.
    """

    def __init__(
        self,
        release_string: Optional[str] = None,
        copyright_string: Optional[str] = None,
        module_name: str = "BUILD_CONSTANTS",
        time_format: str = "%B %d, %Y %H:%M:%S",
        constants: Optional[List[str]] = None,
    ):
        self.module_name: str = module_name
        self.time_format: str = time_format
        self.values: Dict[str, str] = {}
        self.values["BUILD_RELEASE_STRING"] = release_string
        self.values["BUILD_COPYRIGHT"] = copyright_string
        if constants:
            for constant in constants:
                parts = constant.split("=", maxsplit=1)
                if len(parts) == 1:
                    name = constant
                    value = None
                else:
                    name, string_value = parts
                    value = eval(string_value)
                if (not name.isidentifier()) or iskeyword(name):
                    raise ConfigError(
                        f"Invalid constant name in ConstantsModule ({name!r})"
                    )
                self.values[name] = value
        self.module_path: TemporaryPath = TemporaryPath("constants.py")

    def create(self, modules: List[Module]) -> Tuple[Path, str]:
        """
        Create the module which consists of declaration statements for each
        of the values.
        """
        today = datetime.datetime.today()
        source_timestamp = 0
        for module in modules:
            if module.file is None or module.source_is_string:
                continue
            if module.source_is_zip_file:
                continue
            if not module.file.exists():
                raise ConfigError(
                    f"No file named {module.file!s} (for module {module.name})"
                )
            timestamp = module.file.stat().st_mtime
            source_timestamp = max(source_timestamp, timestamp)
        stamp = datetime.datetime.fromtimestamp(source_timestamp)
        self.values["BUILD_TIMESTAMP"] = today.strftime(self.time_format)
        self.values["BUILD_HOST"] = socket.gethostname().split(".")[0]
        self.values["SOURCE_TIMESTAMP"] = stamp.strftime(self.time_format)
        source_parts = []
        names = list(self.values.keys())
        names.sort()
        for name in names:
            value = self.values[name]
            source_parts.append(f"{name} = {value!r}")
        self.module_path.path.write_text("\n".join(source_parts))
        return self.module_path.path, self.module_name
