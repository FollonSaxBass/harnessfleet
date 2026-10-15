"""Scoped worker RPC. Trusted supervisors own storage; clients never open it.

This protocol is an authorization boundary, not same-UID process containment.
"""

import copy
import hashlib
import inspect
import json
import math
import os
import re
import select
import secrets
import socket
import socketserver
import struct
import tempfile
import threading
import uuid
from pathlib import Path

from . import processes
from .contracts import FleetError, error_record

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
OPERATIONS = {
    "routes",
    "start",
    "start_many",
    "status",
    "list",
    "events",
    "wait",
    "output",
    "cancel",
    "stop",
    "resume",
}
MUTATIONS = {"start", "start_many", "cancel", "stop", "resume"}
RESOURCES = {"status", "events", "wait", "output", "cancel", "stop", "resume"}
REQUEST_ID = re.compile(r"[a-f0-9]{32}\Z")


def managed_environment():
    return any(
        key in os.environ
        for key in (
            "HARNESSFLEET_BROKER",
            "HARNESSFLEET_BROKER_OWNER",
            "HARNESSFLEET_PARENT_RUN",
            "HARNESSFLEET_PARENT_GENERATION",
            "HARNESSFLEET_PARENT_TOKEN",
        )
    )


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n").encode()


class Client:
    contract_version = 1

    def __getattr__(self, operation):
        if operation not in OPERATIONS:
            raise FleetError("delegation_scope", "Operation is unavailable to a delegated worker")

        def call(*args, **kwargs):
            request_id = kwargs.pop("request_id", None) or uuid.uuid4().hex
            if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                raise FleetError("request", "Request id must contain 32 hexadecimal characters")
            packet = encoded(
                {
                    "schema_version": 1,
                    "operation": operation,
                    "args": args,
                    "kwargs": kwargs,
                    "request_id": request_id,
                    "token": os.environ.get("HARNESSFLEET_PARENT_TOKEN", ""),
                }
            )
            if len(packet) > MAX_REQUEST:
                raise FleetError("request", "Delegation request exceeds the message limit")
            try:
                owner = json.loads(os.environ.get("HARNESSFLEET_BROKER_OWNER", "null"))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(130)
                    connection.connect(os.environ["HARNESSFLEET_BROKER"])
                    pid, uid, _ = struct.unpack(
                        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    )
                    if uid != os.getuid() or processes.receipt(pid) != owner or not processes.alive(owner):
                        raise FleetError("delegation_auth", "Delegation endpoint ownership is invalid")
                    connection.sendall(packet)
                    data = bytearray()
                    while not data.endswith(b"\n"):
                        chunk = connection.recv(min(65536, MAX_RESPONSE + 1 - len(data)))
                        if not chunk or len(data) + len(chunk) > MAX_RESPONSE:
                            raise ValueError()
                        data.extend(chunk)
                response = json.loads(data)
                if not isinstance(response, dict) or response.get("schema_version") != 1:
                    raise ValueError()
                if "error" in response:
                    raise FleetError(response["error"]["code"], response["error"]["message"])
                return response["result"]
            except (OSError, ValueError, KeyError, TypeError):
                raise FleetError(
                    "delegation_transport",
                    f"Delegation response unavailable; request {request_id} may be consumed. Reuse only this request id; never replay with a new id.",
                )

        return call


def receive(connection):
    """Authenticate the writer of every fragment, including transferred sockets."""
    data = bytearray()
    writer = None
    while not data.endswith(b"\n"):
        chunk, controls, flags, _ = connection.recvmsg(
            min(65536, MAX_REQUEST + 1 - len(data)), socket.CMSG_SPACE(12) + socket.CMSG_SPACE(256)
        )
        credentials = None
        unexpected = False
        for level, kind, value in controls:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) == 12:
                credentials = struct.unpack("3i", value)
            else:
                unexpected = True
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    for index in range(0, len(value) - len(value) % 4, 4):
                        os.close(struct.unpack("i", value[index : index + 4])[0])
        if unexpected or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or not credentials:
            raise FleetError("delegation_auth", "Delegation writer credentials are unavailable")
        if writer is not None and credentials != writer:
            raise FleetError("delegation_auth", "Delegation writer changed within a request")
        writer = credentials
        if not chunk or len(data) + len(chunk) > MAX_REQUEST:
            raise FleetError("request", "Delegation request is incomplete or exceeds its limit")
        data.extend(chunk)
    return json.loads(data), writer


class Portal:
    """One parent's host supervisor serves its generation-specific child authority."""

    def __init__(self, manager, rid, generation):
        self.manager, self.rid, self.generation = manager, rid, generation
        self.native_handle = None
        self.native_receipt = None
        self.directory = tempfile.TemporaryDirectory(prefix="harnessfleet-client-")
        self.path = str(Path(self.directory.name) / "broker.sock")
        portal = self
        slots = threading.BoundedSemaphore(8)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                try:
                    packet, writer = receive(self.request)
                    response = portal.respond(packet, writer)
                except FleetError as error:
                    response = error_record(error.code, str(error))
                except Exception:
                    response = error_record("request", "Invalid delegation request")
                payload = encoded(response)
                if len(payload) > MAX_RESPONSE:
                    payload = encoded(error_record("output", "Delegation response exceeds its limit"))
                try:
                    self.request.sendall(payload)
                except OSError:
                    pass  # durable mutation receipt remains available for the same request id

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            block_on_close = False
            request_queue_size = 16

            def process_request(self, request, address):
                if not slots.acquire(blocking=False):
                    request.close()
                    return
                try:
                    super().process_request(request, address)
                except Exception:
                    slots.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    slots.release()

        self.server = Server(self.path, Handler, bind_and_activate=False)
        self.server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.server.server_bind()
        self.server.server_activate()
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def environment(self):
        return {
            "HARNESSFLEET_BROKER": self.path,
            "HARNESSFLEET_BROKER_OWNER": json.dumps(processes.receipt(os.getpid())),
        }

    def bind_native(self, record):
        self.native_handle = processes.pidfd_open(record["pid"])
        self.native_receipt = record

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.native_handle is not None:
            os.close(self.native_handle)
        self.directory.cleanup()

    def respond(self, packet, writer):
        if not isinstance(packet, dict) or packet.get("schema_version") != 1:
            raise FleetError("request", "Unsupported delegation request")
        with self.manager.store.transaction() as c:
            parent = self.manager.store.get(self.rid, c)
        token = packet.get("token")
        if (
            parent["status"] != "running"
            or parent["generation"] != self.generation
            or not isinstance(token, str)
            or not secrets.compare_digest(token, parent["child_token"])
        ):
            raise FleetError("delegation_auth", "Delegation capability is invalid or obsolete")
        pid, uid, _ = writer
        if (
            uid != os.getuid()
            or self.native_handle is None
            or select.select([self.native_handle], [], [], 0)[0]
            or parent.get("native_process") != self.native_receipt
            or not processes.descendant(pid, self.native_receipt)
        ):
            raise FleetError("delegation_auth", "Writer is not an attributable native descendant")
        operation, args, kwargs = packet.get("operation"), packet.get("args"), packet.get("kwargs")
        if operation not in OPERATIONS:
            raise FleetError("delegation_scope", "Operation is unavailable to a delegated worker")
        if (
            not isinstance(args, list)
            or not isinstance(kwargs, dict)
            or any(key.startswith("_") for key in kwargs)
        ):
            raise FleetError("request", "Invalid delegation arguments")
        manager = copy.copy(self.manager)
        manager._principal = {"id": self.rid, "generation": self.generation, "token": parent["child_token"]}
        method = getattr(manager, operation)
        try:
            bound = inspect.signature(method).bind(*args, **kwargs)
        except TypeError:
            raise FleetError("request", "Invalid delegation arguments")
        if operation in RESOURCES:
            with manager.store.transaction() as c:
                try:
                    run = manager.store.get(bound.arguments["rid"], c)
                except FleetError:
                    raise FleetError("delegation_scope", "Run is outside this parent generation")
                manager._resource(run, c)
        if operation == "wait":
            timeout = bound.arguments.get("timeout")
            if timeout is not None and (
                not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0
            ):
                raise FleetError("request", "Invalid follower timeout")
            bound.arguments["timeout"] = min(timeout, 120) if timeout is not None else 120
        if (
            operation == "start_many"
            and isinstance(bound.arguments.get("targets"), list)
            and len(bound.arguments["targets"]) > 128
        ):
            raise FleetError("request", "Delegation batch exceeds its target limit")

        def invoke():
            if operation == "list":
                from .manager import public

                offset, limit = bound.arguments.get("offset", 0), bound.arguments.get("limit", 50)
                if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 500:
                    raise FleetError("pagination", "Invalid pagination")
                rows = [
                    r
                    for r in manager.store.all()
                    if (r.get("parent_run"), r.get("parent_generation")) == (self.rid, self.generation)
                ]
                return {
                    "schema_version": 1,
                    "runs": [public(r) for r in rows[offset : offset + limit]],
                    "total": len(rows),
                    "next_offset": offset + limit if len(rows) > offset + limit else None,
                }
            if operation == "routes":
                data = manager.routes(False)
                cfg = manager.configuration()
                data["routes"] = [
                    r
                    for r in data["routes"]
                    if r["id"] in parent["grant"]["routes"] and r["id"] in cfg["policy"]["routes"]
                ]
                if bound.arguments.get("diagnose", False):
                    for route in data["routes"]:
                        configured = manager._route(route["id"], cfg)
                        route["readiness"] = manager.adapters[route["harness"]].diagnose(configured)
                return data
            return method(*bound.args, **bound.kwargs)

        if operation not in MUTATIONS:
            return {"schema_version": 1, "result": invoke()}
        request_id = packet.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise FleetError("request", "Request id must contain 32 hexadecimal characters")
        fingerprint = hashlib.sha256(
            encoded({"operation": operation, "args": args, "kwargs": kwargs})
        ).hexdigest()
        key = f"delegation:{self.rid}:{self.generation}:{request_id}"
        with manager.store.transaction() as c:
            previous = c.execute("SELECT data FROM meta WHERE key=?", (key,)).fetchone()
            if previous:
                previous = json.loads(previous[0])
                if previous["fingerprint"] != fingerprint:
                    raise FleetError("request_conflict", "Request id was already bound to another operation")
                if previous["response"] is None:
                    raise FleetError(
                        "request_uncertain", f"Request {request_id} may be consumed; it will not be replayed"
                    )
                return previous["response"]
            c.execute(
                "INSERT INTO meta VALUES (?,?)",
                (key, json.dumps({"fingerprint": fingerprint, "response": None})),
            )
        try:
            response = {"schema_version": 1, "result": invoke()}
        except FleetError as error:
            response = error_record(error.code, str(error)[:4096])
        except Exception:
            response = error_record(
                "request_uncertain",
                f"Request {request_id} may be consumed; inspect child runs without replay",
            )
        with manager.store.transaction() as c:
            c.execute(
                "UPDATE meta SET data=? WHERE key=?",
                (json.dumps({"fingerprint": fingerprint, "response": response}), key),
            )
        return response
