import pytest

from nexo.errors import NexoError
from nexo.stdlib import web


def test_listener_guard_runs_before_socket_creation(monkeypatch):
    monkeypatch.setenv("NEXO_SECURITY_WEB", "memory-v1")
    def forbidden(*args, **kwargs):
        pytest.fail("memory HTTP must never create a socket")
    monkeypatch.setattr(web.socket, "socket", forbidden)
    with pytest.raises(NexoError) as error:
        web.listen(web.Server("unit", 0, None), 0)
    assert error.value.key == "security_web_mode"


def test_memory_lifecycle_never_opens_listener_or_starts_thread(monkeypatch):
    monkeypatch.setenv("NEXO_SECURITY_WEB", "memory-v1")
    monkeypatch.setattr(web, "SERVERS", [web.Server("unit", 0, None)])
    def forbidden(*args, **kwargs):
        pytest.fail("memory HTTP must not start a listener lifecycle")
    monkeypatch.setattr(web, "listen", forbidden)
    monkeypatch.setattr(web.threading, "Thread", forbidden)
    assert web.serve_forever(None) is True
    web.serve_in_background(None)


def test_unconfigured_factory_fails_before_mutating_registry(monkeypatch):
    monkeypatch.setenv("NEXO_SECURITY_ENFORCE", "1")
    monkeypatch.delenv("NEXO_SECURITY_WEB", raising=False)
    monkeypatch.setattr(web, "SERVERS", [])
    class Call:
        span = None
    with pytest.raises(NexoError) as error:
        web.b_web_server(Call(), "unit", 0)
    assert error.value.key == "security_web_mode"
    assert not web.SERVERS
