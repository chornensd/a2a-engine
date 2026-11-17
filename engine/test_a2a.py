# -*- coding: utf-8 -*-
"""Unit tests for a2a.py (19 cases).

Run from anywhere:
    python -m unittest discover -s engine -v

Every test uses an isolated root under .test-tmp so
production tasks are never touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import time
import unittest
import uuid
from pathlib import Path

import a2a

TESTS_ROOT = Path(__file__).resolve().parents[1] / ".test-tmp"


class A2ATestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TESTS_ROOT / f"t-{uuid.uuid4().hex[:10]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = a2a.main(["--db", str(self.db), *[str(a) for a in args]])
            except SystemExit as exc:  # pragma: no cover - argparse only
                code = int(exc.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def jrun(self, *args):
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 0, f"command failed ({code}): {args}\n{err}")
        return json.loads(out)

    def status(self, task_id):
        return self.jrun("status", task_id, "--json")

    def write_artifact(self, task_id, name="plan.md", text="content"):
        directory = self.root / "tasks" / task_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def sql(self, query, params=()):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute(query, params)
            conn.commit()
        finally:
            conn.close()

    def make_task(self, task_id, *extra):
        return self.jrun(
            "submit", "--id", task_id, "--name", task_id, "--goal", "goal",
            "--acceptance", "criterion", *extra,
        )

    def advance_to_verify(self, task_id, *extra):
        self.make_task(task_id, *extra)
        self.jrun("claim", task_id, "--stage", "plan", "--agent", "plan")
        plan = self.write_artifact(task_id, "plan.md", "plan body")
        self.jrun("done", task_id, "--stage", "plan", "--artifact", plan, "--summary", "plan")
        self.jrun("claim", task_id, "--stage", "execute", "--agent", "executor")
        result = self.write_artifact(task_id, "result.md", "result body")
        self.jrun("done", task_id, "--stage", "execute", "--artifact", result, "--summary", "result")
        self.jrun("claim", task_id, "--stage", "verify", "--agent", "correct")

    def event_kinds(self, task_id):
        return [e["kind"] for e in self.jrun("events", task_id, "--json")]

    # -- 1 ----------------------------------------------------------------
    def test_submit_ready_plan_and_acceptance(self):
        self.jrun("submit", "--id", "c1", "--name", "c1", "--goal", "engine smoke",
                  "--acceptance", "submit ok", "--acceptance", "status ok")
        task = self.status("c1")
        self.assertEqual(task["state"], "ready")
        self.assertEqual(task["stage"], "plan")
        self.assertEqual(len(task["acceptance"]), 2)
        self.assertEqual(task["next"]["agent"], "plan")

    # -- 2 ----------------------------------------------------------------
    def test_done_before_claim_rejected(self):
        self.make_task("c2")
        plan = self.write_artifact("c2", "plan.md")
        code, out, err = self.run_cli("done", "c2", "--stage", "plan", "--artifact", plan, "--summary", "x")
        self.assertEqual(code, 2)
        task = self.status("c2")
        self.assertEqual((task["state"], task["stage"]), ("ready", "plan"))
        self.assertIn("ready", err)
        self.assertEqual(self.event_kinds("c2"), ["submit"])

    # -- 3 ----------------------------------------------------------------
    def test_full_pipeline_event_order(self):
        self.advance_to_verify("c3")
        verify = self.write_artifact("c3", "verify.md", "PASS")
        self.jrun("verify", "c3", "--verdict", "PASS", "--artifact", verify, "--summary", "pass")
        self.jrun("deliver", "c3", "--summary", "delivered")
        task = self.status("c3")
        self.assertEqual(task["state"], "delivered")
        kinds = self.event_kinds("c3")
        needed = ["submit", "claim", "done", "done", "verify_pass", "deliver"]
        index = -1
        for kind in needed:
            try:
                index = kinds.index(kind, index + 1)
            except ValueError:
                self.fail(f"missing or out of order event: {kind} in {kinds}")
        code, out, err = self.run_cli("wait", "c3", "--timeout", "1")
        self.assertEqual(code, 0, "waiting on a delivered task must be success")

    # -- 4 ----------------------------------------------------------------
    def test_deliver_before_pass_rejected(self):
        self.advance_to_verify("c4")
        before = self.status("c4")
        code, out, err = self.run_cli("deliver", "c4")
        self.assertEqual(code, 2)
        after = self.status("c4")
        self.assertEqual((after["state"], after["stage"]), (before["state"], before["stage"]))
        self.assertEqual(after["state"], "running")

    # -- 5 ----------------------------------------------------------------
    def test_verify_fail_rework_then_limit(self):
        self.advance_to_verify("c5", "--max-rework", "1")
        verify = self.write_artifact("c5", "verify.md", "FAIL-1")
        self.jrun("verify", "c5", "--verdict", "FAIL", "--artifact", verify, "--summary", "fail1")
        task = self.status("c5")
        self.assertEqual((task["state"], task["stage"], task["rework_count"]),
                         ("ready", "execute", 1))

        self.jrun("claim", "c5", "--stage", "execute", "--agent", "executor")
        result = self.write_artifact("c5", "result.md", "result-2")
        self.jrun("done", "c5", "--stage", "execute", "--artifact", result, "--summary", "x")
        self.jrun("claim", "c5", "--stage", "verify", "--agent", "correct")
        verify2 = self.write_artifact("c5", "verify.md", "FAIL-2")
        self.jrun("verify", "c5", "--verdict", "FAIL", "--artifact", verify2, "--summary", "fail2")
        task = self.status("c5")
        self.assertEqual((task["state"], task["rework_count"]), ("failed", 2))
        kinds = self.event_kinds("c5")
        self.assertIn("fail", kinds)
        self.assertGreaterEqual(kinds.count("verify_fail"), 2)

    # -- 6 ----------------------------------------------------------------
    def test_recover_keeps_checkpoint_and_increments_attempts(self):
        self.make_task("c6")
        self.jrun("claim", "c6", "--stage", "plan", "--agent", "plan")
        code, out, err = self.run_cli("checkpoint", "c6")
        self.assertEqual(code, 2, "checkpoint without --data/--file must not wipe state")
        self.assertIn("--data", err)
        self.jrun("checkpoint", "c6", "--data", '{"step": 2}')
        recovered = self.jrun("recover", "--json")
        task = self.status("c6")
        self.assertEqual((task["state"], task["stage"]), ("ready", "plan"))
        self.assertEqual(task["attempts"], 1)
        self.assertEqual(task["checkpoint"]["step"], 2)
        self.assertIn("restart", self.event_kinds("c6"))
        active = [t for t in recovered["active"] if t["id"] == "c6"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["next"]["agent"], "plan")

    # -- 7 ----------------------------------------------------------------
    def test_block_emit_and_presignal(self):
        self.make_task("c7")
        self.jrun("claim", "c7", "--stage", "plan", "--agent", "plan")
        self.jrun("block", "c7", "--wait", "approval:c7:deploy", "--reason", "need user")
        task = self.status("c7")
        self.assertEqual(task["state"], "waiting")
        self.assertEqual(task["waiting_for"], ["approval:c7:deploy"])
        self.jrun("emit", "approval:c7:deploy")
        task = self.status("c7")
        self.assertEqual(task["state"], "ready")
        self.assertEqual(task["waiting_for"], [])

        self.jrun("claim", "c7", "--stage", "plan", "--agent", "plan")
        self.jrun("emit", "approval:c7:pre")
        self.jrun("block", "c7", "--wait", "approval:c7:pre", "--reason", "pre")
        task = self.status("c7")
        self.assertEqual(task["state"], "running")
        self.assertIn("block_skipped", self.event_kinds("c7"))

    # -- 8 ----------------------------------------------------------------
    def test_sweep_only_stale_running(self):
        self.make_task("c8a")
        self.jrun("claim", "c8a", "--stage", "plan", "--agent", "plan")
        self.make_task("c8b")
        self.jrun("claim", "c8b", "--stage", "plan", "--agent", "executor")
        self.sql("UPDATE tasks SET last_heartbeat=? WHERE id=?",
                 (time.time() - 120, "c8b"))
        self.jrun("sweep", "--stale-minutes", "1")
        self.assertEqual(self.status("c8a")["state"], "running")
        self.assertEqual(self.status("c8b")["state"], "ready")
        self.jrun("heartbeat", "c8a")
        self.assertEqual(self.status("c8a")["state"], "running")

    # -- 9 ----------------------------------------------------------------
    def test_inbox_once_and_bad_json(self):
        inbox = self.root / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "01-submit.json").write_text(json.dumps({
            "action": "submit", "task_id": "c9", "name": "c9", "goal": "inbox",
            "acceptance": ["a"],
        }), encoding="utf-8")
        self.jrun("pump")
        self.assertTrue((inbox / "processed" / "01-submit.json").exists())

        self.jrun("claim", "c9", "--stage", "plan", "--agent", "plan")
        plan = self.write_artifact("c9", "plan.md")
        (inbox / "02-emit.json").write_text(json.dumps({
            "action": "emit", "name": "approval:c9:go",
        }), encoding="utf-8")
        (inbox / "03-done.json").write_text(json.dumps({
            "action": "done", "task_id": "c9", "stage": "plan",
            "artifact": str(plan), "summary": "done",
        }), encoding="utf-8")
        self.jrun("pump")
        self.assertTrue((inbox / "processed" / "02-emit.json").exists())
        self.assertTrue((inbox / "processed" / "03-done.json").exists())
        self.assertEqual(self.event_kinds("c9").count("done"), 1)

        shutil.copyfile(inbox / "processed" / "01-submit.json", inbox / "01-submit.json")
        result = self.jrun("pump")
        self.assertEqual(result["skipped"], 1)
        tasks = [t for t in self.jrun("list", "--json") if t["id"] == "c9"]
        self.assertEqual(len(tasks), 1)

        (inbox / "bad.json").write_text("{bad json", encoding="utf-8")
        self.jrun("pump")
        self.assertTrue((inbox / "failed" / "bad.json").exists())

    # -- 10 ---------------------------------------------------------------
    def test_duplicate_submit_rejected(self):
        self.make_task("c10")
        code, out, err = self.run_cli("submit", "--id", "c10", "--name", "c10",
                                  "--goal", "g", "--acceptance", "a")
        self.assertEqual(code, 2)
        self.assertEqual(len([t for t in self.jrun("list", "--json") if t["id"] == "c10"]), 1)
        self.assertEqual(self.event_kinds("c10").count("submit"), 1)

    # -- 11 ---------------------------------------------------------------
    def test_illegal_transition_delivered_cancel(self):
        self.advance_to_verify("c11")
        verify = self.write_artifact("c11", "verify.md", "PASS")
        self.jrun("verify", "c11", "--verdict", "PASS", "--artifact", verify, "--summary", "pass")
        self.jrun("deliver", "c11")
        code, out, err = self.run_cli("cancel", "c11", "--reason", "late")
        self.assertEqual(code, 2)
        self.assertEqual(self.status("c11")["state"], "delivered")

    # -- 12 ---------------------------------------------------------------
    def test_fail_retry_and_limit(self):
        self.jrun("submit", "--id", "c12", "--name", "c12", "--goal", "g",
                  "--acceptance", "a", "--max-retries", "1")
        self.jrun("claim", "c12", "--stage", "plan", "--agent", "plan")
        self.jrun("fail", "c12", "--error", "boom")
        task = self.status("c12")
        self.assertEqual((task["state"], task["attempts"]), ("ready", 1))
        self.jrun("claim", "c12", "--stage", "plan", "--agent", "plan")
        self.jrun("fail", "c12", "--error", "boom again")
        task = self.status("c12")
        self.assertEqual((task["state"], task["attempts"]), ("failed", 1))
        kinds = self.event_kinds("c12")
        self.assertIn("fail", kinds)
        self.assertIn("retry", kinds)

    # -- 13 ---------------------------------------------------------------
    def test_preempt_keeps_checkpoint(self):
        self.make_task("c13")
        self.jrun("claim", "c13", "--stage", "plan", "--agent", "plan")
        self.jrun("checkpoint", "c13", "--data", '{"line": 7}')
        self.jrun("preempt", "c13", "--reason", "higher priority")
        task = self.status("c13")
        self.assertEqual((task["state"], task["stage"]), ("ready", "plan"))
        self.assertEqual(task["checkpoint"]["line"], 7)
        self.assertIn("preempt", self.event_kinds("c13"))
        self.jrun("claim", "c13", "--stage", "plan", "--agent", "plan")
        self.assertEqual(self.status("c13")["state"], "running")

    # -- 14 ---------------------------------------------------------------
    def test_claim_same_agent_busy_and_force(self):
        self.make_task("c14a")
        self.make_task("c14b")
        self.jrun("claim", "c14a", "--stage", "plan", "--agent", "plan")
        code, out, err = self.run_cli("claim", "c14b", "--stage", "plan", "--agent", "plan")
        self.assertEqual(code, 2)
        self.assertIn("already has running task", err)
        self.jrun("claim", "c14b", "--stage", "plan", "--agent", "plan", "--force")
        self.assertEqual(self.status("c14b")["state"], "running")

    # -- 15 ---------------------------------------------------------------
    def test_deadline_sweep_to_waiting_and_extend(self):
        self.make_task("c15", "--deadline-minutes", "10")
        self.sql("UPDATE tasks SET deadline=? WHERE id=?", (time.time() - 5, "c15"))
        self.jrun("sweep", "--json")
        task = self.status("c15")
        self.assertEqual(task["state"], "waiting")
        self.assertIn("deadline:c15", task["waiting_for"])
        self.assertTrue(task["deadline_expired"])
        nxt = self.jrun("next", "c15", "--json")["next"]
        self.assertIn("extend", nxt["command"], "deadline wait must suggest extend, not emit")
        self.jrun("extend", "c15", "--deadline-minutes", "10")
        task = self.status("c15")
        self.assertEqual(task["state"], "ready")
        self.assertEqual(task["waiting_for"], [])
        kinds = self.event_kinds("c15")
        self.assertIn("deadline", kinds)
        self.assertIn("extend", kinds)

    # -- 16 ---------------------------------------------------------------
    def test_next_reports_agent_and_artifact(self):
        self.make_task("c16")
        data = self.jrun("next", "c16", "--json")
        nxt = data["next"]
        self.assertEqual(nxt["action"], "claim")
        self.assertEqual(nxt["agent"], "plan")
        self.assertTrue(nxt["artifact"].endswith("plan.md"))
        self.assertIn("claim", nxt["command"])

        self.jrun("claim", "c16", "--stage", "plan", "--agent", "plan")
        plan = self.write_artifact("c16", "plan.md")
        self.jrun("done", "c16", "--stage", "plan", "--artifact", plan, "--summary", "plan")
        self.jrun("claim", "c16", "--stage", "execute", "--agent", "executor")
        result = self.write_artifact("c16", "result.md")
        self.jrun("done", "c16", "--stage", "execute", "--artifact", result, "--summary", "result")
        nxt = self.jrun("next", "c16", "--json")["next"]
        self.assertEqual(nxt["agent"], "correct")
        self.assertIn("--stage verify", nxt["command"])
        message = " ".join(nxt["message"])
        self.assertIn("verify c16", message)
        self.assertNotIn("done", message)

    # -- 17 ---------------------------------------------------------------
    def test_submit_requires_acceptance(self):
        code, out, err = self.run_cli("submit", "--id", "c17", "--name", "c17",
                                  "--goal", "g")
        self.assertEqual(code, 2)
        self.assertIn("acceptance", err)
        self.assertEqual(self.jrun("list", "--json"), [])

    # -- 18 ---------------------------------------------------------------
    def test_events_json_ascending_and_signal_persist(self):
        self.make_task("c18")
        self.jrun("claim", "c18", "--stage", "plan", "--agent", "plan")
        self.jrun("emit", "approval:c18:x")
        self.jrun("block", "c18", "--wait", "approval:c18:x", "--reason", "pre")
        self.assertEqual(self.status("c18")["state"], "running")
        signals = self.jrun("signals", "--json")
        self.assertIn("approval:c18:x", [s["name"] for s in signals])
        events = self.jrun("events", "c18", "--json")
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertIn("block_skipped", [e["kind"] for e in events])

    # -- 19 ---------------------------------------------------------------
    def test_require_approval_pending_not_approved(self):
        # a) request -> waiting; pending only in event/waiting_for, not signals
        self.make_task("ap1")
        self.jrun("claim", "ap1", "--stage", "plan", "--agent", "plan")
        first = self.jrun("require-approval", "ap1", "--action", "deploy prod", "--reason", "need")
        self.assertEqual((first["state"], first["blocked"], first["approved"]),
                         ("waiting", True, False))
        self.assertIn("approval:ap1:deploy-prod", self.status("ap1")["waiting_for"])
        self.assertNotIn("approval:ap1:deploy-prod",
                         [s["name"] for s in self.jrun("signals", "--json")])
        self.assertIn("approval_required", self.event_kinds("ap1"))

        # b) recover leaves waiting untouched; direct claim is rejected; after
        #    unblock+claim a repeat request is still blocked (no auto-approve)
        self.jrun("recover", "--json")
        self.assertEqual(self.status("ap1")["state"], "waiting")
        code, out, err = self.run_cli("claim", "ap1", "--stage", "plan", "--agent", "plan")
        self.assertEqual(code, 2)
        self.jrun("unblock", "ap1")
        self.jrun("claim", "ap1", "--stage", "plan", "--agent", "plan")
        self.jrun("recover", "--json")
        self.assertEqual(self.status("ap1")["state"], "ready")
        self.jrun("claim", "ap1", "--stage", "plan", "--agent", "plan")
        second = self.jrun("require-approval", "ap1", "--action", "deploy prod", "--reason", "need")
        self.assertEqual((second["state"], second["blocked"], second["approved"]),
                         ("waiting", True, False))
        self.assertNotIn("approval:ap1:deploy-prod",
                         [s["name"] for s in self.jrun("signals", "--json")])

        # c) emit -> ready; claim -> running; same action is a legal pre-approval
        self.jrun("emit", "approval:ap1:deploy-prod")
        self.assertEqual(self.status("ap1")["state"], "ready")
        self.jrun("claim", "ap1", "--stage", "plan", "--agent", "plan")
        third = self.jrun("require-approval", "ap1", "--action", "deploy prod", "--reason", "need")
        self.assertEqual((third["state"], third["blocked"], third["approved"]),
                         ("running", False, True))
        self.assertIn("approval:ap1:deploy-prod",
                      [s["name"] for s in self.jrun("signals", "--json")])
        self.assertIn("approval_precheck", self.event_kinds("ap1"))

        # d) a direct block on the approval signal blocks until emit
        self.make_task("ap2")
        self.jrun("claim", "ap2", "--stage", "plan", "--agent", "plan2")
        self.jrun("block", "ap2", "--wait", "approval:ap2:ship", "--reason", "need user")
        self.assertEqual(self.status("ap2")["state"], "waiting")
        self.assertNotIn("approval:ap2:ship",
                         [s["name"] for s in self.jrun("signals", "--json")])
        self.jrun("emit", "approval:ap2:ship")
        self.assertEqual(self.status("ap2")["state"], "ready")


if __name__ == "__main__":
    unittest.main()

