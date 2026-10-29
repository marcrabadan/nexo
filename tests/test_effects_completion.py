"""Complete Phase 2: operator ceilings, caller paths and conservative refinement."""
import json
from pathlib import Path

import pytest

from test_check_json import commands
from test_effects import report, summaries, parity

APP = '''policy:
    allow read "data/"
use helper
@tool "Read"
fn entry() -> str!:
    return helper.load()?
agent reader:
    tools = [entry]
    goal = "Read authorized logs"
'''
HELPER = 'pub fn load() -> str!:\n    return read("data/log")?\n'


def configured(commands, tmp_path, source, *options):
    results = [report(cmd, tmp_path, source, options) for cmd in commands]
    assert all(r == results[0] for r in results), results
    return results[0]


def manifest(tmp_path, rules):
    (tmp_path / 'helper.nexo').write_text(HELPER)
    (tmp_path / 'deps.json').write_text(json.dumps({'version': 1, 'modules': {'helper.nexo': rules}}))


@pytest.mark.parametrize('rules,expected', [
    ([['allow', 'read', ['data/']]], 'allowed'),
    ([['deny', 'read', ['data/']]], 'denied'),
    ([], 'denied'),
])
def test_dependency_ceilings(commands, tmp_path, rules, expected):
    # Runtime manifests use a flat array of words per rule.
    rules = [[effect, kind, *targets] for effect, kind, targets in rules]
    manifest(tmp_path, rules)
    code, doc = configured(commands, tmp_path, APP, '--dependencies', 'deps.json')
    data = summaries(doc)['entry']
    [effect] = data['effects']
    assert data['policy_scope'] == 'configured'
    assert code == (0 if expected == 'allowed' else 1)
    assert effect['status'] == expected
    checks = {c['scope']: c for c in effect['checks']}
    assert checks['root']['status'] == 'allowed'
    assert checks['dependency']['status'] == expected


def test_transitive_caller_intersection(commands, tmp_path):
    (tmp_path / 'leaf.nexo').write_text(HELPER)
    (tmp_path / 'helper.nexo').write_text('use leaf\npub fn load() -> str!:\n    return leaf.load()?\n')
    (tmp_path / 'deps.json').write_text(json.dumps({'version': 1, 'modules': {
        'helper.nexo': [['deny', 'read', 'data/']], 'leaf.nexo': [['allow', 'read', 'data/']]}}))
    code, doc = configured(commands, tmp_path, APP, '--dependencies', 'deps.json')
    assert code == 1
    effect = summaries(doc)['entry']['effects'][0]
    rows = {c['identity']: c['status'] for c in effect['checks'] if c['scope'] == 'dependency'}
    assert rows == {'helper.nexo': 'denied', 'leaf.nexo': 'allowed'}
    leaf = next(d for d in doc['diagnostics'] if d['file'] == 'leaf.nexo' and d['data']['function'] == 'load')
    assert leaf['data']['effects'][0]['status'] == 'allowed'


def authorization(tmp_path, tool_rules, agent_rules, tools=None, confirm=True):
    identity = 'app.nexo::entry'
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1,
        'tools': {identity: {'permissions': tool_rules, 'confirm': confirm}},
        'agents': {'app.nexo::reader': {'permissions': agent_rules, 'tools': [identity] if tools is None else tools}}}))


@pytest.mark.parametrize('tool_allow,agent_allow,expected', [(True, True, 'allowed'), (False, True, 'denied'), (True, False, 'denied')])
def test_tool_and_agent_ceilings(commands, tmp_path, tool_allow, agent_allow, expected):
    manifest(tmp_path, [['allow', 'read', 'data/']])
    authorization(tmp_path, [['allow' if tool_allow else 'deny', 'read', 'data/']],
                  [['allow' if agent_allow else 'deny', 'read', 'data/']])
    code, doc = configured(commands, tmp_path, APP, '--authorization', 'auth.json', '--dependencies', 'deps.json')
    effect = summaries(doc)['entry']['effects'][0]
    assert effect['status'] == expected
    rows = {c['scope']: c for c in effect['checks']}
    assert rows['tool']['approval_required'] is True
    assert rows['tool']['status'] == ('allowed' if tool_allow else 'denied')
    assert rows['agent']['status'] == ('allowed' if agent_allow else 'denied')
    assert code == (0 if expected == 'allowed' else 1)


def test_missing_dispatch_allowlist_denies(commands, tmp_path):
    (tmp_path / 'helper.nexo').write_text(HELPER)
    authorization(tmp_path, [['allow', 'read', 'data/']], [['allow', 'read', 'data/']], tools=[])
    code, doc = configured(commands, tmp_path, APP, '--authorization', 'auth.json')
    assert code == 1
    assert any(c['scope'] == 'dispatch' and c['status'] == 'denied' for c in summaries(doc)['entry']['effects'][0]['checks'])


def test_configuration_environment_and_canonical_identity(commands, tmp_path, monkeypatch):
    manifest(tmp_path, [['deny', 'read', 'data/']])
    (tmp_path / 'alias.nexo').symlink_to(tmp_path / 'helper.nexo')
    (tmp_path / 'deps.json').write_text(json.dumps({'version': 1, 'modules': {'alias.nexo': [['deny', 'read', 'data/']]}}))
    monkeypatch.setenv('NEXO_SECURITY_DEPENDENCIES', str(tmp_path / 'deps.json'))
    code, doc = configured(commands, tmp_path, APP)
    assert code == 1
    assert summaries(doc)['entry']['effects'][0]['checks'][0]['identity'] == 'helper.nexo'


@pytest.mark.parametrize('flag,key', [('--dependencies', 'security_context_config'), ('--authorization', 'security_authorization')])
@pytest.mark.parametrize('contents', ['{}', '{"version":1,"version":1}', '[' * 1000, 'x' * 1048577], ids=['empty','duplicate','deep','oversized'])
def test_invalid_configurations_fail_closed(commands, tmp_path, flag, key, contents):
    (tmp_path / 'bad.json').write_text(contents)
    code, doc = configured(commands, tmp_path, 'print("not executed")\n', flag, 'bad.json')
    assert code == 1 and doc['diagnostics'][0]['code'] == key


def test_literal_and_callback_aliases(commands, tmp_path):
    code, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
path = "data/log"
reader = read
alias = reader
fn entry() -> str!:
    return alias(path)?
''')
    assert code == 0
    data = summaries(doc)['entry']
    assert data['complete'] and not data['pure']
    assert data['effects'][0]['target'] == 'data/log'
    code, doc = parity(commands, tmp_path, 'fn f():\n    callback = len\n    return callback([1])\n')
    assert summaries(doc)['f']['pure']


def test_imported_function_alias(commands, tmp_path):
    (tmp_path / 'helper.nexo').write_text(HELPER)
    code, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
use helper
callback = helper.load
fn outer() -> str!:
    return callback()?
''')
    assert code == 0
    assert summaries(doc)['outer']['effects'][0]['file'] == 'helper.nexo'


def test_record_methods_and_self_calls(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
type Reader:
    name: str
    fn load(self) -> str!:
        return read("data/log")?
    fn forward(self) -> str!:
        return self.load()?
r = Reader("logs")
fn outer() -> str!:
    return r.forward()?
''')
    ss = summaries(doc)
    assert ss['Reader.load']['effects'] == ss['Reader.forward']['effects'] == ss['outer']['effects']
    assert ss['outer']['effects'][0]['status'] == 'allowed'


def test_strict_unknowns_and_flag_validation(commands, tmp_path):
    for cmd in commands:
        code, doc = report(cmd, tmp_path, 'fn f(callback):\n    return callback()\n', ('--require-complete',))
        assert code == 1 and not doc['success']
        for extra in [('--dependencies','--json'), ('--dependencies', 'a', '--dependencies', 'b'), ('--authorization','--json')]:
            code, doc = report(cmd, tmp_path, '', extra)
            assert code == 2 and doc['diagnostics'][0]['code'] == 'check_usage'


def test_loaders_do_not_initialize_or_mutate_runtime(tmp_path, monkeypatch):
    from nexo import authorization as auth, security_context as ctx
    from nexo.effects import analyze
    (tmp_path / 'app.nexo').write_text('')
    authorization(tmp_path, [], [])
    state = object()
    monkeypatch.setattr(auth, 'STATE', state)
    rules = object()
    monkeypatch.setattr(ctx, 'RULES', rules)
    analyze('', str(tmp_path / 'app.nexo'), authorization=str(tmp_path / 'auth.json'))
    assert auth.STATE is state and ctx.RULES is rules


def test_tool_registration_retains_importer_ceilings(commands, tmp_path):
    (tmp_path / 'tools.nexo').write_text('@tool "Read"\npub fn load() -> str!:\n    return read("data/log")?\n')
    (tmp_path / 'factory.nexo').write_text('use tools\n')
    (tmp_path / 'deps.json').write_text(json.dumps({'version': 1, 'modules': {
        'factory.nexo': [['deny', 'read', 'data/']], 'tools.nexo': [['allow', 'read', 'data/']]}}))
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1, 'tools': {
        'tools.nexo::load': {'permissions': [['allow', 'read', 'data/']], 'confirm': True}}, 'agents': {
        'app.nexo::reader': {'permissions': [['allow', 'read', 'data/']], 'tools': ['tools.nexo::load']}}}))
    code, doc = configured(commands, tmp_path, 'policy:\n    allow read "data/"\nuse factory\nuse tools\nagent reader:\n    tools = [tools.load]\n    goal = "Read"\n',
        '--dependencies', 'deps.json', '--authorization', 'auth.json')
    assert code == 1
    tool = next(d['data'] for d in doc['diagnostics'] if d['file'] == 'tools.nexo' and d['data']['function'] == 'load')
    assert tool['approval_required']
    assert any(c['scope'] == 'dependency' and c['identity'] == 'factory.nexo' and c['status'] == 'denied'
               for c in tool['effects'][0]['checks'])


def test_nested_tool_registration_is_explicitly_unknown(commands, tmp_path):
    (tmp_path / 'auth.json').write_text(json.dumps({'version': 1,
        'tools': {'app.nexo::inner': {'permissions': [['allow', 'read', 'data/']], 'confirm': True}},
        'agents': {'app.nexo::reader': {'permissions': [['allow', 'read', 'data/']], 'tools': ['app.nexo::inner']}}}))
    _, doc = configured(commands, tmp_path, '''policy:
    allow read "data/"
fn outer():
    @tool "Read"
    fn inner() -> str!:
        return read("data/log")?
''', '--authorization', 'auth.json', '--require-complete')
    data = summaries(doc)['outer.inner']
    assert not data['complete'] and data['approval_required']
    assert any(e['reason'] == 'dynamic_registration_context' for e in data['effects'])


def test_pure_tool_still_requires_approval(commands, tmp_path):
    authorization(tmp_path, [], [])
    code, doc = configured(commands, tmp_path, '@tool "Value"\nfn entry() -> int:\n    return 1\nagent reader:\n    tools = [entry]\n    goal = "Value"\n', '--authorization', 'auth.json')
    assert code == 0
    assert summaries(doc)['entry']['pure'] and summaries(doc)['entry']['approval_required']


def test_immutable_path_alias_cannot_bypass_symlink_policy(commands, tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'private').mkdir()
    (tmp_path / 'private/log').write_text('not read by static analysis')
    (tmp_path / 'data/link').symlink_to(tmp_path / 'private', target_is_directory=True)
    code, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
path = "data/link/log"
fn entry() -> str!:
    return read(path)?
''')
    assert code == 1 and summaries(doc)['entry']['effects'][0]['status'] == 'denied'


def test_module_retention_limit_is_not_silently_pure(tmp_path, monkeypatch):
    from nexo import effects
    monkeypatch.setattr(effects, 'MAX_UNITS', 1)
    (tmp_path / 'helper.nexo').write_text(HELPER)
    (tmp_path / 'app.nexo').write_text('use helper\n')
    diagnostics = effects.analyze('use helper\n', str(tmp_path / 'app.nexo'))
    assert len(diagnostics) == 1
    assert not diagnostics[0]['data']['complete'] and not diagnostics[0]['data']['pure']
    assert diagnostics[0]['data']['effects'][0]['reason'] == 'analysis_limit'


@pytest.mark.parametrize('agent_source', ['', '@tool "Not an agent"\nfn reader() -> int:\n    return 1\n'])
def test_missing_or_wrong_role_registration_is_unknown(commands, tmp_path, agent_source):
    authorization(tmp_path, [['allow', 'read', 'data/']], [['allow', 'read', 'data/']])
    source = '''policy:
    allow read "data/"
@tool "Read"
fn entry() -> str!:
    return read("data/log")?
''' + agent_source
    code, doc = configured(commands, tmp_path, source, '--authorization', 'auth.json', '--require-complete')
    assert code == 1
    data = summaries(doc)['entry']
    assert not data['complete'] and not data['pure']
    assert any(e['reason'] == 'dynamic_registration_context' for e in data['effects'])


def test_effect_only_flags_never_invoke_intent(commands, tmp_path, monkeypatch):
    import os
    import subprocess
    from nexo import cli
    def forbidden(*args):
        raise AssertionError('invalid effect flags must never invoke a model')
    monkeypatch.setattr(cli, 'run_intent', forbidden)
    cases = [('--require-complete',), ('--dependencies', 'missing.json'), ('--authorization', 'missing.json')]
    for flags in cases:
        assert cli.main(['check', '--intent', *flags]) == 2
        for cmd in commands:
            p = subprocess.run([*cmd, 'check', '--intent', *flags], cwd=tmp_path, capture_output=True,
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}, timeout=20)
            assert p.returncode == 2
