import json
import threading
import urllib.request
import urllib.error
import pytest
from harnessfleet.dashboard import server


def populate(instance, count=85):
    source = instance.path / "ui-native.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completeness": "complete",
                "nodes": [
                    {"id": str(i), "label": f"Agent {i:03d}", "reply": "Synthetic reply", "status": "done"}
                    for i in range(count)
                ]
                + [{"id": "child", "parent": "0", "label": "Nested reviewer", "status": "working"}],
            }
        )
    )
    instance.config["sources"] = [
        {
            "id": "ui",
            "harness": "synthetic",
            "environment": "demo",
            "account": "example",
            "path": str(source),
            "enabled": True,
        }
    ]
    instance.save()
    return source


@pytest.fixture
def dashboard(instance):
    populate(instance)
    http = server(instance.m, 0)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    http.server_close()
    thread.join()


def test_http_origin_and_path_gates(dashboard):
    with urllib.request.urlopen(dashboard + "/api/graph") as r:
        assert json.load(r)["total"] == 85
    for path, headers in [("/api/output?id=../../secret", {}), ("/api/graph", {"Host": "attacker.invalid"})]:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(dashboard + path, headers=headers))
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(
            urllib.request.Request(
                dashboard + "/api/stop", data=b'{"id":"fake"}', headers={"Content-Type": "application/json"}
            )
        )
    assert e.value.code == 403


@pytest.mark.parametrize(
    "method,path,headers,body,status",
    [
        ("GET", "/missing", {}, None, 404),
        ("GET", "/api/graph?limit=bad", {}, None, 400),
        ("GET", "/api/graph", {"Host": "attacker.invalid"}, None, 403),
        ("POST", "/api/stop", {}, b"{}", 403),
    ],
)
def test_http_errors_use_the_public_envelope(dashboard, method, path, headers, body, status):
    request = urllib.request.Request(dashboard + path, data=body, headers=headers, method=method)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == status
    result = json.load(error.value)
    assert result["schema_version"] == 1
    assert isinstance(result["error"]["code"], str) and result["error"]["code"]
    assert isinstance(result["error"]["message"], str) and result["error"]["message"]


@pytest.mark.parametrize("body", [b"[]", b"null", b'{"id":42}', b"not-json"])
def test_authorized_malformed_control_is_a_contract_error(dashboard, body):
    with urllib.request.urlopen(dashboard + "/api/session") as response:
        token = json.load(response)["csrf"]
    headers = {"Origin": dashboard, "X-HarnessFleet-Token": token, "Content-Type": "application/json"}
    request = urllib.request.Request(dashboard + "/api/stop", data=body, headers=headers)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    result = json.load(error.value)
    assert error.value.code == 400 and result["schema_version"] == 1


def test_malformed_source_does_not_break_http_graph(dashboard, instance):
    instance.config["sources"].append("invalid-source")
    instance.save()
    with urllib.request.urlopen(dashboard + "/api/graph") as response:
        page = json.load(response)
    assert page["total"] == 85
    assert any(issue["code"] == "source_configuration" for issue in page["issues"])


def test_browser_pagination_focus_selection_and_refresh(dashboard, instance):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(dashboard)
        page.get_by_role("button", name="Load more").click()
        expect(page.locator(".select")).to_have_count(80)
        # Select a node loaded through root pagination, then keep keyboard focus.
        selected = page.locator(".select").nth(60)
        label = selected.inner_text()
        selected.click()
        selected.focus()
        key = selected.get_attribute("data-focus")
        page.evaluate("() => refresh()")
        assert page.locator(".select").count() == 80
        assert page.evaluate("document.activeElement.dataset.focus") == key
        assert label.split("\n")[0] in page.locator("#selection").inner_text()
        page.get_by_role("button", name="Read output").click()
        from playwright.sync_api import expect

        expect(page.locator("#reply")).to_contain_text("Synthetic reply")
        assert not errors
        browser.close()


def test_browser_expansion_and_source_loss_preserve_context(dashboard, instance):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(dashboard)
        # Filtering retains Agent 000, which owns the matching child.
        page.locator("#filter").fill("Nested reviewer")
        expect(page.locator(".select")).to_have_count(1)
        page.get_by_role("button", name="Expand Agent 000", exact=True).click()
        page.get_by_role("treeitem", name="Nested reviewer", exact=False).click()
        instance.path.joinpath("ui-native.json").unlink()
        page.evaluate("() => refresh()")
        assert "Nested reviewer" in page.locator("#selection").inner_text()
        assert page.get_by_role("treeitem", name="Nested reviewer", exact=False).count() == 1
        assert "missing" in page.locator("#sources").inner_text()
        browser.close()


def test_browser_filter_during_initial_load_is_not_lost(dashboard):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        pending = []

        def delay_initial_graph(route):
            if not pending:
                pending.append(route)
            else:
                route.continue_()

        page.route("**/api/graph?*", delay_initial_graph)
        page.clock.install()
        with page.expect_request("**/api/graph?*"):
            page.goto(dashboard)
        page.locator("#filter").fill("Nested reviewer")
        page.clock.fast_forward(1000)
        assert pending, "Initial graph request must still be held while the filter changes"
        pending[0].continue_()
        expect(page.locator(".select")).to_have_count(1)
        expect(page.get_by_role("button", name="Expand Agent 000", exact=True)).to_be_enabled()
        browser.close()


def test_browser_observes_cursor_without_a_chat_store(dashboard, instance):
    from playwright.sync_api import sync_playwright, expect

    root = instance.path / "agent-transcripts"
    session = "20000000-0000-0000-0000-000000000001"
    path = root / session / (session + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"role": "user", "message": {"content": "Cursor IDE review"}}) + "\n")
    child = path.parent / "subagents" / "reviewer.jsonl"
    child.parent.mkdir()
    child.write_text(
        json.dumps({"role": "assistant", "message": {"content": "Observed Cursor findings"}}) + "\n"
    )
    instance.config["sources"] = [
        {
            "id": "cursor-ide",
            "harness": "cursor_app",
            "environment": "app",
            "account": "example",
            "path": str(root),
            "format": "cursor-transcripts-v1",
            "enabled": True,
        }
    ]
    instance.save()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(dashboard)
        page.get_by_role("button", name="Expand Cursor IDE review", exact=True).click()
        page.get_by_role("treeitem").filter(has_text="Native subagent").click()
        page.get_by_role("button", name="Read output").click()
        expect(page.locator("#reply")).to_contain_text("Observed native reply")
        expect(page.locator("#reply")).to_contain_text("Observed Cursor findings")
        expect(page.locator("#stop")).to_be_hidden()
        browser.close()
    assert not instance.calls() and instance.m.list()["total"] == 0
