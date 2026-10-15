"""Optional user-owned webhook. No implicit provider or credential discovery."""

import json
import os
import urllib.request
from .manager import public


def notify(manager, run):
    settings = manager.configuration().get("notifications", {})
    url = settings.get("webhook")
    if not url:
        return
    # Credentials are references; request bodies never contain task text or native logs.
    headers = {"Content-Type": "application/json"}
    if settings.get("bearer_env"):
        token = os.environ.get(settings["bearer_env"])
        if not token:
            record(manager, run, "missing_credential")
            return
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url, data=json.dumps(public(run)).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(4096)
    except Exception:
        record(manager, run, "delivery_failed")


def record(manager, run, code):
    with manager.store.transaction() as c:
        manager.store.event(c, run, "notification_failed", {"code": code})
