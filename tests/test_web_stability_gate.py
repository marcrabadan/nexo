"""The release evidence gate must reject skips and malformed/empty reports."""
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

spec=importlib.util.spec_from_file_location('web_stability',Path(__file__).resolve().parents[1]/'scripts/web/stability.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


def test_junit_counts_nested_suites_and_nonpassing_cases(tmp_path):
    path=tmp_path/'junit.xml'
    path.write_text('''<testsuites><testsuite><testcase/><testcase><skipped/></testcase>
      <testsuite><testcase><failure/></testcase><testcase><error/></testcase></testsuite>
    </testsuite></testsuites>''')
    assert gate.results(path)==dict(tests=4,passed=1,failed=1,errors=1,skipped=1)


def test_junit_empty_and_invalid_reports_are_not_evidence(tmp_path):
    path=tmp_path/'junit.xml';path.write_text('<testsuites/>')
    assert gate.results(path)['tests']==0
    path.write_text('interrupted write')
    with pytest.raises(ET.ParseError):gate.results(path)


@pytest.mark.parametrize('returncode,counts,error',[
    (0,dict(tests=1,passed=0,skipped=1),None),
    (0,dict(tests=0,passed=0),None),
    (1,dict(tests=1,passed=1),None),
    (0,dict(tests=1,passed=1),'ParseError'),
])
def test_gate_blocks_skips_empty_process_failure_and_missing_evidence(returncode,counts,error):
    assert not gate.accepted(returncode,counts,error)


def test_gate_accepts_only_nonempty_complete_success():
    assert gate.accepted(0,dict(tests=2,passed=2))


def test_run_clears_stale_green_evidence_and_inherited_selection(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    (tmp_path/'report.json').write_text('{"status":"passed","stale":true}')
    (tmp_path/'junit.xml').write_text('<testsuites><testcase/></testsuites>')
    monkeypatch.setenv('PYTEST_ADDOPTS','-k nothing --ignore=tests')
    monkeypatch.setattr(gate,'fingerprint',lambda:{'source':'unchanged'})
    monkeypatch.setattr(gate,'version',lambda name:'test-version')
    def run(command,**options):
        assert options['env']['PYTEST_ADDOPTS']==''
        assert not (tmp_path/'report.json').exists() and not (tmp_path/'junit.xml').exists()
        return SimpleNamespace(returncode=2)
    monkeypatch.setattr(gate.subprocess,'run',run)
    assert gate.main(['--browser','chromium','--output',str(tmp_path)])==1
    result=json.loads((tmp_path/'report.json').read_text())
    assert result['status']=='blocked' and result['counts']['tests']==0 and 'stale' not in result


def test_run_blocks_source_changes_even_if_all_tests_pass(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    fingerprints=iter([{'source':'before'},{'source':'after'}])
    monkeypatch.setattr(gate,'fingerprint',lambda:next(fingerprints))
    monkeypatch.setattr(gate,'version',lambda name:'test-version')
    def run(command,**options):
        (tmp_path/'junit.xml').write_text('<testsuites><testsuite><testcase/></testsuite></testsuites>')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(gate.subprocess,'run',run)
    assert gate.main(['--browser','webkit','--output',str(tmp_path)])==1
    result=json.loads((tmp_path/'report.json').read_text())
    assert result['status']=='blocked' and not result['source_unchanged'] and result['counts']['passed']==1
