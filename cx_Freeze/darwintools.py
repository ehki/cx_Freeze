import os
import platform
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Union

from .exception import DarwinException

# In a MachO file, need to deal specially with links that use @executable_path,
# @loader_path, @rpath
#
# @executable_path - where ultimate calling executable is
# @loader_path - directory of current object
# @rpath - list of paths to check
# (earlier rpaths have higher priority, i believe)
#
# Resolving these variables (particularly @rpath) requires tracing through the
# sequence linked MachO files leading the the current file, to determine which
# directories are included in the current rpath.


def _isMachOFile(path: Path) -> bool:
    """Determines whether the file is a Mach-O file."""
    if not path.is_file():
        return False
    if b"Mach-O" in subprocess.check_output(("file", path)):
        return True
    return False


class MachOReference:
    """Represents a linking reference from MachO file to another file."""

    def __init__(
        self,
        source_file: "DarwinFile",
        raw_path: str,
        resolved_path: Optional[Path],
    ):
        """
        :param source_file: DarwinFile object for file in which the reference
        was found
        :param raw_path: The load path that appears in the file
        (may include @rpath, etc.)
        :param resolved_path: The path resolved to an explicit path to a file
        on system. Or None, if the path could not be resolved at the time the
        DarwinFile was processed.
        """
        self.source_file: "DarwinFile" = source_file
        self.raw_path: str = raw_path
        self.resolved_path: Optional[Path] = resolved_path

        # True if the referenced file is copied into the frozen package
        # (i.e., not a non-copied system file)
        self.is_copied = False
        # reference to target DarwinFile (but only if file is copied into app)
        self.target_file: Optional["DarwinFile"] = None

    def isResolved(self) -> bool:
        return self.resolved_path is not None

    def setTargetFile(self, darwin_file: "DarwinFile"):
        self.target_file = darwin_file
        self.is_copied = True


class DarwinFile:
    """A DarwinFile object represents a file that will be copied into the
    application, and record where it was ultimately moved to in the application
    bundle. Mostly used to provide special handling for copied files that are
    Mach-O files."""

    def __init__(
        self,
        path: Union[str, Path],
        referencing_file: Optional["DarwinFile"] = None,
        strict: bool = False,
    ):
        """
        :param path: The original path of the DarwinFile
        (before copying into app)
        :param referencing_file: DarwinFile object representing the referencing
        source file
        :param strict: Do not make guesses about rpath resolution. If the
        load does not resolve, throw an Exception.
        """
        self.path = Path(path).resolve()
        self.referencing_file: Optional["DarwinFile"] = None
        self.strict = strict

        # path to file in build directory (set as part of freeze process)
        self._build_path: Optional[Path] = None

        # commands in a Mach-O file
        self.commands: List[MachOCommand] = []
        self.loadCommands: List[MachOLoadCommand] = []
        self.rpathCommands: List[MachORPathCommand] = []

        # note: if file gets referenced twice (or more), it will only be the
        # first reference that gets recorded.
        # mapping of raw load paths to absolute resolved paths
        # (or None, if no resolution was determined)
        self.libraryPathResolution: Dict[str, Optional[Path]] = {}
        # the is of entries in the rpath in effect for this file.
        self._rpath: Optional[List[Path]] = None

        # dictionary of MachOReference objects, by their paths.
        # Path used is the resolved path, if available, and otherwise the
        # unresolved load path.
        self.machOReferenceForTargetPath: Dict[Path, MachOReference] = {}

        if not _isMachOFile(self.path):
            self.isMachO = False
            return

        # if this is a MachO file, extract linking information from it
        self.isMachO = True
        self.commands = MachOCommand._getMachOCommands(self.path)
        self.loadCommands = [
            c for c in self.commands if isinstance(c, MachOLoadCommand)
        ]
        self.rpathCommands = [
            c for c in self.commands if isinstance(c, MachORPathCommand)
        ]
        self.referencing_file = referencing_file

        self.getRPath()
        self.resolveLibraryPaths()

        # Create MachOReference objects for all the binaries referenced form
        # this file.
        for raw_path, resolved_path in self.libraryPathResolution.items():
            # the path to use for storing in dictionary
            if resolved_path is None:
                dict_path = Path(raw_path)
            else:
                dict_path = resolved_path
            if dict_path in self.machOReferenceForTargetPath:
                raise DarwinException(
                    "Multiple dynamic libraries resolved to the same file."
                )
            reference = MachOReference(
                source_file=self,
                raw_path=raw_path,
                resolved_path=resolved_path,
            )
            self.machOReferenceForTargetPath[dict_path] = reference

    def __str__(self):
        l = []
        # l.append("RPath Commands: {}".format(self.rpathCommands))
        # l.append("Load commands: {}".format(self.loadCommands))
        l.append(f"Mach-O File: {self.path}")
        l.append("Resolved rpath:")
        for rp in self.getRPath():
            l.append(f"   {rp}")
        l.append("Loaded libraries:")
        for rp in self.libraryPathResolution:
            l.append(f"   {rp} -> {self.libraryPathResolution[rp]}")
        return "\n".join(l)

    def fileReferenceDepth(self) -> int:
        """Returns how deep this Mach-O file is in the dynamic load order."""
        if self.referencing_file is not None:
            return self.referencing_file.fileReferenceDepth() + 1
        return 0

    def printFileInformation(self):
        """Prints information about the Mach-O file."""
        print(f"[{self.fileReferenceDepth()}] File: {self.path}")
        print("  Commands:")
        if len(self.commands) > 0:
            for c in self.commands:
                print(f"    {c}")
        else:
            print("    [None]")

        # This can be included for even more detail on the problem file.
        # print("  Load commands:")
        # if len(self.loadCommands) > 0:
        #     for lc in self.loadCommands: print(f'    {lc}')
        # else: print("    [None]")

        print("  RPath commands:")
        if len(self.rpathCommands) > 0:
            for rpc in self.rpathCommands:
                print(f"    {rpc}")
        else:
            print("    [None]")
        print("  Calculated RPath:")
        rpath = self.getRPath()
        if len(rpath) > 0:
            for path in rpath:
                print(f"    {path}")
        else:
            print("    [None]")
        if self.referencing_file is not None:
            print("Referenced from:")
            self.referencing_file.printFileInformation()

    def setBuildPath(self, path: Path):
        self._build_path = path

    def getBuildPath(self) -> Optional[Path]:
        return self._build_path

    @staticmethod
    def isExecutablePath(path: str) -> bool:
        return path.startswith("@executable_path")

    @staticmethod
    def isLoaderPath(path: str) -> bool:
        return path.startswith("@loader_path")

    @staticmethod
    def isRPath(path: str) -> bool:
        return path.startswith("@rpath")

    def resolveLoader(self, path: str) -> Optional[Path]:
        """Resolve a path that includes @loader_path. @loader_path represents
        the directory in which the DarwinFile is located."""
        if self.isLoaderPath(path):
            return self.path.parent / path.replace("@loader_path/", "", 1)
        raise DarwinException(f"resolveLoader() called on bad path: {path}")

    def resolveExecutable(self, path: str) -> Path:
        """@executable_path should resolve to the directory where the original
        executable was located. By default, we set that to the directory of
        the library, so it would resolve in the same was as if linked from an
        executable in the same directory."""
        # consider making this resolve to the directory of the target script
        # instead?
        if self.isExecutablePath(path):
            return self.path.parent / path.replace("@executable_path/", "", 1)
        raise DarwinException(
            f"resolveExecutable() called on bad path: {path}"
        )

    def resolveRPath(self, path: str) -> Optional[Path]:
        for rp in self.getRPath():
            test_path = rp / path.replace("@rpath/", "", 1)
            if _isMachOFile(test_path):
                return test_path
        if not self.strict:
            # If not strictly enforcing rpath, return None here, and leave any
            # error to .finalizeReferences() instead.
            return None
        print(f"\nERROR: Problem resolving RPath [{path}] in file:")
        self.printFileInformation()
        raise DarwinException(f"resolveRPath() failed to resolve path: {path}")

    def getRPath(self) -> List[Path]:
        """Returns the rpath in effect for this file. Determined by rpath
        commands in this file and (recursively) the chain of files that
        referenced this file."""
        if self._rpath is not None:
            return self._rpath
        raw_paths = [c.rpath for c in self.rpathCommands]
        rpath = []
        for rp in raw_paths:
            test_rp = Path(rp)
            if test_rp.is_absolute():
                rpath.append(test_rp)
            elif self.isLoaderPath(rp):
                rpath.append(self.resolveLoader(rp).resolve())
            elif self.isExecutablePath(rp):
                rpath.append(self.resolveExecutable(rp).resolve())
        rpath = [rp for rp in rpath if rp.exists()]

        if self.referencing_file is not None:
            rpath = self.referencing_file.getRPath() + rpath
        self._rpath = rpath
        return rpath

    def resolvePath(self, path: str) -> Optional[Path]:
        """Resolves any @executable_path, @loader_path, and @rpath references
        in a path."""
        if self.isLoaderPath(path):  # replace @loader_path
            return self.resolveLoader(path)
        if self.isExecutablePath(path):  # replace @executable_path
            return self.resolveExecutable(path)
        if self.isRPath(path):  # replace @rpath
            return self.resolveRPath(path)
        test_path = Path(path)
        if test_path.is_absolute():  # just use the path, if it is absolute
            return test_path
        test_path = self.path.parent / path
        if _isMachOFile(test_path):
            return test_path.resolve()
        raise DarwinException(f"Could not resolve path: {path}")

    def resolveLibraryPaths(self):
        for lc in self.loadCommands:
            raw_path = lc.load_path
            resolved_path = self.resolvePath(raw_path)
            self.libraryPathResolution[raw_path] = resolved_path

    def getDependentFilePaths(self) -> Set[Path]:
        """Returns a list the available resolved paths to dependencies."""
        dependents: Set[Path] = set()
        for ref in self.machOReferenceForTargetPath.values():
            # skip load references that could not be resolved
            if ref.isResolved():
                dependents.add(ref.resolved_path)
        return dependents

    def getMachOReferenceList(self) -> List[MachOReference]:
        return list(self.machOReferenceForTargetPath.values())

    def getMachOReferenceForPath(self, path: Path) -> MachOReference:
        """Returns the reference pointing to the specified path, baed on paths
        stored in self.machOReferenceTargetPath. Raises Exception if not
        available."""
        try:
            return self.machOReferenceForTargetPath[path]
        except KeyError:
            raise DarwinException(
                f"Path {path} is not a path referenced from DarwinFile"
            ) from None


class MachOCommand:
    """Represents a load command in a MachO file."""

    def __init__(self, lines: List[str]):
        self.lines = lines

    def displayString(self) -> str:
        l: List[str] = []
        if len(self.lines) > 0:
            l.append(self.lines[0].strip())
        if len(self.lines) > 1:
            l.append(self.lines[1].strip())
        return " / ".join(l)

    def __repr__(self):
        return f"<MachOCommand ({self.displayString()})>"

    @staticmethod
    def _getMachOCommands(path: Path) -> List["MachOCommand"]:
        """Returns a list of load commands in the specified file, using
        otool."""
        shell_command = ("otool", "-l", path)
        commands: List[MachOCommand] = []
        current_command_lines = None

        # split the output into separate load commands
        out = subprocess.check_output(shell_command, encoding="utf-8")
        for line in out.splitlines():
            line = line.strip()
            if line[:12] == "Load command":
                if current_command_lines is not None:
                    commands.append(
                        MachOCommand.parseLines(current_command_lines)
                    )
                current_command_lines = []
            if current_command_lines is not None:
                current_command_lines.append(line)
        if current_command_lines is not None:
            commands.append(MachOCommand.parseLines(current_command_lines))
        return commands

    @staticmethod
    def parseLines(lines: List[str]) -> "MachOCommand":
        if len(lines) < 2:
            return MachOCommand(lines)
        parts = lines[1].split(" ")
        if parts[0] != "cmd":
            return MachOCommand(lines)
        if parts[1] == "LC_LOAD_DYLIB":
            return MachOLoadCommand(lines)
        if parts[1] == "LC_RPATH":
            return MachORPathCommand(lines)
        return MachOCommand(lines)


class MachOLoadCommand(MachOCommand):
    def __init__(self, lines: List[str]):
        super().__init__(lines)
        self.load_path = None
        if len(self.lines) < 4:
            return
        pathline = self.lines[3]
        pathline = pathline.strip()
        if not pathline.startswith("name "):
            return
        pathline = pathline[4:].strip()
        pathline = pathline.split("(offset")[0].strip()
        self.load_path = pathline

    def getPath(self):
        return self.load_path

    def __repr__(self):
        return f"<LoadCommand path={self.load_path!r}>"


class MachORPathCommand(MachOCommand):
    def __init__(self, lines: List[str]):
        super().__init__(lines)
        self.rpath = None
        if len(self.lines) < 4:
            return
        pathline = self.lines[3]
        pathline = pathline.strip()
        if not pathline.startswith("path "):
            return
        pathline = pathline[4:].strip()
        pathline = pathline.split("(offset")[0].strip()
        self.rpath = pathline

    def __repr__(self):
        return f"<RPath path={self.rpath!r}>"


def _printFile(
    darwinFile: DarwinFile,
    seenFiles: Set[DarwinFile],
    level: int,
    noRecurse=False,
):
    """
    Utility function to prints details about a DarwinFile and (optionally)
    recursively any other DarwinFiles that it references.
    """
    print("{}{}".format(level * "|  ", str(darwinFile.path)), end="")
    print(" (already seen)" if noRecurse else "")
    if noRecurse:
        return
    for ref in darwinFile.machOReferenceForTargetPath.values():
        if not ref.is_copied:
            continue
        mf = ref.target_file
        _printFile(
            mf,
            seenFiles=seenFiles,
            level=level + 1,
            noRecurse=(mf in seenFiles),
        )
        seenFiles.add(mf)
    return


def printMachOFiles(fileList: List[DarwinFile]):
    seenFiles = set()
    for mf in fileList:
        if mf not in seenFiles:
            seenFiles.add(mf)
            _printFile(mf, seenFiles=seenFiles, level=0)


def changeLoadReference(
    fileName: str, oldReference: str, newReference: str, VERBOSE: bool = True
):
    """Utility function that uses intall_name_tool to change oldReference to
    newReference in the machO file specified by fileName."""
    if VERBOSE:
        print("Redirecting load reference for ", end="")
        print(f"<{fileName}> {oldReference} -> {newReference}")
    original = os.stat(fileName).st_mode
    newMode = original | stat.S_IWUSR
    os.chmod(fileName, newMode)
    subprocess.call(
        ("install_name_tool", "-change", oldReference, newReference, fileName)
    )
    os.chmod(fileName, original)


def applyAdHocSignature(fileName: str):
    if platform.machine() != "arm64":
        return

    args = (
        "codesign",
        "--sign",
        "-",
        "--force",
        "--preserve-metadata=entitlements,requirements,flags,runtime",
        fileName,
    )
    if subprocess.call(args):
        # It may be a bug in Apple's codesign utility
        # The workaround is to copy the file to another inode, then move it
        # back erasing the previous file. The sign again.
        with tempfile.TemporaryDirectory() as dirName:
            tempName = os.path.join(dirName, os.path.basename(fileName))
            shutil.copy(fileName, tempName)
            shutil.move(tempName, fileName)
        subprocess.call(args)


class DarwinFileTracker:
    """Object to track the DarwinFiles that have been added during a freeze."""

    def __init__(self):
        # list of DarwinFile objects for files being copied into project
        self._copied_file_list: List[DarwinFile] = []

        # mapping of (build directory) target paths to DarwinFile objects
        self._darwin_file_for_build_path: Dict[Path, DarwinFile] = {}

        # mapping of (source location) paths to DarwinFile objects
        self._darwin_file_for_source_path: Dict[Path, DarwinFile] = {}

        # a cache of MachOReference objects pointing to a given source path
        self._reference_cache: Dict[Path, MachOReference] = {}

    def __iter__(self) -> Iterable[DarwinFile]:
        return iter(self._copied_file_list)

    def pathIsAlreadyCopiedTo(self, target_path: Path) -> bool:
        """Check if the given target_path has already has a file copied to
        it."""
        if target_path in self._darwin_file_for_build_path:
            return True
        return False

    def getDarwinFile(
        self, source_path: Path, target_path: Path
    ) -> DarwinFile:
        """Gets the DarwinFile for file copied from source_path to target_path.
        If either (i) nothing, or (ii) a different file has been copied to
        targetPath, raises a DarwinException."""
        # check that the target file came from the specified source
        targetDarwinFile: DarwinFile
        try:
            targetDarwinFile = self._darwin_file_for_build_path[target_path]
        except KeyError:
            raise DarwinException(
                f"File {target_path} already copied to, "
                "but no DarwinFile object found for it."
            ) from None
        real_source = source_path.resolve()
        target_real_source = targetDarwinFile.path.resolve()
        if real_source != target_real_source:
            # raise DarwinException(
            print(
                "*** WARNING ***\n"
                f"Attempting to copy two files to {target_path}\n"
                f"source 1: {targetDarwinFile.path} "
                f"(real: {target_real_source})\n"
                f"source 2: {source_path} (real: {real_source})\n"
                "(This may be caused by including modules in the zip file "
                "that rely on binary libraries with the same name.)"
                "\nUsing only source 1."
            )
        return targetDarwinFile

    def recordCopiedFile(self, target_path: Path, darwin_file: DarwinFile):
        """Record that a DarwinFile is being copied to a given path. If a
        file has been copied to that path, raise a DarwinException."""
        if self.pathIsAlreadyCopiedTo(target_path):
            raise DarwinException(
                "addFile() called with target_path already copied to "
                f"(target_path={target_path})"
            )

        self._copied_file_list.append(darwin_file)
        self._darwin_file_for_build_path[target_path] = darwin_file
        self._darwin_file_for_source_path[darwin_file.path] = darwin_file

    def cacheReferenceTo(self, source_path: Path, reference: MachOReference):
        self._reference_cache[source_path] = reference

    def getCachedReferenceTo(
        self, source_path: Path
    ) -> Optional[MachOReference]:
        return self._reference_cache.get(source_path)

    def findDarwinFileForFilename(self, filename: str) -> Optional[DarwinFile]:
        """Attempts to locate a copied DarwinFile with the specified filename
        and returns that. Otherwise returns None."""
        basename = Path(filename).name
        for df in self._copied_file_list:
            if df.path.name == basename:
                return df
        return None

    def finalizeReferences(self):
        """This function does a final pass through the references for all the
        copied DarwinFiles and attempts to clean up any remaining references
        that are not already marked as copied. It covers two cases where the
        reference might not be marked as copied:
        1) Files where _CopyFile was called without copyDependentFiles=True
           (in which the information would not have been added to the
            references at that time).
        2) Files with broken @rpath references. We try to fix that up here by
           seeing if the relevant file was located *anywhere* as part of the
           freeze process."""
        copied_file: DarwinFile
        reference: MachOReference
        for copied_file in self._copied_file_list:
            for reference in copied_file.getMachOReferenceList():
                if not reference.is_copied:
                    if reference.isResolved():
                        # if reference is resolve, simply check if the resolved
                        # path was otherwise copied and lookup the DarwinFile
                        # object.
                        target_path = reference.resolved_path.resolve()
                        if target_path in self._darwin_file_for_source_path:
                            reference.setTargetFile(
                                self._darwin_file_for_source_path[target_path]
                            )
                    else:
                        # if reference is not resolved, look through the copied
                        # files and try to find a candidate, and use it if
                        # found.
                        potential_target = self.findDarwinFileForFilename(
                            reference.raw_path
                        )
                        if potential_target is None:
                            # If we cannot find any likely candidate, fail.
                            print(
                                "\nERROR: Could not resolve RPath "
                                f"[{reference.raw_path}] in file "
                                f"[{copied_file.path}], and could "
                                "not find any likely intended reference."
                            )
                            copied_file.printFileInformation()
                            raise DarwinException(
                                f"finalizeReferences() failed to resolve path "
                                f"[{reference.raw_path}] in file "
                                f"[{copied_file.path}]."
                            )
                        print(
                            f"WARNING: In file [{copied_file.path}]"
                            f" guessing that {reference.raw_path} "
                            f"resolved to {potential_target.path}."
                        )
                        reference.resolved_path = potential_target.path
                        reference.setTargetFile(potential_target)
