"""Restricted execution of AI-generated Python.

Isolation levels, strongest first. The result always says which one actually ran.

* ``docker``: a throwaway container with no network, a read-only filesystem, memory/CPU/process
  limits, no capabilities and a non-root user. Used when Docker and the image are available.
* ``process-limited``: a separate Python process with an empty environment, a throwaway working
  directory, a hard timeout, OS resource limits (a Windows Job Object, or rlimits elsewhere) and
  an audit-hook guard that refuses network, process creation and file access outside the temp
  folder. This is defence in depth, **not a hard sandbox**: code running inside the interpreter
  can try to undo an in-process guard. It is only used after the user approved running code.

The text deny-list in ``tools.py`` is a cheap pre-check, never the protection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .tools import _RESTRICTED

DEFAULT_IMAGE = "python:3.12-slim"
MAX_OUTPUT = 6000
MEMORY_MB = 256

GUARD = r'''
import os, runpy, sys, threading

TMP = os.path.realpath(os.getcwd())
sys.path.insert(0, TMP)
_roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix, TMP}
READ_OK = tuple(os.path.normcase(os.path.realpath(p)) for p in _roots if p)
WRITE_OK = (os.path.normcase(TMP),)
BLOCKED = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork",
           "os.kill", "os.startfile", "ctypes.", "winreg.", "msvcrt.", "webbrowser.", "pty.", "fcntl.",
           "resource.", "signal.", "mmap.", "multiprocessing.", "_thread.start_new_thread_blocked")
WRITE_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.symlink",
                "os.link", "os.truncate", "os.utime", "shutil.copyfile", "shutil.copymode", "shutil.copystat",
                "shutil.copytree", "shutil.move", "shutil.rmtree", "os.putenv", "os.unsetenv"}
_busy = threading.local()


def _under(path, roots):
    p = os.path.normcase(os.path.realpath(path))
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _hook(event, args):
    if getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        if event.startswith(BLOCKED):
            raise PermissionError("blocked by the TaskWrite sandbox: " + event)
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int) or path is None:
                return
            writing = any(c in str(mode or "") for c in "wax+") or bool(
                (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
            if writing and not _under(path, WRITE_OK):
                raise PermissionError("blocked by the TaskWrite sandbox: writing outside the temp folder")
            if not writing and not _under(path, READ_OK):
                raise PermissionError("blocked by the TaskWrite sandbox: reading outside the temp folder")
        elif event in ("os.listdir", "os.scandir"):
            target = args[0] if args else None
            if isinstance(target, (str, bytes, os.PathLike)) and not _under(target, READ_OK):
                raise PermissionError("blocked by the TaskWrite sandbox: listing a folder outside the temp folder")
        elif event in WRITE_EVENTS:
            for a in args:
                if isinstance(a, (str, bytes, os.PathLike)) and not _under(a, WRITE_OK):
                    raise PermissionError("blocked by the TaskWrite sandbox: changing files outside the temp folder")
    finally:
        _busy.on = False


sys.addaudithook(_hook)
target = sys.argv[1]
sys.argv = [target]
runpy.run_path(target, run_name="__main__")
'''


@dataclass
class SandboxResult:
    level: str = "none"  # docker | process-limited | none
    ran: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    note: str = ""
    limits: list[str] = field(default_factory=list)
    seconds: float = 0.0


def _cap(data: bytes) -> str:
    return data.decode("utf-8", "replace")[:MAX_OUTPUT]


class Sandbox:
    """Runs a small set of Python files; the entry file is executed, the others can be imported."""

    def __init__(self, image: str = DEFAULT_IMAGE, prefer_docker: bool = True, timeout_s: float = 10.0,
                 memory_mb: int = MEMORY_MB, runner: Callable[..., subprocess.CompletedProcess] | None = None) -> None:
        self.image, self.prefer_docker = image, prefer_docker
        self.timeout_s, self.memory_mb = timeout_s, memory_mb
        self._run = runner or subprocess.run
        self._docker_ok: bool | None = None
        self._checked_at = 0.0

    # --- capability ------------------------------------------------------------

    RECHECK_S = 15.0  # "no Docker" is re-checked every few seconds, so starting Docker Desktop is noticed without a restart

    def docker_ready(self) -> bool:
        """True when Docker answers and the image is present. A positive answer is kept; a negative one expires."""
        stale = self._docker_ok is False and time.monotonic() - self._checked_at > self.RECHECK_S
        if self._docker_ok is None or stale:
            self._docker_ok = False
            self._checked_at = time.monotonic()
            if self.prefer_docker and shutil.which("docker"):
                try:
                    info = self._run(["docker", "info"], capture_output=True, timeout=8)
                    img = self._run(["docker", "image", "inspect", self.image], capture_output=True, timeout=8)
                    self._docker_ok = info.returncode == 0 and img.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    self._docker_ok = False
        return self._docker_ok

    def level(self) -> str:
        return "docker" if self.docker_ready() else "process-limited"

    def describe(self) -> dict[str, object]:
        has_docker = bool(shutil.which("docker"))
        level = self.level()
        if level == "docker":
            note = f"Code runs in a Docker container ({self.image}) with no network."
        elif has_docker:
            note = (f"Docker is installed but not usable (is it running, and is '{self.image}' pulled?). "
                    "Code would run in the process-limited mode, which is not a hard sandbox.")
        else:
            note = ("Docker was not found. Code would run in the process-limited mode (separate process, "
                    "no network, files confined to a temp folder, resource limits). This is defence in depth, "
                    "not a hard sandbox.")
        return {"level": level, "docker_installed": has_docker, "note": note}

    # --- running -----------------------------------------------------------------

    def run(self, files: dict[str, str], entry: str, precheck: bool = True) -> SandboxResult:
        if entry not in files:
            return SandboxResult(note="Nothing to run.")
        for name, code in files.items():
            if "/" in name or "\\" in name or not name.endswith(".py"):
                return SandboxResult(note=f"Refused file name {name!r}.")
            if precheck and _RESTRICTED.search(code):
                return SandboxResult(note="Not run: the code uses modules or calls this runner refuses "
                                          "(subprocess, sockets, eval/exec, os.system and similar).")
        with tempfile.TemporaryDirectory(prefix="taskwrite_run_") as tmp:
            for name, code in files.items():
                (Path(tmp) / name).write_text(code, encoding="utf-8")
            if self.docker_ready():
                return self._run_docker(tmp, entry)
            return self._run_local(tmp, entry)

    def docker_command(self, tmp: str, entry: str, name: str) -> list[str]:
        return ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--memory", f"{self.memory_mb}m",
                "--memory-swap", f"{self.memory_mb}m", "--cpus", "1", "--pids-limit", "64",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-u", "65534:65534",
                "-v", f"{tmp}:/work:ro", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
                "-e", "PYTHONIOENCODING=utf-8", self.image, "python", "-s", entry]

    def _run_docker(self, tmp: str, entry: str) -> SandboxResult:
        name = f"taskwrite-{uuid.uuid4().hex[:12]}"
        limits = ["no network", "read-only filesystem", f"{self.memory_mb} MB memory", "1 CPU", "64 processes",
                  "no capabilities", "non-root user"]
        start = time.monotonic()
        try:
            proc = self._run(self.docker_command(tmp, entry, name), capture_output=True,
                             timeout=self.timeout_s + 15, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self._run(["docker", "kill", name], capture_output=True, timeout=10)
            return SandboxResult("docker", True, None, "", "", True, f"Stopped after {self.timeout_s:g}s timeout.",
                                 limits, time.monotonic() - start)
        except (OSError, subprocess.SubprocessError) as exc:
            return SandboxResult("docker", False, note=f"Could not start Docker: {type(exc).__name__}")
        return SandboxResult("docker", True, proc.returncode, _cap(proc.stdout), _cap(proc.stderr), False, "",
                             limits, time.monotonic() - start)

    def _run_local(self, tmp: str, entry: str) -> SandboxResult:
        guard = Path(tmp) / "_tw_guard.py"
        guard.write_text(GUARD, encoding="utf-8")
        python = getattr(sys, "_base_executable", None) or sys.executable
        env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        limits = ["separate process", "empty environment", "temp working folder", f"{self.timeout_s:g}s timeout",
                  "audit-hook guard (no network, no new processes, files confined to the temp folder)"]
        kwargs: dict[str, object] = {}
        if os.name == "posix":
            kwargs["preexec_fn"] = _posix_limits(self.memory_mb, self.timeout_s)
            limits.append("rlimits (memory, CPU, file size)")
        start = time.monotonic()
        try:
            proc = subprocess.Popen([python, "-I", "_tw_guard.py", entry], cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)  # type: ignore[call-overload]
        except OSError as exc:
            return SandboxResult("process-limited", False, note=f"Could not start the interpreter: {type(exc).__name__}")
        job_note = _windows_job(proc, self.memory_mb) if os.name == "nt" else ""
        if os.name == "nt":
            limits.append(job_note or f"Windows Job Object ({self.memory_mb} MB memory, 1 process, kill on close)")
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            return SandboxResult("process-limited", True, None, _cap(out), _cap(err), True,
                                 f"Stopped after {self.timeout_s:g}s timeout.", limits, time.monotonic() - start)
        return SandboxResult("process-limited", True, proc.returncode, _cap(out), _cap(err), False, "",
                             limits, time.monotonic() - start)


def _posix_limits(memory_mb: int, timeout_s: float) -> Callable[[], None]:
    def apply() -> None:  # runs in the child before exec
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s) + 2,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)

    return apply


def _windows_job(proc: subprocess.Popen, memory_mb: int) -> str:
    """Put the child in a Job Object (memory cap, one process, kill on close).

    Returns "" on success or a note saying the limits could not be applied.
    """
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return "Windows Job Object limits could not be applied (CreateJobObject failed)."
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x8 | 0x100 | 0x2000  # active process, process memory, kill on close
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
        if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            return "Windows Job Object limits could not be applied (SetInformationJobObject failed)."
        if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))):  # type: ignore[attr-defined]
            return "Windows Job Object limits could not be applied (the process could not be assigned)."
        proc._tw_job = job  # type: ignore[attr-defined]  # keep the handle alive until the process is done
        return ""
    except (OSError, AttributeError, ValueError) as exc:
        return f"Windows Job Object limits could not be applied ({type(exc).__name__})."
