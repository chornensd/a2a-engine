#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A2A v2 task state machine CLI (Python standard library only).

Persistent, resumable, auditable task state for the plan/executor/correct/build
collaboration flow.

Usage:
    python a2a.py [--db PATH] [--actor NAME] <command> [options]

The database lives in ``<repo>/state.db`` by default; override it with ``--db``.
``ROOT`` is the directory containing the database: tasks, inbox and BOARD.md are
created relative to it, which keeps tests fully isolated from production.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Default data root: the directory holding state.db, tasks/ and inbox/.
DEFAULT_DB = str(Path(__file__).resolve().parents[1] / "state.db")


def _allowed_roots() -> tuple[Path, ...]:
    """Allowed filesystem roots: A2A_ALLOWED_ROOTS (pathsep-separated) or repo root."""
    raw = os.environ.get("A2A_ALLOWED_ROOTS", "")
    roots = tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip())
    return roots or (Path(__file__).resolve().parents[1],)


ALLOWED_ROOTS = _allowed_roots()
SCRIPT_PATH = Path(__file__).resolve()
PROG = "a2a.py"

STAGES = ("plan", "execute", "verify", "deliver")
STATES = ("new", "ready", "running", "waiting", "completed", "delivered", "failed", "cancelled")
TERMINAL = {"delivered", "failed", "cancelled"}
ACTIVE = {"new", "ready", "running", "waiting", "completed"}
CLAIM_STAGES = ("plan", "execute", "verify")
DONE_NEXT = {"plan": "execute", "execute": "verify"}
ARTIFACT_NAMES = {"plan": "plan.md", "result": "result.md", "verify": "verify.md"}
INBOX_ACTIONS = {"submit", "emit", "done", "fail", "verify", "block", "cancel"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  goal           TEXT NOT NULL,
  acceptance     TEXT NOT NULL,
  stage          TEXT NOT NULL CHECK (stage IN ('plan','execute','verify','deliver')),
  state          TEXT NOT NULL CHECK (state IN
                   ('new','ready','running','waiting','completed','delivered','failed','cancelled')),
  priority       INTEGER NOT NULL DEFAULT 0,
  assignee       TEXT,
  artifacts      TEXT NOT NULL DEFAULT '{}',
  rework_count   INTEGER NOT NULL DEFAULT 0,
  max_rework     INTEGER NOT NULL DEFAULT 2,
  attempts       INTEGER NOT NULL DEFAULT 0,
  max_retries    INTEGER NOT NULL DEFAULT 0,
  waiting_for    TEXT NOT NULL DEFAULT '[]',
  checkpoint     TEXT NOT NULL DEFAULT '{}',
  deadline       REAL,
  verdict        TEXT CHECK (verdict IS NULL OR verdict IN ('PASS','FAIL')),
  summary        TEXT NOT NULL DEFAULT '',
  error          TEXT,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  last_heartbeat REAL NOT NULL,
  delivered_at   REAL
);

CREATE TABLE IF NOT EXISTS events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  task_id  TEXT,
  kind     TEXT NOT NULL,
  stage    TEXT,
  actor    TEXT,
  detail   TEXT NOT NULL DEFAULT '{}',
  idem_key TEXT
);

CREATE TABLE IF NOT EXISTS signals (
  name   TEXT PRIMARY KEY,
  ts     REAL NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_state          ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_stage          ON tasks(stage);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_state ON tasks(assignee, state);
CREATE INDEX IF NOT EXISTS idx_events_task_seq      ON events(task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idem   ON events(idem_key) WHERE idem_key IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
"""


# ---------------------------------------------------------------------------
# errors / helpers
# ---------------------------------------------------------------------------


class A2AError(Exception):
    """User-facing error; carries an exit code and optional task context."""

    def __init__(self, message: str, code: int = 2, task_id: str | None = None,
                 state: str | None = None, stage: str | None = None):
        super().__init__(message)
        self.code = code
        self.task_id = task_id
        self.state = state
        self.stage = stage


def now_ts() -> float:
    return time.time()


def load_json(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return default
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _norm(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def is_under(child: Path, parent: Path) -> bool:
    try:
        c = _norm(child)
        p = _norm(parent)
    except OSError:
        return False
    return c == p or c.startswith(p + os.sep)


def is_allowed(path: Path) -> bool:
    return any(is_under(path, root) for root in ALLOWED_ROOTS)


def ensure_allowed(path: Path, label: str = "path") -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p)
    p = p.resolve()
    if not is_allowed(p):
        raise A2AError(f"{label} must be under one of: {', '.join(str(r) for r in ALLOWED_ROOTS)}: {p}")
    return p


def parse_json_object(text: str | None, label: str) -> dict:
    if text is None or not str(text).strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise A2AError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise A2AError(f"{label} must be a JSON object")
    return value


def parse_wait_list(values: Iterable[str] | str | None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    items: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in items:
                items.append(part)
    return items


def fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_deadline(ts: float | None) -> str:
    return "-" if ts is None else fmt_ts(ts)


def slugify(action: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", action.strip().lower()).strip("-")[:40]
    if not slug:
        slug = "action-" + hashlib.sha1(action.encode("utf-8")).hexdigest()[:8]
    return slug


def read_nonempty(path: Path) -> bytes:
    if not path.is_file():
        raise A2AError(f"artifact not found or not a file: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise A2AError(f"cannot read artifact {path}: {exc}") from exc
    if not data.strip():
        raise A2AError(f"artifact is empty: {path}")
    return data


def command_prefix(db_path: Path) -> str:
    return f'"{sys.executable}" "{SCRIPT_PATH}" --db "{db_path}"'


def artifact_path(root: Path, task_id: str, key: str) -> Path:
    return root / "tasks" / task_id / ARTIFACT_NAMES[key]


# ---------------------------------------------------------------------------
# task model
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    name: str
    goal: str
    acceptance: list
    stage: str
    state: str
    priority: int
    assignee: str | None
    artifacts: dict
    rework_count: int
    max_rework: int
    attempts: int
    max_retries: int
    waiting_for: list
    checkpoint: dict
    deadline: float | None
    verdict: str | None
    summary: str
    error: str | None
    created_at: float
    updated_at: float
    last_heartbeat: float
    delivered_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        data = dict(row)
        data["acceptance"] = load_json(data.get("acceptance"), [])
        data["artifacts"] = load_json(data.get("artifacts"), {})
        data["waiting_for"] = load_json(data.get("waiting_for"), [])
        data["checkpoint"] = load_json(data.get("checkpoint"), {})
        return cls(**data)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.id,
            "name": self.name,
            "goal": self.goal,
            "acceptance": list(self.acceptance),
            "stage": self.stage,
            "state": self.state,
            "priority": self.priority,
            "assignee": self.assignee,
            "artifacts": dict(self.artifacts),
            "rework_count": self.rework_count,
            "max_rework": self.max_rework,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "waiting_for": list(self.waiting_for),
            "checkpoint": dict(self.checkpoint),
            "deadline": self.deadline,
            "verdict": self.verdict,
            "summary": self.summary,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat": self.last_heartbeat,
            "delivered_at": self.delivered_at,
        }


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, db_path: str | os.PathLike):
        raw = Path(db_path).expanduser()
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        self.db_path = ensure_allowed(raw, "db")
        self.root = self.db_path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    @contextlib.contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            with contextlib.suppress(Exception):
                self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- reads -------------------------------------------------------------
    def get_task(self, task_id: str, conn: sqlite3.Connection | None = None) -> Task | None:
        conn = conn or self.conn
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def require_task(self, task_id: str, conn: sqlite3.Connection | None = None) -> Task:
        if not task_id or not str(task_id).strip():
            raise A2AError("task id is required", 2)
        task = self.get_task(str(task_id).strip(), conn)
        if task is None:
            raise A2AError(f"task not found: {task_id}", 1, task_id=str(task_id))
        return task

    def all_tasks(self, conn: sqlite3.Connection | None = None) -> list[Task]:
        conn = conn or self.conn
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        return [Task.from_row(row) for row in rows]

    # -- writes ------------------------------------------------------------
    def save_task(self, task: Task, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.conn
        task.updated_at = now_ts()
        conn.execute(
            """UPDATE tasks SET
                 name=?, goal=?, acceptance=?, stage=?, state=?, priority=?, assignee=?,
                 artifacts=?, rework_count=?, max_rework=?, attempts=?, max_retries=?,
                 waiting_for=?, checkpoint=?, deadline=?, verdict=?, summary=?, error=?,
                 updated_at=?, last_heartbeat=?, delivered_at=?
               WHERE id=?""",
            (
                task.name,
                task.goal,
                dump_json(task.acceptance),
                task.stage,
                task.state,
                int(task.priority),
                task.assignee,
                dump_json(task.artifacts),
                int(task.rework_count),
                int(task.max_rework),
                int(task.attempts),
                int(task.max_retries),
                dump_json(task.waiting_for),
                dump_json(task.checkpoint),
                task.deadline,
                task.verdict,
                task.summary or "",
                task.error,
                task.updated_at,
                task.last_heartbeat,
                task.delivered_at,
                task.id,
            ),
        )

    def add_event(self, conn: sqlite3.Connection, kind: str, *, task_id: str | None = None,
                  stage: str | None = None, actor: str = "cli", detail: dict | None = None,
                  idem_key: str | None = None, ts: float | None = None) -> None:
        conn.execute(
            "INSERT INTO events(ts, task_id, kind, stage, actor, detail, idem_key) VALUES (?,?,?,?,?,?,?)",
            (ts if ts is not None else now_ts(), task_id, kind, stage, actor,
             dump_json(detail or {}), idem_key),
        )


# ---------------------------------------------------------------------------
# next-action mapping
# ---------------------------------------------------------------------------


def build_next(task: Task, root: Path, db_path: Path) -> dict:
    """Return the next actor/action/artifact/command for a task."""
    tid = task.id
    prefix = command_prefix(db_path)
    plan = artifact_path(root, tid, "plan")
    result = artifact_path(root, tid, "result")
    verify = artifact_path(root, tid, "verify")

    if task.state == "ready" and task.stage in CLAIM_STAGES:
        agent = {"plan": "plan", "execute": "executor", "verify": "correct"}[task.stage]
        key = {"plan": "plan", "execute": "result", "verify": "verify"}[task.stage]
        artifact = {"plan": plan, "execute": result, "verify": verify}[task.stage]
        finish = (
            f'verify {tid} --verdict PASS|FAIL --artifact "{artifact}" --summary "<摘要>"'
            if task.stage == "verify"
            else f'done {tid} --stage {task.stage} --artifact "{artifact}" --summary "<摘要>"'
        )
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "claim",
            "agent": agent,
            "artifact": str(artifact),
            "command": f'{prefix} claim {tid} --stage {task.stage} --agent {agent}',
            "message": [
                f"目标：{task.goal}",
                f"验收：{'；'.join(task.acceptance) if task.acceptance else '（无）'}",
                f"完成后写 {ARTIFACT_NAMES[key]} 并执行 {finish}。",
            ],
        }

    if task.state == "running" and task.stage in ("plan", "execute"):
        agent = "plan" if task.stage == "plan" else "executor"
        key = "plan" if task.stage == "plan" else "result"
        artifact = plan if key == "plan" else result
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "await_plan" if task.stage == "plan" else "await_result",
            "agent": agent,
            "artifact": str(artifact),
            "command": f'{prefix} done {tid} --stage {task.stage} --artifact "{artifact}" --summary "<摘要>"',
            "message": [
                f"{agent} 正在执行 {task.stage} 阶段。",
                f"产物：{ARTIFACT_NAMES[key]}。",
                f"完成后 done -> 下一 stage。",
            ],
        }

    if task.state == "running" and task.stage == "verify":
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "await_verify",
            "agent": "correct",
            "artifact": str(verify),
            "command": f'{prefix} verify {tid} --verdict PASS|FAIL --artifact "{verify}" --summary "<摘要>"',
            "message": [
                "correct 正在核验。",
                "PASS -> completed/deliver；FAIL -> 返工或 failed。",
            ],
        }

    if task.state == "completed":
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "deliver",
            "agent": "build",
            "artifact": str(verify),
            "command": f'{prefix} deliver {tid} --summary "<摘要>"',
            "message": ["核验已 PASS，等待 build 交付用户。"],
        }

    if task.state == "waiting":
        deadline_marker = f"deadline:{tid}"
        if deadline_marker in task.waiting_for:
            return {
                "task_id": tid,
                "state": task.state,
                "stage": task.stage,
                "action": "wait_signal",
                "agent": "build",
                "artifact": None,
                "command": f'{prefix} extend {tid} --deadline-minutes <分钟>',
                "message": [
                    f"等待信号：{', '.join(task.waiting_for)}",
                    "deadline 已到期：用 extend 设新期限（或 cancel）；不要 emit deadline 信号。",
                ],
            }
        signal = task.waiting_for[0] if task.waiting_for else f"approval:{tid}:<action>"
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "wait_signal",
            "agent": "build",
            "artifact": None,
            "command": f'{prefix} emit "{signal}"',
            "message": [
                f"等待信号：{', '.join(task.waiting_for) or signal}",
                "若为审批信号，先向用户确认再 emit。",
            ],
        }

    if task.state == "failed":
        return {
            "task_id": tid,
            "state": task.state,
            "stage": task.stage,
            "action": "decide",
            "agent": "build",
            "artifact": None,
            "command": f'{prefix} cancel {tid} --reason "<reason>"',
            "message": [f"任务失败：{task.error or '未知原因'}",
                        "build 决定取消，或换新 task-id 重新提交；同一 task-id 不能重复 submit。"],
        }

    return {
        "task_id": tid,
        "state": task.state,
        "stage": task.stage,
        "action": "none",
        "agent": None,
        "artifact": None,
        "command": None,
        "message": ["任务处于终态，无需动作。" if task.state in TERMINAL else "无可执行动作。"],
    }


def task_payload(store: Store, task: Task, include_next: bool = True) -> dict:
    data = task.to_dict()
    if include_next:
        data["next"] = build_next(task, store.root, store.db_path)
    return data


def change_result(store: Store, task: Task, **extra: Any) -> dict:
    payload = {
        "ok": True,
        "task_id": task.id,
        "state": task.state,
        "stage": task.stage,
        "next": build_next(task, store.root, store.db_path),
        "task": task_payload(store, task),
    }
    payload.update(extra)
    return payload


def sort_tasks(tasks: Iterable[Task]) -> list[Task]:
    return sorted(
        tasks,
        key=lambda t: (
            -int(t.priority),
            t.deadline is None,
            t.deadline if t.deadline is not None else 0.0,
            t.created_at,
        ),
    )


# ---------------------------------------------------------------------------
# artifact handling
# ---------------------------------------------------------------------------


def resolve_artifact(root: Path, value: str | None) -> Path:
    if value is None or not str(value).strip():
        raise A2AError("--artifact is required")
    raw = Path(str(value).strip())
    if not raw.is_absolute():
        raw = root / raw
    path = ensure_allowed(raw, "artifact")
    read_nonempty(path)
    return path


def canonicalize_artifact(root: Path, task_id: str, key: str, source: Path) -> Path:
    dest = artifact_path(root, task_id, key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != dest.resolve():
        shutil.copyfile(source, dest)
    read_nonempty(dest)
    return dest


def check_artifact_nonempty(path_value: str | None) -> bool:
    if not path_value:
        return False
    try:
        path = Path(path_value)
        return path.is_file() and bool(path.read_bytes().strip())
    except OSError:
        return False


# ---------------------------------------------------------------------------
# state transitions
# ---------------------------------------------------------------------------


def op_submit(store: Store, task_id: str, name: str, goal: str,
              acceptance: Iterable[str] | None, max_rework: int = 2,
              max_retries: int = 0, priority: int = 0,
              deadline_minutes: float | None = None, actor: str = "cli",
              idem_key: str | None = None) -> dict:
    task_id = (task_id or "").strip()
    name = (name or "").strip()
    goal = (goal or "").strip()
    if not task_id:
        raise A2AError("--id is required")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", task_id):
        raise A2AError("--id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    if not name:
        raise A2AError("--name is required")
    if not goal:
        raise A2AError("--goal is required")
    if isinstance(acceptance, str):
        acceptance = [acceptance]
    items = [str(a).strip() for a in (acceptance or []) if str(a).strip()]
    if not items:
        raise A2AError("acceptance requires at least one non-empty --acceptance item")
    warnings: list[str] = []
    if not re.match(r"^a2a-\d{8}-\d{4}-", task_id):
        warnings.append("task id does not follow a2a-YYYYMMDD-HHMM-<slug> (allowed, warning only)")
    max_rework = max(0, int(max_rework))
    max_retries = max(0, int(max_retries))
    deadline = None
    if deadline_minutes is not None and float(deadline_minutes) > 0:
        deadline = now_ts() + float(deadline_minutes) * 60.0

    with store.tx() as conn:
        existing = store.get_task(task_id, conn)
        if existing is not None:
            raise A2AError(f"task already exists: {task_id}", 2, existing.id, existing.state, existing.stage)
        ts = now_ts()
        conn.execute(
            """INSERT INTO tasks
               (id, name, goal, acceptance, stage, state, priority, assignee, artifacts,
                rework_count, max_rework, attempts, max_retries, waiting_for, checkpoint,
                deadline, verdict, summary, error, created_at, updated_at, last_heartbeat, delivered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, name, goal, dump_json(items), "plan", "new", int(priority), None,
                dump_json({}), 0, max_rework, 0, max_retries, dump_json([]), dump_json({}),
                deadline, None, "", None, ts, ts, ts, None,
            ),
        )
        conn.execute("UPDATE tasks SET state='ready' WHERE id=?", (task_id,))
        store.add_event(
            conn, "submit", task_id=task_id, stage="plan", actor=actor,
            detail={"name": name, "goal": goal, "acceptance": items,
                    "state_from": "new", "state_to": "ready", "priority": int(priority)},
            idem_key=idem_key,
        )
        task = store.require_task(task_id, conn)
    with contextlib.suppress(OSError):
        (store.root / "tasks" / task_id).mkdir(parents=True, exist_ok=True)
    return change_result(store, task, warnings=warnings)


def op_claim(store: Store, task_id: str, stage: str, agent: str,
             force: bool = False, actor: str | None = None) -> dict:
    agent = (agent or "").strip()
    if not agent:
        raise A2AError("--agent is required")
    actor = actor or agent
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "ready":
            raise A2AError(
                f"cannot claim {task.id}: state={task.state}, stage={task.stage}; claim requires state=ready",
                2, task.id, task.state, task.stage,
            )
        if stage not in CLAIM_STAGES:
            raise A2AError("claim --stage must be plan, execute or verify (deliver is build's deliver command)",
                           2, task.id, task.state, task.stage)
        if stage != task.stage:
            raise A2AError(
                f"cannot claim {task.id}: stage={task.stage}, requested stage={stage}",
                2, task.id, task.state, task.stage,
            )
        if not force:
            busy = conn.execute(
                "SELECT id FROM tasks WHERE assignee=? AND state='running' AND id<>? LIMIT 1",
                (agent, task.id),
            ).fetchone()
            if busy is not None:
                raise A2AError(
                    f"agent {agent} already has running task {busy['id']}; use --force to override",
                    2, task.id, task.state, task.stage,
                )
        ts = now_ts()
        task.state = "running"
        task.assignee = agent
        task.last_heartbeat = ts
        store.save_task(task, conn)
        store.add_event(
            conn, "claim", task_id=task.id, stage=task.stage, actor=actor,
            detail={"agent": agent, "force": bool(force), "stage": task.stage},
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_done(store: Store, task_id: str, stage: str, artifact: str,
            summary: str = "", actor: str = "cli", idem_key: str | None = None) -> dict:
    if stage == "verify":
        raise A2AError("done is not allowed for the verify stage; use the verify command", 2, task_id)
    if stage not in ("plan", "execute"):
        raise A2AError("done --stage must be plan or execute")
    source = resolve_artifact(store.root, artifact)
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot done {task.id}: state={task.state}, stage={task.stage}; done requires state=running",
                2, task.id, task.state, task.stage,
            )
        if task.stage != stage:
            raise A2AError(
                f"cannot done {task.id}: stage={task.stage}, requested stage={stage}",
                2, task.id, task.state, task.stage,
            )
        key = "plan" if stage == "plan" else "result"
        dest = canonicalize_artifact(store.root, task.id, key, source)
        next_stage = DONE_NEXT[stage]
        task.artifacts[key] = str(dest)
        task.stage = next_stage
        task.state = "ready"
        task.assignee = None
        task.summary = summary or ""
        store.save_task(task, conn)
        store.add_event(
            conn, "done", task_id=task.id, stage=stage, actor=actor,
            detail={"stage": stage, "artifact": str(dest), "summary": summary or "",
                    "next_stage": next_stage},
            idem_key=idem_key,
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_verify(store: Store, task_id: str, verdict: str, artifact: str,
              summary: str = "", actor: str = "cli", idem_key: str | None = None) -> dict:
    verdict = (verdict or "").strip().upper()
    if verdict not in ("PASS", "FAIL"):
        raise A2AError("--verdict must be PASS or FAIL")
    source = resolve_artifact(store.root, artifact)
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running" or task.stage != "verify":
            raise A2AError(
                f"cannot verify {task.id}: state={task.state}, stage={task.stage}; verify requires running/verify",
                2, task.id, task.state, task.stage,
            )
        dest = canonicalize_artifact(store.root, task.id, "verify", source)
        task.artifacts["verify"] = str(dest)
        task.verdict = verdict
        task.summary = summary or task.summary
        task.assignee = None

        if verdict == "PASS":
            task.state = "completed"
            task.stage = "deliver"
            store.save_task(task, conn)
            store.add_event(
                conn, "verify_pass", task_id=task.id, stage="verify", actor=actor,
                detail={"verdict": "PASS", "artifact": str(dest), "summary": summary or ""},
                idem_key=idem_key,
            )
        else:
            task.rework_count += 1
            over = task.rework_count > task.max_rework
            if over:
                task.state = "failed"
                task.error = f"rework limit exceeded ({task.rework_count}/{task.max_rework})"
                task.waiting_for = []
            else:
                task.state = "ready"
                task.stage = "execute"
            store.save_task(task, conn)
            store.add_event(
                conn, "verify_fail", task_id=task.id, stage="verify", actor=actor,
                detail={"verdict": "FAIL", "artifact": str(dest), "summary": summary or "",
                        "rework_count": task.rework_count, "max_rework": task.max_rework},
                idem_key=idem_key,
            )
            if over:
                store.add_event(
                    conn, "fail", task_id=task.id, stage=task.stage, actor=actor,
                    detail={"reason": "rework limit", "rework_count": task.rework_count,
                            "max_rework": task.max_rework},
                )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_deliver(store: Store, task_id: str, summary: str = "", actor: str = "cli",
               idem_key: str | None = None) -> dict:
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "completed" or task.stage != "deliver":
            raise A2AError(
                f"cannot deliver {task.id}: state={task.state}, stage={task.stage}; deliver requires completed/deliver",
                2, task.id, task.state, task.stage,
            )
        if task.verdict != "PASS":
            raise A2AError(f"cannot deliver {task.id}: verdict={task.verdict}; PASS required",
                           2, task.id, task.state, task.stage)
        vpath = task.artifacts.get("verify")
        if not check_artifact_nonempty(vpath):
            raise A2AError(f"cannot deliver {task.id}: verify artifact missing or empty: {vpath}",
                           2, task.id, task.state, task.stage)
        warnings: list[str] = []
        if not check_artifact_nonempty(task.artifacts.get("result")):
            warnings.append("result.md missing or empty (not required for delivery)")
        task.state = "delivered"
        task.delivered_at = now_ts()
        task.assignee = None
        if summary:
            task.summary = summary
        store.save_task(task, conn)
        store.add_event(
            conn, "deliver", task_id=task.id, stage="deliver", actor=actor,
            detail={"verdict": "PASS", "verify": vpath, "summary": summary or "",
                    "warnings": warnings},
            idem_key=idem_key,
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task, warnings=warnings)


def op_fail(store: Store, task_id: str, error: str, actor: str = "cli",
            idem_key: str | None = None) -> dict:
    error = (error or "").strip()
    if not error:
        raise A2AError("--error is required")
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state not in ("running", "waiting"):
            raise A2AError(
                f"cannot fail {task.id}: state={task.state}; fail requires running or waiting",
                2, task.id, task.state, task.stage,
            )
        task.error = error
        task.assignee = None
        store.add_event(
            conn, "fail", task_id=task.id, stage=task.stage, actor=actor,
            detail={"error": error, "attempts": task.attempts, "max_retries": task.max_retries},
            idem_key=idem_key,
        )
        extra: dict[str, Any] = {"retried": False}
        if task.attempts < task.max_retries:
            task.attempts += 1
            task.state = "ready"
            task.waiting_for = []
            store.save_task(task, conn)
            store.add_event(
                conn, "retry", task_id=task.id, stage=task.stage, actor=actor,
                detail={"attempts": task.attempts, "max_retries": task.max_retries},
            )
            extra["retried"] = True
        else:
            task.state = "failed"
            task.waiting_for = []
            store.save_task(task, conn)
        task = store.require_task(task.id, conn)
    return change_result(store, task, **extra)


def op_block(store: Store, task_id: str, waits: Iterable[str] | None,
             reason: str = "", checkpoint: dict | None = None,
             actor: str = "cli", idem_key: str | None = None) -> dict:
    wait_items = parse_wait_list(waits)
    if not wait_items:
        raise A2AError("--wait requires at least one signal name")
    if isinstance(checkpoint, str):
        checkpoint = parse_json_object(checkpoint, "checkpoint")
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot block {task.id}: state={task.state}; block requires state=running",
                2, task.id, task.state, task.stage,
            )
        existing = {
            row["name"] for row in conn.execute(
                "SELECT name FROM signals WHERE name IN (%s)" % ",".join("?" * len(wait_items)),
                tuple(wait_items),
            ).fetchall()
        }
        remaining = [w for w in wait_items if w not in existing]
        satisfied = [w for w in wait_items if w in existing]
        if checkpoint:
            task.checkpoint.update(checkpoint)
        if not remaining:
            store.save_task(task, conn)
            store.add_event(
                conn, "block_skipped", task_id=task.id, stage=task.stage, actor=actor,
                detail={"wait": wait_items, "satisfied": satisfied, "reason": reason or "",
                        "blocked": False},
                idem_key=idem_key,
            )
            task = store.require_task(task.id, conn)
            return change_result(store, task, blocked=False)
        task.state = "waiting"
        for name in remaining:
            if name not in task.waiting_for:
                task.waiting_for.append(name)
        task.assignee = None
        store.save_task(task, conn)
        store.add_event(
            conn, "block", task_id=task.id, stage=task.stage, actor=actor,
            detail={"wait": remaining, "satisfied": satisfied, "reason": reason or "",
                    "blocked": True},
            idem_key=idem_key,
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task, blocked=True)


def op_unblock(store: Store, task_id: str, waits: Iterable[str] | None = None,
               actor: str = "cli") -> dict:
    wait_items = parse_wait_list(waits)
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "waiting":
            raise A2AError(
                f"cannot unblock {task.id}: state={task.state}; unblock requires state=waiting",
                2, task.id, task.state, task.stage,
            )
        if wait_items:
            removed = [w for w in wait_items if w in task.waiting_for]
            task.waiting_for = [w for w in task.waiting_for if w not in wait_items]
        else:
            removed = list(task.waiting_for)
            task.waiting_for = []
        if not task.waiting_for:
            task.state = "ready"
        store.save_task(task, conn)
        store.add_event(
            conn, "unblock", task_id=task.id, stage=task.stage, actor=actor,
            detail={"removed": removed, "remaining": list(task.waiting_for),
                    "state": task.state},
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task, removed=removed)


def op_emit(store: Store, name: str, detail: dict | None = None,
            actor: str = "cli", idem_key: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise A2AError("signal name is required")
    if isinstance(detail, str):
        detail = parse_json_object(detail, "detail")
    detail = detail or {}
    affected: list[dict] = []
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO signals(name, ts, detail) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, detail=excluded.detail",
            (name, now_ts(), dump_json(detail)),
        )
        store.add_event(
            conn, "signal", task_id=None, stage=None, actor=actor,
            detail={"name": name, "detail": detail}, idem_key=idem_key,
        )
        rows = conn.execute("SELECT * FROM tasks WHERE state='waiting' ORDER BY created_at").fetchall()
        for row in rows:
            task = Task.from_row(row)
            if name not in task.waiting_for:
                continue
            task.waiting_for = [w for w in task.waiting_for if w != name]
            if not task.waiting_for:
                task.state = "ready"
            store.save_task(task, conn)
            store.add_event(
                conn, "unblock", task_id=task.id, stage=task.stage, actor=actor,
                detail={"signal": name, "remaining": list(task.waiting_for),
                        "state": task.state},
            )
            affected.append(task_payload(store, task))
    return {"ok": True, "signal": name, "affected": affected}


def op_require_approval(store: Store, task_id: str, action: str, reason: str = "",
                        actor: str = "cli") -> dict:
    action = (action or "").strip()
    if not action:
        raise A2AError("--action is required")
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot require approval for {task.id}: state={task.state}; requires running",
                2, task.id, task.state, task.stage,
            )
        signal = f"approval:{task.id}:{slugify(action)}"
        already = conn.execute("SELECT 1 FROM signals WHERE name=?", (signal,)).fetchone()
        if already is not None:
            store.add_event(
                conn, "approval_precheck", task_id=task.id, stage=task.stage, actor=actor,
                detail={"action": action, "reason": reason or "", "signal": signal,
                        "approved": True, "state": task.state},
            )
            task = store.require_task(task.id, conn)
            return change_result(store, task, approved=True, blocked=False, signal=signal)
        # Pending is not approved: do NOT write the signals table here. Only
        # build's emit (after user confirmation) creates the signal row and
        # wakes the task. A later require-approval for the same action sees no
        # signal and must block again, never auto-approve.
        task.state = "waiting"
        if signal not in task.waiting_for:
            task.waiting_for.append(signal)
        task.assignee = None
        store.save_task(task, conn)
        store.add_event(
            conn, "approval_required", task_id=task.id, stage=task.stage, actor=actor,
            detail={"action": action, "reason": reason or "", "signal": signal,
                    "pending": True},
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task, approved=False, blocked=True, signal=signal)


def op_preempt(store: Store, task_id: str, reason: str = "",
               checkpoint: dict | None = None, actor: str = "cli") -> dict:
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot preempt {task.id}: state={task.state}; preempt requires running",
                2, task.id, task.state, task.stage,
            )
        if checkpoint:
            task.checkpoint.update(checkpoint)
        task.state = "ready"
        task.assignee = None
        store.save_task(task, conn)
        store.add_event(
            conn, "preempt", task_id=task.id, stage=task.stage, actor=actor,
            detail={"reason": reason or "", "checkpoint": task.checkpoint},
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_recover(store: Store, actor: str = "cli") -> dict:
    recovered: list[dict] = []
    with store.tx() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE state='running' ORDER BY created_at").fetchall()
        for row in rows:
            task = Task.from_row(row)
            task.state = "ready"
            task.attempts += 1
            task.assignee = None
            store.save_task(task, conn)
            store.add_event(
                conn, "restart", task_id=task.id, stage=task.stage, actor=actor,
                detail={"attempts": task.attempts, "checkpoint": task.checkpoint,
                        "previous_state": "running"},
            )
            recovered.append(task_payload(store, task))
        active = [
            task_payload(store, t)
            for t in sort_tasks(t for t in store.all_tasks(conn) if t.state not in TERMINAL)
        ]
    return {"ok": True, "recovered": recovered, "active": active}


def op_sweep(store: Store, stale_minutes: float = 30.0, actor: str = "cli") -> dict:
    now = now_ts()
    stale_cutoff = now - float(stale_minutes) * 60.0
    deadline_hits: list[dict] = []
    stale_hits: list[dict] = []
    with store.tx() as conn:
        tasks = store.all_tasks(conn)
        # 1) deadlines first
        for task in tasks:
            if task.state in TERMINAL or task.state == "completed":
                continue
            if task.deadline is None or task.deadline > now:
                continue
            marker = f"deadline:{task.id}"
            if marker in task.waiting_for:
                continue
            previous_state, previous_stage = task.state, task.stage
            task.state = "waiting"
            task.waiting_for.append(marker)
            task.assignee = None
            store.save_task(task, conn)
            store.add_event(
                conn, "deadline", task_id=task.id, stage=task.stage, actor=actor,
                detail={"previous_state": previous_state, "previous_stage": previous_stage,
                        "deadline": task.deadline},
            )
            deadline_hits.append(task_payload(store, task))
        # 2) stale running tasks
        for task in tasks:
            if task.state != "running":
                continue
            if task.last_heartbeat >= stale_cutoff:
                continue
            task.state = "ready"
            task.assignee = None
            store.save_task(task, conn)
            store.add_event(
                conn, "preempt", task_id=task.id, stage=task.stage, actor=actor,
                detail={"reason": "stale", "stale_minutes": float(stale_minutes),
                        "last_heartbeat": task.last_heartbeat, "now": now},
            )
            stale_hits.append(task_payload(store, task))
    return {
        "ok": True,
        "stale": stale_hits,
        "deadline": deadline_hits,
        "counts": {"stale": len(stale_hits), "deadline": len(deadline_hits)},
    }


def op_heartbeat(store: Store, task_id: str, actor: str = "cli") -> dict:
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot heartbeat {task.id}: state={task.state}; requires running",
                2, task.id, task.state, task.stage,
            )
        task.last_heartbeat = now_ts()
        store.save_task(task, conn)
    return change_result(store, task)


def op_checkpoint(store: Store, task_id: str, data: dict | None = None,
                  actor: str = "cli") -> dict:
    data = data or {}
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state != "running":
            raise A2AError(
                f"cannot checkpoint {task.id}: state={task.state}; requires running",
                2, task.id, task.state, task.stage,
            )
        task.checkpoint = dict(data)
        store.save_task(task, conn)
        store.add_event(
            conn, "checkpoint", task_id=task.id, stage=task.stage, actor=actor,
            detail={"checkpoint": task.checkpoint},
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_extend(store: Store, task_id: str, deadline_minutes: float, actor: str = "cli") -> dict:
    try:
        minutes = float(deadline_minutes)
    except (TypeError, ValueError):
        raise A2AError("--deadline-minutes must be a number")
    if minutes < 0:
        raise A2AError("--deadline-minutes must be >= 0 (0 clears the deadline)")
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state in TERMINAL:
            raise A2AError(
                f"cannot extend {task.id}: state={task.state} is terminal",
                2, task.id, task.state, task.stage,
            )
        task.deadline = None if minutes == 0 else now_ts() + minutes * 60.0
        marker = f"deadline:{task.id}"
        task.waiting_for = [w for w in task.waiting_for if w != marker]
        cleared = False
        if task.state == "waiting" and not task.waiting_for:
            task.state = "ready"
            cleared = True
        store.save_task(task, conn)
        store.add_event(
            conn, "extend", task_id=task.id, stage=task.stage, actor=actor,
            detail={"deadline": task.deadline, "deadline_minutes": minutes},
        )
        if cleared:
            store.add_event(
                conn, "unblock", task_id=task.id, stage=task.stage, actor=actor,
                detail={"reason": "extend", "remaining": []},
            )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


def op_cancel(store: Store, task_id: str, reason: str = "", actor: str = "cli",
              idem_key: str | None = None) -> dict:
    with store.tx() as conn:
        task = store.require_task(task_id, conn)
        if task.state in TERMINAL:
            raise A2AError(
                f"cannot cancel {task.id}: state={task.state} is terminal",
                2, task.id, task.state, task.stage,
            )
        previous_state = task.state
        task.state = "cancelled"
        task.assignee = None
        task.waiting_for = []
        store.save_task(task, conn)
        store.add_event(
            conn, "cancel", task_id=task.id, stage=task.stage, actor=actor,
            detail={"reason": reason or "", "previous_state": previous_state},
            idem_key=idem_key,
        )
        task = store.require_task(task.id, conn)
    return change_result(store, task)


# ---------------------------------------------------------------------------
# read operations
# ---------------------------------------------------------------------------


def op_status(store: Store, task_id: str | None = None) -> Any:
    if task_id:
        task = store.require_task(task_id)
        data = task_payload(store, task)
        data["artifacts_exist"] = {
            key: check_artifact_nonempty(value) for key, value in task.artifacts.items()
        }
        data["deadline_expired"] = bool(task.deadline is not None and task.deadline <= now_ts())
        return data
    return [task_payload(store, t) for t in sort_tasks(store.all_tasks())]


def op_list(store: Store, state: str | None = None, stage: str | None = None) -> list[dict]:
    tasks = store.all_tasks()
    if state:
        tasks = [t for t in tasks if t.state == state]
    if stage:
        tasks = [t for t in tasks if t.stage == stage]
    return [task_payload(store, t) for t in sort_tasks(tasks)]


def op_next(store: Store, task_id: str | None = None) -> dict:
    if task_id:
        task = store.require_task(task_id)
    else:
        candidates = [t for t in store.all_tasks() if t.state not in TERMINAL]
        if not candidates:
            raise A2AError("no active tasks", 1)
        task = sort_tasks(candidates)[0]
    return {
        "ok": True,
        "task_id": task.id,
        "state": task.state,
        "stage": task.stage,
        "next": build_next(task, store.root, store.db_path),
        "task": task_payload(store, task),
    }


def op_events(store: Store, task_id: str | None = None, limit: int = 1000) -> list[dict]:
    conn = store.conn
    if task_id:
        store.require_task(task_id)
        rows = conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY seq ASC LIMIT ?",
            (task_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY seq ASC LIMIT ?", (int(limit),)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["detail"] = load_json(item.get("detail"), {})
        result.append(item)
    return result


def op_signals(store: Store) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM signals ORDER BY name").fetchall()
    return [{"name": r["name"], "ts": r["ts"], "detail": load_json(r["detail"], {})} for r in rows]


def op_stats(store: Store) -> dict:
    by_state: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    rework = 0
    tasks = store.all_tasks()
    for task in tasks:
        by_state[task.state] = by_state.get(task.state, 0) + 1
        by_stage[task.stage] = by_stage.get(task.stage, 0) + 1
        rework += task.rework_count
    event_count = store.conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
    signal_count = store.conn.execute("SELECT count(*) AS n FROM signals").fetchone()["n"]
    inbox = store.root / "inbox"
    processed = len([p for p in (inbox / "processed").glob("*.json")]) if (inbox / "processed").is_dir() else 0
    failed = len([p for p in (inbox / "failed").glob("*.json")]) if (inbox / "failed").is_dir() else 0
    return {
        "ok": True,
        "tasks": len(tasks),
        "by_state": by_state,
        "by_stage": by_stage,
        "rework_total": rework,
        "events": event_count,
        "signals": signal_count,
        "inbox": {"processed": processed, "failed": failed},
    }


def op_wait(store: Store, task_id: str, timeout: float = 30.0, interval: float = 0.2) -> int:
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        task = store.get_task(task_id)
        if task is None:
            print(f"task not found: {task_id}")
            return 1
        if task.state == "completed":
            print(f"{task.id}: completed stage={task.stage}")
            return 0
        if task.state == "delivered":
            print(f"{task.id}: delivered")
            return 0
        if task.state == "failed":
            print(f"{task.id}: failed {task.error or ''}".rstrip())
            return 1
        if task.state == "cancelled":
            print(f"{task.id}: cancelled")
            return 2
        if time.time() >= deadline:
            print(f"{task.id}: timeout state={task.state} stage={task.stage}")
            return 3
        time.sleep(max(0.05, float(interval)))


def op_watch(store: Store, task_id: str | None = None, interval: float = 0.5) -> int:
    last_seq = store.conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM events").fetchone()["m"]
    while True:
        if task_id:
            rows = store.conn.execute(
                "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq ASC",
                (task_id, last_seq),
            ).fetchall()
        else:
            rows = store.conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq ASC", (last_seq,)
            ).fetchall()
        for row in rows:
            last_seq = row["seq"]
            detail = load_json(row["detail"], {})
            print(f"[{fmt_ts(row['ts'])}] {row['seq']} {row['kind']} "
                  f"{row['task_id'] or '-'} {dump_json(detail)}")
        if task_id:
            task = store.get_task(task_id)
            if task is None:
                print(f"task not found: {task_id}")
                return 1
            if task.state == "completed":
                return 0
            if task.state == "delivered":
                return 0
            if task.state == "failed":
                return 1
            if task.state == "cancelled":
                return 2
        time.sleep(max(0.1, float(interval)))


def op_board(store: Store) -> Path:
    tasks = sort_tasks(store.all_tasks())
    running = [t for t in tasks if t.state == "running"]
    waiting = [t for t in tasks if t.state == "waiting"]
    recent = [t for t in tasks if t.state in ("completed", "delivered")]
    recent = sorted(recent, key=lambda t: t.updated_at, reverse=True)[:10]

    def row(t: Task) -> str:
        return (f"| {t.id} | {t.stage} | {t.state} | {t.assignee or '-'} | "
                f"{fmt_deadline(t.deadline)} | {t.summary or ''} |")

    header = "| id | stage | state | assignee | deadline | summary |\n|---|---|---|---|---|---|"
    lines = ["# A2A BOARD", "", f"Updated: {fmt_ts(now_ts())}", ""]
    lines += ["## 进行中 (running)", "", header]
    lines += [row(t) for t in running] or ["| - | - | - | - | - | - |"]
    lines += ["", "## 等待 (waiting)", "", header]
    lines += [row(t) for t in waiting] or ["| - | - | - | - | - | - |"]
    lines += ["", "## 最近完成 (completed/delivered)", "", header]
    lines += [row(t) for t in recent] or ["| - | - | - | - | - | - |"]
    lines.append("")
    path = store.root / "BOARD.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# inbox pump
# ---------------------------------------------------------------------------


def _unique_dest(directory: Path, filename: str) -> Path:
    dest = directory / filename
    if not dest.exists():
        return dest
    stem, suffix = Path(filename).stem, Path(filename).suffix
    return directory / f"{stem}.{int(time.time())}{suffix}"


def _move_file(src: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(directory, src.name)
    shutil.move(str(src), str(dest))
    return dest


def _inbox_dispatch(store: Store, payload: dict, actor: str, idem_key: str) -> dict:
    action = str(payload.get("action") or "").strip().lower()
    if action not in INBOX_ACTIONS:
        raise A2AError(f"unknown inbox action: {action!r}")
    task_id = payload.get("task_id") or payload.get("id")
    if action == "submit":
        return op_submit(
            store,
            task_id=task_id,
            name=payload.get("name"),
            goal=payload.get("goal"),
            acceptance=payload.get("acceptance"),
            max_rework=payload.get("max_rework", 2),
            max_retries=payload.get("max_retries", 0),
            priority=payload.get("priority", 0),
            deadline_minutes=payload.get("deadline_minutes"),
            actor=actor,
            idem_key=idem_key,
        )
    if action == "emit":
        return op_emit(store, payload.get("name"), payload.get("detail") or {},
                       actor=actor, idem_key=idem_key)
    if action == "done":
        return op_done(store, task_id, payload.get("stage"), payload.get("artifact"),
                       payload.get("summary", ""), actor=actor, idem_key=idem_key)
    if action == "fail":
        return op_fail(store, task_id, payload.get("error"), actor=actor, idem_key=idem_key)
    if action == "verify":
        return op_verify(store, task_id, payload.get("verdict"), payload.get("artifact"),
                         payload.get("summary", ""), actor=actor, idem_key=idem_key)
    if action == "block":
        return op_block(store, task_id, payload.get("wait"), payload.get("reason", ""),
                        payload.get("checkpoint"), actor=actor, idem_key=idem_key)
    if action == "cancel":
        return op_cancel(store, task_id, payload.get("reason", ""), actor=actor,
                         idem_key=idem_key)
    raise A2AError(f"unsupported inbox action: {action}")


def op_pump(store: Store) -> dict:
    inbox = store.root / "inbox"
    processed_dir = inbox / "processed"
    failed_dir = inbox / "failed"
    for directory in (inbox, processed_dir, failed_dir):
        directory.mkdir(parents=True, exist_ok=True)

    processed = skipped = failed = 0
    files: list[dict] = []
    for path in sorted(inbox.glob("*.json")):
        if not path.is_file():
            continue
        filename = path.name
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            idem_key = f"inbox:{filename}:{digest}"
            exists = store.conn.execute(
                "SELECT 1 FROM events WHERE idem_key=? LIMIT 1", (idem_key,)
            ).fetchone()
            if exists is not None:
                _move_file(path, processed_dir)
                skipped += 1
                files.append({"file": filename, "status": "skipped", "reason": "idempotency key already applied"})
                continue
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise A2AError(f"invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise A2AError("inbox payload must be a JSON object")
            result = _inbox_dispatch(store, payload, f"inbox:{filename}", idem_key)
            _move_file(path, processed_dir)
            processed += 1
            files.append({"file": filename, "status": "processed",
                          "task_id": result.get("task_id") or result.get("signal")})
        except Exception as exc:  # bad file must not stop the pump
            with contextlib.suppress(Exception):
                _move_file(path, failed_dir)
            with contextlib.suppress(Exception):
                with store.tx() as conn:
                    store.add_event(
                        conn, "inbox_failed", task_id=None, stage=None,
                        actor=f"inbox:{filename}",
                        detail={"filename": filename, "error": f"{type(exc).__name__}: {exc}"},
                    )
            failed += 1
            files.append({"file": filename, "status": "failed", "error": str(exc)})
    return {"ok": True, "processed": processed, "skipped": skipped, "failed": failed,
            "files": files}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A2A v2 persistent task state machine (stdlib only). "
                    "--db/--actor may appear before or after the subcommand.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--actor", default="cli", help="event actor name")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text)

    p = add("submit", "create a task (ready/plan)")
    p.add_argument("id", metavar="ID", nargs="?", default=None)
    p.add_argument("--id", dest="id_opt")
    p.add_argument("--name", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--acceptance", action="append", default=[])
    p.add_argument("--max-rework", type=int, default=2)
    p.add_argument("--max-retries", type=int, default=0)
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--deadline-minutes", type=float, default=None)

    p = add("claim", "claim the current stage (ready -> running)")
    p.add_argument("id")
    p.add_argument("--stage", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--force", action="store_true")

    p = add("done", "complete plan/execute stage with an artifact")
    p.add_argument("id")
    p.add_argument("--stage", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--summary", default="")

    p = add("verify", "record verify verdict (running/verify)")
    p.add_argument("id")
    p.add_argument("--verdict", required=True)
    p.add_argument("--artifact", required=True)
    p.add_argument("--summary", default="")

    p = add("deliver", "deliver a verified task (completed -> delivered)")
    p.add_argument("id")
    p.add_argument("--summary", default="")

    p = add("fail", "fail a running/waiting task (auto retry if allowed)")
    p.add_argument("id")
    p.add_argument("--error", required=True)

    p = add("block", "block a running task on signals")
    p.add_argument("id")
    p.add_argument("--wait", action="append", default=[])
    p.add_argument("--reason", default="")
    p.add_argument("--checkpoint", default=None)

    p = add("unblock", "remove waiting signals (all or a subset)")
    p.add_argument("id")
    p.add_argument("--wait", action="append", default=[])

    p = add("emit", "persist a signal and wake matching waiters")
    p.add_argument("name")
    p.add_argument("--detail", default=None)

    p = add("require-approval", "block on approval:<id>:<action>")
    p.add_argument("id")
    p.add_argument("--action", required=True)
    p.add_argument("--reason", default="")

    p = add("preempt", "return a running task to ready, keeping checkpoint")
    p.add_argument("id")
    p.add_argument("--reason", default="")
    p.add_argument("--checkpoint", default=None)

    p = add("recover", "restart recovery: all running -> ready, attempts+1")
    p.add_argument("--json", action="store_true")

    p = add("sweep", "expire deadlines and reset stale running tasks")
    p.add_argument("--stale-minutes", type=float, default=30.0)
    p.add_argument("--json", action="store_true")

    p = add("heartbeat", "refresh last_heartbeat for a running task")
    p.add_argument("id")

    p = add("checkpoint", "store a JSON checkpoint for a running task")
    p.add_argument("id")
    p.add_argument("--data", default=None)
    p.add_argument("--file", default=None)

    p = add("extend", "set or clear a task deadline")
    p.add_argument("id")
    p.add_argument("--deadline-minutes", type=float, required=True)

    p = add("cancel", "cancel a non-terminal task")
    p.add_argument("id")
    p.add_argument("--reason", default="")

    p = add("status", "show one task or all tasks")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")

    p = add("list", "list tasks")
    p.add_argument("--state", default=None)
    p.add_argument("--stage", default=None)
    p.add_argument("--json", action="store_true")

    p = add("next", "show the next agent/action/artifact")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")

    p = add("events", "replay events in seq order")
    p.add_argument("id", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=1000)

    p = add("signals", "list persisted signals")
    p.add_argument("--json", action="store_true")

    p = add("stats", "counts and inbox status")
    p.add_argument("--json", action="store_true")

    p = add("wait", "wait for a terminal state")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--interval", type=float, default=0.2)

    p = add("watch", "tail events until terminal state")
    p.add_argument("id", nargs="?")
    p.add_argument("--interval", type=float, default=0.5)

    p = add("pump", "process inbox/*.json exactly once")
    p.add_argument("--json", action="store_true")

    add("board", "write BOARD.md")
    return parser


def extract_global(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Pull --db/--actor out of argv so they work before or after the command."""
    db = None
    actor = None
    out: list[str] = []
    i = 0
    while i < len(argv):
        item = argv[i]
        if item == "--db":
            if i + 1 >= len(argv):
                raise A2AError("--db requires a value")
            db = argv[i + 1]
            i += 2
            continue
        if item.startswith("--db="):
            db = item.split("=", 1)[1]
            i += 1
            continue
        if item == "--actor":
            if i + 1 >= len(argv):
                raise A2AError("--actor requires a value")
            actor = argv[i + 1]
            i += 2
            continue
        if item.startswith("--actor="):
            actor = item.split("=", 1)[1]
            i += 1
            continue
        out.append(item)
        i += 1
    return db, actor, out


def _human_status(data: Any) -> None:
    if isinstance(data, list):
        if not data:
            print("no tasks")
            return
        print(f"{'id':<36} {'state':<10} {'stage':<8} {'assignee':<9} {'rework':<6} deadline")
        for t in data:
            print(f"{t['id']:<36} {t['state']:<10} {t['stage']:<8} "
                  f"{t['assignee'] or '-':<9} {t['rework_count']}/{t['max_rework']:<4} "
                  f"{fmt_deadline(t['deadline'])}")
        return
    print(f"id:        {data['id']}")
    print(f"name:      {data['name']}")
    print(f"goal:      {data['goal']}")
    print(f"state:     {data['state']}")
    print(f"stage:     {data['stage']}")
    print(f"assignee:  {data['assignee'] or '-'}")
    print(f"rework:    {data['rework_count']}/{data['max_rework']}")
    print(f"attempts:  {data['attempts']}/{data['max_retries']}")
    print(f"deadline:  {fmt_deadline(data['deadline'])}")
    print(f"artifacts: {dump_json(data['artifacts'])}")
    print("acceptance:")
    for item in data["acceptance"]:
        print(f"  - {item}")
    nxt = data.get("next") or {}
    print(f"next:      {nxt.get('action')} ({nxt.get('agent') or '-'})")
    if nxt.get("command"):
        print(f"command:   {nxt['command']}")


def _human_events(events: list[dict]) -> None:
    for e in events:
        print(f"{e['seq']:>6}  {fmt_ts(e['ts'])}  {e['kind']:<18} "
              f"{e['task_id'] or '-':<36} {e['stage'] or '-':<8} {dump_json(e['detail'])}")


def _human_next(data: dict) -> None:
    nxt = data["next"]
    print(f"task:    {data['task_id']}")
    print(f"state:   {data['state']}")
    print(f"stage:   {data['stage']}")
    print(f"action:  {nxt['action']}")
    print(f"agent:   {nxt['agent'] or '-'}")
    print(f"artifact:{(' ' + nxt['artifact']) if nxt.get('artifact') else ' -'}")
    if nxt.get("command"):
        print(f"command: {nxt['command']}")
    for line in nxt.get("message") or []:
        print(f"  {line}")


def dispatch(store: Store, args: argparse.Namespace, actor: str) -> tuple[Any, int]:
    cmd = args.command
    if cmd == "submit":
        task_id = args.id_opt or args.id
        return op_submit(store, task_id, args.name, args.goal, args.acceptance,
                         args.max_rework, args.max_retries, args.priority,
                         args.deadline_minutes, actor=actor), 0
    if cmd == "claim":
        return op_claim(store, args.id, args.stage, args.agent, args.force, actor=actor), 0
    if cmd == "done":
        return op_done(store, args.id, args.stage, args.artifact, args.summary, actor=actor), 0
    if cmd == "verify":
        return op_verify(store, args.id, args.verdict, args.artifact, args.summary, actor=actor), 0
    if cmd == "deliver":
        return op_deliver(store, args.id, args.summary, actor=actor), 0
    if cmd == "fail":
        return op_fail(store, args.id, args.error, actor=actor), 0
    if cmd == "block":
        checkpoint = parse_json_object(args.checkpoint, "--checkpoint")
        return op_block(store, args.id, args.wait, args.reason, checkpoint, actor=actor), 0
    if cmd == "unblock":
        return op_unblock(store, args.id, args.wait, actor=actor), 0
    if cmd == "emit":
        detail = parse_json_object(args.detail, "--detail")
        return op_emit(store, args.name, detail, actor=actor), 0
    if cmd == "require-approval":
        return op_require_approval(store, args.id, args.action, args.reason, actor=actor), 0
    if cmd == "preempt":
        checkpoint = parse_json_object(args.checkpoint, "--checkpoint")
        return op_preempt(store, args.id, args.reason, checkpoint, actor=actor), 0
    if cmd == "recover":
        return op_recover(store, actor=actor), 0
    if cmd == "sweep":
        return op_sweep(store, args.stale_minutes, actor=actor), 0
    if cmd == "heartbeat":
        return op_heartbeat(store, args.id, actor=actor), 0
    if cmd == "checkpoint":
        if args.file:
            path = ensure_allowed(Path(args.file), "--file")
            if not path.is_file():
                raise A2AError(f"--file not found: {path}")
            data = parse_json_object(path.read_text(encoding="utf-8"), "--file")
        elif args.data is not None:
            data = parse_json_object(args.data, "--data")
        else:
            raise A2AError("checkpoint requires --data JSON or --file PATH")
        return op_checkpoint(store, args.id, data, actor=actor), 0
    if cmd == "extend":
        return op_extend(store, args.id, args.deadline_minutes, actor=actor), 0
    if cmd == "cancel":
        return op_cancel(store, args.id, args.reason, actor=actor), 0
    if cmd == "status":
        data = op_status(store, args.id)
        if args.json:
            print_json(data)
        else:
            _human_status(data)
        return None, 0
    if cmd == "list":
        data = op_list(store, args.state, args.stage)
        if args.json:
            print_json(data)
        else:
            _human_status(data)
        return None, 0
    if cmd == "next":
        data = op_next(store, args.id)
        if args.json:
            print_json(data)
        else:
            _human_next(data)
        return None, 0
    if cmd == "events":
        data = op_events(store, args.id, args.limit)
        if args.json:
            print_json(data)
        else:
            _human_events(data)
        return None, 0
    if cmd == "signals":
        data = op_signals(store)
        if args.json:
            print_json(data)
        else:
            if not data:
                print("no signals")
            for signal in data:
                print(f"{signal['name']}  {fmt_ts(signal['ts'])}  {dump_json(signal['detail'])}")
        return None, 0
    if cmd == "stats":
        data = op_stats(store)
        if args.json:
            print_json(data)
        else:
            print(f"tasks:    {data['tasks']}")
            print(f"states:   {dump_json(data['by_state'])}")
            print(f"stages:   {dump_json(data['by_stage'])}")
            print(f"rework:   {data['rework_total']}")
            print(f"events:   {data['events']}")
            print(f"signals:  {data['signals']}")
            print(f"inbox:    processed={data['inbox']['processed']} failed={data['inbox']['failed']}")
        return None, 0
    if cmd == "wait":
        return None, op_wait(store, args.id, args.timeout, args.interval)
    if cmd == "watch":
        return None, op_watch(store, args.id, args.interval)
    if cmd == "pump":
        return op_pump(store), 0
    if cmd == "board":
        path = op_board(store)
        print(path)
        return None, 0
    raise A2AError(f"unknown command: {cmd}")


def print_json(value: Any) -> None:
    # ASCII-escaped JSON keeps stdout parseable under non-UTF-8 console code pages
    # (e.g. gb2312); every parser decodes \uXXXX correctly.
    print(json.dumps(value, ensure_ascii=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    db_path = None
    actor = "cli"
    store: Store | None = None
    try:
        db_override, actor_override, cleaned = extract_global(argv)
        parser = build_parser()
        args = parser.parse_args(cleaned)
        if not getattr(args, "command", None):
            parser.print_help()
            return 0
        db_path = db_override or getattr(args, "db", None) or DEFAULT_DB
        actor = actor_override or getattr(args, "actor", None) or "cli"
        store = Store(db_path)
        data, code = dispatch(store, args, actor)
        if data is not None:
            print_json(data)
        return code
    except A2AError as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "task_id": exc.task_id,
            "state": exc.state,
            "stage": exc.stage,
        }, ensure_ascii=True), file=sys.stderr)
        return exc.code
    except SystemExit as exc:  # argparse
        return int(exc.code or 0)
    except Exception as exc:  # unexpected: keep machine-readable stderr
        print(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "task_id": None,
            "state": None,
            "stage": None,
        }, ensure_ascii=True), file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


def cli() -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return main()


if __name__ == "__main__":
    sys.exit(cli())
