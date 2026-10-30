"""Input+output token admission, inherited provider work and durable counters."""
import pytest
from test_agent_lifecycle import backend, model, source, execute, tool_call, TOOL
from test_agent_recovery import prepare, execute as durable, start, stop, wait_state, decrypt


def metered(model,program,limit):
    return program.replace('max_steps = 3',f'max_steps = 3\n    max_tokens = {limit}')


def usage(model,provider='openai',input=3,output=2):
    model[1]['provider']=provider
    if provider=='ollama':value={'prompt_eval_count':input,'eval_count':output}
    elif provider=='anthropic':value={'usage':{'input_tokens':input,'output_tokens':output}}
    elif provider=='gemini':value={'usageMetadata':{'promptTokenCount':input,'candidatesTokenCount':output}}
    else:value={'usage':{'prompt_tokens':input,'completion_tokens':output}}
    model[1]['usage_response']=value


@pytest.mark.parametrize('provider',['openai','ollama','anthropic','gemini'])
def test_provider_neutral_token_budget(tmp_path,backend,model,provider):
    usage(model,provider)
    model[1]['calls']=[tool_call()]
    program=metered(model,source(model,'print(reviewer.handle("text", as: Finding))',TOOL),10)
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert len(model[2])==2
    assert (tmp_path/'marker').exists()
    for request,cap in zip(model[2],[10,5]):
        actual=(request['options']['num_predict'] if provider=='ollama' else
                request['generationConfig']['maxOutputTokens'] if provider=='gemini' else request['max_tokens'])
        assert actual==cap


@pytest.mark.parametrize('limit',['0','-1','true','1.5','none'])
def test_invalid_token_budget_fails_before_provider(tmp_path,backend,model,limit):
    program=metered(model,source(model,'print(reviewer.handle("text"))'),limit)
    result=execute(tmp_path,backend,program)
    assert result.returncode==1,result.stdout.decode()
    assert b'positive integer' in result.stdout
    assert not model[2]


@pytest.mark.parametrize('missing',[False,True])
def test_excess_or_unknown_tokens_never_dispatch_tools(tmp_path,backend,model,missing):
    usage(model)
    model[1]['calls']=[tool_call()]
    if missing:model[1]['usage_response']={}
    result=execute(tmp_path,backend,metered(model,source(model,'print(reviewer.handle("text"))',TOOL),4))
    assert result.returncode==1,result.stdout.decode()
    assert len(model[2])==1
    assert (b'valid token usage' if missing else b'token budget') in result.stdout
    assert not (tmp_path/'marker').exists()


def test_token_counter_survives_approval_restart(tmp_path,backend,model):
    usage(model)
    model[1]['calls']=[tool_call()]
    program,_=prepare(tmp_path,model,tool=TOOL,confirm=True)
    (tmp_path/'main.nexo').write_text(metered(model,program,5))
    process=start(tmp_path,backend)
    try:wait_state(tmp_path,'approval',process)
    finally:stop(process)
    result=durable(tmp_path,backend,stdin=b'yes\n')
    assert result.returncode==1,result.stdout.decode()
    assert b'token budget' in result.stdout
    assert (tmp_path/'marker').exists()
    assert len(model[2])==1
    assert next(v['tokens'] for _,v in decrypt(tmp_path) if 'phase' in v)==[[5,0,False]]


def test_spawned_ask_shares_tokens_and_completed_cache(tmp_path,backend,model):
    usage(model,input=1,output=2)
    model[1].update(calls=[tool_call()],nested_ask=True)
    tool='''use nexo.sync
fn nested(done):
    try:
        answer = ask("nested")
        done.send(answer)
    else e:
        done.send("failed")
@tool "Consumes the inherited token account"
fn report() -> str:
    done = channel(size: 1)
    spawn nested(done)
    return done.receive()
'''
    program,_=prepare(tmp_path,model,tool=tool)
    (tmp_path/'main.nexo').write_text(metered(model,program,9))
    first=durable(tmp_path,backend)
    assert first.returncode==0,first.stdout.decode()
    second=durable(tmp_path,backend)
    assert second.returncode==0,second.stdout.decode()
    assert first.stdout==second.stdout
    assert len(model[2])==3
    assert [r['max_tokens'] for r in model[2]]==[9,6,3]
    assert next(v['tokens'] for _,v in decrypt(tmp_path) if 'phase' in v)==[[9,0,False]]


def test_parallel_tool_requests_share_one_token_reservation(tmp_path,backend,model):
    usage(model,input=1,output=0)
    model[1].update(calls=[tool_call()],nested_ask=True,delay=0.2)
    tool='''use nexo.sync
fn nested(done):
    try:
        answer = ask("nested")
        done.send("ok")
    else e is AIError:
        done.send("blocked")
@tool "Starts competing requests"
fn report() -> str:
    done = channel(size: 2)
    spawn nested(done)
    spawn nested(done)
    return str([done.receive(),done.receive()])
'''
    program=metered(model,source(model,'print(reviewer.handle("text"))',tool),7)
    result=execute(tmp_path,backend,program)
    assert result.returncode==0,result.stdout.decode()
    assert len(model[2])==3
    assert [r['max_tokens'] for r in model[2]]==[7,6,5]
    results=[m['content'] for m in model[2][-1]['messages'] if m.get('role')=='tool']
    assert 'blocked' in results[0] and 'ok' in results[0]


def test_late_child_receipt_survives_stale_approval_checkpoint(tmp_path,backend,model):
    import json
    import time
    usage(model,input=1,output=0)
    model[1].update(calls=[tool_call(),tool_call(name='later',id='call-2')],nested_ask=True)
    import threading
    model[1]['release_late']=threading.Event()
    tool=f'''use nexo.sync
fn background(client,url):
    try:
        client.get(url,retries: 0)
        answer = ask("late work")
    else e:
        print("background failed")
@tool "Starts delayed provider work"
fn report() -> str:
    spawn background(http,"{model[0]}/late")
    return "queued"
@tool "Requires a new decision"
fn later() -> str!:
    write("marker", "must not execute")?
    return "done"
'''
    program,config=prepare(tmp_path,model,tool=tool)
    program=metered(model,program,2).replace('tools = [report]','tools = [report, later]')
    (tmp_path/'main.nexo').write_text(program)
    config['tools']['main.nexo::later']={**config['tools']['main.nexo::report'],'confirm':True}
    config['agents']['main.nexo::reviewer']['tools'].append('main.nexo::later')
    (tmp_path/'authorization.json').write_text(json.dumps(config))
    process=start(tmp_path,backend)
    try:
        wait_state(tmp_path,'approval',process)
        model[1]['release_late'].set()
        until=time.monotonic()+20
        while time.monotonic()<until:
            ledgers=[v for _,v in decrypt(tmp_path) if 'limit' in v]
            if ledgers and ledgers[0]['remaining']==0 and not ledgers[0]['pending']:break
            time.sleep(.02)
        else:pytest.fail('late receipt did not settle')
        frame=next(v for _,v in decrypt(tmp_path) if 'phase' in v)
        assert frame['tokens']==[[2,1,False]]  # Deliberately stale snapshot.
    finally:
        model[1]['release_late'].set()
        stop(process)
    result=durable(tmp_path,backend,stdin=b'no\n')
    assert result.returncode==1,result.stdout.decode()
    assert b'token budget' in result.stdout
    assert len(model[2])==2
    assert not (tmp_path/'marker').exists()
