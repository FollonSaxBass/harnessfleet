"""Check source/wheel archives for private paths, runtime files and credential material."""

import argparse
import re
import tarfile
import zipfile
from pathlib import Path

BANNED = (
    b"/home/" + b"riccardo-folloni",
    b"gitlab." + b"axyon.ai",
    b"BEGIN " + b"PRIVATE KEY",
    b"BEGIN RSA " + b"PRIVATE KEY",
)
TOKEN = re.compile(rb"\b(?:gh[oprsu]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{32,})\b")
RUNTIME = {
    ".credentials.json",
    "auth.json",
    "fleet.sqlite3",
    "run.json",
    "events.jsonl",
    "config_srv.yaml",
    ".env",
}


def check(name, data):
    path = Path(name)
    if any(
        part in (".audit", "local", "knowledge", "second-brain", "__pycache__", ".venv", ".git")
        for part in path.parts
    ):
        raise ValueError("Excluded runtime or private directory: " + name)
    if path.name in RUNTIME or path.suffix in (".pyc", ".db", ".sqlite3"):
        raise ValueError("Excluded runtime file: " + name)
    if any(p in data for p in BANNED) or TOKEN.search(data):
        raise ValueError("Private content pattern in " + name)


def audit(path):
    count = 0
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    check(name, archive.read(name))
                    count += 1
    else:
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("Archive link: " + member.name)
                if member.isfile():
                    check(member.name, archive.extractfile(member).read())
                    count += 1
    return count


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("archives", nargs="+", type=Path)
    for path in p.parse_args().archives:
        print(f"{path.name}: {audit(path)} regular files audited")
