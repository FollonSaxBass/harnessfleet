"""Explicit read-only observation of one Cursor project's agent-transcripts directory."""

import contextlib
import json
import os
import re
import stat
from pathlib import Path

SESSION = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
CHILD = re.compile(r"[A-Za-z0-9_-]{1,128}\.jsonl")
QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
SOURCE_BYTES = 16 * 1024 * 1024
TRANSCRIPT_BYTES = 256 * 1024


@contextlib.contextmanager
def directory(path, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def text(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    )


def records(parent, name, budget):
    if budget[0] <= 0:
        raise ValueError("source_budget")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("cursor_transcript_unreadable")
        limit = min(TRANSCRIPT_BYTES, budget[0])
        limited = size > limit
        if limited:
            head_size = min(65536, limit // 2)
            head = stream.read(head_size)
            stream.seek(size - (limit - head_size))
            tail = stream.read(limit - head_size)
            budget[0] -= len(head) + len(tail)
            chunks = [head.rpartition(b"\n")[0], tail.partition(b"\n")[2]]
        else:
            chunks = [stream.read(limit)]
            budget[0] -= len(chunks[0])
    parsed, malformed = [], False
    for index, chunk in enumerate(chunks):
        for line in chunk.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError()
                # A windowed head supplies the session label, never current output/status.
                if limited and index == 0 and record.get("role") != "user":
                    continue
                parsed.append(record)
            except (ValueError, RecursionError):
                malformed = True
    return parsed, limited, malformed


def load(source):
    info = {
        "id": source["id"],
        "harness": source["harness"],
        "availability": "available",
        "completeness": "partial",
        "capabilities": {"observation": True, "control": False, "output": True},
        "issues": [],
    }
    nodes, budget = [], [SOURCE_BYTES]

    def issue(code, identifier):
        info["issues"].append({"code": code, "id": identifier})

    def row(parent, name, identifier, kind, native_parent=None):
        node = {
            "id": identifier,
            "kind": kind,
            "label": identifier,
            "status": "unknown",
            "completeness": "partial",
        }
        if native_parent:
            node["parent"] = native_parent
        nodes.append(node)
        try:
            parsed, limited, malformed = records(parent, name, budget)
        except FileNotFoundError:
            node["completeness"] = "missing"
            issue("cursor_transcript_missing", identifier)
            return
        except (OSError, ValueError) as error:
            code = str(error) if isinstance(error, ValueError) else "cursor_transcript_unreadable"
            node["completeness"] = "partial" if code == "source_budget" else "unreadable"
            issue(code, identifier)
            return
        for record in parsed:
            role = record.get("role")
            if record.get("type") == "turn_ended":
                node["status"] = "done" if record.get("status") == "success" else "unknown"
            elif role in ("user", "assistant"):
                node["status"] = "unknown"
                value = text(record)
                if role == "user" and node["label"] == identifier:
                    match = QUERY.search(value)
                    label = match.group(1) if match else value if not value.lstrip().startswith("<") else ""
                    if label.strip():
                        node["label"] = label.strip()
                elif role == "assistant" and value:
                    node["reply"] = value
        if limited:
            issue("cursor_transcript_partial", identifier)
        if malformed:
            node["status"] = "unknown"
            issue("cursor_record", identifier)

    try:
        with directory(Path(source["path"]).expanduser()) as root:
            for session in sorted(os.listdir(root)):
                if not SESSION.fullmatch(session):
                    continue
                try:
                    with directory(session, root) as parent:
                        row(parent, session + ".jsonl", session, "session")
                        try:
                            with directory("subagents", parent) as children:
                                for name in sorted(os.listdir(children)):
                                    if CHILD.fullmatch(name):
                                        row(
                                            children,
                                            name,
                                            session + ":subagent:" + name[:-6],
                                            "native_subagent",
                                            session,
                                        )
                        except FileNotFoundError:
                            pass
                        except OSError:
                            issue("cursor_children_unreadable", session)
                except OSError:
                    nodes.append(
                        {
                            "id": session,
                            "label": session,
                            "kind": "session",
                            "status": "unknown",
                            "completeness": "unreadable",
                        }
                    )
                    issue("cursor_directory_unreadable", session)
    except FileNotFoundError:
        info.update(availability="missing", completeness="missing")
    except OSError:
        info.update(availability="unreadable", completeness="unreadable")
    return info, {"schema_version": 1, "completeness": info["completeness"], "nodes": nodes}
