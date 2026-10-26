"""Opt-in integration tests against a built image, not Dockerfile snapshots.

NEXO_SHIELD_DOCKER_IMAGE=nexo-shield:local pytest -q tests/test_shield_docker.py
"""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIELD = ROOT / "docker/shield"


@pytest.fixture(scope="module")
def image():
    value = os.environ.get("NEXO_SHIELD_DOCKER_IMAGE")
    if not value:
        pytest.skip("set NEXO_SHIELD_DOCKER_IMAGE to test an explicitly built Docker image")
    subprocess.run(["docker", "image", "inspect", value], check=True, stdout=subprocess.DEVNULL)
    return value


def launch(image, *args, timeout=90):
    return subprocess.run([str(SHIELD / "run.sh"), *map(str,args)], cwd=ROOT,
                          env=dict(os.environ, NEXO_SHIELD_IMAGE=image), capture_output=True,
                          text=True, timeout=timeout)


def project(tmp_path, source):
    directory = tmp_path / "project with spaces"
    directory.mkdir(mode=0o755)
    script = directory / "main.nexo"
    script.write_text(source)
    script.chmod(0o644)
    output = tmp_path / "output"
    output.mkdir(mode=0o777)
    # These are disposable test fixtures, never user directories. Root CI uses
    # image UID 10001; the normal host launcher maps the current non-root UID.
    output.chmod(0o777)
    return directory, output


def test_packaged_example_runs_in_hardened_container(image):
    p = launch(image)
    assert p.returncode == 0, p.stderr
    assert p.stdout == "Shield Docker: ok\n"


def test_readonly_project_and_explicit_output_mount(image,tmp_path):
    directory, output = project(tmp_path, 'policy:\n    allow write "/out"\nwrite("/out/result.txt", "ok")\nprint("done")\n')
    p = launch(image,"--project",directory,"--output",output,"run","/app/main.nexo")
    assert p.returncode == 0,p.stderr
    assert p.stdout == "done\n"
    assert (output/"result.txt").read_text() == "ok"
    # Even a policy grant cannot write through the Docker read-only bind.
    (directory/"main.nexo").write_text('policy:\n    allow write "/app"\nwrite("/app/forbidden.txt", "bad")\n')
    p = launch(image,"--project",directory,"--output",output,"run","/app/main.nexo")
    assert p.returncode != 0
    assert not (directory/"forbidden.txt").exists()


def test_api_deny_prevents_output_even_with_writable_mount(image,tmp_path):
    directory, output = project(tmp_path, 'policy:\n    deny write "/out"\ntry:\n    write("/out/forbidden.txt", "bad")\nelse e:\n    print("caught")\n')
    p = launch(image,"--project",directory,"--output",output,"run","/app/main.nexo")
    assert p.returncode == 1,p.stderr
    assert not (output/"forbidden.txt").exists()
    assert "caught\n" not in p.stdout
    assert "policy denies" in p.stderr


def test_probe_and_explicit_linux_mode_never_fall_back(image,tmp_path):
    p = launch(image,"probe")
    assert p.returncode == 0,p.stderr
    probe = json.loads(p.stdout)
    required = launch(image,"probe","--require-linux")
    assert required.returncode == (0 if probe["supported"] else 125),required.stderr
    directory, output = project(tmp_path, 'policy:\n    allow write "/out"\nwrite("/out/result.txt", "ok")\n')
    p = launch(image,"--project",directory,"--output",output,"run-linux","/app/main.nexo")
    if probe["supported"]:
        assert p.returncode == 0,p.stderr
        assert (output/"result.txt").read_text() == "ok"
    else:
        assert p.returncode == 125,p.stderr
        assert not (output/"result.txt").exists()


def test_compose_actual_security_configuration_and_start(image):
    name="nexo-shield-test-"+uuid.uuid4().hex[:12]
    compose=["docker","compose","-p",name,"-f",str(SHIELD/"compose.yaml")]
    env=dict(os.environ,NEXO_SHIELD_IMAGE=image)
    try:
        created=subprocess.run([*compose,"create","--no-build","--pull","never","shield"],env=env,cwd=ROOT,capture_output=True,text=True)
        assert created.returncode==0,created.stderr
        cid=subprocess.check_output([*compose,"ps","-aq","shield"],env=env,cwd=ROOT,text=True).strip()
        configuration=json.loads(subprocess.check_output(["docker","inspect",cid],text=True))[0]
        host=configuration["HostConfig"]
        assert configuration["Config"]["User"]=="10001:10001"
        assert host["ReadonlyRootfs"] and not host["Privileged"]
        assert host["NetworkMode"]=="none" and host["CgroupnsMode"]=="private"
        assert host["CapDrop"]==["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert any(s.startswith("seccomp=") for s in host["SecurityOpt"])
        assert host["Memory"]==host["MemorySwap"]==512*1024*1024
        assert host["NanoCpus"]==1_000_000_000 and host["PidsLimit"]==64
        assert host["PidMode"]!="host" and host["IpcMode"]!="host"
        assert all(m.get("Destination")!="/var/run/docker.sock" for m in configuration["Mounts"])
        assert "noexec" in host["Tmpfs"]["/out"]
        p=subprocess.run(["docker","start","-a",cid],capture_output=True,text=True,timeout=90)
        state=json.loads(subprocess.check_output(["docker","inspect",cid],text=True))[0]["State"]
        assert state["ExitCode"]==0,p.stderr
        assert p.stdout=="Shield Docker: ok\n"
    finally:
        subprocess.run([*compose,"down","--volumes","--remove-orphans"],env=env,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def hardened_options():
    return ["--network=none","--user=10001:10001","--read-only","--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",f"--security-opt=seccomp={SHIELD/'seccomp.json'}",
        "--memory=512m","--memory-swap=512m","--cpus=1","--pids-limit=64"]


@pytest.mark.parametrize("omitted",["--read-only","--cap-drop=ALL","--security-opt=no-new-privileges:true",
                                    "--network=none","--cpus=1","--pids-limit=64"])
def test_incomplete_container_configuration_is_rejected(image,omitted):
    options=[s for s in hardened_options() if s!=omitted]
    # Some daemons supply a finite default PID limit even without the flag.
    # Exercise an effectively unbounded cgroup, not just an omitted argument.
    if omitted == "--pids-limit=64":
        options.append("--pids-limit=-1")
    p=subprocess.run(["docker","run","--rm",*options,image,"probe"],capture_output=True,text=True,timeout=30)
    assert p.returncode==125,p.stderr
    assert "Shield container requires" in p.stderr
    assert p.stdout==""


def test_root_configuration_is_rejected(image):
    options=[s for s in hardened_options() if not s.startswith("--user=")]
    p=subprocess.run(["docker","run","--rm",*options,"--user=0",image,"probe"],capture_output=True,text=True,timeout=30)
    assert p.returncode==125
    assert "non-root" in p.stderr


def test_container_cannot_write_rootfs_or_reach_external_network(image):
    # Override only the command/entrypoint as a trusted operator to test the
    # actual kernel boundary independently of Nexo's API guards.
    cmd=["docker","run","--rm",*hardened_options(),"--entrypoint=/bin/sh",image,"-c",
         'test ! -e /var/run/docker.sock && ! touch /usr/local/bin/forbidden 2>/dev/null && test "$(ls /sys/class/net)" = lo && test "$(cat /sys/fs/cgroup/memory.max)" = 536870912']
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=30)
    assert p.returncode==0,p.stderr


def test_output_mount_cannot_make_project_writable_through_alias(image,tmp_path):
    directory, _ = project(tmp_path,'policy:\n    deny shell\nprint("bad")\n')
    output=directory/"output"; output.mkdir()
    p=launch(image,"--project",directory,"--output",output,"probe")
    assert p.returncode==2
    assert "disjoint" in p.stderr
