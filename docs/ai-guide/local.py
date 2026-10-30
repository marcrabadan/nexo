#!/usr/bin/env python3
"""Local-only guide tools: no model APIs, network requests or API keys."""
import argparse
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

def sha(data):
    return hashlib.sha256(data).hexdigest()

def dump(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)+'\n'

def runtime_revision():
    paths = sorted((ROOT/'nexo').rglob('*.py'))
    return sha(dump({str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in paths}).encode())

def execute(source, command, native=None):
    """Isolated working directory, not a hostile-code security sandbox."""
    with tempfile.TemporaryDirectory(prefix='nexo-guide-') as folder:
        app = Path(folder)/'app.nexo'
        app.write_text(source)
        cli = [str(Path(native).resolve())] if native else [sys.executable,'-m','nexo.cli']
        args = [] if command=='run' else (['build','-o',str(Path(folder)/'native-app')] if command=='build' else command.split())
        started = time.perf_counter()
        try:
            result = subprocess.run([*cli,*args,str(app)],cwd=folder,
                env={**os.environ,'PYTHONPATH':str(ROOT),'PYTHONHASHSEED':'0','TZ':'UTC'},
                text=True,capture_output=True,timeout=30)
            return {'returncode':result.returncode,'stdout':result.stdout.replace(str(app),'app.nexo'),
                    'stderr':result.stderr.replace(str(app),'app.nexo'),'seconds':time.perf_counter()-started}
        except subprocess.TimeoutExpired:
            return {'returncode':124,'stdout':'','stderr':'local tool deadline exceeded','seconds':time.perf_counter()-started}

def verify_examples(record=False):
    config=json.loads((HERE/'source/examples.json').read_text())
    results={}
    for example in config['examples']:
        source=(ROOT/example['path']).read_text()
        checked=execute(source,'check')
        ran=execute(source,example['command'])
        if checked['returncode'] or ran['returncode']:
            raise ValueError(f"Example failed: {example['id']}: {checked['stderr']} {ran['stdout']} {ran['stderr']}")
        results[example['id']]={'source_sha256':sha(source.encode()),'check':'Passed','execution':'Passed',
            'command':example['command'],'stdout':ran['stdout'],'stderr':ran['stderr'],
            'backend':'Python interpreter','native_compilation':'Unknown'}
    report={'schema_version':1,'tool_sha256':sha(Path(__file__).read_bytes()),'runtime_revision':runtime_revision(),
            'python_package':tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version'],
            'results':results}
    target=HERE/'examples-verification.json'
    if record:
        target.write_text(dump(report))
    elif json.loads(target.read_text())!=report:
        raise ValueError('Example verification changed; review and run verify-examples --record')
    return report

def tokenizer():
    import importlib.metadata
    import tiktoken
    if importlib.metadata.version('tiktoken')!='0.12.0':
        raise ValueError('Use tiktoken==0.12.0')
    return tiktoken.get_encoding('cl100k_base')

def profile(task, encoding=None):
    encoding=encoding or tokenizer()
    iterations=task.get('iterations',[])
    if not isinstance(iterations,list) or not iterations:
        raise ValueError('iterations must contain input/output text per model turn')
    for turn in iterations:
        if not isinstance(turn.get('input'),str) or not isinstance(turn.get('output'),str):
            raise ValueError('Each iteration needs input and output strings; include full resent context')
    inputs=sum(len(encoding.encode_ordinary(t['input'])) for t in iterations)
    outputs=sum(len(encoding.encode_ordinary(t['output'])) for t in iterations)
    pricing=task.get('pricing')
    cost=None
    if pricing is not None:
        if not isinstance(pricing.get('currency'),str) or not pricing['currency'].strip():
            raise ValueError('Pricing requires currency')
        rates=[pricing.get('input_per_million'),pricing.get('output_per_million')]
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in rates):
            raise ValueError('Prices must be finite, nonnegative numbers')
        cost=(inputs*rates[0]+outputs*rates[1])/1_000_000
    for key in ('compilation','tests'):
        if task.get(key,'Unknown') not in ('Passed','Failed','Unknown'):
            raise ValueError(f'Invalid {key} result')
    return {'input_tokens':inputs,'output_tokens':outputs,'total_tokens':inputs+outputs,
        'correction_iterations':len(iterations)-1,'estimated_cost':cost,'currency':pricing['currency'] if pricing else None,
        'compilation':task.get('compilation','Unknown'),'tests':task.get('tests','Unknown'),
        'result_provenance':'Compilation/test fields are user-reported, not executed by profiler',
        'tokenizer':'tiktoken==0.12.0/cl100k_base/encode_ordinary',
        'limitation':'Reference tokens; target-model tokens and costs are estimates; excludes provider framing/caching/tool billing'}

def readiness(document):
    rubric=json.loads((HERE/'source/readiness.json').read_text())
    unknown=set(document)-{c['id'] for c in rubric['categories']}
    if unknown: raise ValueError(f'Unknown categories: {sorted(unknown)}')
    rows=[];blockers=[];passed=0;applicable=0
    for category in rubric['categories']:
        row=dict(document.get(category['id'],{}))
        status=row.get('status','Unknown')
        if status not in rubric['statuses']: raise ValueError('Invalid readiness status')
        evidence=row.get('evidence','')
        if not isinstance(evidence,str): raise ValueError('Evidence must be text')
        if status in ('Passed','Not applicable') and not evidence.strip():
            status='Unknown'
        if status!='Not applicable':
            applicable+=1;passed+=status=='Passed'
        critical=category['critical'] or row.get('critical',False)
        if critical and status in ('Unknown','Failed'): blockers.append(category['id'])
        rows.append({**category,'critical':critical,'status':status,'evidence':evidence,
            'risks':row.get('risks','Not assessed'), 'remediation':row.get('remediation','Collect and review evidence')})
    return {'score':round(100*passed/applicable,2) if applicable else None,
        'blockers':blockers,'categories':rows,'rule':rubric['scoring'],'certification':False}

def adr(document):
    fields=('context','decision','alternatives','consequences','cost_complexity','reconsider_when')
    if any(not isinstance(document.get(k),str) or not document[k].strip() for k in fields):
        raise ValueError('ADR requires '+', '.join(fields))
    return '# '+document.get('title','Architecture decision')+'\n\nStatus: Proposed (requires explicit user approval)\n\n'+ '\n\n'.join('## '+k.replace('_',' ').title()+'\n\n'+document[k] for k in fields)+'\n'

def benchmark(document, base, native=None):
    """Paired offline candidates; evaluates supplied artifacts, never invokes a model."""
    tasks={e['id']:e for e in json.loads((HERE/'source/examples.json').read_text())['examples']}
    if document.get('task_catalog_sha256') != sha((HERE/'source/examples.json').read_bytes()):
        raise ValueError('Task catalog hash mismatch')
    guide_revision=document.get('guide_revision','')
    if len(guide_revision)!=64 or not (HERE/'versions'/('v1-'+guide_revision[:20])/'manifest.json').is_file():
        raise ValueError('Known pinned guide_revision required')
    manifest=json.loads((HERE/'versions'/('v1-'+guide_revision[:20])/'manifest.json').read_text())
    if manifest['revision']!=guide_revision: raise ValueError('Guide revision mismatch')
    verification=json.loads((HERE/'examples-verification.json').read_text())
    records=document['records'];pairs={};results=[]
    if not records: raise ValueError('At least one pair is required')
    for record in records:
        task=record['task'];arm=record['arm']
        if task not in tasks or arm not in ('guide','without-guide'):
            raise ValueError('Unknown task or evaluation arm')
        if (task,arm) in pairs: raise ValueError('Duplicate task/arm')
        if not isinstance(record.get('model_configuration'),dict) or not record['model_configuration']:
            raise ValueError('Explicit model configuration required (use fixture_only for non-AI tests)')
        pairs[task,arm]=record
    for task,arm in pairs:
        other=pairs.get((task,'without-guide' if arm=='guide' else 'guide'))
        if not other or other['model_configuration']!=pairs[task,arm]['model_configuration']:
            raise ValueError('Paired tasks require identical model configuration')
        if other.get('pricing')!=pairs[task,arm].get('pricing'):
            raise ValueError('Paired tasks require identical pricing')
    for record in records:
        completion=record.get('completion_seconds')
        if completion is not None and (isinstance(completion,bool) or not isinstance(completion,(int,float)) or not math.isfinite(completion) or completion<0):
            raise ValueError('completion_seconds must be a finite nonnegative measurement')
        source=(base/record['candidate']).resolve().read_text()
        checked=execute(source,'check')
        tested=execute(source,tasks[record['task']]['command'])
        security=execute(source,'security check') if '\npolicy:' in '\n'+source else None
        compiled=execute(source,'build',native) if native else None
        measurement=profile(record)
        expected=verification['results'][record['task']]['stdout']
        acceptance=tested['returncode']==0 and tested['stdout']==expected
        measurement['compilation']='Unknown' if compiled is None else ('Passed' if compiled['returncode']==0 else 'Failed')
        measurement['tests']='Passed' if acceptance else 'Failed'
        measurement['result_provenance']='Local check/execution measured; optional native build measured when configured'
        results.append({'task':record['task'],'arm':record['arm'],'candidate_sha256':sha(source.encode()),
            'model_configuration':record['model_configuration'],'measurements':measurement,
            'semantic_check':checked,'execution':tested,'native_build':compiled,'security_check':security,
            'security_runtime_validation':'Unknown', 'security_validation_scope':'Static policy check only; denied effects not exercised', 'security_validation':'Unknown' if security is None else ('Passed' if security['returncode']==0 else 'Failed'),
            'completion_seconds_reported':record.get('completion_seconds'),
            'output_regression_passed':acceptance, 'successful_validation':checked['returncode']==0 and acceptance and (compiled is None or compiled['returncode']==0)})
    aggregates={}
    for arm in ('guide','without-guide'):
        group=[r for r in results if r['arm']==arm];success=sum(r['successful_validation'] for r in group)
        costs=[r['measurements']['estimated_cost'] for r in group]
        currencies={r['measurements']['currency'] for r in group}
        aggregates[arm]={'tasks':len(group),'successful_validations':success,
            'semantic_check_rate':sum(r['semantic_check']['returncode']==0 for r in group)/len(group) if group else None,
            'execution_pass_rate':sum(r['execution']['returncode']==0 for r in group)/len(group) if group else None,
            'test_pass_rate':sum(r['output_regression_passed'] for r in group)/len(group) if group else None,
            'estimated_cost_currency':next(iter(currencies)) if len(currencies)==1 else None,
            'native_compilation_rate':sum(r['measurements']['compilation']=='Passed' for r in group)/len(group) if native and group else None,
            'estimated_cost_per_success':sum(costs)/success if success and len(currencies)==1 and all(c is not None for c in costs) else None}
    return {'schema_version':1,'runtime_revision':runtime_revision(),'python':sys.version.split()[0], 'platform':platform.platform(), 'tool_sha256':sha(Path(__file__).read_bytes()), 'native_compiler_sha256':sha(Path(native).read_bytes()) if native else None, 'task_catalog_sha256':document['task_catalog_sha256'],
        'guide_revision':guide_revision, 'fixture_only':document.get('fixture_only',False),'results':results,'aggregates':aggregates,
        'limitations':'Offline paired candidate validation. No model invoked. Generation time/model provenance user-reported; tool times measured. Security check is not runtime isolation. Native compilation measured only when configured. Output regression against catalog oracle is not exhaustive functional validation. No causal improvement claim.'}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['verify-examples','profile','readiness','adr','benchmark'])
    parser.add_argument('input',nargs='?',type=Path)
    parser.add_argument('--record',action='store_true')
    parser.add_argument('--execute',action='store_true',help='Explicitly execute trusted local benchmark candidates; not a sandbox')
    parser.add_argument('--native-compiler')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.command=='verify-examples': result=verify_examples(args.record)
    else:
        if args.input is None: parser.error('input JSON file required')
        document=json.loads(args.input.read_text())
        if args.command=='benchmark':
            if not args.execute: parser.error('benchmark requires --execute for trusted candidates')
            result=benchmark(document,args.input.resolve().parent,args.native_compiler)
        else: result={'profile':profile,'readiness':readiness,'adr':adr}[args.command](document)
    output=result if isinstance(result,str) else dump(result)
    if args.output: args.output.write_text(output)
    else: print(output,end='')

if __name__=='__main__': main()
