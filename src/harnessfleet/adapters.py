"""Native protocol boundaries and public entry-point discovery."""

import json
import os
import shutil
import subprocess
from dataclasses import asdict
from importlib.metadata import entry_points
from pathlib import Path
from .contracts import Capabilities, FleetError, Result

AUTH_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CURSOR_API_KEY",
    "CURSOR_AUTH_TOKEN",
)


def inline_toml(value):
    """Encode the owned stdio configuration without interpolating native TOML."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + inline_toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(inline_toml(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


class Base:
    contract_version = 1
    capabilities = Capabilities()
    executable = ""

    def diagnose(self, route):
        executable = route.get("executable", self.executable)
        found = shutil.which(executable)
        return {
            "available": bool(found),
            "executable_found": bool(found),
            "authentication": "unknown",
            "guidance": f"Install {self.executable} and authenticate using its native CLI",
        }

    def environment(self, route, ambient):
        # Only explicitly referenced credentials survive; no fallback to ambient API keys.
        env = {
            k: v
            for k, v in ambient.items()
            if k not in AUTH_KEYS and not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
        }
        env.pop("CLAUDECODE", None)
        for key, reference in route.get("credential_env", {}).items():
            if reference not in ambient:
                raise FleetError("authentication", "Configured credential reference is unavailable")
            env[key] = ambient[reference]
        for key, value in route.get("environment_vars", {}).items():
            if key not in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "DSH_HOME"):
                raise FleetError("configuration", "Unsupported native configuration reference")
            env[key] = str(Path(value).expanduser().absolute())
        return env

    def consume(self, event, result):
        if event.get("type") == "result":
            result.completed = event.get("status") == "success"
            result.output = event.get("output", "")
            result.session_id = event.get("session_id", result.session_id)
        classify(event, result)


class Codex(Base):
    id = "codex_cli"
    executable = "codex"
    capabilities = Capabilities(continuation=True, permissions=("read-only", "workspace-write"))

    def diagnose(self, route):
        data = super().diagnose(route)
        if data["available"]:
            try:
                env = self.environment(route, os.environ)
                r = subprocess.run(
                    [route.get("executable", self.executable), "login", "status"],
                    env=env,
                    capture_output=True,
                    timeout=5,
                )
                # No model invocation; never return the native account string.
                data["authentication"] = "ready" if r.returncode == 0 else "missing"
                data["available"] = r.returncode == 0
            except (OSError, subprocess.TimeoutExpired, FleetError):
                data.update(available=False, authentication="unavailable")
        return data

    def command(self, run, directory):
        route = run["route"]
        a = [route.get("executable", self.executable), "exec"]
        if run["generation"] > 1:
            a += ["resume", run["resume_session_id"]]
        a += [
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--json",
            "-c",
            "sandbox_mode=" + json.dumps(run["permission"]),
            "-c",
            'approval_policy="never"',
            "-c",
            "mcp_servers=" + inline_toml(run.get("_mcp_servers", {})),
            "-c",
            "forced_login_method=" + json.dumps("chatgpt" if route["billing"] == "subscription" else "api"),
        ]
        if run.get("model"):
            a += ["-m", run["model"]]
        return a + ["--", run["assignment"]]

    def consume(self, event, result):
        t = event.get("type")
        if result.completed:
            raise FleetError("protocol", "Native event arrived after verified completion")
        if t == "thread.started":
            session = event.get("thread_id")
            if result.session_id and result.session_id != session:
                raise FleetError("protocol", "Native session changed inside one generation")
            result.session_id = session
        if t == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
            result.output = event["item"].get("text", "")
        if t == "turn.completed":
            result.completed = True
        if t in ("turn.failed", "error"):
            result.failure = "execution"
        classify(event, result)


class Claude(Base):
    id = "claude_code"
    executable = "claude"
    capabilities = Capabilities(continuation=True, permissions=("read-only", "workspace-write", "delegate"))

    def settings(self, route):
        return [
            "--setting-sources",
            "",
            "--settings",
            json.dumps(
                {
                    "disableAllHooks": True,
                    "forceLoginMethod": "claudeai" if route["billing"] == "subscription" else "console",
                }
            ),
        ]

    def diagnose(self, route):
        data = super().diagnose(route)
        if data["available"]:
            try:
                r = subprocess.run(
                    [
                        route.get("executable", self.executable),
                        *self.settings(route),
                        "auth",
                        "status",
                        "--json",
                    ],
                    env=self.environment(route, os.environ),
                    capture_output=True,
                    timeout=5,
                )
                v = json.loads(r.stdout)
                ready = r.returncode == 0 and v.get("loggedIn") is True
                if route["billing"] == "subscription":
                    ready = (
                        ready and v.get("authMethod") == "claude.ai" and v.get("apiProvider") == "firstParty"
                    )
                data.update(available=ready, authentication="ready" if ready else "missing")
            except (OSError, ValueError, subprocess.TimeoutExpired, FleetError):
                data.update(available=False, authentication="unavailable")
        return data

    def command(self, run, directory):
        route = run["route"]
        tools = "Read,Glob,Grep"
        if run["permission"] == "workspace-write":
            tools += ",Edit,Write,NotebookEdit"
        if run["permission"] == "delegate":
            tools += ",Edit,Write,NotebookEdit,Bash"
        a = [
            route.get("executable", self.executable),
            *self.settings(route),
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--system-prompt-snapshot",
            "off",
        ]
        if run.get("model"):
            a += ["--model", run["model"]]
        if run["generation"] > 1:
            a += ["--resume", run["resume_session_id"], "--fork-session"]
        return a + ["--", run["assignment"]]

    def consume(self, event, result):
        # Forwarded subagent replies do not replace this generation's result.
        if result.completed:
            raise FleetError("protocol", "Native event arrived after verified completion")
        if event.get("parent_tool_use_id"):
            return
        if event.get("type") == "system" and event.get("subtype") == "init":
            result.session_id = event.get("session_id")
        if event.get("type") == "assistant":
            if event.get("error") == "authentication_failed":
                result.failure = "authentication"
            text = "\n".join(
                b.get("text", "")
                for b in event.get("message", {}).get("content", [])
                if b.get("type") == "text"
            )
            if text:
                result.output = text
        if event.get("type") == "result":
            result.completed = event.get("subtype") == "success" and not event.get("is_error", False)
            result.output = event.get("result", result.output)
            final_session = event.get("session_id", result.session_id)
            if result.session_id and final_session != result.session_id:
                raise FleetError("protocol", "Native completion session changed")
            result.session_id = final_session
            if not result.completed:
                result.failure = result.failure or "execution"
                message = str(event.get("result", ""))
                if event.get("is_error") and message.startswith(
                    ("Failed to refresh OAuth token:", "Invalid API key")
                ):
                    result.failure = "authentication"
        classify(event, result)


class Cursor(Base):
    id = "cursor_agent"
    executable = "cursor-agent"
    capabilities = Capabilities(permissions=("unrestricted",))

    def command(self, run, directory):
        if run["route"]["billing"] != "metered":
            raise FleetError(
                "billing", "Cursor billing cannot be proven Subscription; select an authorized Metered Route"
            )
        return [
            run["route"].get("executable", self.executable),
            "--print",
            "--output-format",
            "stream-json",
            "--force",
            "--model",
            run["model"],
            run["assignment"],
        ]

    def consume(self, event, result):
        if event.get("type") == "result":
            result.completed = event.get("subtype") == "success" and not event.get("is_error", False)
            result.output = event.get("result", "")
            result.session_id = event.get("session_id")
        classify(event, result)


class Unqualified(Base):
    capabilities = Capabilities(
        launch=False,
        output=False,
        permissions=(),
        platforms={"linux": "unsupported", "darwin": "unsupported"},
    )

    def __init__(self, id, executable, reason):
        self.id, self.executable, self.reason = id, executable, reason

    def diagnose(self, route):
        return {**super().diagnose(route), "available": False, "guidance": self.reason}

    def command(self, run, directory):
        raise FleetError("unsupported", self.reason)


class Dsh(Base):
    id = "dsh"
    executable = "dsh"
    capabilities = Capabilities(permissions=("unrestricted",))

    def command(self, run, directory):
        route = run["route"]
        profile = route.get("transport") or {}
        if route["billing"] != "metered" or not all(
            profile.get(k) for k in ("base_url", "api_key_env", "provider")
        ):
            raise FleetError("transport", "DSH needs an explicit Metered OpenAI-compatible transport profile")
        provider = profile["provider"]
        overlay = [
            {"id": "agent-default-model", "config": {"provider": provider, "model": run["model"]}},
            {
                "id": "llm-pi-ai",
                "config": {
                    "providers": {
                        provider: {
                            "api": "openai-completions",
                            "baseURL": profile["base_url"],
                            "apiKeyEnv": profile["api_key_env"],
                            "retryPolicy": {"mode": "normal", "maxRetries": 0},
                            "models": [{"id": run["model"], "name": run["model"]}],
                        }
                    }
                },
            },
        ]
        path = Path(directory) / "dsh-overlay.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(overlay, f)
        return [
            route.get("executable", self.executable),
            "--headless",
            "--patch",
            str(path),
            run["assignment"],
        ]


BUILTINS = {
    a.id: a
    for a in [
        Codex(),
        Claude(),
        Cursor(),
        Dsh(),
        Unqualified("codex_app", "codex", "Attachment awaits an explicit connected-store lease backend"),
        Unqualified(
            "agy_cli", "agy", "Windows process ownership is not qualified; native CLI launch is disabled"
        ),
        Unqualified("agy_app", "agy", "App attachment and external process ownership are not qualified"),
    ]
}


def registry():
    adapters = dict(BUILTINS)
    for ep in entry_points(group="harnessfleet.adapters"):
        try:
            a = ep.load()()
            if a.contract_version != 1 or a.id in adapters or not isinstance(a.capabilities, Capabilities):
                raise ValueError()
            for method in ("diagnose", "command", "environment", "consume"):
                if not callable(getattr(a, method, None)):
                    raise ValueError()
            if a.capabilities.quota_recovery and not callable(getattr(a, "quota_ready", None)):
                raise ValueError()
            adapters[a.id] = a
        except Exception as e:
            raise FleetError("plugin", f"Adapter plugin {ep.name} violates contract v1") from e
    return adapters


def describe(a):
    capabilities = asdict(a.capabilities)
    support = {}
    for name in ("launch", "continuation", "attachment", "quota_recovery", "output", "native_lineage"):
        support[name] = {
            platform: level if getattr(a.capabilities, name) else "unsupported"
            for platform, level in a.capabilities.platforms.items()
        }
    support["permissions"] = {
        permission: dict(a.capabilities.platforms) for permission in a.capabilities.permissions
    }
    return {
        "id": a.id,
        "contract_version": a.contract_version,
        "capabilities": capabilities,
        "capability_support": support,
    }


def classify(event, result: Result):
    """Only structured error fields classify quota/auth; ordinary prose never does."""
    if not isinstance(event, dict):
        raise FleetError("protocol", "Native event must be an object")
    error = event.get("error")
    code = (error.get("code") or error.get("type")) if isinstance(error, dict) else None
    if event.get("type") in ("error", "turn.failed", "result"):
        code = code or event.get("error_code")
        if code in ("insufficient_quota", "usage_limit_reached", "quota_exceeded", "rate_limit_exceeded"):
            result.failure = "quota"
            reset = event.get("reset_at") or (error or {}).get("reset_at")
            if isinstance(reset, (int, float)):
                result.reset_at = reset
        elif code in ("unauthorized", "authentication_error", "invalid_api_key"):
            result.failure = "authentication"
