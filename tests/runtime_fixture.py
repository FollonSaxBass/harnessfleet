"""Optional administrator-pinned runtime for actual kernel qualification.

No fixture is generated from the current host and blessed automatically.
"""

import hashlib
import json
import os
from pathlib import Path

from harnessfleet.runtime import RuntimeFile, RuntimeManifest


def pinned_runtime():
    path = os.environ.get("HF_BOUNDARY_RUNTIME_MANIFEST")
    expected = os.environ.get("HF_BOUNDARY_RUNTIME_SHA256")
    if not path or not expected:
        return None
    with Path(path).open("rb") as source:
        data = source.read(512 * 1024 + 1)
    assert len(data) <= 512 * 1024
    assert hashlib.sha256(data).hexdigest() == expected
    manifest = json.loads(data)
    return RuntimeManifest(
        tuple(RuntimeFile(**item) for item in manifest["files"]),
        python_version=manifest["python_version"],
        version=manifest["version"],
    )
