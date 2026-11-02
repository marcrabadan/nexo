"""Local static-host qualification; no cloud/backend certification is implied."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import uuid

import pytest

from test_web_production import nginx_container, require_optimizer


def fetch(url, *, context=None):
    start = time.perf_counter()
    try:
        with urlopen(url, context=context, timeout=10) as response:
            return response.status, response.headers, response.read(), time.perf_counter() - start
    except HTTPError as error:
        return error.code, error.headers, error.read(), time.perf_counter() - start


def test_nginx_bounded_load_rate_limit_and_recovery(tmp_path):
    with nginx_container(tmp_path) as (base, docker, container, dist):
        # Warm runtime and Docker forwarding before measuring. Each worker must
        # consume the body; rejected requests count separately from successes.
        assert fetch(base)[0] == 200
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=16) as pool:
            samples = list(pool.map(lambda _: fetch(base), range(160)))
        elapsed = time.perf_counter() - started
        counts = {str(code): sum(row[0] == code for row in samples) for code in {row[0] for row in samples}}
        assert set(counts) <= {'200', '429'}, counts
        assert counts.get('200', 0) > 0 and counts.get('429', 0) > 0, counts
        expected = (dist / 'index.html').read_bytes()
        assert all(body == expected for status, _, body, _ in samples if status == 200)
        assert all(headers['cache-control'] == 'no-store' for status, headers, _, _ in samples if status == 429)
        time.sleep(2.1)  # Drain the configured burst of 40 at 20 requests/s.
        assert fetch(base)[0] == 200
        latencies = sorted(row[3] * 1000 for row in samples)
        report = {
            'schema_version': 1, 'scope': 'local static fixture; not a production capacity/SLO claim',
            'requests': len(samples), 'concurrency': 16, 'elapsed_seconds': elapsed,
            'statuses': counts, 'all_responses_per_second': len(samples) / elapsed,
            'latency_ms_all_responses': {'p50': latencies[math.ceil(len(latencies) * .5)-1],
                                        'p95': latencies[math.ceil(len(latencies) * .95)-1]},
            'index_sha256': hashlib.sha256(expected).hexdigest(),
            'rate_limit_recovered': True,
        }
        # Restart the same immutable image/config, then verify readiness and body.
        result = docker('restart', container)
        assert result.returncode == 0, result.stderr
        ports = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', container).stdout)
        base = 'http://127.0.0.1:' + ports['8080/tcp'][0]['HostPort'] + '/'
        for _ in range(100):
            try:
                if fetch(base + 'healthz')[0] == 200:
                    break
            except (OSError, URLError):
                pass
            time.sleep(.1)
        else:
            pytest.fail('Static host failed recovery after restart')
        assert fetch(base)[2] == expected
        report['restart_recovered'] = True
        output = os.environ.get('NEXO_WEB_LOAD_REPORT')
        if output:
            Path(output).write_text(json.dumps(report, indent=2) + '\n')


def test_nginx_verified_tls_edge_and_http_redirect(tmp_path):
    if not shutil.which('openssl'):
        pytest.fail('OpenSSL is required for local TLS qualification')
    # Private disposable certificate, trusted only by this test's verified client.
    # No certificate verification bypass and no change to system trust.
    tls = tmp_path / 'tls'
    tls.mkdir(mode=0o755)
    tls.chmod(0o755)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(tls/'key.pem'), '-out', str(tls/'cert.pem'), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, capture_output=True, timeout=30)
    for path in tls.iterdir():
        path.chmod(0o444)
    context = ssl.create_default_context(cafile=str(tls/'cert.pem'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with nginx_container(tmp_path) as (base, docker, container, dist):
        name = 'nexo-edge-' + uuid.uuid4().hex[:12]
        network = name + '-net'
        edge = None
        created = docker('network', 'create', network)
        assert created.returncode == 0, created.stderr
        try:
            connected = docker('network', 'connect', '--alias', 'static', network, container)
            assert connected.returncode == 0, connected.stderr
            config = tls/'nginx.conf'
            config.write_text('''worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr crit;
events { worker_connections 256; }
http {
    access_log off;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    server {
        listen 8081;
        return 308 https://localhost$request_uri;
    }
    server {
        listen 8443 ssl;
        ssl_certificate /tls/cert.pem;
        ssl_certificate_key /tls/key.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        add_header Strict-Transport-Security "max-age=31536000" always;
        location / { proxy_pass http://static:8080; }
    }
}
''')
            config.chmod(0o444)
            image = docker('inspect', '--format', '{{.Config.Image}}', container).stdout.strip()
            result = docker('run', '-d', '--name', name, '--network', network, '--read-only',
                            '--user', '101:101', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777',
                            '--mount', f'type=bind,src={tls},dst=/tls,readonly',
                            '--mount', f'type=bind,src={config},dst=/etc/nginx/nginx.conf,readonly',
                            '-p', '127.0.0.1::8443', '-p', '127.0.0.1::8081', image)
            assert result.returncode == 0, result.stderr
            edge = result.stdout.strip()
            ports = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Ports}}', edge).stdout)
            assert '8443/tcp' in ports, docker('logs', edge).stderr
            https = 'https://127.0.0.1:' + ports['8443/tcp'][0]['HostPort'] + '/'
            for _ in range(100):
                try:
                    response = fetch(https, context=context)
                    if response[0] == 200:
                        break
                except (OSError, URLError):
                    pass
                time.sleep(.1)
            else:
                pytest.fail('TLS edge failed readiness: ' + docker('logs', edge).stderr)
            assert response[2] == (dist/'index.html').read_bytes()
            assert response[1]['strict-transport-security'] == 'max-age=31536000'
            assert "frame-ancestors 'none'" in response[1]['content-security-policy']
            # Prove that success depends on the explicit test CA.
            with pytest.raises(URLError):
                fetch(https)
            for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
                context.minimum_version = context.maximum_version = version
                assert fetch(https+'healthz', context=context)[0] == 200
            # Inspect the redirect without following it onto standard port 443.
            import http.client
            client = http.client.HTTPConnection('127.0.0.1', int(ports['8081/tcp'][0]['HostPort']), timeout=5)
            try:
                client.request('GET', '/example?test=1')
                redirect = client.getresponse()
                assert redirect.status == 308
                assert redirect.getheader('Location') == 'https://localhost/example?test=1'
                redirect.read()
            finally:
                client.close()
        finally:
            if edge:
                docker('rm', '-f', edge)
            docker('network', 'disconnect', network, container)
            docker('network', 'rm', network)
