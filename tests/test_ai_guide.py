"""Static AI guide provenance, composition and site contracts (no network)."""
import importlib.util
import json
import re
import pytest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ai_guide_build', ROOT/'docs/ai-guide/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def generated():
    return build.generate()


def test_generated_context_is_fresh_and_deterministic():
    outputs = generated()
    assert outputs == generated()
    for path, expected in outputs.items():
        assert (ROOT/path).read_text() == expected, path


def test_evidence_hashes_and_statuses():
    catalog, sources = build.inputs()
    assert {c['status'] for c in catalog['claims']} == {'implemented', 'experimental', 'planned', 'unverified'}
    assert sources['nexo/stdlib/api.json'] == build.digest((ROOT/'nexo/stdlib/api.json').read_bytes())
    changed = dict(sources)
    changed['docs/ai-guide/source/engineering.md'] = 'changed'
    assert build.digest(build.serialized(sources).encode()) != build.digest(build.serialized(changed).encode())
    # No HEAD, timestamps, or unrelated files enter this revision.
    assert '.git/HEAD' not in sources
    assert 'README.md' not in sources


def test_prompt_composition_and_measurement_integrity():
    outputs = generated()
    data = json.loads(next(v for k,v in outputs.items() if k.endswith('/prompts.json')))
    prompts = data['prompts']
    assert len(prompts) == 48
    metrics = json.loads(outputs['docs/ai-guide/token-counts.json'])
    assert metrics['tokenizer'] == 'tiktoken==0.12.0'
    for profile in build.PROFILES:
        for mode in ('compact', 'full'):
            offline, url = (prompts[f'{profile}-{mode}-{access}'] for access in ('offline', 'url'))
            assert offline['tokens'] > url['tokens']
            for prompt in (offline,url):
                assert data['revision'] in prompt['text']
                assert build.PROFILES[profile][0] in prompt['text']
                assert '[describe the application or change]' in prompt['text']
                key = f'{profile}-{mode}-'+('offline' if prompt is offline else 'url')
                assert metrics['payload_sha256'][key] == build.digest(prompt['text'].encode())
            assert 'Browsing is not required' in offline['text']
            assert 'fn welcome' in offline['text']
            assert '/versions/v1-' in url['text']
            assert 'If retrieval fails' in url['text']
        assert prompts[f'{profile}-full-offline']['tokens'] > prompts[f'{profile}-compact-offline']['tokens']


def test_unsupported_features_are_not_promoted():
    compact = generated()['docs/llms.txt']
    for phrase in ('PLANNED [interop-roadmap]', 'PLANNED [registry]', 'EXPERIMENTAL [web-target]', 'UNVERIFIED [operations]', 'Linux AMD64 only', 'no exactly-once', 'not a production certification', 'Kernel isolation requires', 'not invoice guarantees'):
        assert phrase in compact
    syntax = generated()['docs/nexo-syntax.txt']
    assert "interpreter's `parallel` is sequential" in syntax
    assert 'Do not fabricate a `nexo.agent`' in syntax


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.ids=set()
    def handle_starttag(self, tag, attrs):
        values=dict(attrs)
        if 'id' in values: self.ids.add(values['id'])
        if tag in ('a','link','script','img'):
            value=values.get('href',values.get('src'))
            if value: self.links.append(value)


def test_generated_local_and_versioned_links_resolve():
    outputs=generated()
    page=Links();page.feed(outputs['docs/ai-guide.html'])
    urls=list(page.links)
    for path,content in outputs.items():
        if path.endswith('.txt'):
            urls.extend(re.findall(r'\]\((https://marcrabadan.github.io/nexo/[^)]+)\)',content))
    for url in urls:
        parsed=urlparse(url)
        if parsed.netloc and parsed.netloc != 'marcrabadan.github.io': continue
        path=unquote(parsed.path)
        if parsed.netloc: path=path.removeprefix('/nexo/')
        target=ROOT/'docs'/path if path else ROOT/'docs/ai-guide.html'
        assert target.is_file(),url
        if parsed.fragment and target.suffix=='.html':
            linked=Links();linked.feed(target.read_text())
            assert parsed.fragment in linked.ids,url


def test_navigation_and_default_accessibility():
    for path in [* (ROOT/'docs').glob('*.html'),ROOT/'docs/libraries/page.tmpl',ROOT/'docs/reference/page.tmpl']:
        text=path.read_text()
        if 'aria-label="Main"' in text:
            main_nav=text[text.index('aria-label="Main"'):text.index('</nav>',text.index('aria-label="Main"'))]
            assert 'href="ai-guide.html"' in main_nav,path
    text=(ROOT/'docs/ai-guide.html').read_text()
    assert 'name="mode" value="compact" checked' in text
    assert 'name="access" value="offline" checked' in text
    assert 'aria-live="polite"' in text
    assert '<noscript>' in text
    assert 'id="copy-prompt"' in text
    assert 'class="copy"' not in text[text.index('<main'):text.index('</main>')]


def test_source_change_rejects_stale_measurements(monkeypatch):
    catalog, sources = build.inputs()
    sources['nexo/cli.py'] = 'changed'
    monkeypatch.setattr(build, 'inputs', lambda: (catalog, sources))
    with pytest.raises(ValueError, match='Stale token measurements'):
        build.generate()


def test_template_navigation_matches_published_pages():
    for name in ('libraries', 'reference'):
        template = (ROOT/f'docs/{name}/page.tmpl').read_text()
        page = (ROOT/f'docs/{name}.html').read_text()
        assert template[template.index('<header class="site-header">'):template.index('</header>')] == page[page.index('<header class="site-header">'):page.index('</header>')]


def test_shield_shared_assessment_and_progressive_disclosure():
    outputs = generated()
    data = json.loads(next(v for k,v in outputs.items() if k.endswith('/prompts.json')))
    brief = (ROOT/'docs/ai-guide/source/shield-brief.md').read_text()
    detail = next(v for k,v in outputs.items() if k.endswith('/shield-security.txt'))
    for key, prompt in data['prompts'].items():
        assert prompt['text'].count(brief) == 1, key
        assert 'preferred security foundation' in prompt['text']
        assert 'never silently weaken required security' in prompt['text']
        assert '## Configure the smallest supported boundary' not in prompt['text']
        if '-compact-' in key:
            assert prompt['tokens'] < (2000 if '-offline' in key else 650)
    for text in (outputs['docs/llms.txt'], outputs['docs/llms-full.txt']):
        assert '/shield-security.txt)' in text
        assert 'when relevant' in text
        # Detailed guidance and underlying documents are separate snapshots.
        assert '## Configure the smallest supported boundary' not in text
        assert '# Shield online profile' not in text
    catalog, sources = build.inputs()
    for name in ('shield','shield-isolation','shield-online','shield-dispatch','shield-docker'):
        path = f'docs/{name}.md'
        assert path in sources
        assert any(d['path']==path and d.get('when')=='security' for d in catalog['documents'])
        assert any(path in v for k,v in outputs.items() if '/sources/' in k)
    for item in ('recommendation', 'Required filesystem/network/environment', 'Selected execution profile', 'Unsupported operations', 'Isolation limitations', 'Security validation commands', 'Remaining risks'):
        assert item in detail
    assert '@@SHIELD_DOCS@@' not in detail


def test_shield_profile_limits_and_documented_commands():
    detail = next(v for k,v in generated().items() if k.endswith('/shield-security.txt'))
    for command in ('check app.nexo', 'run app.nexo', 'trace app.nexo', 'audit app.nexo', 'probe'):
        assert f'nexo security {command}' in detail
    for phrase in ('not OS isolation', '--sandbox=linux', '--sandbox=linux-online', '--network=none', 'no weaker fallback', 'human-confirmation calls fail closed', 'per process, not aggregate', 'Shell/C/Python integrations remain rejected', 'capabilities, `allowPrivilegeEscalation:', 'nested-agent grants can only reduce authority'):
        assert phrase.lower() in detail.lower()

    assert 'not implemented merely because' in detail
    assert 'not all transitive native calls or dependency CVEs' in ' '.join(detail.split())


def test_shield_minimal_example_and_denial(tmp_path):
    import os
    import subprocess
    import sys
    text = (ROOT/'docs/ai-guide/source/shield-security.md').read_text()
    code = re.search(r'```nexo\n(.*?)```', text, re.S).group(1)
    (tmp_path/'public').mkdir()
    (tmp_path/'public/report.txt').write_text('public sample\n')
    app = tmp_path/'app.nexo'
    app.write_text(code)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    def run(*args):
        return subprocess.run([sys.executable,'-m','nexo.cli','security',*args,'app.nexo'], cwd=tmp_path, env=env, capture_output=True,text=True,timeout=15)
    for command in ('check','audit','run','trace'):
        result = run(command)
        assert result.returncode == 0, result.stdout+result.stderr
    assert 'public sample' in run('run').stdout
    # Preserve the grant and attempt a write outside it: denial must precede effect.
    app.write_text(code+'\nwrite("escaped.txt", "denied")\n')
    denied = run('run')
    assert denied.returncode != 0
    assert not (tmp_path/'escaped.txt').exists()
