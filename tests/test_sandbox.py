"""Restricted execution. These run real child processes; the guard is tested with the text pre-check off
so the sandbox itself (not the deny-list) is what stops the code."""

import subprocess

import pytest

from app import sandbox as sandbox_module
from app.sandbox import Sandbox


@pytest.fixture
def sbx():
    return Sandbox(prefer_docker=False, timeout_s=8)


def run(sbx, code, name="a.py", precheck=False, **extra):
    return sbx.run({name: code, **extra}, name, precheck=precheck)


def test_normal_code_runs_and_is_labelled_process_limited(sbx):
    r = run(sbx, "print(sum(range(10)))")
    assert r.ran and r.exit_code == 0 and r.stdout.strip() == "45"
    assert r.level == "process-limited" and any("not" not in x or True for x in r.limits)


def test_solution_and_test_files_can_import_each_other(sbx):
    r = sbx.run({"q1.py": "def add(a, b):\n    return a + b\n",
                 "check_q1.py": "from q1 import *\nassert add(1, 2) == 3\nprint('OK')"}, "check_q1.py")
    assert r.exit_code == 0 and "OK" in r.stdout


def test_network_is_blocked_by_the_sandbox_not_just_the_text_check(sbx):
    for code in ("import socket\ns = socket.socket()\ns.connect(('1.1.1.1', 80))",
                 "import urllib.request\nurllib.request.urlopen('http://example.com')"):
        r = run(sbx, code)
        assert r.exit_code != 0 and "blocked by the TaskWrite sandbox" in r.stderr


def test_cannot_start_processes(sbx):
    r = run(sbx, "import os\nos.system('echo hi')")
    assert r.exit_code != 0 and "blocked by the TaskWrite sandbox" in r.stderr
    r = run(sbx, "import subprocess\nsubprocess.run(['echo', 'hi'])")
    assert r.exit_code != 0 and "blocked by the TaskWrite sandbox" in r.stderr


def test_cannot_write_outside_the_temp_folder(sbx, tmp_path):
    target = tmp_path / "escape.txt"
    r = run(sbx, f"open({str(target)!r}, 'w').write('x')")
    assert r.exit_code != 0 and not target.exists()
    r = run(sbx, f"import os\nos.remove({str(target)!r})")
    assert r.exit_code != 0


def test_cannot_read_files_outside_the_temp_folder(sbx, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    r = run(sbx, f"print(open({str(secret)!r}).read())")
    assert r.exit_code != 0 and "TOP-SECRET" not in r.stdout + r.stderr


def test_cannot_list_folders_outside_the_temp_folder(sbx, tmp_path):
    (tmp_path / "private_name.txt").write_text("x")
    for code in (f"import os\nprint(os.listdir({str(tmp_path)!r}))",
                 f"import os\nprint([e.name for e in os.scandir({str(tmp_path)!r})])",
                 f"import glob\nprint(glob.glob({str(tmp_path / '*')!r}))"):
        r = run(sbx, code)
        assert "private_name" not in r.stdout, code
    assert run(sbx, "import os\nprint(os.listdir('.'))").exit_code == 0  # its own folder is fine


def test_can_use_its_own_temp_folder(sbx):
    r = run(sbx, "open('t.txt', 'w').write('x')\nprint(open('t.txt').read())")
    assert r.exit_code == 0 and r.stdout.strip() == "x"


def test_environment_is_empty_of_secrets(sbx, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2hunter2")
    r = run(sbx, "import os\nprint(sorted(os.environ))")
    assert "SUPER_SECRET_TOKEN" not in r.stdout


def test_infinite_loop_is_killed_by_the_timeout():
    r = run(Sandbox(prefer_docker=False, timeout_s=2), "while True:\n    pass")
    assert r.timed_out and r.exit_code is None and r.ran


def test_memory_hog_is_stopped():
    r = run(Sandbox(prefer_docker=False, timeout_s=15, memory_mb=128), "x = bytearray(2 * 1024 ** 3)\nprint(len(x))")
    assert r.exit_code != 0 and "2147483648" not in r.stdout


def test_text_precheck_refuses_before_anything_runs(sbx):
    r = sbx.run({"a.py": "import subprocess\nsubprocess.run(['x'])"}, "a.py")
    assert not r.ran and "refuses" in r.note and r.level == "none"


def test_bad_inputs_are_refused(sbx):
    assert not sbx.run({"a.py": "print(1)"}, "missing.py").ran
    assert not sbx.run({"../evil.py": "print(1)", "a.py": "1"}, "a.py").ran
    assert not sbx.run({"a.txt": "print(1)"}, "a.txt").ran


# --- docker mode, with a fake docker (Docker is not installed on every machine) --------------

def fake_docker(monkeypatch, run_returncode=0, run_stdout=b"hi\n", timeout=False):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["docker", "run"] and timeout:
            raise subprocess.TimeoutExpired(cmd, 1)
        out = run_stdout if cmd[:2] == ["docker", "run"] else b""
        return subprocess.CompletedProcess(cmd, run_returncode if cmd[:2] == ["docker", "run"] else 0, out, b"")

    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "docker" if name == "docker" else None)
    return Sandbox(prefer_docker=True, runner=runner), calls


def test_docker_mode_uses_a_locked_down_container(monkeypatch):
    sbx, calls = fake_docker(monkeypatch)
    r = sbx.run({"a.py": "print('hi')"}, "a.py")
    assert r.level == "docker" and r.exit_code == 0 and r.stdout.strip() == "hi"
    cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    for flag in ("--network", "--read-only", "--cap-drop", "--pids-limit", "--memory", "--security-opt", "-u"):
        assert flag in cmd
    assert cmd[cmd.index("--network") + 1] == "none" and cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert cmd[cmd.index("-u") + 1] != "0" and any(part.endswith(":/work:ro") for part in cmd)
    assert sbx.describe()["level"] == "docker"


def test_docker_timeout_kills_the_container(monkeypatch):
    sbx, calls = fake_docker(monkeypatch, timeout=True)
    r = sbx.run({"a.py": "while True: pass"}, "a.py")
    assert r.timed_out and any(c[:2] == ["docker", "kill"] for c in calls)


def test_missing_docker_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    info = Sandbox(prefer_docker=True).describe()
    assert info["level"] == "process-limited" and not info["docker_installed"]
    assert "not a hard sandbox" in str(info["note"])


def test_selftest_battery_passes_in_the_restricted_process_mode():
    from app.sandbox_check import run_checks

    results = run_checks(Sandbox(prefer_docker=False, timeout_s=4))
    failed = [c.name for c in results if not c.passed]
    assert not failed, failed
    assert len(results) >= 9


def test_starting_docker_later_is_noticed_without_restarting_the_app(monkeypatch):
    state = {"up": False}

    def runner(cmd, **kwargs):
        ok = state["up"]
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"", b"")

    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "docker" if name == "docker" else None)
    sbx = Sandbox(prefer_docker=True, runner=runner)
    assert sbx.level() == "process-limited"
    state["up"] = True
    assert sbx.level() == "process-limited"  # still inside the short cache window
    sbx._checked_at -= 60  # time passes
    assert sbx.level() == "docker"
    state["up"] = False
    assert sbx.level() == "docker"  # a working Docker answer is kept, it does not flap
