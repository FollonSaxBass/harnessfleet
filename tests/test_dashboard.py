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
