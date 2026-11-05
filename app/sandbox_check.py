"""Self-test for the code sandbox: `python -m app.sandbox_check`.

It runs a small battery of hostile programs through the same sandbox TaskWrite uses (with the text
pre-check switched off, so the sandbox itself is what is being tested) and reports which isolation
level ran and whether each attack was stopped. Run it after installing Docker to confirm that
"docker" mode is really in use, and any time you want to re-check the restricted-process mode.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, load_dotenv
from .sandbox import Sandbox

CANARY_ENV = "TASKWRITE_SELFTEST_SECRET"
CANARY_TEXT = "TOP-SECRET-CANARY-7731"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def _blocked(result, secret: str = "") -> bool:
    """An attack counts as stopped if the program failed (or never ran) and leaked nothing."""
    leaked = bool(secret) and secret in (result.stdout + result.stderr)
    return not leaked and (not result.ran or result.exit_code != 0 or result.timed_out)


def run_checks(sandbox: Sandbox) -> list[Check]:
    out: list[Check] = []
    os.environ[CANARY_ENV] = CANARY_TEXT
    with tempfile.TemporaryDirectory(prefix="taskwrite_check_") as tmp:
        canary = Path(tmp) / "secret.txt"
        canary.write_text(CANARY_TEXT, encoding="utf-8")
        target = Path(tmp) / "escape.txt"

        def go(code: str, **kw):
            return sandbox.run({"a.py": code}, "a.py", precheck=False, **kw)

        r = go("print(sum(range(10)))")
        out.append(Check("normal code runs", r.ran and r.exit_code == 0 and r.stdout.strip() == "45",
                         r.stderr.strip()[-120:]))
        r2 = sandbox.run({"q1.py": "def add(a, b):\n    return a + b\n",
                          "check_q1.py": "from q1 import *\nassert add(1, 2) == 3\nprint('IMPORTED_OK')"},
                         "check_q1.py", precheck=False)
        out.append(Check("a solution file and its test file can import each other", "IMPORTED_OK" in r2.stdout,
                         (r2.stderr.strip().splitlines() or [""])[-1][:120]))
        r = go("import socket\ns = socket.socket()\ns.settimeout(3)\ns.connect(('1.1.1.1', 80))\nprint('CONNECTED')")
        out.append(Check("network is blocked", _blocked(r) and "CONNECTED" not in r.stdout))
        r = go(f"print(open({str(canary)!r}).read())")
        out.append(Check("cannot read a file outside its folder", _blocked(r, CANARY_TEXT)))
        r = go(f"open({str(target)!r}, 'w').write('x')")
        out.append(Check("cannot write outside its folder", _blocked(r) and not target.exists()))
        r = go("import os\nprint(os.environ.get('" + CANARY_ENV + "', 'unset'))")
        out.append(Check("does not see your environment variables", CANARY_TEXT not in (r.stdout + r.stderr)))
        if sandbox.level() == "docker":
            # Inside the container starting programs is harmless; what matters is that it cannot flood the machine.
            r = go("import subprocess\nps = []\ntry:\n    for _ in range(200):\n        ps.append(subprocess.Popen(['sleep', '2']))\n"
                   "    print('UNLIMITED', len(ps))\nexcept Exception:\n    print('LIMITED', len(ps))\n")
            out.append(Check("cannot flood the machine with processes (limit 64)", "LIMITED" in r.stdout, r.stdout.strip()[:40]))
        else:
            r = go("import os\nos.system('echo SPAWNED')")
            out.append(Check("cannot start other programs", "SPAWNED" not in r.stdout))
        r = go("while True:\n    pass")
        out.append(Check("an endless loop is stopped by the timeout", r.timed_out, f"{r.seconds:.1f}s"))
        r = go("x = bytearray(3 * 1024 ** 3)\nprint('ALLOCATED')")
        out.append(Check("a huge memory grab is stopped", _blocked(r) and "ALLOCATED" not in r.stdout))
    os.environ.pop(CANARY_ENV, None)
    return out


def main() -> int:
    load_dotenv()
    settings = Settings.from_env()
    sandbox = Sandbox(settings.sandbox_image, settings.sandbox_docker, min(settings.sandbox_timeout_s, 5.0))
    info = sandbox.describe()
    print(f"Isolation level in use: {info['level']}")
    print(info["note"])
    print()
    results = run_checks(sandbox)
    for c in results:
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail and not c.passed else ""))
    failed = [c for c in results if not c.passed]
    print()
    if failed:
        print(f"{len(failed)} check(s) FAILED. Do not run code you do not trust in this mode.")
        return 1
    if info["level"] != "docker":
        print("All checks passed in the restricted-process mode. This is defence in depth, not a hard sandbox. "
              "Install Docker Desktop and run `docker pull " + settings.sandbox_image + "` for real isolation.")
    else:
        print("All checks passed in Docker mode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
