"""Pinned, immutable CPython 3.12 guest files. No runtime discovery or enrollment."""

from dataclasses import dataclass
import ctypes
import fcntl
import hashlib
import io
import os
from pathlib import PurePosixPath
import re
import stat
import struct
import zipfile

from .contracts import FleetError

MAX_FILES = 768
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ZIP = 16 * 1024 * 1024
SEALS = 0x000F
PYTHON = "/usr/bin/python3.12"
LOADER = "/usr/lib64/ld-linux-x86-64.so.2"


@dataclass(frozen=True)
class RuntimeFile:
    source: str
    guest: str
    sha256: str
    role: str


@dataclass(frozen=True)
class RuntimeManifest:
    files: tuple[RuntimeFile, ...]
    python_version: str = "3.12"
    version: int = 1


def _failure():
    return FleetError("boundary_manifest", "Invalid or unqualified runtime manifest")


def _stamp(info):
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_uid",
            "st_gid",
            "st_mode",
            "st_nlink",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _seal_bytes(data):
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.memfd_create(b"harnessfleet-runtime", 3)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "Runtime snapshot creation failed")
    try:
        view = memoryview(data)
        while view:
            size = os.write(fd, view)
            if size <= 0:
                raise OSError("Runtime snapshot write failed")
            view = view[size:]
        fcntl.fcntl(fd, 1033, SEALS)
        if fcntl.fcntl(fd, 1034) & SEALS != SEALS:
            raise _failure()
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def seal_file(source_fd, expected_sha256, limit=MAX_FILE):
    """Copy from one already-authorized opening; aliases cannot mutate the result."""
    before = os.fstat(source_fd)
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
        raise _failure()
    data = bytearray()
    while len(data) <= before.st_size:
        chunk = os.pread(source_fd, min(65536, before.st_size + 1 - len(data)), len(data))
        if not chunk:
            break
        data.extend(chunk)
    if (
        len(data) != before.st_size
        or _stamp(os.fstat(source_fd)) != _stamp(before)
        or hashlib.sha256(data).hexdigest() != expected_sha256
    ):
        raise _failure()
    return _seal_bytes(data)


def _safe_source(path):
    if not isinstance(path, str):
        raise _failure()
    p = PurePosixPath(path)
    if not p.is_absolute() or str(p) != path or ".." in p.parts or "\0" in path or len(path) > 4096:
        raise _failure()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(p.parts[1:]):
            info = os.fstat(directory)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise _failure()
            final = index == len(p.parts) - 2
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if not final:
                flags |= os.O_DIRECTORY
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if (
            info.st_uid != 0
            or info.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise _failure()
        result, directory = directory, None
        return result
    finally:
        if directory is not None:
            os.close(directory)


def _elf(data):
    """Validate a declared ELF closure, without searching libraries or executing sources."""
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise _failure()
    kind, machine, version, _, phoff, _, _, ehsize, entsize, count, _, _, _ = struct.unpack_from(
        "<HHIQQQIHHHHHH", data, 16
    )
    if (
        kind not in (2, 3)
        or machine != 62
        or version != 1
        or ehsize != 64
        or entsize != 56
        or not 0 < count <= 128
    ):
        raise _failure()
    if phoff < 64 or phoff + count * entsize > len(data):
        raise _failure()
    loads, dynamics, interpreters = [], [], []
    for offset in range(phoff, phoff + count * entsize, entsize):
        typ, _, fileoff, address, _, size, memory, _ = struct.unpack_from("<IIQQQQQQ", data, offset)
        if fileoff + size > len(data) or (typ == 1 and size > memory):
            raise _failure()
        if typ == 1:
            loads.append((address, fileoff, size))
        elif typ == 2:
            dynamics.append((fileoff, size))
        elif typ == 3:
            raw = data[fileoff : fileoff + size]
            if not raw.endswith(b"\0") or raw != b"/lib64/ld-linux-x86-64.so.2\0":
                raise _failure()
            interpreters.append(LOADER)
    if not loads or len(dynamics) != 1 or len(interpreters) > 1:
        raise _failure()
    start, size = dynamics[0]
    if size % 16 or size > 4096 * 16:
        raise _failure()
    tags = []
    for offset in range(start, start + size, 16):
        tag, value = struct.unpack_from("<qQ", data, offset)
        if tag == 0:
            break
        if tag in (15, 29, 0x6FFFFEFA, 0x6FFFFEFB, 0x6FFFFEFC, 0x7FFFFFFD, 0x7FFFFFFF):
            raise _failure()
        tags.append((tag, value))
    else:
        raise _failure()
    strings = [value for tag, value in tags if tag == 5]
    sizes = [value for tag, value in tags if tag == 10]
    if len(strings) != 1 or len(sizes) != 1:
        raise _failure()
    locations = [
        fileoff + strings[0] - address
        for address, fileoff, size in loads
        if address <= strings[0] and strings[0] + sizes[0] <= address + size
    ]
    if len(locations) != 1:
        raise _failure()
    table = data[locations[0] : locations[0] + sizes[0]]
    names, sonames = [], []
    for tag, index in tags:
        if tag not in (1, 14):
            continue
        end = table.find(b"\0", index)
        if end < 0:
            raise _failure()
        name = table[index:end]
        if not re.fullmatch(rb"[A-Za-z0-9_.+\-]{1,128}", name):
            raise _failure()
        (names if tag == 1 else sonames).append(name.decode("ascii"))
    if len(sonames) > 1:
        raise _failure()
    return names, sonames, interpreters


def _validate(manifest):
    if (
        not isinstance(manifest, RuntimeManifest)
        or type(manifest.version) is not int
        or manifest.version != 1
        or manifest.python_version != "3.12"
        or not isinstance(manifest.files, tuple)
        or not 0 < len(manifest.files) <= MAX_FILES
    ):
        raise _failure()
    seen = set()
    for item in manifest.files:
        if not isinstance(item, RuntimeFile) or not isinstance(item.guest, str) or item.guest in seen:
            raise _failure()
        seen.add(item.guest)
        if not isinstance(item.sha256, str) or not re.fullmatch("[a-f0-9]{64}", item.sha256):
            raise _failure()
        path = PurePosixPath(item.guest)
        if (
            len(item.guest) > 512
            or str(path) != item.guest
            or any(part in ("..", "site-packages", "dist-packages", "__pycache__") for part in path.parts)
            or "\\" in item.guest
            or any(not 32 <= ord(char) < 127 for char in item.guest)
        ):
            raise _failure()
        if item.role == "stdlib":
            if (
                path.is_absolute()
                or not item.guest.isascii()
                or not item.guest.endswith(".py")
                or path.name in ("sitecustomize.py", "usercustomize.py")
            ):
                raise _failure()
        elif item.role == "interpreter":
            if item.guest != PYTHON:
                raise _failure()
        elif item.role == "loader":
            if item.guest != LOADER:
                raise _failure()
        elif item.role == "library":
            if str(path.parent) != "/usr/lib/x86_64-linux-gnu":
                raise _failure()
        elif item.role == "extension":
            if str(path.parent) != "/usr/lib/python3.12/lib-dynload" or not path.name.endswith(".so"):
                raise _failure()
        else:
            raise _failure()
    required = {
        PYTHON,
        LOADER,
        "encodings/__init__.py",
        "encodings/aliases.py",
        "encodings/ascii.py",
        "encodings/utf_8.py",
        "os.py",
    }
    if not required <= seen or sum(item.role == "interpreter" for item in manifest.files) != 1:
        raise _failure()


class PreparedRuntime:
    def __init__(self):
        self.mounts = []
        self._close_error = None

    def close(self):
        if self._close_error is not None:
            raise self._close_error
        retained = []
        for mount in self.mounts:
            try:
                os.close(mount[1])
            except BaseException as error:
                retained.append(mount)
                if self._close_error is None:
                    self._close_error = error
                else:
                    self._close_error.add_note(
                        "Runtime descriptor cleanup also failed: " + type(error).__name__
                    )
        self.mounts = retained
        if self._close_error is not None:
            # Retain uncertain descriptors; a close error never authorizes a numeric retry.
            raise self._close_error


def prepare_runtime(manifest):
    _validate(manifest)
    result = PreparedRuntime()
    temporary = []
    try:
        total, elf = 0, []
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for item in sorted(manifest.files, key=lambda item: item.guest):
                source = _safe_source(item.source)
                try:
                    descriptor = seal_file(source, item.sha256)
                finally:
                    os.close(source)
                temporary.append(descriptor)
                size = os.fstat(descriptor).st_size
                total += size
                if total > MAX_TOTAL:
                    raise _failure()
                data = os.pread(descriptor, size, 0)
                if len(data) != size:
                    raise _failure()
                if item.role == "stdlib":
                    entry = zipfile.ZipInfo(item.guest, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.external_attr = 0o100444 << 16
                    bundle.writestr(entry, data)
                    if archive.tell() > MAX_ZIP:
                        raise _failure()
                else:
                    elf.append((item, _elf(data)))
                    result.mounts.append((item.guest, descriptor))
                    temporary.remove(descriptor)
        names = {}
        for item, (_, sonames, _) in elf:
            if item.role in ("library", "loader"):
                name = PurePosixPath(item.guest).name
                if name in names or (sonames and sonames != [name]):
                    raise _failure()
                names[name] = item.guest
        for item, (needed, _, interpreters) in elf:
            if any(name not in names for name in needed) or any(
                path not in {mount[0] for mount in result.mounts} for path in interpreters
            ):
                raise _failure()
            if item.role == "interpreter" and interpreters != [LOADER]:
                raise _failure()
        data = archive.getvalue()
        if len(data) > MAX_ZIP:
            raise _failure()
        result.mounts.append(("/usr/lib/python312.zip", _seal_bytes(data)))
        return result
    except BaseException as error:
        result.close()
        if isinstance(error, (OSError, ValueError, struct.error, UnicodeError, FleetError)):
            raise _failure() from error
        raise
    finally:
        for descriptor in temporary:
            os.close(descriptor)
