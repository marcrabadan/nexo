"""Static effect reports share the v1 interface and never confer authority."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_check_json import commands, ROOT, SCHEMA, validate

EFFECT_SCHEMA = json.loads((ROOT / "docs/effects-v2.schema.json").read_text())


def report(cmd, tmp_path, source, extra=()):
    (tmp_path / 'app.nexo').write_text(source)
    p = subprocess.run([*cmd, 'check', '--effects', '--json', *extra, 'app.nexo'],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60,
                       env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert p.stderr == '', p.stderr
    doc = json.loads(p.stdout)
    validate(doc, SCHEMA)
    for diagnostic in doc['diagnostics']:
        if diagnostic['code'] == 'effects_summary':
            data = diagnostic['data']
            validate(data, EFFECT_SCHEMA, EFFECT_SCHEMA)
            assert data['effects_version'] == 2 and data['policy_scope'] in ('root', 'configured')
            assert type(data['complete']) is bool and type(data['pure']) is bool
            assert not data['pure'] or data['complete'] and not data['effects']
    return p.returncode, doc


def parity(commands, tmp_path, source):
    results = [report(cmd, tmp_path, source) for cmd in commands]
    assert all(r == results[0] for r in results), results
    return results[0]


def summaries(doc):
    return {d['data']['function']: d['data'] for d in doc['diagnostics'] if d['code'] == 'effects_summary'}


def test_transitive_and_recursive_requirements(commands, tmp_path):
    code, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
fn first() -> str!:
    return second()?
fn second() -> str!:
    if false:
        return first()?
    return read("data/log.txt")?
print(first())
''')
    assert code == 0
    ss = summaries(doc)
    assert set(ss) == {'<module>', 'first', 'second'}
    for name in ('first', 'second'):
        assert ss[name]['complete'] and not ss[name]['pure']
        [e] = ss[name]['effects']
        assert e['capability'] == 'read' and e['target'] == 'data/log.txt' and e['status'] == 'allowed'


def test_deny_wins_and_missing_is_denied(commands, tmp_path):
    for rules in ('allow read "data/"\n    deny read "data/private"',
                  'deny read "data/private"\n    allow read "data/"', 'deny shell'):
        code, doc = parity(commands, tmp_path, 'policy:\n    ' + rules + '''
fn load() -> str!:
    return read("data/private/token")?
''')
        assert code == 1 and not doc['success']
        assert summaries(doc)['load']['effects'][0]['status'] == 'denied'
    assert not (tmp_path / 'data').exists()  # static reporting never reads the target


def test_dynamic_target_and_callback_are_unknown(commands, tmp_path):
    code, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
fn load(path: str) -> str!:
    return read(path)?
fn invoke(callback):
    return callback()
''')
    assert code == 0  # advisory unknowns, never authorization
    ss = summaries(doc)
    assert not ss['load']['complete'] and not ss['load']['pure']
    assert ss['load']['effects'][0]['reason'] == 'dynamic_target'
    assert ss['invoke']['effects'][0]['reason'] == 'dynamic_call'


def test_shadowing_does_not_create_a_builtin_grant(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''fn forward(read) -> str!:
    return read("data/log")?
''')
    facts = summaries(doc)['forward']['effects']
    assert all(e['capability'] != 'read' for e in facts)
    assert any(e['reason'] == 'dynamic_call' for e in facts)


def test_imported_functions_and_initializers(commands, tmp_path):
    (tmp_path / 'helper.nexo').write_text('''pub fn load() -> str!:
    return read("data/log")?
print("module initialized only at runtime")
''')
    _, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
use helper as logs
fn outer() -> str!:
    return logs.load()?
print(outer())
''')
    ds = [d for d in doc['diagnostics'] if d['data']['function'] == 'outer']
    [effect] = ds[0]['data']['effects']
    assert effect['file'] == 'helper.nexo' and effect['status'] == 'allowed'
    assert effect['range']['start']['line'] == 1
    assert any(d['file'] == 'helper.nexo' and d['data']['function'] == '<module>' for d in doc['diagnostics'])


def test_network_environment_database_and_process(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''use nexo.db
policy:
    allow net "api.example.com"
    allow env "TOKEN"
    allow read "state.db"
    allow write "state.db"
    deny shell
fn fetch() -> str!:
    return http.get("https://api.example.com/logs?redacted=1")?.text
fn database() -> int!:
    connect("state.db")?
    return 0
fn credentials():
    return secret("TOKEN")
fn process() -> int!:
    run("private command arguments")?
    return 0
''')
    ss = summaries(doc)
    assert any(e['capability'] == 'net' and e['target'] == 'api.example.com' for e in ss['fetch']['effects'])
    assert {e['capability'] for e in ss['database']['effects']} == {'read', 'write'}
    assert ss['credentials']['effects'][0]['capability'] == 'env'
    assert ss['process']['effects'][0]['status'] == 'denied'
    assert 'private command arguments' not in json.dumps(doc)
    assert '?redacted' not in json.dumps(doc)


def test_unknown_ai_native_aliases_and_unmodeled_api(commands, tmp_path):
    for source in [
        'fn ai() -> str!:\n    return ask("hello")?\n',
        'fn g():\n    return now()\n',
        'use python math\n',
    ]:
        _, doc = parity(commands, tmp_path, source)
        assert any(not d['data']['complete'] for d in doc['diagnostics'] if d['code'] == 'effects_summary'), doc


def test_pure_functions_and_defaults(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''fn add(a: int, b: int = 2) -> int:
    return a + b
''')
    assert summaries(doc)['add']['pure']
    _, doc = parity(commands, tmp_path, '''policy:
    allow env "TOKEN"
fn value(key = secret("TOKEN")):
    return key
''')
    assert summaries(doc)['value']['effects'][0]['capability'] == 'env'


def test_no_model_or_target_execution(commands, tmp_path):
    source = '''policy:
    allow write "marker"
fn change() -> int!:
    write("marker", "must not execute")?
    return 0
change()
'''
    _, doc = parity(commands, tmp_path, source)
    assert "change" in summaries(doc)
    assert not (tmp_path / 'marker').exists()


def test_dynamic_and_credential_urls_are_not_assumed_allowed(commands, tmp_path):
    for url in ('https://user:password@api.example.com/x', 'https://[::1]/', 'https://api.example.com:99999/'):
        _, doc = parity(commands, tmp_path, 'fn f() -> str!:\n    return http.get(' + json.dumps(url) + ')?.text\n')
        assert any(e['status'] == 'unknown' for e in summaries(doc)['f']['effects'])
        assert 'password' not in json.dumps(doc)


def test_budget_is_explicit_unknown(monkeypatch):
    import nexo.effects as effects
    monkeypatch.setattr(effects, 'MAX_FACTS', 1)
    ds = effects.analyze('fn f() -> str!:\n    return read("a")?\nfn g() -> str!:\n    return f()?\n', 'memory.nexo')
    assert all(not d['data']['complete'] and not d['data']['pure'] for d in ds)
    assert all(d['data']['effects'][0]['reason'] == 'analysis_limit' for d in ds)


def test_runtime_denial_still_prevents_writes(tmp_path):
    source = '''policy:
    allow write "."
    deny write "marker"
fn change() -> int!:
    write("marker", "blocked")?
    return 0
change()
'''
    cmd = [sys.executable, '-m', 'nexo.cli']
    assert report(cmd, tmp_path, source)[0] == 1
    p = subprocess.run([*cmd, 'security', 'run', 'app.nexo'], cwd=tmp_path, capture_output=True,
                       env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert p.returncode == 1 and not (tmp_path / 'marker').exists()


def test_nested_function_propagation(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
fn outer() -> str!:
    fn inner() -> str!:
        return read("data/log")?
    return inner()?
''')
    ss = summaries(doc)
    assert ss['outer']['effects'] == ss['outer.inner']['effects']
    assert ss['outer']['effects'][0]['status'] == 'allowed'


def test_deferred_iteration_and_indexing_are_unknown(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''fn indexed(xs):
    return xs[0]
fn iteration(xs):
    for item in xs:
        print(item)
''')
    assert not summaries(doc)['indexed']['complete']
    assert not summaries(doc)['iteration']['complete']


def test_runtime_dependency_ceiling_still_denies(tmp_path):
    (tmp_path / 'helper.nexo').write_text('''pub fn load() -> str!:
    return read("data/log")?
''')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/log').write_text('must-not-be-disclosed')
    (tmp_path / 'ceilings.json').write_text(json.dumps({
        'version': 1, 'modules': {'helper.nexo': [['deny', 'read', 'data/']]}}))
    source = '''policy:
    allow read "data/"
use helper
try:
    print(helper.load())
else e:
    print("not a catchable denial")
'''
    cmd = [sys.executable, '-m', 'nexo.cli']
    code, doc = report(cmd, tmp_path, source)
    assert code == 0
    assert all(d['data']['policy_scope'] == 'root' for d in doc['diagnostics'])
    p = subprocess.run([*cmd, 'security', 'run', '--dependencies', 'ceilings.json', 'app.nexo'],
                       cwd=tmp_path, capture_output=True,
                       env={**os.environ, 'PYTHONPATH': str(ROOT)})
    assert p.returncode == 1
    assert b'must-not-be-disclosed' not in p.stdout
    assert b'not a catchable denial' not in p.stdout


def test_runtime_context_intersections_are_unchanged(monkeypatch):
    from nexo import security_context as ctx
    monkeypatch.setattr(ctx, 'RULES', {'lib.nexo': [('allow', 'read', ['data/'])]})
    monkeypatch.setattr(ctx, 'ROOT', 'app.nexo')
    token = ctx.STACK.set(([('deny', 'read', ['data/private'])],))
    try:
        assert not ctx.allows('read', 'data/public', 'lib.nexo')  # caller lacks allow
        assert not ctx.allows('read', 'data/private/log', 'lib.nexo')
        ctx.STACK.set(([('allow', 'read', ['data/']), ('deny', 'read', ['data/private'])],))
        assert ctx.allows('read', 'data/public', 'lib.nexo')
        assert not ctx.allows('read', 'data/private/log', 'lib.nexo')
        assert not ctx.allows('write', 'data/public', 'lib.nexo')
    finally:
        ctx.STACK.reset(token)


def test_effect_options_and_human_output(commands, tmp_path):
    source = 'fn add(a: int, b: int) -> int:\n    return a + b\n'
    for cmd in commands:
        p = report(cmd, tmp_path, source, ('--effects',))
        assert p[0] == 2 and p[1]['diagnostics'][0]['code'] == 'check_usage'
        p = report(cmd, tmp_path, source, ('--intent',))
        assert p[0] == 2
        human = subprocess.run([*cmd, 'check', '--effects', 'app.nexo'], cwd=tmp_path,
                               capture_output=True, text=True,
                               env={**os.environ, 'PYTHONPATH': str(ROOT)})
        assert human.returncode == 0 and human.stderr == ''
        assert 'effects_summary: add' in human.stdout and not human.stdout.startswith('{')


def test_conditional_definitions_do_not_hide_outer_effects(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''policy:
    allow read "data/"
fn action() -> str!:
    return read("data/log")?
fn wrapper() -> str!:
    if false:
        fn action() -> str!:
            return "local"
    return action()?
''')
    assert not summaries(doc)['wrapper']['complete']
    assert not summaries(doc)['wrapper']['pure']


def test_user_function_shadows_builtin_conservatively(commands, tmp_path):
    _, doc = parity(commands, tmp_path, '''fn read(path: str) -> str!:
    return path
fn wrapper() -> str!:
    return read("data/log")?
''')
    assert not summaries(doc)['wrapper']['complete']
    assert not summaries(doc)['wrapper']['pure']


def test_catalog_covers_api_builtin_names():
    import re
    from nexo.effects import CATALOG
    api = json.loads((ROOT / 'nexo/stdlib/api.json').read_text())
    names = {m[1] for section in api['sections'] for entry in section['entries']
             if (m := re.match(r'([A-Za-z_]\w*)', entry[0]))}
    assert names <= set(CATALOG['builtin_roots'])
    assert CATALOG['version'] == 1
    for specs in CATALOG['calls'].values():
        for spec in specs:
            assert spec['capability'] in (None, 'read', 'write', 'net', 'env', 'shell', 'c', 'python')
            assert spec['argument'] is None or type(spec['argument']) is int and spec['argument'] >= 0


def test_invalid_import_is_a_diagnostic(commands, tmp_path):
    (tmp_path / 'helper.nexo').write_bytes(b'\xff')
    _, doc = parity(commands, tmp_path, 'use helper\n')
    assert doc['diagnostics'][0]['code'] == 'module_not_found'


def test_oversized_url_port_is_unknown(commands, tmp_path):
    source = 'fn f() -> str!:\n    return http.get("https://api.example.com:' + '9' * 5000 + '/")?.text\n'
    _, doc = parity(commands, tmp_path, source)
    assert not summaries(doc)['f']['complete']


def test_builtin_calls_do_not_hide_lazy_value_effects(commands, tmp_path):
    for source in ('fn measure(xs):\n    return len(xs)\n',
                   'fn display(xs):\n    return "value: {xs}"\n'):
        _, doc = parity(commands, tmp_path, source)
        name = 'measure' if 'measure' in source else 'display'
        assert not summaries(doc)[name]['complete'] and not summaries(doc)[name]['pure']
        assert any(e['reason'] == 'dynamic_value' for e in summaries(doc)[name]['effects'])
