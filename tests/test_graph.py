import json
from harnessfleet.graph import Graph, native_key, run_key


def connect(instance, nodes, **options):
    path = instance.path / "native.json"
    path.write_text(json.dumps({"schema_version": 1, "completeness": "complete", "nodes": nodes}))
    source = {
        "id": "source-one",
        "harness": "codex_cli",
        "environment": "host",
        "account": "example",
        "path": str(path),
        "enabled": True,
        **options,
    }
    instance.config["sources"] = [source]
    instance.save()
    return source, path


def test_complete_tree_pagination_and_ancestor_filter(instance):
    nodes = [{"id": "root", "label": "root"}]
    nodes += [{"id": str(i), "parent": "root", "label": "target" if i == 187 else str(i)} for i in range(220)]
    source, _ = connect(instance, nodes)
    g = Graph(instance.m)
    root = g.page()["nodes"][0]
    assert root["available_children"] == 220
    seen = []
    offset = 0
    while offset is not None:
        p = g.page(root["id"], offset=offset, limit=37)
        seen.extend(n["id"] for n in p["nodes"])
        offset = p["next_offset"]
    assert len(set(seen)) == 220
    filtered = g.page(query="target")
    assert filtered["nodes"][0]["id"] == root["id"]
    assert g.page(root["id"], query="target")["total"] == 1


def test_depth_is_not_silently_truncated(instance):
    nodes = [{"id": str(i), "parent": str(i - 1)} if i else {"id": "0"} for i in range(260)]
    connect(instance, nodes)
    g = Graph(instance.m)
    # Snapshot contains the entire available chain, independent of page budget.
    snapshot = g.snapshot()
    assert len(snapshot["nodes"]) == 260 and len(snapshot["edges"]) == 259
    deepest = g.page(query='"label": "259"')["nodes"]
    assert len(deepest) == 1


def test_colliding_ids_are_namespaced(instance):
    source, path = connect(instance, [{"id": "same"}])
    instance.config["sources"].append({**source, "id": "source-two", "account": "other"})
    instance.save()
    graph = Graph(instance.m).snapshot()
    assert len(graph["nodes"]) == 2
    assert len(set(graph["nodes"])) == 2


def test_duplicate_cycles_and_unknown_parent_are_explicit(instance):
    connect(
        instance,
        [
            {"id": "a", "parent": "b"},
            {"id": "b", "parent": "a"},
            {"id": "a"},
            {"id": "missing", "parent": "unknown"},
        ],
    )
    graph = Graph(instance.m).snapshot()
    assert {"cycle", "duplicate_native_identity", "unknown_native_parent"} <= {
        i["code"] for i in graph["issues"]
    }
    assert len(Graph(instance.m).page()["nodes"]) >= 1


def test_missing_source_preserves_observed_ancestry(instance):
    source, path = connect(
        instance, [{"id": "parent"}, {"id": "child", "parent": "parent", "reply": "observed reply"}]
    )
    graph = Graph(instance.m)
    assert len(graph.snapshot()["nodes"]) == 2
    path.unlink()
    snapshot = graph.snapshot()
    assert len(snapshot["nodes"]) == 2
    assert snapshot["sources"][0]["availability"] == "missing"
    assert all(
        n["completeness"] == "expired" and not n["output_available"] for n in snapshot["nodes"].values()
    )
    assert not graph.page()["nodes"][0]["leaf_confirmed"]


def test_disabled_sources_are_never_read(instance):
    source, path = connect(instance, [{"id": "parent"}], enabled=False)
    assert Graph(instance.m).snapshot()["sources"] == []
    assert Graph(instance.m).snapshot()["nodes"] == {}


def test_empty_is_distinct_from_missing_unsupported_and_malformed(instance):
    source, path = connect(instance, [])
    g = Graph(instance.m)
    assert g.page()["sources"][0]["completeness"] == "complete"
    path.write_text("not JSON")
    assert g.page()["sources"][0]["completeness"] == "unreadable"
    instance.config["sources"][0]["format"] = "unknown"
    instance.save()
    assert g.page()["sources"][0]["completeness"] == "unsupported"


def test_malformed_record_does_not_hide_other_nodes(instance):
    connect(instance, [{"id": "bad", "completeness": []}, {"id": "bad2", "parent": {}}, {"id": "good"}])
    g = Graph(instance.m).snapshot()
    assert len(g["nodes"]) == 1
    assert g["sources"][0]["completeness"] == "partial"
    assert len(g["issues"]) == 2


def test_observation_has_no_control_authority(instance):
    source, _ = connect(instance, [{"id": "observed", "reply": "native answer"}])
    g = Graph(instance.m)
    node = g.page()["nodes"][0]
    assert node["observed"] and not node["control"]
    assert g.output(node["id"]) == {
        "schema_version": 1,
        "id": node["id"],
        "verified": False,
        "observed": True,
        "text": "native answer",
    }
    assert instance.m.list()["total"] == 0


def test_exact_caller_and_generation_occurrences(instance):
    parent = instance.start("mode:recursive")
    instance.finish(parent)
    child = [r for r in instance.m.store.all() if r["parent_run"] == parent["id"]][0]
    old = instance.m.store.get(parent["id"])
    nextgen = instance.m.resume(parent["id"], "mode:normal")
    instance.finish(nextgen)
    binding1 = {"run_id": parent["id"], "generation": 1}
    binding2 = {"run_id": parent["id"], "generation": 2}
    source, _ = connect(
        instance,
        [
            {
                "id": old["session_id"],
                "binding": binding1,
                "label": "generation one",
                "delegations": [{"run_id": child["id"], "generation": 1}],
            },
            {"id": old["session_id"], "binding": binding2, "label": "generation two"},
        ],
    )
    g = Graph(instance.m).snapshot()
    caller = g["nodes"][run_key(child["id"], 1)]["run"]["caller"]
    assert caller == native_key(source, old["session_id"], binding1)
    assert caller != native_key(source, old["session_id"], binding2)
    assert g["nodes"][run_key(child["id"], 1)]["run"]["parent_generation"] == 1


def test_foreign_account_cannot_bind_managed_generation(instance):
    run = instance.start()
    instance.finish(run)
    session = instance.m.status(run["id"])["session_id"]
    source, _ = connect(
        instance, [{"id": session, "binding": {"run_id": run["id"], "generation": 1}}], account="other"
    )
    g = Graph(instance.m).snapshot()
    assert len(g["nodes"]) == 1
    assert any(i["code"] == "invalid_generation_binding" for i in g["issues"])
