"""Pinned runtime preparation with real Linux sealing and synthetic metadata."""

from dataclasses import replace
import errno
import fcntl
import hashlib
import io
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zipfile

import pytest

from harnessfleet import FleetError
from harnessfleet.runtime import RuntimeFile, RuntimeManifest, prepare_runtime, seal_file

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux memfd qualification")


def elf_image(soname=None, needed=None, interpreter=False, rpath=False):
    """Tiny ELF metadata fixture, never an executable qualification."""
    data = bytearray(1024)
    data[:7] = b"\x7fELF\x02\x01\x01"
    count = 3 if interpreter else 2
    struct.pack_into("<HHIQQQIHHHHHH", data, 16, 3, 62, 1, 0, 64, 0, 0, 64, 56, count, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", data, 64, 1, 5, 0, 0x1000, 0, len(data), len(data), 4096)
    strings = b"\0"
    tags = []
    for value, tag in ((soname, 14), (needed, 1)):
        if value:
            tags.append((tag, len(strings)))
            strings += value.encode() + b"\0"
    tags = [(5, 0x1000 + 768), (10, len(strings)), *tags]
    if rpath:
        tags.append((29, 0))
    tags.append((0, 0))
    struct.pack_into("<IIQQQQQQ", data, 120, 2, 4, 256, 0x1100, 0, len(tags) * 16, len(tags) * 16, 8)
    for index, pair in enumerate(tags):
        struct.pack_into("<qQ", data, 256 + index * 16, *pair)
    data[768 : 768 + len(strings)] = strings
    if interpreter:
        loader = b"/lib64/ld-linux-x86-64.so.2\0"
        data[512 : 512 + len(loader)] = loader
        struct.pack_into("<IIQQQQQQ", data, 176, 3, 4, 512, 0x1200, 0, len(loader), len(loader), 1)
    return bytes(data)


@pytest.fixture
def synthetic_runtime(tmp_path, monkeypatch):
    original = os.fstat
    writable_ancestors = set()

    def administrator_metadata(fd):
        info = original(fd)
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_uid"] = 0
        if (info.st_mode & 0o170000) == 0o040000 and (info.st_dev, info.st_ino) not in writable_ancestors:
            values["st_mode"] &= ~0o022
        return SimpleNamespace(**values)

    # Root identity/ancestor policy is injected; copying, aliases, contents and seals are real.
    monkeypatch.setattr(os, "fstat", administrator_metadata)
    files = []

    def add(guest, role, data):
        path = tmp_path / ("source-" + str(len(files)))
        path.write_bytes(data)
        entry = RuntimeFile(str(path), guest, hashlib.sha256(data).hexdigest(), role)
        files.append(entry)
        return entry

    add("/usr/bin/python3.12", "interpreter", elf_image(needed="libsynthetic.so", interpreter=True))
    add("/usr/lib64/ld-linux-x86-64.so.2", "loader", elf_image(soname="ld-linux-x86-64.so.2"))
    add("/usr/lib/x86_64-linux-gnu/libsynthetic.so", "library", elf_image(soname="libsynthetic.so"))
    for name in (
        "encodings/__init__.py",
        "encodings/aliases.py",
        "encodings/ascii.py",
        "encodings/utf_8.py",
        "os.py",
    ):
        add(name, "stdlib", b"x=1\n")
    return files, add, writable_ancestors


def test_runtime_is_exact_sealed_files_with_deterministic_stored_stdlib(synthetic_runtime):
    files, _, _ = synthetic_runtime
    archives = []
    for references in (files, list(reversed(files))):
        prepared = prepare_runtime(RuntimeManifest(tuple(references)))
        try:
            assert {guest for guest, _ in prepared.mounts} == {
                "/usr/bin/python3.12",
                "/usr/lib64/ld-linux-x86-64.so.2",
                "/usr/lib/x86_64-linux-gnu/libsynthetic.so",
                "/usr/lib/python312.zip",
            }
            for guest, descriptor in prepared.mounts:
                assert fcntl.fcntl(descriptor, 1034) & 0x000F == 0x000F
                with pytest.raises(OSError) as error:
                    os.pwrite(descriptor, b"change", 0)
                assert error.value.errno == errno.EPERM
                with pytest.raises(OSError):
                    os.ftruncate(descriptor, 0)
                if guest.endswith(".zip"):
                    archives.append(os.pread(descriptor, os.fstat(descriptor).st_size, 0))
            with zipfile.ZipFile(io.BytesIO(archives[-1])) as archive:
                assert len(archive.namelist()) == 5
                assert archive.read("os.py") == b"x=1\n"
                assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        finally:
            prepared.close()
    assert archives[0] == archives[1]


def test_snapshot_ignores_real_hardlink_and_preopened_host_writer(tmp_path):
    source = tmp_path / "approved"
    source.write_bytes(b"approved-runtime-v1")
    alias = tmp_path / "alias"
    os.link(source, alias)
    writer = os.open(alias, os.O_RDWR)
    reader = os.open(source, os.O_RDONLY)
    snapshot = None
    try:
        snapshot = seal_file(reader, "a893bceb4cf301e77b7038195f617b7c2a25c83ca991d83d372627e15bd57d68")
        os.pwrite(writer, b"host-changed-by-alias", 0)
        os.ftruncate(writer, 4)
        assert source.read_bytes() == b"host"
        assert os.pread(snapshot, 100, 0) == b"approved-runtime-v1"
        assert fcntl.fcntl(snapshot, 1034) & 0x000F == 0x000F
    finally:
        for descriptor in (reader, writer, snapshot):
            if descriptor is not None:
                os.close(descriptor)


@pytest.mark.parametrize(
    "failure",
    ["writable-file", "writable-ancestor", "symlink", "wrong-hash", "bad-elf", "missing-library", "runpath"],
)
def test_unqualified_sources_refuse_preparation(synthetic_runtime, tmp_path, failure):
    files, add, writable = synthetic_runtime
    if failure == "writable-file":
        Path(files[0].source).chmod(0o666)
    elif failure == "writable-ancestor":
        tmp_path.chmod(0o777)
        info = tmp_path.stat()
        writable.add((info.st_dev, info.st_ino))
    elif failure == "symlink":
        link = tmp_path / "link"
        link.symlink_to(files[0].source)
        files[0] = replace(files[0], source=str(link))
    elif failure == "wrong-hash":
        files[0] = replace(files[0], sha256="0" * 64)
    elif failure == "bad-elf":
        data = b"not-an-elf"
        Path(files[0].source).write_bytes(data)
        files[0] = replace(files[0], sha256=hashlib.sha256(data).hexdigest())
    elif failure == "missing-library":
        del files[2]
    elif failure == "runpath":
        item = add(
            "/usr/lib/x86_64-linux-gnu/libpath.so", "library", elf_image(soname="libpath.so", rpath=True)
        )
        assert item in files
    with pytest.raises(FleetError, match="runtime manifest"):
        prepare_runtime(RuntimeManifest(tuple(files)))


@pytest.mark.parametrize(
    "guest",
    [
        "../escape.py",
        "/absolute.py",
        "a//b.py",
        "a\\b.py",
        "x.pth",
        "sitecustomize.py",
        "site-packages/x.py",
        "null\0.py",
    ],
)
def test_archive_member_policy_rejects_unsafe_additions(synthetic_runtime, guest):
    files, _, _ = synthetic_runtime
    references = [*files, replace(files[-1], guest=guest)]
    with pytest.raises(FleetError):
        prepare_runtime(RuntimeManifest(tuple(references)))


def test_manifest_source_count_is_bounded(synthetic_runtime):
    files, _, _ = synthetic_runtime
    references = [
        *files,
        *(replace(files[-1], guest=f"extra{number}.py") for number in range(769 - len(files))),
    ]
    with pytest.raises(FleetError):
        prepare_runtime(RuntimeManifest(tuple(references)))


def test_individual_snapshot_size_is_bounded(tmp_path):
    source = tmp_path / "oversized"
    size = 16 * 1024 * 1024 + 1
    with source.open("wb") as handle:
        handle.truncate(size)
    digest = hashlib.sha256(bytes(size)).hexdigest()
    descriptor = os.open(source, os.O_RDONLY)
    snapshot = None
    try:
        with pytest.raises(FleetError):
            snapshot = seal_file(descriptor, digest)
    finally:
        os.close(descriptor)
        if snapshot is not None:
            os.close(snapshot)


def test_opened_source_survives_path_replacement(synthetic_runtime, monkeypatch):
    files, _, _ = synthetic_runtime
    selected = files[-1]
    original_open = os.open
    replaced = False

    def replace_after_open(path, flags, *args, **kwargs):
        nonlocal replaced
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == Path(selected.source).name and not replaced:
            replaced = True
            Path(selected.source).unlink()
            Path(selected.source).write_bytes(b"unexpected replacement\n")
        return descriptor

    monkeypatch.setattr(os, "open", replace_after_open)
    prepared = prepare_runtime(RuntimeManifest(tuple(files)))
    try:
        assert replaced
        descriptor = dict(prepared.mounts)["/usr/lib/python312.zip"]
        with zipfile.ZipFile(io.BytesIO(os.pread(descriptor, os.fstat(descriptor).st_size, 0))) as archive:
            assert archive.read(selected.guest) == b"x=1\n"
        assert Path(selected.source).read_bytes() == b"unexpected replacement\n"
    finally:
        prepared.close()


def test_late_failure_releases_runtime_descriptors(synthetic_runtime):
    files, add, _ = synthetic_runtime
    add("/usr/lib/x86_64-linux-gnu/zz-invalid.so", "library", b"invalid elf")

    def descriptors():
        result = set()
        for entry in Path("/proc/self/fd").iterdir():
            try:
                target = entry.readlink()
            except FileNotFoundError:
                continue
            if "memfd:harnessfleet-runtime" in str(target):
                result.add(int(entry.name))
        return result

    before = descriptors()
    with pytest.raises(FleetError):
        prepare_runtime(RuntimeManifest(tuple(files)))
    assert descriptors() == before


def test_invalid_runtime_never_starts_a_process(synthetic_runtime, monkeypatch):
    from harnessfleet import boundary

    files, _, _ = synthetic_runtime
    files[0] = replace(files[0], sha256="0" * 64)
    monkeypatch.setattr(boundary, "_validate", lambda spec: "/usr/bin/bwrap")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid runtime reached process creation")

    monkeypatch.setattr(boundary.subprocess, "Popen", forbidden)
    with pytest.raises(FleetError):
        boundary.launch(boundary.LaunchSpec("pass", RuntimeManifest(tuple(files))))


def test_mount_plan_uses_only_declared_file_descriptors(synthetic_runtime):
    from harnessfleet.boundary import BOOTSTRAP, LaunchSpec, _command

    files, _, _ = synthetic_runtime
    spec = LaunchSpec("pass", RuntimeManifest(tuple(files)))
    prepared = prepare_runtime(spec.runtime)
    try:
        command = _command(spec, "/usr/bin/bwrap", prepared.mounts, 10000, 10001, 10002)
        assert not {"--bind", "--ro-bind", "--dev-bind", "--bind-try", "--ro-bind-try"} & set(command)
        observed = []
        for index, option in enumerate(command):
            if option == "--ro-bind-data":
                observed.append((command[index + 2], int(command[index + 1])))
        assert observed == prepared.mounts
        assert command[-6:] == ["/usr/bin/python3", "-I", "-S", "-c", BOOTSTRAP, "pass"]
    finally:
        prepared.close()
