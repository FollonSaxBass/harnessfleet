"""Native-shaped Claude quota refusals through the installed command."""

import json
import pytest

RESET = 2_000_000_000


def envelope():
    return {
        "type": "assistant",
        "error": "rate_limit",
        "is_api_error_message": True,
        "api_error_status": 429,
        "api_error": "usage_limit_reached",
        "api_error_params": {
            "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": RESET}
        },
        "message": {"content": [{"type": "text", "text": "Individual spend limit reached."}]},
    }


@pytest.mark.parametrize(
    "change,status,reset",
    [
        ({}, "quota", RESET),
        ({"api_error_params": {}}, "quota", None),
        ({"api_error_params": []}, "quota", None),
        ({"api_error_params": {"rate_limit_info": {"status": "rejected", "resetsAt": True}}}, "quota", None),
        (
            {"api_error_params": {"rate_limit_info": {"status": "rejected", "resetsAt": float("inf")}}},
            "quota",
            None,
        ),
        ({"api_error_params": {"rate_limit_info": {"status": "allowed", "resetsAt": RESET}}}, "quota", None),
        (
            {"api_error_params": {"rate_limit_info": {"status": "rejected", "overageResetsAt": RESET}}},
            "quota",
            None,
        ),
        (
            {
                "api_error_params": {
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "seven_day",
                        "resetsAt": 2_000_000_000_000,
                    }
                }
            },
            "quota",
            None,
        ),
        (
            {
                "api_error_params": {
                    "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": RESET}
                }
            },
            "quota",
            None,
        ),
        ({"session_id": "22222222-2222-4222-8222-222222222222"}, "failed", None),
        ({"error": "authentication_failed"}, "authentication", None),
        ({"fixture_exit": 1}, "quota", RESET),
        (
            {
                "error": None,
                "message": {
                    "content": [{"type": "text", "text": "HTTP 429 usage_limit_reached is an example."}]
                },
            },
            "finished",
            None,
        ),
        ({"parent_tool_use_id": "native-child"}, "finished", None),
        ({"is_api_error_message": False}, "failed", None),
        ({"api_error": "server_overloaded"}, "failed", None),
        ({"api_error_status": 503}, "failed", None),
        ({"type": "fixture", "error": None}, "finished", None),
    ],
)
def test_native_quota_envelope_cli_preserves_failure_and_no_replay(instance, change, status, reset):
    event = {**envelope(), **change}
    binary = instance.path / "fake-native"
    fixture = f"""
if 'mode:claude-telemetry' in task:
    event=json.loads({json.dumps(event)!r})
    event.setdefault('session_id',session)
    emit(event)
    failed=bool(event.get('error')) and not event.get('parent_tool_use_id')
    emit({{'type':'result','subtype':'success','is_error':failed,'session_id':session,'result':'synthetic final'}})
    raise SystemExit(event.get('fixture_exit',0))
"""
    source = binary.read_text()
    source = source.replace("if 'mode:capability' in task:", fixture + "\nif 'mode:capability' in task:", 1)
    binary.write_text(source)
    run = json.loads(instance.cli("run", "start", "--route", "claude-local", "mode:claude-telemetry").stdout)
    follow = instance.cli("run", "follow", run["id"], "--timeout", "10", ok=status == "finished")
    assert follow.returncode == (0 if status == "finished" else 1)
    final = json.loads(follow.stdout)
    assert final["status"] == status
    assert final["cleanup_confirmed"] is True
    assert final["reset_at"] == reset
    assert final["accepted"] is (status == "finished")
    assert final["auto_remaining"] == 0
    output = instance.cli("run", "output", run["id"], ok=status == "finished")
    assert (output.returncode == 0) is (status == "finished")
    instance.m.recover(run["id"])
    assert instance.m.status(run["id"])["generation"] == 1
    assert len(instance.calls()) == 1


def test_normal_claude_prose_is_not_quota(instance):
    binary = instance.path / "fake-native"
    source = binary.read_text().replace(
        "output='synthetic final'",
        "output='HTTP 429 usage_limit_reached is an example, not a live quota refusal.'",
        1,
    )
    binary.write_text(source)
    run = json.loads(instance.cli("run", "start", "--route", "claude-local", "mode:normal").stdout)
    final = instance.finish(run)
    assert final["status"] == "finished" and final["accepted"]
    assert final["reset_at"] is None
    assert instance.m.output(run["id"])["verified"]


def test_claude_quota_auto_recovery_requires_adapter_capability(instance):
    response = instance.cli(
        "run", "start", "--route", "claude-local", "--auto-resume", "1", "mode:normal", ok=False
    )
    assert response.returncode == 2
    assert json.loads(response.stderr)["error"]["code"] == "quota_recovery"
    assert not instance.calls()
