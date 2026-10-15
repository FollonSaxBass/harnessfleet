"""Versioned public contracts; storage is deliberately private."""

from dataclasses import dataclass, field
from typing import Protocol

CONTRACT_VERSION = 1
TERMINAL = {"finished", "failed", "stopped", "quota", "authentication", "cancelled", "rejected"}
ACTIVE = {"starting", "running", "stopping", "uncertain"}


def error_record(code: str, message: str):
    return {"schema_version": CONTRACT_VERSION, "error": {"code": code, "message": message}}


class FleetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Capabilities:
    launch: bool = True
    continuation: bool = False
    attachment: bool = False
    quota_recovery: bool = False
    output: bool = True
    native_lineage: bool = False
    permissions: tuple[str, ...] = ("read-only",)
    platforms: dict[str, str] = field(default_factory=lambda: {"linux": "experimental"})


@dataclass
class Result:
    completed: bool = False
    output: str = ""
    session_id: str | None = None
    failure: str | None = None
    reset_at: float | None = None


class Adapter(Protocol):
    """Plugins are trusted local Python code, never an isolation boundary."""

    contract_version: int
    id: str
    capabilities: Capabilities

    def diagnose(self, route: dict) -> dict: ...
    def command(self, run: dict, directory: str) -> list[str]: ...
    def environment(self, route: dict, ambient: dict) -> dict: ...
    def consume(self, event: dict, result: Result) -> None: ...
