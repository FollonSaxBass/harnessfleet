import json
import pytest
from harnessfleet.graph import Graph


SESSION = "10000000-0000-0000-0000-000000000001"
CHILD = "10000000-0000-0000-0000-000000000002"


def message(role, text):
    return {"role": role, "message": {"content": [{"type": "text", "text": text}]}}


def transcript(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records))


def connect_cursor(instance, root):
    instance.config["sources"] = [
        {
            "id": "cursor-project",
            "harness": "cursor_app",
            "environment": "app",
            "account": "example",
            "path": str(root),
            "format": "cursor-transcripts-v1",
            "enabled": True,
        }
    ]
    instance.save()


def test_cursor_ide_transcripts_need_no_chat_store(instance):
    root = instance.path / "agent-transcripts"
    transcript(
        root / SESSION / (SESSION + ".jsonl"),
        [
            message("user", "<context>PRIVATE CONTEXT</context><user_query>Review the project</user_query>"),
            message("assistant", "First reply"),
            {"type": "turn_ended", "status": "success"},
            message("user", "Follow-up"),
            message("assistant", "Final summary"),
            {"type": "turn_ended", "status": "success"},
        ],
    )
    transcript(
        root / SESSION / "subagents" / (CHILD + ".jsonl"),
        [
            message("user", "Inspect the public interface"),
            message("assistant", "Child findings"),
        ],
    )
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    page = json.loads(instance.cli("graph").stdout)
    assert page["total"] == 1
    parent = page["nodes"][0]
    assert parent["label"] == "Review the project" and parent["status"] == "done"
    assert parent["observed"] and not parent["control"] and parent["available_children"] == 1
    assert "reply" not in parent and "PRIVATE CONTEXT" not in json.dumps(page)
    child = graph.page(parent["id"])["nodes"][0]
    assert child["kind"] == "native_subagent" and child["status"] == "unknown"
    assert child["label"] == "Inspect the public interface"
    assert graph.output(parent["id"])["text"] == "Final summary"
    output = graph.output(child["id"])
    assert output["text"] == "Child findings" and output["observed"] and not output["verified"]
    assert page["sources"][0]["completeness"] == "partial" and not child["leaf_confirmed"]
    assert instance.m.list()["total"] == 0 and not instance.calls()


def test_cursor_disabled_missing_and_expired_sources_are_distinct(instance):
    root = instance.path / "agent-transcripts"
    transcript(
        root / SESSION / (SESSION + ".jsonl"), [message("user", "Visible"), message("assistant", "Reply")]
    )
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    node = graph.page()["nodes"][0]
    moved = root.with_name("offline")
    root.rename(moved)
    missing = graph.page()
    assert missing["sources"][0]["availability"] == "missing"
    assert missing["nodes"][0]["id"] == node["id"]
    assert missing["nodes"][0]["completeness"] == "expired"
    assert not missing["nodes"][0]["output_available"]
    instance.config["sources"][0]["enabled"] = False
    instance.save()
    assert graph.page()["sources"] == [] and graph.page()["total"] == 0


def test_cursor_malformed_and_missing_transcripts_preserve_other_sessions(instance):
    root = instance.path / "agent-transcripts"
    transcript(
        root / SESSION / (SESSION + ".jsonl"),
        [
            message("user", "Visible"),
            message("assistant", "Accepted observation"),
            {"type": "turn_ended", "status": "success"},
        ],
    )
    with (root / SESSION / (SESSION + ".jsonl")).open("a") as stream:
        stream.write('{"role":"user",')
    (root / CHILD).mkdir()
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    result = graph.page()
    assert result["total"] == 2
    assert any(issue["code"] == "cursor_record" for issue in result["issues"])
    assert any(issue["code"] == "cursor_transcript_missing" for issue in result["issues"])
    assert all(node["status"] == "unknown" for node in result["nodes"])


def test_cursor_excessively_nested_record_does_not_crash_graph(instance):
    root = instance.path / "agent-transcripts"
    path = root / SESSION / (SESSION + ".jsonl")
    transcript(path, [message("user", "Visible")])
    with path.open("a") as stream:
        stream.write("[" * 20000 + "0" + "]" * 20000 + "\n")
    connect_cursor(instance, root)
    result = json.loads(instance.cli("graph").stdout)
    assert result["total"] == 1
    assert any(issue["code"] == "cursor_record" for issue in result["issues"])


@pytest.mark.parametrize("replacement", ["root", "session", "transcript", "children"])
def test_cursor_symlink_sources_never_expose_target_replies(instance, replacement):
    root = instance.path / "agent-transcripts"
    session = root / SESSION
    transcript(session / (SESSION + ".jsonl"), [message("user", "Public label")])
    outside = instance.path / "outside"
    transcript(outside / "sensitive.jsonl", [message("assistant", "OUTSIDE SENTINEL")])
    if replacement == "root":
        root.rename(outside / "saved-root")
        root.symlink_to(outside, target_is_directory=True)
    elif replacement == "session":
        session.rename(outside / "saved-session")
        session.symlink_to(outside, target_is_directory=True)
    elif replacement == "transcript":
        (session / (SESSION + ".jsonl")).unlink()
        (session / (SESSION + ".jsonl")).symlink_to(outside / "sensitive.jsonl")
    else:
        (session / "subagents").symlink_to(outside, target_is_directory=True)
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    page = graph.page()
    assert "OUTSIDE SENTINEL" not in json.dumps(graph.snapshot())
    if replacement == "root":
        assert page["sources"][0]["availability"] == "unreadable"
    else:
        assert any("unreadable" in issue["code"] for issue in page["issues"])


def test_cursor_latest_reply_is_bounded_and_redacted_after_large_history(instance):
    root = instance.path / "agent-transcripts"
    secret = "sk-" + "SyntheticSentinel12345"
    records = [message("user", "Inspect " + secret)]
    records += [message("assistant", "Old " + "x" * 200000)] * 3
    records += [
        message("assistant", "Latest " + secret + " " + "y" * 80000),
        {"type": "turn_ended", "status": "success"},
    ]
    transcript(root / SESSION / (SESSION + ".jsonl"), records)
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    page = graph.page()
    node = page["nodes"][0]
    output = graph.output(node["id"])
    assert secret not in json.dumps(page) + output["text"]
    assert output["text"].startswith("Latest [redacted]") and len(output["text"]) <= 65536
    assert node["status"] == "done" and not node["leaf_confirmed"]
    assert any(issue["code"] == "cursor_transcript_partial" for issue in page["issues"])


@pytest.mark.parametrize("huge_reply", [False, True])
def test_cursor_windowed_history_never_presents_head_reply_as_latest(instance, huge_reply):
    root = instance.path / "agent-transcripts"
    records = [message("user", "Inspect history"), message("assistant", "STALE HEAD REPLY")]
    if huge_reply:
        records.append(message("assistant", "Latest " + "x" * 400000))
    else:
        records += [
            {"type": "progress", "data": "x" * 200000},
            message("assistant", "LATEST IN OMITTED MIDDLE"),
            {"type": "progress", "data": "x" * 200000},
        ]
    records.append({"type": "turn_ended", "status": "success"})
    transcript(root / SESSION / (SESSION + ".jsonl"), records)
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    page = graph.page()
    node = page["nodes"][0]
    assert node["label"] == "Inspect history"
    assert not node["output_available"]
    assert any(issue["code"] == "cursor_transcript_partial" for issue in page["issues"])


def test_cursor_subagent_identifiers_do_not_merge_across_sessions(instance):
    root = instance.path / "agent-transcripts"
    for identifier in [SESSION, CHILD]:
        transcript(root / identifier / (identifier + ".jsonl"), [message("user", identifier)])
        transcript(root / identifier / "subagents" / "same-child.jsonl", [message("user", "Child")])
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    roots = graph.page()["nodes"]
    children = [graph.page(node["id"])["nodes"][0] for node in roots]
    assert len({node["id"] for node in children}) == 2
    assert all(node["available_children"] == 1 for node in roots)


def test_cursor_read_budget_keeps_every_discovered_session_navigable(instance):
    root = instance.path / "agent-transcripts"
    for index in range(70):
        identifier = f"10000000-0000-0000-0000-{index:012x}"
        transcript(
            root / identifier / (identifier + ".jsonl"),
            [
                message("user", f"Session {index}"),
                message("assistant", "x" * 300000),
            ],
        )
    connect_cursor(instance, root)
    graph = Graph(instance.m)
    first = graph.page(limit=40)
    second = graph.page(offset=first["next_offset"], limit=40)
    assert first["total"] == 70 and len(first["nodes"]) + len(second["nodes"]) == 70
    assert any(issue["code"] == "source_budget" for issue in first["issues"])
    assert all(not node["leaf_confirmed"] for node in first["nodes"] + second["nodes"])
