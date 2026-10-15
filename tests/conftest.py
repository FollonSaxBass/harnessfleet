import json
import subprocess
import sys
from pathlib import Path
import pytest
from harnessfleet import RunManager
from harnessfleet.config import defaults

FAKE = r"""#!/usr/bin/env python3
import json,os,sys,time,uuid,subprocess,signal
args=sys.argv[1:]
if args[:2]==['login','status']:
    raise SystemExit(0)
if 'auth' in args:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty'}))
    raise SystemExit(0)
if '--version' in args:
    print('synthetic-native 1');raise SystemExit(0)
with open(os.environ['HF_TEST_WITNESS'],'a') as f:
    f.write(json.dumps({'args':args,'run':os.environ['HARNESSFLEET_PARENT_RUN'],'service_token_present':'HARNESSFLEET_SERVICE_TOKEN' in os.environ})+'\n')
task=args[-1]
claude='--print' in args
session=str(uuid.uuid4())
if 'resume' in args:
    session=args[args.index('resume')+1]
def emit(data):
    print(json.dumps(data),flush=True)
emit({'type':'system','subtype':'init','session_id':session} if claude else {'type':'thread.started','thread_id':session})
if 'mode:slow' in task:
    time.sleep(8)
if 'mode:descendant' in task:
    subprocess.Popen([sys.executable,'-c','import os,time;os.setsid();time.sleep(60)'])
if 'mode:malformed' in task:
    print('not json',flush=True);raise SystemExit(0)
if 'mode:quota' in task or 'mode:auth' in task:
    emit({'type':'turn.failed','error':{'code':'quota_exceeded' if 'mode:quota' in task else 'authentication_error'}})
    raise SystemExit(1)
if 'mode:recursive' in task:
    cli=[sys.executable,'-m','harnessfleet','run','start','--route','claude-local','--model','model-b','mode:leaf']
    p=subprocess.run(cli,capture_output=True,text=True)
    if p.returncode:
        output='CHILD_ERROR:'+p.stderr
    else:
        rid=json.loads(p.stdout)['id']
        subprocess.run([sys.executable,'-m','harnessfleet','run','follow',rid,'--timeout','10'],capture_output=True)
        r=subprocess.run([sys.executable,'-m','harnessfleet','run','output',rid],capture_output=True,text=True)
        if 'mode:recursive-resume' in task:
            again=subprocess.run([sys.executable,'-m','harnessfleet','run','resume',rid,'bounded follow-up'],capture_output=True,text=True)
            if again.returncode:
                output='CONTINUATION_ERROR:'+again.stderr
            else:
                subprocess.run([sys.executable,'-m','harnessfleet','run','follow',rid,'--timeout','10'],capture_output=True)
                r=subprocess.run([sys.executable,'-m','harnessfleet','run','output',rid],capture_output=True,text=True)
                output='Continued '+json.loads(r.stdout)['text']
        else:
            output='Integrated '+json.loads(r.stdout)['text']
elif 'mode:forbidden-child' in task:
    p=subprocess.run([sys.executable,'-m','harnessfleet','run','start','--route','claude-local','--model','model-b','--permission','delegate','mode:leaf'],capture_output=True,text=True)
    output='Child refusal: '+json.loads(p.stderr)['error']['code']
else:
    output='synthetic final' if 'mode:neutral-quota' not in task else 'No quota exhaustion occurred.'
if 'mode:no-final' in task:
    raise SystemExit(0)
if claude:
    emit({'type':'result','subtype':'success','is_error':False,'session_id':session,'result':output})
else:
    emit({'type':'item.completed','item':{'type':'agent_message','text':output}})
    emit({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}})
"""


@pytest.fixture
def instance(tmp_path, monkeypatch):
    binary = tmp_path / "fake-native"
    binary.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
    binary.chmod(0o755)
    cp = tmp_path / "config.json"
    sp = tmp_path / "state"
    witness = tmp_path / "invocations.jsonl"
    cfg = defaults()
    cfg["policy"].update(
        routes=["codex-local", "claude-local", "metered"],
        permissions=["read-only", "workspace-write", "delegate"],
        cwd_roots=[str(tmp_path)],
        experimental=True,
        max_parallel=5,
    )
    cfg["routes"] = [
        {
            "id": rid,
            "harness": h,
            "environment": "host",
            "account": "example",
            "billing": billing,
            "models": [model],
            "default_model": model,
            "executable": str(binary),
            "concurrency": 1,
        }
        for rid, h, billing, model in [
            ("codex-local", "codex_cli", "subscription", "model-a"),
            ("claude-local", "claude_code", "subscription", "model-b"),
            ("metered", "codex_cli", "metered", "model-a"),
        ]
    ]
    cp.write_text(json.dumps(cfg))
    monkeypatch.setenv("HARNESSFLEET_CONFIG", str(cp))
    monkeypatch.setenv("HARNESSFLEET_STATE", str(sp))
    monkeypatch.setenv("HF_TEST_WITNESS", str(witness))
    monkeypatch.chdir(tmp_path)
    for name in (
        "HARNESSFLEET_PARENT_RUN",
        "HARNESSFLEET_PARENT_TOKEN",
        "HARNESSFLEET_PARENT_GENERATION",
        "HARNESSFLEET_BOX",
        "CODEX_BOX_ACCOUNT",
        "CURSOR_BOX_ACCOUNT",
    ):
        monkeypatch.delenv(name, raising=False)
    manager = RunManager()

    class Instance:
        m = manager
        path = tmp_path
        config = cfg

        def save(self):
            cp.write_text(json.dumps(self.config))

        def cli(self, *args, ok=True):
            executable = Path(sys.executable).parent / "harnessfleet"
            assert executable.exists(), "Tests must exercise an installed package"
            p = subprocess.run([str(executable), *args], capture_output=True, text=True, timeout=20)
            if ok:
                assert p.returncode == 0, p.stderr + "\n" + p.stdout
            return p

        def calls(self):
            return [json.loads(line) for line in witness.read_text().splitlines()] if witness.exists() else []

        def start(self, task="mode:normal", **kwargs):
            return self.m.start("codex-local", task, **kwargs)

        def finish(self, run):
            return self.m.wait(run["id"], 15)

    obj = Instance()
    yield obj
    for r in manager.store.all():
        if r["status"] in ("starting", "running", "uncertain", "stopping"):
            manager.stop(r["id"])
