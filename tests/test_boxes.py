import json
import sys
import pytest
from harnessfleet import FleetError
from harnessfleet.boxes import daemon, verify

DOCKER = r"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['HF_DOCKER_STATE'])
data=json.loads(p.read_text()) if p.exists() else {'daemon':'synthetic-daemon','containers':{},'calls':[]}
a=sys.argv[3:]
data['calls'].append(a)
def save():p.write_text(json.dumps(data))
save()
if a[0]=='info':
 print(json.dumps({'ID':data['daemon'],'OSType':'linux','Architecture':'x86_64','OperatingSystem':'synthetic Linux'}))
elif a[:2]==['container','create']:
 name=a[a.index('--name')+1];label=a[a.index('--label')+1].split('=',1)
 cid='a'*64
 data['containers'][cid]={'Id':cid,'Name':'/'+name,'Config':{'Labels':{label[0]:label[1]}},'State':{'Running':False}}
 save()
 if os.environ.get('HF_DOCKER_MISSING_ACK'):
  data['containers']={};save();raise SystemExit(0)
 print(cid)
elif a[:2]==['container','inspect']:
 rows=[r for r in data['containers'].values() if a[2] in (r['Id'],r['Name'].lstrip('/'))]
 if not rows:raise SystemExit(1)
 print(json.dumps(rows))
elif a[:2]==['container','start']:
 cid=a[-1];data['containers'][cid]['State']['Running']=False;save()
 print(json.dumps({'type':'thread.started','thread_id':'synthetic-container-session'}),flush=True)
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Synthetic Box result'}}),flush=True)
 print(json.dumps({'type':'turn.completed'}),flush=True)
elif a[:2]==['container','stop']:
 data['containers'][a[-1]]['State']['Running']=False;save();print(a[-1])
elif a[:2]==['container','rm']:
 data['containers'].pop(a[-1]);save();print(a[-1])
else:raise SystemExit(2)
"""


def configure(instance, monkeypatch):
    executable = instance.path / "fake-docker"
    executable.write_text(DOCKER.replace("#!/usr/bin/env python3", "#!" + sys.executable))
    executable.chmod(0o755)
    state = instance.path / "docker-state.json"
    monkeypatch.setenv("HF_DOCKER_STATE", str(state))
    account = instance.path / "dedicated-account"
    account.mkdir()
    box = {
        "daemon": "unix:///synthetic/docker.sock",
        "docker_executable": str(executable),
        "image": "synthetic-image",
        "account_home": str(account),
        "mounts": [{"source": str(instance.path), "target": "/project", "mode": "ro"}],
    }
    route = {**instance.config["routes"][0], "id": "box", "environment": "box:example", "box": box}
    instance.config["routes"].append(route)
    instance.config["policy"]["routes"].append("box")
    instance.save()
    return box, state


def test_box_lifecycle_receipts_and_mounts(instance, monkeypatch):
    box, state = configure(instance, monkeypatch)
    r = instance.m.start("box", "bounded assignment")
    assert instance.finish(r)["status"] == "finished"
    assert instance.m.output(r["id"])["text"] == "Synthetic Box result"
    record = instance.m.store.get(r["id"])
    assert record["box_receipt"]["released"]
    data = json.loads(state.read_text())
    create = [a for a in data["calls"] if a[:2] == ["container", "create"]][0]
    assert "HARNESSFLEET_BOX=1" in create
    assert not any("HARNESSFLEET_PARENT_TOKEN" in arg for arg in create)
    assert not data["containers"]


def test_daemon_replacement_refuses_cleanup_before_container_effect(instance, monkeypatch):
    box, state = configure(instance, monkeypatch)
    identity = daemon(box)
    data = json.loads(state.read_text())
    data["daemon"] = "replacement-daemon"
    state.write_text(json.dumps(data))
    with pytest.raises(FleetError) as e:
        verify(box, {"daemon": identity})
    assert e.value.code == "daemon_changed"
    assert all(call[0] == "info" for call in json.loads(state.read_text())["calls"])


def test_missing_creation_ack_retains_uncertainty(instance, monkeypatch):
    box, state = configure(instance, monkeypatch)
    monkeypatch.setenv("HF_DOCKER_MISSING_ACK", "1")
    r = instance.m.start("box", "bounded assignment")
    assert instance.finish(r)["status"] == "uncertain"
    assert not instance.m.status(r["id"])["cleanup_confirmed"]
    data = json.loads(state.read_text())
    assert len([a for a in data["calls"] if a[:2] == ["container", "create"]]) == 1
    assert not [a for a in data["calls"] if a[:2] == ["container", "start"]]
    instance.m.recover(r["id"])
    assert instance.m.status(r["id"])["status"] == "uncertain"


def test_invalid_mount_refused_without_docker_effect(instance, monkeypatch):
    box, state = configure(instance, monkeypatch)
    instance.config["routes"][-1]["box"]["mounts"][0]["source"] = str(instance.path / "absent")
    instance.save()
    with pytest.raises(FleetError) as e:
        instance.m.start("box", "assignment")
    assert e.value.code == "mount" and not state.exists()
