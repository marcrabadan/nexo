"""Phase 2: dynamic dependency ceilings and kernel-enforced offline workers."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from phases import commands, run, compiler


@pytest.fixture(params=commands(), ids=lambda p: p[0])
def backend(request):
    return request.param[1]


def app(tmp_path, backend, body, modules, grants, command="run"):
    for name, source in modules.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    (tmp_path / "public.txt").write_text("PUBLIC")
    (tmp_path / "private.txt").write_text("PRIVATE")
    (tmp_path / "app.nexo").write_text('policy:\n    allow read "."\n' + body)
    (tmp_path / "permissions.json").write_text(json.dumps({"version": 1, "modules": grants}))
    return run(backend, ["security", command, "--dependencies", "permissions.json", "app.nexo"], tmp_path)


def test_dependency_can_read_its_grant_without_restricting_root(tmp_path, backend):
    proc = app(tmp_path, backend, 'use helper\nprint(helper.grab())\nprint(read("private.txt"))\n',
               {"helper.nexo": 'pub fn grab() -> str!:\n    return read("public.txt")?\n'},
               {"helper.nexo": [["allow", "read", "public.txt"]]})
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode() == "PUBLIC\nPRIVATE\n"


def test_unlisted_dependency_defaults_to_no_capabilities(tmp_path, backend):
    proc = app(tmp_path, backend, 'use helper\nprint(helper.grab())\n',
               {"helper.nexo": 'pub fn grab() -> str!:\n    return read("private.txt")?\n'}, {})
    assert proc.returncode == 1
    assert "\nPRIVATE\n" not in proc.stdout.decode()
    assert "active dependency context" in proc.stdout.decode()


def test_importing_a_more_privileged_dependency_does_not_escalate(tmp_path, backend):
    proc = app(tmp_path, backend, 'use first\nprint(first.grab())\n', {
        "first.nexo": 'use second\npub fn grab() -> str!:\n    return second.grab()?\n',
        "second.nexo": 'pub fn grab() -> str!:\n    return read("private.txt")?\n',
    }, {"first.nexo": [["allow", "read", "public.txt"]], "second.nexo": [["allow", "read", "private.txt"]]})
    assert proc.returncode == 1, proc.stdout.decode()
    assert "\nPRIVATE\n" not in proc.stdout.decode()
    assert "active dependency context" in proc.stdout.decode()


def test_root_callback_cannot_launder_dependency_permissions(tmp_path, backend):
    proc = app(tmp_path, backend,
               'use helper\nfn grab() -> str!:\n    return read("private.txt")?\nprint(helper.invoke(grab))\n',
               {"helper.nexo": 'pub fn invoke(callback: any) -> str!:\n    return callback()?\n'},
               {"helper.nexo": [["allow", "read", "public.txt"]]})
    assert proc.returncode == 1, proc.stdout.decode()
    assert "active dependency context" in proc.stdout.decode()
    assert "\nPRIVATE\n" not in proc.stdout.decode()


def test_context_is_restored_after_caught_failure(tmp_path, backend):
    proc = app(tmp_path, backend, 'use helper\ntry:\n    print(helper.grab())\nelse e:\n    print("caught")\nprint(read("private.txt"))\n',
               {"helper.nexo": 'pub fn grab() -> str!:\n    return read("missing.txt")?\n'},
               {"helper.nexo": [["allow", "read", "missing.txt"]]})
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode() == "caught\nPRIVATE\n"


def test_module_initialization_retains_importer_context(tmp_path, backend):
    proc = app(tmp_path, backend, 'use first\n', {
        "first.nexo": 'use second\nprint(second.grab())\n',
        "second.nexo": 'pub fn grab() -> str!:\n    return read("private.txt")?\n',
    }, {"first.nexo": [], "second.nexo": [["allow", "read", "private.txt"]]})
    assert proc.returncode == 1, proc.stdout.decode()
    assert "active dependency context" in proc.stdout.decode()
    assert "\nPRIVATE\n" not in proc.stdout.decode()


def test_explicit_dependency_deny_wins(tmp_path, backend):
    proc = app(tmp_path, backend, 'use helper\nprint(helper.grab())\n',
               {"helper.nexo": 'pub fn grab() -> str!:\n    return read("private.txt")?\n'},
               {"helper.nexo": [["allow", "read", "."], ["deny", "read", "private.txt"]]})
    assert proc.returncode == 1
    assert "active dependency context" in proc.stdout.decode()


@pytest.mark.parametrize("source", ['pipeline flow:\n    pass\n', 'use nexo.stream\n',
                                    'server api:\n    pass\n'])
def test_async_contexts_fail_closed_instead_of_dropping_permissions(tmp_path, backend, source):
    proc = app(tmp_path, backend, source, {}, {})
    assert proc.returncode == 1
    assert "does not support event loops" in proc.stdout.decode()


@pytest.mark.parametrize("doc", [{"version":2,"modules":{}}, {"version":1,"modules":{"missing.nexo":[]}},
                                {"version":1,"modules":{"app.nexo":[]}}, {"version":1,"modules":{},"extra":1}])
def test_invalid_dependency_configuration_is_rejected(tmp_path, backend, doc):
    (tmp_path / "app.nexo").write_text('policy:\n    deny shell\nprint("bad")\n')
    (tmp_path / "permissions.json").write_text(json.dumps(doc))
    proc = run(backend, ["security", "run", "--dependencies", "permissions.json", "app.nexo"], tmp_path)
    assert proc.returncode == 1
    assert "\nbad\n" not in proc.stdout.decode()


@pytest.fixture(scope="module")
def worker():
    c = compiler()
    if c is None:
        pytest.skip("needs Rust toolchain")
    result = Path(c).with_name("nexo-shield-worker")
    assert result.is_file(), "cargo build --workspace must include the native worker"
    return result


@pytest.fixture(scope="module")
def capabilities(worker):
    proc = subprocess.run([str(worker), "probe"], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def test_probe_reports_real_kernel_support(worker, capabilities):
    assert capabilities["profile"] == "linux-offline-v1"
    assert capabilities["supported"] == (capabilities.get("landlock_abi",0) >= 3 and capabilities.get("seccomp",False) and capabilities.get("openat2",False) and capabilities.get("unprivileged",False) and capabilities.get("landlock_enforced",False) and capabilities.get("pidfd",False) and capabilities.get("supervisor",False))


def test_unavailable_isolation_never_executes_payload(worker, capabilities, tmp_path):
    if capabilities["supported"]:
        pytest.skip("failure branch requires an unsupported host")
    marker = tmp_path / "marker"
    proc = subprocess.run([str(worker), "--", "/usr/bin/touch", str(marker)], capture_output=True, text=True)
    assert proc.returncode == 125
    assert "no weaker fallback" in proc.stderr or "requires Linux" in proc.stderr or "non-root" in proc.stderr
    assert not marker.exists()


@pytest.mark.parametrize("limit,value", [("--cpu-seconds","0"),("--memory-mib","15"),("--wall-seconds","0"),("--file-size-mib","-1")])
def test_invalid_limits_fail_before_execution(worker, tmp_path, limit, value):
    marker = tmp_path / "marker"
    proc = subprocess.run([str(worker),limit,value,"--","/usr/bin/touch",str(marker)],capture_output=True,text=True)
    assert proc.returncode == 125
    assert not marker.exists()


@pytest.fixture
def linux_host(capabilities):
    if not capabilities["supported"]:
        pytest.skip("Landlock ABI >=3/openat2/seccomp unavailable; no sandbox substitute")


@pytest.fixture
def payload(tmp_path, linux_host):
    source = tmp_path / "probe.c"
    source.write_text(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/resource.h>
int main(int n, char **a) {
    if (!strcmp(a[1],"read")) { int f=open(a[2],O_RDONLY); if(f<0)return 40; char b[64]; int x=read(f,b,64); if(x>0)write(1,b,x); return 0; }
    if (!strcmp(a[1],"write")) { int f=open(a[2],O_WRONLY|O_CREAT|O_TRUNC,0600); if(f<0)return 41; write(f,"changed",7); return 0; }
    if (!strcmp(a[1],"network")) return socket(AF_INET,SOCK_STREAM,0)<0 && errno==EPERM ? 0:42;
    if (!strcmp(a[1],"fd")) { char b[1]; return read(atoi(a[2]),b,1)<0 && errno==EBADF ? 0:48; }
    if (!strcmp(a[1],"fork")) return fork()<0 && errno==EPERM ? 0:43;
    if (!strcmp(a[1],"cpu")) { for(;;){} }
    if (!strcmp(a[1],"sleep")) { sleep(60); return 44; }
    if (!strcmp(a[1],"memory")) { void*p=malloc(256UL*1024*1024); return p==NULL?0:45; }
    if (!strcmp(a[1],"file")) { int f=open(a[2],O_WRONLY|O_CREAT,0600); char b[4096]={0}; for(;;)write(f,b,sizeof b); }
    if (!strcmp(a[1],"env")) return getenv("SHIELD_HOST_SECRET")?46:0;
    return 47;
}
''')
    exe=tmp_path / "payload"
    subprocess.run(["cc","-O0",str(source),"-o",str(exe)],check=True)
    return exe


def native(worker, payload, *args, read=(), write=(), limits=()):
    cmd=[str(worker),"--read","/usr/lib"]
    if Path("/etc/ld.so.cache").exists(): cmd += ["--read","/etc/ld.so.cache"]
    for path in read: cmd += ["--read",str(path)]
    for path in write: cmd += ["--write",str(path)]
    return subprocess.run([*cmd,*limits,"--",str(payload),*map(str,args)],capture_output=True,text=True,timeout=12)


def test_kernel_read_and_symlink_escape(worker,payload,tmp_path):
    allowed=tmp_path/"allowed"; allowed.mkdir()
    secret=tmp_path/"secret"; secret.write_text("CONFIDENTIAL")
    public=allowed/"public"; public.write_text("PUBLIC")
    (allowed/"escape").symlink_to(secret)
    assert native(worker,payload,"read",public,read=[allowed]).stdout=="PUBLIC"
    for path in [secret,allowed/"escape",allowed/".."/"secret"]:
        p=native(worker,payload,"read",path,read=[allowed])
        assert p.returncode==40 and "CONFIDENTIAL" not in p.stdout, p.stderr


def test_kernel_write_boundary(worker,payload,tmp_path):
    out=tmp_path/"out"; out.mkdir()
    forbidden=tmp_path/"forbidden"
    assert native(worker,payload,"write",out/"ok",write=[out]).returncode==0
    assert native(worker,payload,"write",forbidden,write=[out]).returncode==41
    assert not forbidden.exists()


@pytest.mark.parametrize("op",["network","fork","env","memory"])
def test_native_syscalls_and_environment_are_confined(worker,payload,op,monkeypatch):
    monkeypatch.setenv("SHIELD_HOST_SECRET","do-not-inherit")
    p=native(worker,payload,op,limits=["--memory-mib","64"])
    assert p.returncode==0,p.stderr


@pytest.mark.parametrize("op,limits,expected",[("cpu",["--cpu-seconds","1"],152),("sleep",["--wall-seconds","1"],124)])
def test_actual_cpu_and_wall_limits(worker,payload,op,limits,expected):
    p=native(worker,payload,op,limits=limits)
    assert p.returncode==expected,p.stderr


def test_actual_file_size_limit(worker,payload,tmp_path):
    out=tmp_path/"out";out.mkdir()
    p=native(worker,payload,"file",out/"large",write=[out],limits=["--file-size-mib","1"])
    assert p.returncode==153,p.stderr
    assert (out/"large").stat().st_size<=1024*1024


def test_kernel_symlink_race_cannot_open_outside_grant(worker,payload,tmp_path):
    import threading
    allowed=tmp_path/"allowed";allowed.mkdir()
    public=allowed/"public";public.write_text("PUBLIC")
    secret=tmp_path/"secret";secret.write_text("CONFIDENTIAL")
    link=allowed/"race";link.symlink_to(public)
    stop=threading.Event()
    def swap():
        temporary=allowed/"new-link"
        while not stop.is_set():
            for target in [public,secret]:
                temporary.symlink_to(target)
                os.replace(temporary,link)
    thread=threading.Thread(target=swap);thread.start()
    try:
        for _ in range(8):
            p=native(worker,payload,"read",link,read=[allowed])
            assert p.returncode in (0,40),p.stderr
            assert "CONFIDENTIAL" not in p.stdout
    finally:
        stop.set();thread.join()


def test_inherited_file_descriptor_is_not_a_capability_escape(worker,payload,tmp_path):
    import fcntl
    secret=tmp_path/"secret";secret.write_text("CONFIDENTIAL")
    with secret.open() as source:
        fd=fcntl.fcntl(source.fileno(),fcntl.F_DUPFD,100)
        try:
            os.set_inheritable(fd,True)
            cmd=[str(worker),"--read","/usr/lib","--",str(payload),"fd",str(fd)]
            p=subprocess.run(cmd,close_fds=False,capture_output=True,text=True,timeout=12)
            assert p.returncode==0,p.stderr
        finally:
            os.close(fd)


def test_uninstrumented_binary_cannot_silently_accept_dependency_contexts(tmp_path):
    c=compiler()
    if c is None: pytest.skip("needs compiler")
    (tmp_path/"app.nexo").write_text('policy:\n    allow read "."\nprint(read("private.txt"))\n')
    (tmp_path/"private.txt").write_text("PRIVATE")
    (tmp_path/"permissions.json").write_text('{"version":1,"modules":{}}')
    built=run([str(c)],["build","app.nexo","-o","app"],tmp_path)
    assert built.returncode==0,built.stdout.decode()
    p=run([str(tmp_path/"app")],[],tmp_path,env={"NEXO_SECURITY_DEPENDENCIES":str(tmp_path/"permissions.json")})
    assert p.returncode==1
    assert "\nPRIVATE\n" not in p.stdout.decode()
    assert "Dependency permissions are invalid" in p.stdout.decode()


@pytest.mark.parametrize("returned",["second.grab","[second.grab]","read"])
def test_dependency_cannot_export_unsealed_authority(tmp_path,backend,returned):
    proc=app(tmp_path,backend,'use first\nvalue = first.choose()\nprint("escaped")\n',{
        "first.nexo":f'use second\npub fn choose() -> any:\n    return {returned}\n',
        "second.nexo":'pub fn grab() -> str!:\n    return read("private.txt")?\n',
    },{"first.nexo":[],"second.nexo":[["allow","read","private.txt"]]})
    assert proc.returncode==1,proc.stdout.decode()
    assert "cannot export callable" in proc.stdout.decode()
    assert "\nescaped\n" not in proc.stdout.decode()


def test_plain_container_results_cross_dependency_boundary(tmp_path,backend):
    proc=app(tmp_path,backend,'use helper\nvalue=helper.data()\nprint(len(value["items"]))\n',
        {"helper.nexo":'pub fn data() -> any:\n    return {"items": [1, 2], "ok": true}\n'},
        {"helper.nexo":[]})
    assert proc.returncode==0,proc.stdout.decode()
    assert proc.stdout.decode()=="2\n"


def test_nexo_linux_profile_does_not_execute_when_unavailable(tmp_path,backend,capabilities):
    if capabilities["supported"]: pytest.skip("negative branch needs an unsupported host")
    (tmp_path/"app.nexo").write_text('policy:\n    deny shell\nprint("MUST_NOT_EXECUTE")\n')
    p=run(backend,["security","run","--sandbox=linux","app.nexo"],tmp_path)
    assert p.returncode==125,p.stdout.decode()
    assert "MUST_NOT_EXECUTE" not in p.stdout.decode()


def test_nexo_linux_profile_runs_allowed_program(tmp_path,backend,linux_host):
    (tmp_path/"input.txt").write_text("PUBLIC")
    (tmp_path/"app.nexo").write_text('policy:\n    allow read "input.txt"\nprint(read("input.txt"))\n')
    p=run(backend,["security","run","--sandbox=linux","app.nexo"],tmp_path)
    assert p.returncode==0,p.stdout.decode()
    assert p.stdout.decode()=="PUBLIC\n"


def test_loader_bindings_are_rejected_before_nexo_sandbox_start(tmp_path,backend):
    (tmp_path/"app.nexo").write_text('policy:\n    allow env "LD_SHIELD_TEST"\nprint("MUST_NOT_EXECUTE")\n')
    p=run(backend,["security","run","--sandbox=linux","app.nexo"],tmp_path)
    assert p.returncode==125,p.stdout.decode()
    assert "Loader-control bindings" in p.stdout.decode()
    assert "MUST_NOT_EXECUTE" not in p.stdout.decode()


@pytest.mark.parametrize("default", ["second.grab", "[second.grab]", "read", "second.Box"])
def test_record_constructor_cannot_export_dependency_callable(tmp_path, backend, default):
    body = 'use first\nb = first.Bridge()\n'
    body += 'print(b.action())\n' if default == "second.grab" else 'print("escaped")\n'
    proc = app(tmp_path, backend, body, {
        "first.nexo": f'use second\npub type Bridge:\n    action: any = {default}\n',
        "second.nexo": 'pub type Box:\n    value: int = 1\npub fn grab() -> str!:\n    return read("private.txt")?\n',
    }, {"first.nexo": [], "second.nexo": [["allow", "read", "private.txt"]]})
    assert proc.returncode == 1, proc.stdout.decode()
    assert "cannot export callable" in proc.stdout.decode()
    assert proc.stdout.decode().startswith("Error in app.nexo, line 4:\n")
    assert "\nPRIVATE\n" not in proc.stdout.decode()
    assert "\nescaped\n" not in proc.stdout.decode()


def test_record_default_keeps_declaring_dependency_ceiling(tmp_path, backend):
    proc = app(tmp_path, backend, 'use first\nb = first.Box()\nprint(b.value)\n', {
        "first.nexo": 'use second\npub type Box:\n    value: str = second.grab()\n',
        "second.nexo": 'pub fn grab() -> str:\n    try:\n        return read("private.txt")\n    else e:\n        return "missing"\n',
    }, {"first.nexo": [], "second.nexo": [["allow", "read", "private.txt"]]})
    assert proc.returncode == 1, proc.stdout.decode()
    assert "active dependency context" in proc.stdout.decode()
    assert "\nPRIVATE\n" not in proc.stdout.decode()


def test_plain_record_defaults_remain_available_with_dependencies(tmp_path, backend):
    proc = app(tmp_path, backend, 'use helper\nb = helper.Box()\nprint(b.value["items"][0])\n',
               {"helper.nexo": 'pub type Box:\n    value: any = {"items": [7], "ok": true}\n'},
               {"helper.nexo": []})
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode() == "7\n"


def test_root_record_defaults_do_not_gain_a_dependency_ceiling(tmp_path, backend):
    proc = app(tmp_path, backend,
               'use helper\ntype Box:\n    action: any = helper.grab\nb = Box()\nprint(b.action())\n',
               {"helper.nexo": 'pub fn grab() -> str!:\n    return read("private.txt")?\n'},
               {"helper.nexo": [["allow", "read", "private.txt"]]})
    assert proc.returncode == 0, proc.stdout.decode()
    assert proc.stdout.decode() == "PRIVATE\n"
