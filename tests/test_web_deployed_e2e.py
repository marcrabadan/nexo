"""Validation of deployed E2E target configuration and contract shape."""
import importlib.util
from pathlib import Path
import argparse
import pytest
from test_web_frontend import browser

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('deployed_web',ROOT/'tests/e2e/web/run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


@pytest.mark.parametrize('value',['file:///tmp/app','https://u:p@example.test','https://example.test/path','https://example.test/?token=secret','https://example.test/#x','https:///'])
def test_target_rejects_non_origin_urls_and_credentials(value):
    with pytest.raises((argparse.ArgumentTypeError,ValueError)):runner.base_url(value)


@pytest.mark.parametrize('value,expected',[('https://example.test','https://example.test/'),('http://127.0.0.1:8080/','http://127.0.0.1:8080/')])
def test_target_preserves_valid_origin(value,expected):
    assert runner.base_url(value)==expected



def test_real_browser_failed_deployment_retains_diagnostics(tmp_path,browser):
    import functools,json,shutil,threading,subprocess,sys
    from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
    (tmp_path/'index.html').write_text('<html><body><h1>Wrong deployment</h1></body></html>')
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    args=['--base-url',f'http://127.0.0.1:{server.server_port}','--browser',browser.browser_type.name,
          '--timeout-ms','1000','--results',str(tmp_path/'results'),'--artifact-grace-seconds','0']
    if browser.browser_type.name=='chromium':args+=['--executable-path',shutil.which('chromium')]
    try:
        result=subprocess.run([sys.executable,str(ROOT/'tests/e2e/web/run.py'),*args],capture_output=True,text=True)
        assert result.returncode==1, result.stdout+result.stderr
        result=json.loads((tmp_path/'results/report.json').read_text())
        assert result['status']=='failed' and len(result['tests'])==5 and result['tls_verification']
        assert len(list((tmp_path/'results').glob('*.png')))==5
        assert len(list((tmp_path/'results').glob('*.zip')))==5
        assert (tmp_path/'results/junit.xml').exists()
    finally:server.shutdown();server.server_close();thread.join()
