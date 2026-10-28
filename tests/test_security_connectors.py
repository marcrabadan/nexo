"""New main connectors must fail closed before transport or credential access."""
import json
import socket
import sys
import pytest
from phases import commands, run


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'mysql', 'mongodb', 'elasticsearch'])
@pytest.mark.parametrize('options', [[], ['--web=memory'], ['--streams=memory']])
def test_remote_databases_never_connect_in_shield(tmp_path, backend, scheme, options):
    with socket.socket() as peer:
        peer.bind(('127.0.0.1', 0))
        peer.listen()
        peer.settimeout(0.05)
        port = peer.getsockname()[1]
        (tmp_path / 'main.nexo').write_text(f'''policy:
    allow net "127.0.0.1"
use nexo.db
try:
    db = connect("{scheme}://reader@127.0.0.1:{port}/data", key: secret("CONNECTOR_TEST_KEY"))
else e:
    print("CAUGHT")
print("AFTER")
''')
        result = run(backend, ['security', 'trace', *options, 'main.nexo'], tmp_path, env={'CONNECTOR_TEST_KEY':'synthetic-fixture'})
        assert result.returncode == 1, result.stdout
        assert b'This connector is not available in Shield' in result.stdout, result.stdout
        assert b'CAUGHT' not in result.stdout and b'AFTER' not in result.stdout
        assert b'environment.read' not in result.stdout
        with pytest.raises(socket.timeout):
            peer.accept()


@pytest.mark.parametrize('source', [
    'kafka("events", servers: "127.0.0.1:1")',
    'rabbit("events", url: "amqp://127.0.0.1:1/")',
    'sqs("events", region: "eu-west-1")',
    'kinesis("events", region: "eu-west-1")',
    'servicebus("events", namespace: "example")',
    'eventhub("events", namespace: "example")',
    'pubsub("events", project: "example")',
])
def test_remote_stream_options_cannot_silently_use_memory(tmp_path, backend, source):
    (tmp_path / 'main.nexo').write_text('policy:\n    allow net "127.0.0.1"\nuse nexo.stream\n' + source + '\nprint("AFTER")\n')
    result = run(backend, ['security', 'run', '--streams=memory', 'main.nexo'], tmp_path)
    assert result.returncode == 1, result.stdout
    assert b'This connector is not available in Shield' in result.stdout, result.stdout
    assert not (tmp_path / '.nexo').exists()


def test_mcp_is_rejected_before_starting_a_process(tmp_path, backend):
    (tmp_path / 'escape.py').write_text('from pathlib import Path\nPath("STARTED").write_text("bad")\n')
    (tmp_path / 'nexo.toml').write_text('[mcp.escape]\ncommand = ' + json.dumps([sys.executable, 'escape.py']) + '\n')
    (tmp_path / 'main.nexo').write_text('policy:\n    allow shell\nuse nexo.ai\nmcp("escape")\n')
    result = run(backend, ['security', 'run', 'main.nexo'], tmp_path)
    assert result.returncode == 1, result.stdout
    assert b'This connector is not available in Shield' in result.stdout, result.stdout
    assert not (tmp_path / 'STARTED').exists()


@pytest.mark.parametrize('provider', ['azure','bedrock'])
def test_cloud_ai_rejected_before_ambient_credentials(tmp_path, backend, provider):
    (tmp_path / 'main.nexo').write_text(f'''policy:
    allow net "127.0.0.1"
use nexo.ai
ai:
    provider = "{provider}"
    model = "test"
    url = "http://127.0.0.1:1"
print(ask("hello"))
''')
    result = run(backend, ['security', 'trace', 'main.nexo'], tmp_path)
    assert result.returncode == 1, result.stdout
    assert b'This connector is not available in Shield' in result.stdout, result.stdout
    assert b'environment.read' not in result.stdout


from test_security_dispatch import model, configuration, program, execute


def test_shield_ai_does_not_discover_user_config_or_write_telemetry(tmp_path, backend, model):
    home = tmp_path / 'user-config'
    folder = home / 'nexo'
    folder.mkdir(parents=True)
    (folder / 'ai.toml').write_text('this is not valid configuration\n')
    usage = folder / 'usage.csv'
    usage.write_text('SYNTHETIC EXISTING USAGE\n')
    result = execute(tmp_path, backend, program(model[0]), configuration(tmp_path), env={'XDG_CONFIG_HOME':str(home)})
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == b'done'
    assert usage.read_text() == 'SYNTHETIC EXISTING USAGE\n'


def test_memory_stream_key_is_included_in_payload_budget(tmp_path, backend):
    (tmp_path / 'key.txt').write_text('x' * 1048576)
    (tmp_path / 'main.nexo').write_text('''policy:
    allow read "key.txt"
use nexo.stream
s = kafka("events")
fn publish():
    key = read("key.txt")?
    s.send("x", key: key)
publish()
''')
    result = run(backend, ['security', 'run', '--streams=memory', 'main.nexo'], tmp_path)
    assert result.returncode == 1, result.stdout
    assert b'budget' in result.stdout.lower(), result.stdout[-500:]
    assert not (tmp_path / '.nexo').exists()
