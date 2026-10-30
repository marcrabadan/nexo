"""Local-provider parity for operator checkpoints, redacted OTLP and recovery."""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from phases import ROOT, run
from test_agent_lifecycle import backend, model, source, tool_call, TOOL

KEY='ab'*32


def prepare(tmp_path, model, suffix='print(reviewer.handle("text", run: "case-1", as: Finding))',tool='',confirm=False,budget=None):
    program='policy:\n    allow read "."\n    allow write "."\n    allow env "NEXO_AI_KEY"\n    allow net "127.0.0.1"\n'+source(model,suffix,tool,confirm)
    (tmp_path/'main.nexo').write_text(program)
    directory=tmp_path/'state'
    directory.mkdir(mode=0o700,exist_ok=True)
    permissions=[['allow','read','.'],['allow','write','.'],['allow','net','127.0.0.1'],['allow','env','NEXO_AI_KEY']]
    tools={'main.nexo::report':{'permissions':permissions,'confirm':confirm}} if tool else {}
    config={'version':2,'agents':{'main.nexo::reviewer':{'permissions':permissions,'tools':list(tools)}},'tools':tools,
            'state':{'main.nexo::reviewer':{'directory':'state','key':'NEXO_STATE_KEY','max_bytes':1048576,'budget':budget}}}
    (tmp_path/'authorization.json').write_text(json.dumps(config))
    return program,config


def execute(tmp_path,backend,stdin=b'',env=None):
    return run(backend,['security','run','--authorization','authorization.json','main.nexo'],tmp_path,stdin=stdin,
               env={'NEXO_AI_KEY':'SECRET-API-KEY','NEXO_STATE_KEY':KEY,'XDG_CONFIG_HOME':str(tmp_path/'config'),**(env or {})})


def decrypt(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    values=[]
    for p in (tmp_path/'state').glob('*.state'):
        data=p.read_bytes()
        values.append((p,json.loads(AESGCM(bytes.fromhex(KEY)).decrypt(data[:12],data[12:],p.stem.encode()))))
    return values


def test_completed_typed_run_returns_cache_without_dispatch(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    prepare(tmp_path,model,tool=TOOL)
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    assert len(model[2])==2
    assert (tmp_path/'marker').read_text()=='executed'
    (tmp_path/'marker').unlink()
    second=execute(tmp_path,backend)
    assert second.returncode==0,second.stdout.decode()
    assert second.stdout==first.stdout
    assert len(model[2])==2
    assert not (tmp_path/'marker').exists()
    assert decrypt(tmp_path)[0][1]['phase']=='complete'
    for p in (tmp_path/'state').iterdir():
        assert p.stat().st_mode & 0o777 == 0o600
        assert b'answer' not in p.read_bytes()


@pytest.mark.parametrize('mutation',['input','source','grant','key','corrupt','symlink','oversized','permissions'])
def test_invalid_resume_fails_before_any_provider(tmp_path,backend,model,mutation):
    prepare(tmp_path,model,suffix='print(reviewer.handle(read("input.txt"), run: "case-1"))')
    (tmp_path/'input.txt').write_text('first')
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    p=next((tmp_path/'state').glob('*.state'))
    env={}
    if mutation=='input': (tmp_path/'input.txt').write_text('second')
    elif mutation=='source':
        path=tmp_path/'main.nexo';path.write_text(path.read_text()+'\n# edited\n')
    elif mutation=='grant':
        path=tmp_path/'authorization.json';path.write_text(path.read_text()+'\n')
    elif mutation=='key': env['NEXO_STATE_KEY']='cd'*32
    elif mutation=='corrupt':
        data=p.read_bytes();p.write_bytes(data[:-1]+bytes([data[-1]^1]))
    elif mutation=='symlink':
        p.unlink();p.symlink_to(tmp_path/'input.txt')
    elif mutation=='oversized': p.write_bytes(b'x'*1048577)
    elif mutation=='permissions': p.chmod(0o644)
    result=execute(tmp_path,backend,env=env)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout,result.stdout.decode()
    assert len(model[2])==1


@pytest.mark.parametrize('operation',['read("state/missing")','write("state/owned", "bad")','write("authorization.json", "bad")','key'])
def test_application_cannot_access_granted_state_or_key(tmp_path,backend,model,operation):
    if operation=='key': operation=f'http.get("{model[0]}/leak", headers: {{"X-Key": secret("NEXO_STATE_KEY")}})'
    prepare(tmp_path,model,suffix='print('+operation+')')
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert not model[2]
    assert not (tmp_path/'state'/'owned').exists()


def test_output_defaults_rejected_before_provider(tmp_path,backend,model):
    prepare(tmp_path,model,suffix='type Reply:\n    answer: str = "default"\nprint(reviewer.handle("text", run: "case-1", as: Reply))')
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert not model[2]


def start(tmp_path,backend):
    env=dict(os.environ,PYTHONPATH=str(ROOT),PYTHONIOENCODING='utf-8',NEXO_LEAKCHECK='1',NEXO_AI_KEY='SECRET-API-KEY',NEXO_STATE_KEY=KEY,XDG_CONFIG_HOME=str(tmp_path/'config'))
    return subprocess.Popen([*backend,'security','run','--authorization','authorization.json','main.nexo'],cwd=tmp_path,env=env,
                            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)


def stop(process):
    if process.poll() is None: os.killpg(process.pid,signal.SIGKILL)
    return process.communicate(timeout=10)[0]


def wait_state(tmp_path,phase,process):
    until=time.monotonic()+30
    while time.monotonic()<until:
        if process.poll() is not None: pytest.fail(process.communicate()[0].decode())
        if any(v.get('phase')==phase for _,v in decrypt(tmp_path)): return
        time.sleep(.02)
    pytest.fail('checkpoint never reached '+phase)


def test_crash_awaiting_approval_requests_fresh_decision(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    prepare(tmp_path,model,tool=TOOL,confirm=True)
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'approval',process)
    finally: stop(process)
    assert len(model[2])==1
    assert not (tmp_path/'marker').exists()
    refused=execute(tmp_path,backend,stdin=b'no\n')
    assert refused.returncode==0,refused.stdout.decode()
    assert b'Use report()?' in refused.stdout
    assert not (tmp_path/'marker').exists()
    assert len(model[2])==2


def test_crash_after_tool_dispatch_never_replays_effect(tmp_path,backend,model):
    model[1].update(calls=[tool_call()],tool_delay=15)
    tool=f'''@tool "Writes before a long request"
fn report() -> str!:
    write("marker", "one effect")?
    http.get("{model[0]}/slow", retries: 0)?
    return "done"
'''
    prepare(tmp_path,model,tool=tool)
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'tool_pending',process)
        until=time.monotonic()+5
        while not (tmp_path/'marker').exists() and time.monotonic()<until: time.sleep(.02)
        assert (tmp_path/'marker').read_text()=='one effect'
    finally: stop(process)
    (tmp_path/'marker').unlink()
    resumed=execute(tmp_path,backend)
    assert resumed.returncode==1,resumed.stdout.decode()
    assert b'Agent state' in resumed.stdout
    assert not (tmp_path/'marker').exists()
    assert len(model[2])==1


def test_crash_during_provider_keeps_uncertain_reservation(tmp_path,backend,model):
    model[1].update(provider='openai',delay=10)
    budget={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['1','1']}}
    prepare(tmp_path,model,budget=budget)
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'provider_pending',process)
        until=time.monotonic()+5
        while not model[2] and time.monotonic()<until: time.sleep(.02)
        assert len(model[2])==1
    finally: stop(process)
    resumed=execute(tmp_path,backend)
    assert resumed.returncode==1,resumed.stdout.decode()
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    assert len(entries)==1 and entries[0]['pending'] and entries[0]['amount']=='1'
    # A new run cannot release the crashed run's hold.
    path=tmp_path/'main.nexo';path.write_text(path.read_text().replace('case-1','case-2'))
    blocked=execute(tmp_path,backend)
    assert blocked.returncode==1,blocked.stdout.decode()
    assert len(model[2])==1


def test_run_lock_rejects_concurrent_process(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    prepare(tmp_path,model,tool=TOOL,confirm=True)
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'approval',process)
        blocked=execute(tmp_path,backend)
        assert blocked.returncode==1,blocked.stdout.decode()
        assert b'Agent state' in blocked.stdout
        assert len(model[2])==1
    finally: stop(process)


def trace_records(output):
    return [json.loads(line) for line in output.decode().splitlines() if line.startswith('{"resourceSpans":')]


def test_trace_opt_in_redacts_all_application_data(tmp_path,backend,model):
    model[1]['calls']=[tool_call()]
    prepare(tmp_path,model,tool=TOOL)
    result=execute(tmp_path,backend,env={'NEXO_AGENT_TRACE':'stderr'})
    assert result.returncode==0,result.stdout.decode()
    records=trace_records(result.stdout)
    from otlp_profile import validate
    schema=json.loads((ROOT/"docs/schemas/agent-trace-v1.json").read_text())
    for record in records: validate(record,schema)
    spans=[r['resourceSpans'][0]['scopeSpans'][0]['spans'][0] for r in records]
    assert sorted(s['name'] for s in spans)==['nexo.agent.provider','nexo.agent.provider','nexo.agent.run','nexo.agent.tool']
    import re
    for span in spans:
        assert re.fullmatch('[0-9a-f]{32}',span['traceId'])
        assert re.fullmatch('[0-9a-f]{16}',span['spanId'])
        assert span['kind']==1 and span['status']['code'] in (1,2)
        assert span['startTimeUnixNano'].isdigit() and int(span['endTimeUnixNano'])>=int(span['startTimeUnixNano'])
        assert set(span)<= {'name','traceId','spanId','parentSpanId','kind','startTimeUnixNano','endTimeUnixNano','status'}
    parent=next(s for s in spans if s['name']=='nexo.agent.run')
    assert all(s['traceId']==parent['traceId'] and s.get('parentSpanId')==parent['spanId'] for s in spans if s is not parent)
    encoded=json.dumps(records)
    for sensitive in ('SECRET-API-KEY','case-1','Review the supplied input','executed','main.nexo','127.0.0.1','ready'):
        assert sensitive not in encoded
    cached=execute(tmp_path,backend)
    assert cached.returncode==0,cached.stdout.decode()
    assert not trace_records(cached.stdout)


def test_invalid_exporter_fails_before_provider(tmp_path,backend,model):
    prepare(tmp_path,model)
    result=execute(tmp_path,backend,env={'NEXO_AGENT_TRACE':'https://collector.invalid'})
    assert result.returncode==1,result.stdout.decode()
    assert not model[2]


def test_changed_resolved_model_rejects_cached_run(tmp_path,backend,model):
    program,_=prepare(tmp_path,model)
    (tmp_path/'main.nexo').write_text(program.replace('    model = "mock"','    model = read("model.txt")'))
    (tmp_path/'model.txt').write_text('mock')
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    (tmp_path/'model.txt').write_text('changed')
    second=execute(tmp_path,backend)
    assert second.returncode==1,second.stdout.decode()
    assert b'Agent state' in second.stdout
    assert len(model[2])==1


def test_durable_receipts_charge_once_across_processes(tmp_path,backend,model):
    model[1].update(provider='openai',usage_response={'usage':{'prompt_tokens':10,'completion_tokens':20}})
    budget={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['10000','20000']}}
    prepare(tmp_path,model,budget=budget)
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    cached=execute(tmp_path,backend)
    assert cached.returncode==0,cached.stdout.decode()
    assert len(model[2])==1
    path=tmp_path/'main.nexo'
    path.write_text(path.read_text().replace('case-1','case-2'))
    second=execute(tmp_path,backend)
    assert second.returncode==0,second.stdout.decode()
    path.write_text(path.read_text().replace('case-2','case-3'))
    third=execute(tmp_path,backend)
    assert third.returncode==1,third.stdout.decode()
    assert len(model[2])==2
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    from decimal import Decimal
    assert sum(Decimal(e['amount']) for e in entries)==1
    assert all(not e['pending'] for e in entries)


def test_cross_backend_resume_refuses_migration_before_effects(tmp_path,model):
    import sys
    from phases import commands
    if sys.platform!='linux': pytest.skip('native agent persistence requires Linux AMD64')
    implementations=commands()
    if len(implementations)<2: pytest.skip('both backends required')
    model[1]['calls']=[tool_call()]
    prepare(tmp_path,model,tool=TOOL)
    first=execute(tmp_path,implementations[0][1])
    assert first.returncode==0,first.stdout.decode()
    (tmp_path/'marker').unlink()
    second=execute(tmp_path,implementations[1][1])
    assert second.returncode==1,second.stdout.decode()
    assert b'Agent state' in second.stdout
    assert len(model[2])==2
    assert not (tmp_path/'marker').exists()


@pytest.mark.parametrize('mutation',['fifo','hardlink','directory','lock_symlink','root_permissions'])
def test_unsafe_files_never_block_or_resume(tmp_path,backend,model,mutation):
    prepare(tmp_path,model)
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    path=next((tmp_path/'state').glob('*.state'))
    if mutation=='fifo':
        path.unlink();os.mkfifo(path,0o600)
    elif mutation=='hardlink': os.link(path,tmp_path/'linked')
    elif mutation=='directory':
        path.unlink();path.mkdir(mode=0o700)
    elif mutation=='lock_symlink':
        lock=path.with_suffix('.lock');lock.unlink();lock.symlink_to(tmp_path/'main.nexo')
    else: (tmp_path/'state').chmod(0o755)
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout or b'authorization' in result.stdout.lower()
    assert len(model[2])==1


def test_missing_usage_retains_durable_hold_before_tool(tmp_path,backend,model):
    model[1].update(provider='openai',calls=[tool_call()])
    budget={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['1','1']}}
    prepare(tmp_path,model,tool=TOOL,budget=budget)
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert not (tmp_path/'marker').exists()
    assert len(model[2])==1
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    assert entries[0]['pending'] and entries[0]['amount']=='1'


def test_step_exhaustion_is_terminal_without_replay(tmp_path,backend,model):
    model[1].update(calls=[tool_call()],repeat_calls=True)
    program,_=prepare(tmp_path,model,tool=TOOL)
    (tmp_path/'main.nexo').write_text(program.replace('max_steps = 3','max_steps = 2'))
    first=execute(tmp_path,backend)
    assert first.returncode==1,first.stdout.decode()
    assert len(model[2])==2
    assert (tmp_path/'marker').exists()
    (tmp_path/'marker').unlink()
    second=execute(tmp_path,backend)
    assert second.returncode==1,second.stdout.decode()
    assert b'Agent state' in second.stdout
    assert len(model[2])==2
    assert not (tmp_path/'marker').exists()
    assert decrypt(tmp_path)[0][1]['phase']=='failed'


def test_completed_call_is_not_reapproved_or_replayed_after_crash(tmp_path,backend,model):
    model[1]['calls']=[tool_call(),tool_call(id='call-2')]
    prepare(tmp_path,model,tool=TOOL,confirm=True)
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'approval',process)
        process.stdin.write(b'yes\n');process.stdin.flush()
        until=time.monotonic()+20
        while time.monotonic()<until:
            if process.poll() is not None: pytest.fail(process.communicate()[0].decode())
            frames=[v for _,v in decrypt(tmp_path) if v.get('phase')=='approval' and len(v['calls'])==1]
            if frames: break
            time.sleep(.02)
        else: pytest.fail('second approval checkpoint not reached')
        assert (tmp_path/'marker').read_text()=='executed'
    finally: stop(process)
    (tmp_path/'marker').unlink()
    result=execute(tmp_path,backend,stdin=b'no\n')
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout.count(b'Use report()?')==1
    assert not (tmp_path/'marker').exists()
    assert len(model[2])==2


@pytest.mark.parametrize('invalid',['version','phase','identity','step','target','queue','final_queue','tokens'])
def test_authenticated_but_invalid_checkpoint_is_rejected(tmp_path,backend,model,invalid):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    prepare(tmp_path,model)
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    path,value=decrypt(tmp_path)[0]
    if invalid=='version': value['version']=True
    elif invalid=='phase': value['phase']='approved'
    elif invalid=='identity': value['identity']='other'
    elif invalid=='step': value['step']=99999
    elif invalid=='target': value['target']='other'
    elif invalid=='queue': value['calls']=[['id','report','[]']]
    elif invalid=='tokens': value['tokens']=[[1,1,False]]
    else: value['calls']=[['id','report','{}']]
    nonce=os.urandom(12)
    path.write_bytes(nonce+AESGCM(bytes.fromhex(KEY)).encrypt(nonce,json.dumps(value).encode(),path.stem.encode()))
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert len(model[2])==1


def test_granted_agent_requires_run_identity(tmp_path,backend,model):
    prepare(tmp_path,model,suffix='print(reviewer.handle("text"))')
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert not model[2]


def test_spawned_tool_provider_uses_inherited_durable_account(tmp_path,backend,model):
    model[1].update(provider='openai',calls=[tool_call()],nested_ask=True,
                    usage_response={'usage':{'prompt_tokens':10,'completion_tokens':20}})
    budget={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['1000','1000']}}
    tool='''use nexo.sync
fn nested(done):
    try:
        answer = ask("Nested prompt")
        done.send(answer)
    else e:
        done.send("nested failed")
@tool "Uses the same account in a child task"
fn report() -> str:
    done = channel(size: 1)
    spawn nested(done)
    return done.receive()
'''
    prepare(tmp_path,model,tool=tool,budget=budget)
    result=execute(tmp_path,backend)
    assert result.returncode==0,result.stdout.decode()
    assert len(model[2])==3
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    assert len(entries)==3 and all(not e['pending'] for e in entries)
    from decimal import Decimal
    assert sum(Decimal(e['amount']) for e in entries)==Decimal('0.09')


def test_durable_transport_failure_never_retries_automatically(tmp_path,backend,model):
    model[1]['disconnect']=True
    prepare(tmp_path,model)
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert len(model[2])==1
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert len(model[2])==1


def test_cross_process_active_hold_blocks_another_run(tmp_path,backend,model):
    model[1].update(provider='openai',delay=10)
    budget={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['1','1']}}
    prepare(tmp_path,model,suffix='print(reviewer.handle("text", run: read("run.txt")))',budget=budget)
    (tmp_path/'run.txt').write_text('first')
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'provider_pending',process)
        until=time.monotonic()+5
        while not model[2] and time.monotonic()<until: time.sleep(.02)
        assert len(model[2])==1
        (tmp_path/'run.txt').write_text('second')
        other=execute(tmp_path,backend)
        assert other.returncode==1,other.stdout.decode()
        assert len(model[2])==1
    finally:stop(process)
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    assert len(entries)==1 and entries[0]['pending']


def test_small_durable_debits_remain_readable(tmp_path,backend,model):
    model[1].update(provider='openai',usage_response={'usage':{'prompt_tokens':10,'completion_tokens':20}})
    budget={'limit':'0.000001','currency':'USD','period':'day','prices':{'openai:mock':['0.000001','0.000001']}}
    prepare(tmp_path,model,budget=budget)
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    path=tmp_path/'main.nexo';path.write_text(path.read_text().replace('case-1','case-2'))
    second=execute(tmp_path,backend)
    assert second.returncode==0,second.stdout.decode()
    assert len(model[2])==2
    entries=[v['entries'] for _,v in decrypt(tmp_path) if 'entries' in v][0]
    from decimal import Decimal
    assert sum(Decimal(e['amount']) for e in entries)==Decimal('0.00000000006')


@pytest.mark.parametrize('invalid',['field','key','currency','fraction','number','bound','unknown_agent','symlink','overlap','owner'])
def test_invalid_state_grant_fails_before_execution(tmp_path,backend,model,invalid):
    _,config=prepare(tmp_path,model)
    grant=config['state']['main.nexo::reviewer']
    if invalid=='field':grant['unknown']=True
    elif invalid=='key':grant['key']=''
    elif invalid=='bound':grant['max_bytes']=True
    elif invalid=='unknown_agent':config['state']['main.nexo::missing']=config['state'].pop('main.nexo::reviewer')
    elif invalid=='symlink':
        (tmp_path/'alias').symlink_to(tmp_path/'state',target_is_directory=True)
        grant['directory']='alias'
    elif invalid=='owner':
        candidates=[p for p in (Path('/root'),Path('/var/root')) if p.exists() and p.stat().st_uid!=os.geteuid() and p.stat().st_mode & 0o777==0o700]
        if not candidates: pytest.skip('no private directory owned by another UID')
        grant['directory']=str(candidates[0])
    elif invalid=='overlap':
        child=tmp_path/'state'/'nested';child.mkdir(mode=0o700)
        config['agents']['main.nexo::other']={'permissions':[],'tools':[]}
        config['state']['main.nexo::other']={**grant,'directory':'state/nested'}
    else:
        grant['budget']={'limit':'1','currency':'USD','period':'day','prices':{'openai:mock':['1','1']}}
        if invalid=='currency':grant['budget']['currency']='US1'
        elif invalid=='fraction':grant['budget']['prices']['openai:mock'][0]='0.0000001'
        else:grant['budget']['limit']='.'
    (tmp_path/'authorization.json').write_text(json.dumps(config))
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'authorization' in result.stdout.lower(),result.stdout.decode()
    assert not model[2]
    assert not list((tmp_path/'state').glob('*.state'))


def test_nested_output_alias_is_rejected_before_provider(tmp_path,backend,model):
    prepare(tmp_path,model,suffix='type Status = "active" | "closed"\ntype Reply:\n    status: Status\nprint(reviewer.handle("text", run: "case-1", as: Reply))')
    result=execute(tmp_path,backend)
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert not model[2]


def test_durable_date_field_is_safe_and_revalidated(tmp_path,backend,model):
    model[1]['content']='{"day":"2026-10-30"}'
    prepare(tmp_path,model,suffix='type Reply:\n    day: date\nprint(reviewer.handle("text", run: "case-1", as: Reply))')
    first=execute(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    second=execute(tmp_path,backend)
    assert second.returncode==0,second.stdout.decode()
    assert second.stdout==first.stdout
    assert len(model[2])==1


def test_cancelled_entry_creates_no_checkpoint_or_lock(tmp_path,backend,model):
    prepare(tmp_path,model,suffix='''control = reviewer.control()
control.cancel()
try:
    reviewer.handle("text", run: "case-1", cancel: control)
else e is Cancelled:
    print("cancelled")''')
    result=execute(tmp_path,backend)
    assert result.returncode==0,result.stdout.decode()
    assert result.stdout==b'cancelled\n'
    assert not model[2]
    assert not list((tmp_path/'state').iterdir())


@pytest.mark.parametrize('change',['caller','dependencies'])
def test_cached_result_is_bound_to_execution_authority(tmp_path,backend,model,change):
    model[1].update(content='PRIVATE-FINDING',calls=[tool_call()])
    (tmp_path/'helper.nexo').write_text('pub fn forward(callback: any) -> str!:\n    return callback()?\n')
    tool='''@tool "Reads under caller ceilings"
fn report() -> str!:
    return read("private.txt")?
'''
    suffix='''fn invoke() -> str!:
    return reviewer.handle("text", run: "case-1")?
if read("mode.txt") == "root":
    print(invoke())
else:
    print(helper.forward(invoke))'''
    program,_=prepare(tmp_path,model,suffix=suffix,tool=tool)
    (tmp_path/'main.nexo').write_text(program.replace('ai:\n','use helper\nai:\n',1))
    (tmp_path/'private.txt').write_text('PRIVATE-DATA')
    (tmp_path/'public.txt').write_text('PUBLIC')
    (tmp_path/'mode.txt').write_text('root')
    profile={'version':1,'modules':{'helper.nexo':[['allow','net','127.0.0.1'],['allow','env','NEXO_AI_KEY'],['allow','read','public.txt']]}}
    (tmp_path/'permissions.json').write_text(json.dumps(profile))
    env={'NEXO_AI_KEY':'local-test','NEXO_STATE_KEY':KEY,'XDG_CONFIG_HOME':str(tmp_path/'config')}
    command=['security','run','--authorization','authorization.json','--dependencies','permissions.json','main.nexo']
    first=run(backend,command,tmp_path,env=env)
    assert first.returncode==0,first.stdout.decode()
    assert first.stdout==b'PRIVATE-FINDING\n'
    assert len(model[2])==2
    if change=='caller':(tmp_path/'mode.txt').write_text('helper')
    else:
        profile['modules']['helper.nexo'].append(['deny','read','private.txt'])
        (tmp_path/'permissions.json').write_text(json.dumps(profile))
    second=run(backend,command,tmp_path,env=env)
    assert second.returncode==1,second.stdout.decode()
    assert b'Agent state' in second.stdout
    assert b'PRIVATE-FINDING' not in second.stdout
    assert len(model[2])==2


def test_application_cannot_rewrite_dependency_authority(tmp_path,backend,model):
    prepare(tmp_path,model,suffix='print(write("permissions.json", "tampered"))')
    original=json.dumps({'version':1,'modules':{}})
    (tmp_path/'permissions.json').write_text(original)
    result=run(backend,['security','run','--authorization','authorization.json','--dependencies','permissions.json','main.nexo'],tmp_path,
               env={'NEXO_AI_KEY':'local-test','NEXO_STATE_KEY':KEY,'XDG_CONFIG_HOME':str(tmp_path/'config')})
    assert result.returncode==1,result.stdout.decode()
    assert b'Agent state' in result.stdout
    assert (tmp_path/'permissions.json').read_text()==original
    assert not model[2]
