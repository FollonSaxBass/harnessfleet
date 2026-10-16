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
    f.write(json.dumps({'args':args,'run':os.environ['HARNESSFLEET_PARENT_RUN'],'service_token_present':'HARNESSFLEET_SERVICE_TOKEN' in os.environ,'state_path_present':'HARNESSFLEET_STATE' in os.environ,'config_path_present':'HARNESSFLEET_CONFIG' in os.environ,'broker_present':bool(os.environ.get('HARNESSFLEET_BROKER'))})+'\n')
task=args[-1]
claude='--print' in args
session=str(uuid.uuid4())
if 'resume' in args:
    session=args[args.index('resume')+1]
def emit(data):
    print(json.dumps(data),flush=True)
emit({'type':'system','subtype':'init','session_id':session} if claude else {'type':'thread.started','thread_id':session})
if 'mode:capability' in task:
    with open(os.environ['HF_TEST_CAPABILITY_WITNESS']+'.tmp','w') as f:
        json.dump({key:os.environ[key] for key in ('HARNESSFLEET_BROKER','HARNESSFLEET_BROKER_OWNER','HARNESSFLEET_PARENT_TOKEN')},f)
    os.replace(os.environ['HF_TEST_CAPABILITY_WITNESS']+'.tmp',os.environ['HF_TEST_CAPABILITY_WITNESS'])
if 'mode:handoff' in task:
    import socket,struct
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as broker, socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as handoff:
        broker.connect(os.environ['HARNESSFLEET_BROKER']);handoff.connect(os.environ['HF_TEST_HANDOFF'])
        handoff.sendmsg([b'x'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,struct.pack('i',broker.fileno()))])
if 'mode:slow' in task:
    time.sleep(8)
if 'mode:descendant' in task:
    subprocess.Popen([sys.executable,'-c','import os,time;os.setsid();time.sleep(60)'])
if 'mode:malformed' in task:
    print('not json',flush=True);raise SystemExit(0)
if 'mode:quota' in task or 'mode:auth' in task:
    emit({'type':'turn.failed','error':{'code':'quota_exceeded' if 'mode:quota' in task else 'authentication_error'}})
    raise SystemExit(1)
if 'mode:progress-burst' in task:
    for number in range(300):emit({'type':f'fixture.{number}'})
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
elif 'mode:held-child' in task:
    from pathlib import Path
    from harnessfleet import RunManager,FleetError,processes
    m=RunManager()
    child=m.start('claude-local','mode:leaf',queue=True)
    if 'action:resume' in task:m.wait(child['id'],10)
    watch=processes.Changes(Path(os.environ['HF_TEST_CHILD_READY']).parent)
    try:
        Path(os.environ['HF_TEST_CHILD_READY']).write_text('ready')
        if 'action:resume' in task:
            deadline=time.monotonic()+15
            while not Path(os.environ['HF_TEST_CHILD_RELEASE']).exists():
                assert time.monotonic()<deadline
                watch.wait(1)
            try:m.resume(child['id'],'bounded continuation');output='UNAUTHORIZED_RESUME'
            except FleetError as error:output=error.code
        else:
            final=m.wait(child['id'],15)
            output=final['status']+' '+str(final['failure'])
    finally:watch.close()
elif 'mode:leaf-context' in task:
    from harnessfleet import RunManager,FleetError
    from harnessfleet.storage import Store
    def forbidden(*args,**kwargs):raise AssertionError('Leaf attempted Store access')
    Store.__init__=forbidden
    try:RunManager().start('claude-local','mode:leaf');code='UNAUTHORIZED_SUCCESS'
    except FleetError as error:code=error.code
    output=json.dumps({'broker':bool(os.environ.get('HARNESSFLEET_BROKER')),'token':bool(os.environ.get('HARNESSFLEET_PARENT_TOKEN')),'result':code,'skill_instruction':'For permitted delegation read' in task})
elif 'mode:caller-cwd' in task:
    from harnessfleet import RunManager
    os.chdir('caller')
    options={} if 'cwd:omitted' in task else {'cwd':'relative'}
    if 'binding:cli' in task:
        command=[sys.executable,'-m','harnessfleet','run']
        if 'operation:batch' in task:
            command+=['batch','--targets',json.dumps([{'route':'claude-local','model':'model-b'}])]
        else:command+=['start','--route','claude-local','--model','model-b']
        if options:command+=['-C',options['cwd']]
        response=json.loads(subprocess.check_output(command+['mode:leaf'],text=True))
    else:
        m=RunManager()
        response=m.start_many([{'route':'claude-local','model':'model-b'}],'mode:leaf',**options) if 'operation:batch' in task else m.start('claude-local','mode:leaf',model='model-b',**options)
    child=response['results'][0]['run'] if 'operation:batch' in task else response
    RunManager().wait(child['id'],10)
    output=child['cwd']
elif 'mode:receipt-failure' in task:
    from harnessfleet import RunManager,FleetError
    m=RunManager();request_id='b'*32
    outcomes=[]
    for _ in range(2):
        try:m.start('claude-local','mode:leaf',request_id=request_id);outcomes.append('UNEXPECTED_SUCCESS')
        except FleetError as error:outcomes.append({'code':error.code,'message':str(error)})
    children=m.list()['runs']
    for child in children:m.wait(child['id'],10)
    output=json.dumps({'outcomes':outcomes,'children':len(children),'request_id':request_id})
elif 'mode:foreign-control' in task:
    outcomes={}
    for action in ('status','stop'):
        p=subprocess.run([sys.executable,'-m','harnessfleet','run',action,os.environ['HF_TEST_VICTIM']],capture_output=True,text=True)
        outcomes[action]=json.loads(p.stderr)['error']['code'] if p.returncode else 'UNAUTHORIZED_SUCCESS'
    output=json.dumps(outcomes)
elif 'mode:cleared-parent' in task:
    env=dict(os.environ)
    for key in ('HARNESSFLEET_PARENT_RUN','HARNESSFLEET_PARENT_GENERATION'):
        env.pop(key,None)
    p=subprocess.run([sys.executable,'-m','harnessfleet','run','start','--route','claude-local','--permission','delegate','leaf'],env=env,capture_output=True,text=True)
    output=json.loads(p.stderr)['error']['code'] if p.returncode else 'UNAUTHORIZED_ROOT'
elif 'mode:idempotent' in task:
    from harnessfleet import RunManager
    from harnessfleet.storage import Store
    def forbidden(*args,**kwargs):raise AssertionError('Worker attempted local Store access')
    Store.__init__=forbidden
    m=RunManager();request_id='a'*32
    if 'mode:lost-reply' in task:
        import socket
        packet={'schema_version':1,'operation':'start','args':['claude-local','mode:leaf'],'kwargs':{'model':'model-b','cwd':os.getcwd()},'request_id':request_id,'token':os.environ['HARNESSFLEET_PARENT_TOKEN']}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect(os.environ['HARNESSFLEET_BROKER']);connection.sendall((json.dumps(packet)+'\n').encode())
            with connection.makefile('rb') as stream:assert stream.readline(100000).endswith(b'\n')
    first=m.start('claude-local','mode:leaf',model='model-b',request_id=request_id)
    second=m.start('claude-local','mode:leaf',model='model-b',request_id=request_id)
    try:
        m.start('claude-local','different assignment',model='model-b',request_id=request_id)
        conflict='UNAUTHORIZED_REBIND'
    except Exception as error:
        conflict=getattr(error,'code',None)
    m.wait(first['id'],10)
    result=m.output(first['id'])
    output=json.dumps({'ids':[first['id'],second['id']],'verified':result['verified'],'children':m.list()['total'],'conflict':conflict})
elif 'mode:missing-broker' in task:
    env=dict(os.environ);env['HARNESSFLEET_BROKER']='/nonexistent/harnessfleet.sock'
    p=subprocess.run([sys.executable,'-m','harnessfleet','run','start','--route','claude-local','leaf'],env=env,capture_output=True,text=True)
    output=json.loads(p.stderr)['error']['code'] if p.returncode else 'UNAUTHORIZED_FALLBACK'
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
        delegation_transport="experimental-unix-v1",
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
        "HARNESSFLEET_BROKER",
        "HARNESSFLEET_BROKER_OWNER",
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
            kwargs.setdefault("delegation", "required")
            return self.m.start("codex-local", task, **kwargs)

        def finish(self, run):
            return self.m.wait(run["id"], 15)

    obj = Instance()
    yield obj
    for r in manager.store.all():
        if r["status"] in ("starting", "running", "uncertain", "stopping"):
            manager.stop(r["id"])
