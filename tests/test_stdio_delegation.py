import json
import subprocess
import sys
from pathlib import Path

import pytest

from harnessfleet import FleetError


MCP = r"""
if 'mode:stdio' in task:
    import tomllib
    settings=next(tomllib.loads(value)['mcp_servers'] for value in args if value.startswith('mcp_servers='))
    entry=settings['harnessfleet']
    helper=subprocess.Popen([entry['command'],*entry['args']],cwd=entry['cwd'],env=entry['env'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    sequence=0
    def rpc(method,params):
        global sequence
        sequence+=1
        helper.stdin.write(json.dumps({'jsonrpc':'2.0','id':sequence,'method':method,'params':params})+'\n');helper.stdin.flush()
        reply=json.loads(helper.stdout.readline())
        assert reply['id']==sequence,reply
        return reply
    def call(operation,arguments=None,request_id=None):
        body={'operation':operation,'arguments':arguments or {}}
        if request_id:body['request_id']=request_id
        reply=rpc('tools/call',{'name':'run','arguments':body})
        return json.loads(reply['result']['content'][0]['text'])
    try:
        rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
        helper.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n');helper.stdin.flush()
        rpc('tools/list',{})
        if 'mode:stdio-store-contention' in task:
            from pathlib import Path
            project=Path.cwd()
            (project/'native-ready').touch()
            deadline=time.monotonic()+8
            while not (project/'native-release').exists():
                assert time.monotonic()<deadline
                time.sleep(0.001)
            emit({'type':'item.completed','item':{'type':'agent_message','text':'final during progress contention'}})
            emit({'type':'turn.completed'})
            time.sleep(0.5)
            helper.kill();helper.wait(timeout=3)
            (project/'helper-dead').touch()
            raise SystemExit(0)
        routes=call('routes')['result']['routes']
        if 'mode:stdio-crash' in task:
            helper.kill();helper.wait(timeout=5);time.sleep(8)
        if 'mode:stdio-sibling' in task:
            from harnessfleet.delegation import Client
            os.environ.update(entry['env'])
            try:
                Client().start('claude-local','mode:leaf',request_id='c'*32)
                code='UNAUTHORIZED_SUCCESS'
            except Exception as error:code=getattr(error,'code',None)
            emit({'type':'item.completed','item':{'type':'agent_message','text':str(code)}})
            emit({'type':'turn.completed'})
            raise SystemExit(0)
        if 'mode:stdio-slow' in task:time.sleep(8)
        kwargs={'route':'claude-local','assignment':'mode:leaf','model':'model-b'}
        if 'cwd:relative' in task:kwargs['cwd']='caller'
        first=call('start',kwargs,'a'*32)
        if 'error' in first:
            first['observed_total']=call('list')['result']['total']
            emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(first)}})
            emit({'type':'turn.completed'})
            raise SystemExit(0)
        again=call('start',kwargs,'a'*32)
        forbidden=call('start',{**kwargs,'permission':'delegate'},'b'*32)
        conflict=call('start',{**kwargs,'assignment':'different'},'a'*32)
        rid=first['result']['id']
        call('follow',{'rid':rid,'timeout':10})
        output=call('output',{'rid':rid})
        result={'ids':[rid,again['result']['id']],'forbidden':forbidden['error']['code'],
                'conflict':conflict['error']['code'],'verified':output['result']['verified'],
                'cwd':first['result']['cwd'],'actor':first['result']['requesting_actor'],
                'routes':[r['id'] for r in routes]}
        emit({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}})
        emit({'type':'turn.completed'})
    finally:
        helper.stdin.close();helper.wait(timeout=5)
    raise SystemExit(0)
elif 'mode:recursive' in task:
"""


@pytest.fixture
def stdio(instance):
    binary = Path(instance.config["routes"][0]["executable"])
    binary.write_text(binary.read_text().replace("if 'mode:recursive' in task:", MCP))
    instance.config["policy"]["delegation_transport"] = "experimental-stdio-mcp-v1"
    instance.save()
    return instance


def test_managed_stdio_requires_capability_without_creating_storage(tmp_path, monkeypatch):
    state = tmp_path / "must-not-exist"
    monkeypatch.setenv("HARNESSFLEET_STATE", str(state))
    process = subprocess.run(
        [sys.executable, "-m", "harnessfleet", "managed-stdio"], capture_output=True, timeout=5
    )
    assert process.returncode == 2 and not process.stdout
    assert not state.exists()


def test_stdio_delegates_with_core_receipts_permissions_and_honest_actor(stdio):
    parent = stdio.start("mode:stdio")
    assert stdio.finish(parent)["status"] == "finished"
    result = json.loads(stdio.m.output(parent["id"])["text"])
    assert result["ids"][0] == result["ids"][1]
    assert result["forbidden"] == "permission" and result["conflict"] == "request_conflict"
    assert result["verified"]
    actor = result["actor"]
    assert actor["kind"] == "stdio_bridge" and actor["exact_native_caller"] is None
    assert actor["completeness"] == "partial" and actor["generation"] == 1
    children = [r for r in stdio.m.list()["runs"] if r["parent_run"] == parent["id"]]
    assert len(children) == 1 and children[0]["caller"] is None
    assert len(stdio.calls()) == 2


def test_stdio_child_relative_cwd_uses_recorded_parent_directory(stdio):
    (stdio.path / "caller").mkdir()
    parent = stdio.start("mode:stdio cwd:relative")
    assert stdio.finish(parent)["status"] == "finished"
    assert json.loads(stdio.m.output(parent["id"])["text"])["cwd"] == str(stdio.path / "caller")


@pytest.mark.parametrize("name", ["project-é", "project-🚀", "project-\x7f"])
def test_stdio_accepts_unicode_working_directories(stdio, name):
    directory = stdio.path / name
    directory.mkdir()
    parent = stdio.start("mode:stdio", cwd=str(directory))
    assert stdio.finish(parent)["status"] == "finished"
    result = json.loads(stdio.m.output(parent["id"])["text"])
    assert result["verified"] and result["cwd"] == str(directory)


def test_explicit_transport_authorization_keeps_origin_separate_from_child_selection(stdio):
    stdio.config["policy"]["delegation_transport"] = "experimental-unix-v1"
    stdio.config["policy"]["delegation_transports"] = ["experimental-unix-v1", "experimental-stdio-mcp-v1"]
    stdio.config["routes"][0]["delegation_transport"] = "experimental-stdio-mcp-v1"
    stdio.save()
    parent = stdio.start("mode:stdio")
    assert stdio.finish(parent)["status"] == "finished"
    assert json.loads(stdio.m.output(parent["id"])["text"])["verified"]


def test_stdio_selection_rejects_unsupported_adapter_before_admission(stdio):
    with pytest.raises(FleetError) as error:
        stdio.m.start("claude-local", "not invoked", delegation="required")
    assert error.value.code == "delegation_transport"
    assert not stdio.calls() and not stdio.m.list()["runs"]


def test_native_sibling_with_copied_bridge_capability_cannot_impersonate_helper(stdio):
    parent = stdio.start("mode:stdio-sibling")
    assert stdio.finish(parent)["status"] == "finished"
    assert stdio.m.output(parent["id"])["text"] == "delegation_auth"
    assert len(stdio.calls()) == 1 and stdio.m.list()["total"] == 1


def test_helper_crash_fails_parent_without_automatic_restart_or_child(stdio):
    parent = stdio.start("mode:stdio-crash")
    final = stdio.finish(parent)
    assert final["status"] == "failed" and final["failure"] == "delegation_transport"
    assert final["cleanup_confirmed"] and not final["accepted"]
    assert stdio.m.list()["total"] == 1 and len(stdio.calls()) == 1


def test_stdio_continuation_rotates_helper_and_keeps_generation_attribution(stdio):
    parent = stdio.start("mode:stdio")
    assert stdio.finish(parent)["status"] == "finished"
    old = stdio.m.store.get(parent["id"])
    continued = stdio.m.resume(parent["id"], "mode:stdio")
    assert stdio.finish(continued)["status"] == "finished"
    new = stdio.m.store.get(parent["id"])
    assert old["bridge_instance"] != new["bridge_instance"]
    first = json.loads(stdio.m.output(parent["id"], 1)["text"])
    second = json.loads(stdio.m.output(parent["id"], 2)["text"])
    assert first["ids"][0] != second["ids"][0]
    assert first["actor"]["generation"] == 1 and second["actor"]["generation"] == 2
    assert new["session_id"] == old["session_id"]


@pytest.mark.parametrize("scope", ["route", "policy"])
def test_stdio_revocation_refuses_new_children_but_keeps_scoped_observation(stdio, scope):
    from harnessfleet import processes
    import time

    watch = processes.Changes(stdio.path)
    try:
        parent = stdio.start("mode:stdio-slow")
        deadline = time.monotonic() + 5
        while not stdio.m.store.get(parent["id"]).get("bridge_process"):
            assert time.monotonic() < deadline
            watch.wait(1)
        target = stdio.config["policy"] if scope == "policy" else stdio.config["routes"][0]
        target["delegation_transport"] = "disabled"
        stdio.save()
        assert stdio.finish(parent)["status"] == "finished"
        result = json.loads(stdio.m.output(parent["id"])["text"])
        assert result["error"]["code"] == "delegation_transport" and result["observed_total"] == 0
        assert len(stdio.calls()) == 1 and stdio.m.list()["total"] == 1
    finally:
        watch.close()


def test_malformed_transport_authorization_does_not_broaden_policy(instance):
    instance.config["policy"]["delegation_transports"] = "experimental-unix-v1"
    instance.save()
    with pytest.raises(FleetError) as error:
        instance.m.routes()
    assert error.value.code == "configuration"


def test_store_contention_does_not_delay_observing_live_helper_completion(stdio):
    from harnessfleet import processes
    import time

    watch = processes.Changes(stdio.path)
    try:
        parent = stdio.start("mode:stdio-store-contention")
        deadline = time.monotonic() + 8
        while not (stdio.path / "native-ready").exists():
            assert time.monotonic() < deadline, "Native MCP did not initialize"
            watch.wait(1)
        with stdio.m.store.transaction():
            (stdio.path / "native-release").touch()
            deadline = time.monotonic() + 4
            while not (stdio.path / "helper-dead").exists():
                assert time.monotonic() < deadline, "Native did not finish its bounded shutdown"
                watch.wait(1)
        final = stdio.finish(parent)
        assert final["status"] == "finished" and final["accepted"] and final["cleanup_confirmed"]
        assert stdio.m.output(parent["id"])["text"] == "final during progress contention"
    finally:
        watch.close()
