"""Installed CLI, the primary cross-harness delegation boundary."""

import argparse
import json
import tempfile
from pathlib import Path
from . import __version__, config
from .contracts import FleetError
from .manager import RunManager


def parser():
    p = argparse.ArgumentParser(
        prog="harnessfleet", description="Local cross-harness delegation with explicit authority"
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config")
    p.add_argument("--state")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    d = sub.add_parser("demo")
    d.add_argument("--directory")
    d.add_argument("--serve", action="store_true")
    d.add_argument("--port", type=int, default=8421)
    i = sub.add_parser("integrate")
    i.add_argument("--harness", choices=config.HARNESS_IDS)
    i.add_argument("--directory", required=True)
    i.add_argument("--compatibility", action="store_true")
    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int)
    svc = sub.add_parser("service")
    svc.add_argument("action", choices=("start", "stop", "status", "run", "once"))
    c = sub.add_parser("connectors")
    c.add_argument("kind", choices=("tasks", "handoffs", "ownership", "documentation", "quotas", "projects"))
    g = sub.add_parser("graph")
    g.add_argument("--parent")
    g.add_argument("--offset", type=int, default=0)
    g.add_argument("--limit", type=int, default=50)
    g.add_argument("--query", default="")
    r = sub.add_parser("run").add_subparsers(dest="action", required=True)
    r.add_parser("routes").add_argument("--diagnose", action="store_true")
    start = r.add_parser("start")
    start.add_argument("assignment")
    start.add_argument("--route", required=True)
    start.add_argument("-C", "--cwd")
    start.add_argument("--model")
    start.add_argument("--billing", choices=("subscription", "metered"), default="subscription")
    start.add_argument(
        "--permission",
        choices=("read-only", "workspace-write", "delegate", "unrestricted"),
        default="read-only",
    )
    start.add_argument("--queue", action="store_true")
    start.add_argument("--selection", type=json.loads)
    start.add_argument("--auto-resume", type=int, default=0)
    many = r.add_parser("batch")
    many.add_argument("assignment")
    many.add_argument("--targets", type=json.loads, required=True)
    many.add_argument("--selection", type=json.loads)
    many.add_argument("-C", "--cwd")
    many.add_argument("--billing", default="subscription", choices=("subscription", "metered"))
    many.add_argument("--permission", default="read-only")
    many.add_argument("--queue", action="store_true")
    li = r.add_parser("list")
    li.add_argument("--offset", type=int, default=0)
    li.add_argument("--limit", type=int, default=50)
    for action in ("status", "stop", "cancel", "keep"):
        r.add_parser(action).add_argument("id")
    follow = r.add_parser("follow")
    follow.add_argument("id")
    follow.add_argument("--timeout", type=float)
    output = r.add_parser("output")
    output.add_argument("id")
    output.add_argument("--generation", type=int)
    output.add_argument("--partial", action="store_true")
    ev = r.add_parser("events")
    ev.add_argument("id")
    ev.add_argument("--after", type=int, default=0)
    resume = r.add_parser("resume")
    resume.add_argument("id")
    resume.add_argument("assignment")
    resume.add_argument("--expected-generation", type=int)
    attach = r.add_parser("attach")
    attach.add_argument("--route", required=True)
    attach.add_argument("--conversation", required=True)
    recover = r.add_parser("recover")
    recover.add_argument("id", nargs="?")
    r.add_parser("dispatch")
    r.add_parser("prune")
    return p


def demo(directory=None, serve=False, port=8421):
    directory = Path(directory or tempfile.mkdtemp(prefix="harnessfleet-demo-")).absolute()
    directory.mkdir(parents=True, exist_ok=True)
    cp = directory / "config.json"
    sp = directory / "state"
    source = directory / "synthetic-native.json"
    config.initialize(cp)
    cfg = config.defaults()
    payload = {
        "schema_version": 1,
        "completeness": "complete",
        "nodes": [
            {"id": "parent", "label": "Project analysis · Codex", "status": "working"},
            {
                "id": "reviewer",
                "parent": "parent",
                "kind": "native_subagent",
                "label": "Review · Claude Code",
                "status": "done",
                "reply": "Synthetic review: the example contract is consistent.",
            },
            {
                "id": "tests",
                "parent": "reviewer",
                "kind": "native_subagent",
                "label": "Protocol tests",
                "status": "done",
            },
            {
                "id": "partial",
                "parent": "parent",
                "label": "Unconnected branch",
                "status": "unknown",
                "completeness": "partial",
            },
        ],
    }
    source.write_text(json.dumps(payload))
    cfg["sources"] = [
        {
            "id": "demo",
            "harness": "synthetic",
            "environment": "demo",
            "account": "example",
            "path": str(source),
            "enabled": True,
        }
    ]
    cp.write_text(json.dumps(cfg, indent=2))
    m = RunManager(cp, sp)
    if serve:
        from .dashboard import server

        server(m, port).serve_forever()
    return {
        "schema_version": 1,
        "synthetic": True,
        "directory": str(directory),
        "config": str(cp),
        "state": str(sp),
        "next": ["harnessfleet", "--config", str(cp), "--state", str(sp), "dashboard", "--port", str(port)],
    }


def execute(args):
    if args.command == "init":
        return {"schema_version": 1, "config": config.initialize(args.config)}
    if args.command == "demo":
        return demo(args.directory, args.serve, args.port)
    if args.command == "integrate":
        from .integration import install_skill, compatibility

        if args.compatibility:
            return compatibility(args.directory)
        if not args.harness:
            raise FleetError("integration", "Select --harness explicitly")
        return install_skill(args.harness, args.directory)
    m = RunManager(args.config, args.state)
    if args.command == "doctor":
        from . import processes

        try:
            processes.require_linux()
            platform = {"execution": True}
        except FleetError as e:
            platform = {"execution": False, "reason": str(e)}
        return {"schema_version": 1, "platform": platform, **m.routes(diagnose=True), "models_invoked": False}
    if args.command == "dashboard":
        from .dashboard import server

        port = (
            args.port if args.port is not None else m.configuration().get("dashboard", {}).get("port", 8421)
        )
        server(m, port).serve_forever()
        return None
    if args.command == "graph":
        from .graph import Graph

        return Graph(m).page(args.parent, args.offset, args.limit, args.query)
    if args.command == "connectors":
        from .connectors import inspect

        return inspect(m, args.kind)
    if args.command == "service":
        from . import service

        return {
            "once": service.controller_once,
            "start": service.start,
            "stop": service.stop,
            "status": service.status,
            "run": service.serve,
        }[args.action](m)
    action = args.action
    if action == "routes":
        return m.routes(args.diagnose)
    if action == "start":
        return m.start(
            args.route,
            args.assignment,
            cwd=args.cwd,
            model=args.model,
            billing=args.billing,
            permission=args.permission,
            queue=args.queue,
            selection=args.selection,
            auto_resume=args.auto_resume,
        )
    if action == "batch":
        return m.start_many(
            args.targets,
            args.assignment,
            selection=args.selection,
            cwd=args.cwd,
            billing=args.billing,
            permission=args.permission,
            queue=args.queue,
        )
    if action == "list":
        return m.list(args.offset, args.limit)
    if action in ("status", "stop", "cancel", "keep"):
        return getattr(m, action)(args.id)
    if action == "follow":
        return m.wait(args.id, args.timeout)
    if action == "output":
        from .worker import redact

        data = m.output(args.id, args.generation, args.partial)
        data["text"] = redact(data["text"])
        return data
    if action == "events":
        return {"schema_version": 1, "events": m.store.events(args.id, args.after)}
    if action == "resume":
        return m.resume(args.id, args.assignment, expected_generation=args.expected_generation)
    if action == "attach":
        raise FleetError(
            "unsupported",
            "Attachment requires a qualified connected-session lease backend; observation grants no control",
        )
    if action == "recover":
        return m.recover(args.id)
    if action == "dispatch":
        return m.dispatch()
    if action == "prune":
        return m.prune()


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        result = execute(args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        if args.command == "run" and args.action == "follow" and result["status"] != "finished":
            return 1
        if isinstance(result, dict) and result.get("all_started") is False:
            return 1
        return 0
    except FleetError as e:
        print(
            json.dumps({"schema_version": 1, "error": {"code": e.code, "message": str(e)}}),
            file=__import__("sys").stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, KeyError):
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "error": {
                        "code": "operation",
                        "message": "Operation failed; inspect local configuration and diagnostics",
                    },
                }
            ),
            file=__import__("sys").stderr,
        )
        return 2
