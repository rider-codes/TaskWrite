"""Native "choose a folder" dialog. The backend runs on the user's machine, so it can show one.

It runs in a short-lived child process so a hung dialog can never block the API.
"""

from __future__ import annotations

import subprocess
import sys

_SCRIPT = (
    "import tkinter as tk\n"
    "from tkinter import filedialog\n"
    "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
    "print(filedialog.askdirectory(title='Choose the TaskWrite workspace folder') or '')\n"
)


def pick_folder(timeout_s: float = 180.0) -> str:
    """Return the chosen folder, or "" if cancelled or no dialog is available."""
    try:
        proc = subprocess.run([sys.executable, "-c", _SCRIPT], capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""
