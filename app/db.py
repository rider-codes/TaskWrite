"""SQLite persistence: sessions, action log, and non-secret settings."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .models import SessionState, SessionStatus, now_iso
from .safety import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    step_cap INTEGER NOT NULL DEFAULT 8,
    steps_used INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    step_id TEXT,
    summary TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class SessionNotFound(KeyError):
    pass


class Database:
    def __init__(self, path: str, secrets: tuple[str, ...] = ()) -> None:
        self.path = path
        self.secrets = secrets
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- sessions ---------------------------------------------------------

    def create_session(self, state: SessionState) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, filename, status, step_cap, steps_used, state_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (state.id, state.filename, state.status.value, state.step_cap, state.steps_used,
                 state.model_dump_json(), state.created_at, state.updated_at),
            )

    def save_session(self, state: SessionState) -> None:
        """Persist state. Budget and cancel flag columns are owned by targeted updates."""
        state.updated_at = now_iso()
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET status=?, state_json=?, updated_at=? WHERE id=?",
                (state.status.value, state.model_dump_json(), state.updated_at, state.id),
            )

    def load_session(self, session_id: str) -> SessionState:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        state = SessionState.model_validate_json(row["state_json"])
        state.status = SessionStatus(row["status"])
        state.cancel_requested = bool(row["cancel_requested"])
        state.step_cap = row["step_cap"]
        state.steps_used = row["steps_used"]
        return state

    def list_sessions(self) -> list[dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, filename, status, steps_used, step_cap, updated_at FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """Remove a chat and its action log from the database. Files already saved in a workspace are not touched."""
        with self._conn() as conn:
            conn.execute("DELETE FROM action_log WHERE session_id=?", (session_id,))
            return conn.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount == 1

    def transition(self, session_id: str, allowed_from: set[SessionStatus], to: SessionStatus) -> bool:
        """Atomically change status only if the current status is allowed."""
        marks = ",".join("?" for _ in allowed_from)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE sessions SET status=?, updated_at=? WHERE id=? AND status IN ({marks})",
                (to.value, now_iso(), session_id, *(s.value for s in allowed_from)),
            )
            return cur.rowcount == 1

    def set_message(self, session_id: str, message: str) -> None:
        """Update only the status message, so it cannot overwrite a running step's saved progress."""
        with self._conn() as conn:
            row = conn.execute("SELECT state_json FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return
            import json
            data = json.loads(row["state_json"])
            data["message"] = message
            conn.execute("UPDATE sessions SET state_json=? WHERE id=?", (json.dumps(data), session_id))

    def set_cancel(self, session_id: str, value: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE sessions SET cancel_requested=? WHERE id=?", (int(value), session_id))

    def set_budget(self, session_id: str, *, used: int | None = None, cap: int | None = None) -> None:
        with self._conn() as conn:
            if used is not None:
                conn.execute("UPDATE sessions SET steps_used=? WHERE id=?", (used, session_id))
            if cap is not None:
                conn.execute("UPDATE sessions SET step_cap=? WHERE id=?", (cap, session_id))

    def mark_interrupted(self) -> int:
        """On startup, any session left 'running' lost its worker; make it resumable."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET status=? WHERE status=?",
                (SessionStatus.interrupted.value, SessionStatus.running.value),
            )
            return cur.rowcount

    # --- action log -------------------------------------------------------

    def log_action(self, session_id: str, kind: str, summary: str, result: str = "ok",
                   step_id: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO action_log (session_id, ts, kind, step_id, summary, result) VALUES (?,?,?,?,?,?)",
                (session_id, now_iso(), kind, step_id,
                 redact(summary, self.secrets)[:600], redact(result, self.secrets)[:600]),
            )

    def get_log(self, session_id: str) -> list[dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, kind, step_id, summary, result FROM action_log WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- settings (non-secret only) ---------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
