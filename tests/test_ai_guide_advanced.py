import importlib.util
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('guide_local',ROOT/'docs/ai-guide/local.py')
local=importlib.util.module_from_spec(spec);spec.loader.exec_module(local)


def test_readiness_unknown_and_unsubstantiated_pass():
    report=local.readiness({'functional':{'status':'Passed'}})
    assert report['score']==0
    assert 'functional' in report['blockers']
    assert not report['certification']


def test_readiness_evidence_na_and_blockers():
    values={c['id']:{'status':'Passed','evidence':'reviewed artifact'} for c in json.loads((local.HERE/'source/readiness.json').read_text())['categories']}
    values['security']['status']='Failed'
    report=local.readiness(values)
    assert report['score']==87.5 and 'security' in report['blockers']
    for value in values.values():value['status']='Not applicable'
    assert local.readiness(values)['score'] is None
    with pytest.raises(ValueError):local.readiness({'typo':{}})


def test_adr_is_proposed_and_requires_content():
    with pytest.raises(ValueError):local.adr({})
    text=local.adr({k:'Review this choice' for k in ('context','decision','alternatives','consequences','cost_complexity','reconsider_when')})
    assert 'Status: Proposed' in text and 'explicit user approval' in text


def test_profiler_no_prices_or_keys_required():
    class Encoder:
        def encode_ordinary(self,text):return list(text)
    task={'iterations':[{'input':'abc','output':'de'},{'input':'abcd','output':'f'}]}
    report=local.profile(task,Encoder())
    assert (report['input_tokens'],report['output_tokens'],report['total_tokens'])==(7,3,10)
    assert report['correction_iterations']==1 and report['estimated_cost'] is None
    assert report['compilation']=='Unknown'
    task['pricing']={'currency':'EUR','input_per_million':2,'output_per_million':4}
    assert local.profile(task,Encoder())['estimated_cost']==pytest.approx(26/1e6)
    task['pricing']['input_per_million']=float('nan')
    with pytest.raises(ValueError):local.profile(task,Encoder())


def test_verified_examples_match_sources_and_have_declared_limits():
    report=json.loads((local.HERE/'examples-verification.json').read_text())
    assert report['runtime_revision']==local.runtime_revision()
    catalog=json.loads((local.HERE/'source/examples.json').read_text())
    assert {e['id'] for e in catalog['examples']}=={'cli','rest','database','events','ai'}
    for example in catalog['examples']:
        row=report['results'][example['id']]
        assert row['source_sha256']==local.sha((ROOT/example['path']).read_bytes())
        assert row['check']==row['execution']=='Passed'
        assert row['native_compilation']=='Unknown'
        assert example['scope'] and example['features'] and example['domains']


def test_paired_benchmark_rejects_unmatched_or_different_models(monkeypatch):
    index=json.loads((local.HERE/'versions.json').read_text())
    document={'guide_revision':index['current'],'task_catalog_sha256':local.sha((local.HERE/'source/examples.json').read_bytes()),'records':[{'task':'cli','arm':'guide','model_configuration':{'model':'fixture'}}]}
    with pytest.raises(ValueError,match='Paired tasks'):local.benchmark(document,ROOT)
    document['records'].append({'task':'cli','arm':'without-guide','model_configuration':{'model':'other'}})
    with pytest.raises(ValueError,match='identical model'):local.benchmark(document,ROOT)


def test_actual_tokenizer_is_reproducible_when_installed():
    pytest.importorskip('tiktoken')
    report=local.profile({'iterations':[{'input':'Hello Nexo','output':'ok'}]})
    assert report['input_tokens']==3 and report['output_tokens']==1


def test_benchmark_does_not_count_empty_candidate_as_success(tmp_path,monkeypatch):
    index=json.loads((local.HERE/'versions.json').read_text())
    document={'guide_revision':index['current'],'task_catalog_sha256':local.sha((local.HERE/'source/examples.json').read_bytes()),'records':[]}
    (tmp_path/'empty.nexo').write_text('')
    for arm in ('guide','without-guide'):
        document['records'].append({'task':'cli','arm':arm,'candidate':'empty.nexo','model_configuration':{'fixture_only':True},'iterations':[{'input':'x','output':''}]})
    monkeypatch.setattr(local,'profile',lambda _: {'estimated_cost':None,'currency':None})
    monkeypatch.setattr(local,'execute',lambda *args,**kwargs: {'returncode':0,'stdout':'','stderr':'','seconds':0})
    result=local.benchmark(document,tmp_path)
    assert result['aggregates']['guide']['successful_validations']==0
    assert result['aggregates']['guide']['test_pass_rate']==0
    assert result['results'][0]['measurements']['compilation']=='Unknown'
    assert result['results'][0]['security_runtime_validation']=='Unknown'


@pytest.mark.parametrize('values',[{}, {'functional':{'status':'Passed'}}, {'security':{'status':'Passed','evidence':'reviewed security test'}}, {'deployment':{'status':'Not applicable','evidence':'No deployment in this reviewed scope'}}])
def test_browser_and_local_readiness_rules_agree(values):
    import subprocess
    script="const e=require('./docs/assets/ai-engineering.js');const r=require('./docs/ai-guide/source/readiness.json');console.log(JSON.stringify(e.readiness(r,JSON.parse(process.argv[1]))));"
    output=subprocess.check_output(['node','-e',script,json.dumps(values)],cwd=ROOT,text=True)
    assert json.loads(output)==local.readiness(values)
