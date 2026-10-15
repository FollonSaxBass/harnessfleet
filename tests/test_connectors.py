import json


def test_connector_items_are_bounded_redacted_metadata(instance):
    secret = "sk-" + "SyntheticSentinel12345"
    source = instance.path / "tasks.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completeness": "complete",
                "nodes": [
                    {
                        "id": "task-1",
                        "label": secret + "x" * 1000,
                        "kind": "task",
                        "reply": secret + "y" * 70000,
                        "private": {"value": secret},
                    },
                    "invalid-row",
                    {"id": "bad", "label": []},
                ],
            }
        )
    )
    instance.config["connectors"] = {"tasks": {"enabled": True, "path": str(source)}}
    instance.save()
    response = instance.cli("connectors", "tasks")
    result = json.loads(response.stdout)
    assert secret not in response.stdout
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["id"] == "task-1" and item["kind"] == "task"
    assert item["label"].startswith("[redacted]") and len(item["label"]) <= 200
    assert "reply" not in item and "private" not in item
    assert result["completeness"] == "partial"
    assert len(result["issues"]) == 2
    assert not instance.calls()
