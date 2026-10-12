"""Contracts, checks, conditional steps, automatic loops, external waits and
requirements: the engine features that let a generated flow work on its first run."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "skills" / "agentic-loop-builder"
sys.path.insert(0, str(BUILDER))

import state_manager as sm  # noqa: E402

VERDICT = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"enum": ["PASS", "FAIL"]}}}


class FlowTestCase(unittest.TestCase):
    """Runs a small flow in a temporary project directory."""

    inputs = {"topic": "x"}

    def make(self, steps, **extra):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        cwd = os.getcwd()
        os.chdir(self.dir)  # checks and waits run in the project directory recorded at init
        self.addCleanup(os.chdir, cwd)
        flow = {"name": "demo", "inputs": {"topic": "what"}, "schemas": {"verdict": VERDICT}, "steps": steps, **extra}
        self.flow_path = self.dir / "flow.json"
        self.flow_path.write_text(json.dumps(flow))
        self.run = sm.Run.create(self.flow_path, self.dir / "runs", "R1", dict(self.inputs))
        return self.run

    def write(self, rel, content):
        path = self.run.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))

    def step(self, path, files):
        act = self.run.next_action()
        self.assertEqual((act["action"], act["step"]), ("run_step", path), act)
        self.run.start(path)
        for rel, content in files.items():
            self.write(rel, content)
        return self.run.complete(path)


class TestContracts(FlowTestCase):
    def setUp(self):
        self.make([{"id": "draft", "skill": "d.md", "artifacts": [
            {"path": "draft.md", "sections": ["## Summary", "## Body"], "contains": [r"\bR\d+\b"], "min_bytes": 30},
            {"path": "meta.json", "json_schema": "verdict"},
            {"path": "notes.md", "optional": True}]}])

    def test_refuses_with_every_problem_and_counts_tries(self):
        self.run.start("draft")
        self.write("draft.md", "## Summary\nshort")
        self.write("meta.json", {"verdict": "MAYBE"})
        with self.assertRaises(sm.VerificationError) as cm:
            self.run.complete("draft")
        msg = str(cm.exception)
        self.assertIn("missing the heading `## Body`", msg)
        self.assertIn(r"does not match /\bR\d+\b/", msg)
        self.assertIn("needs at least 30", msg)
        self.assertIn("meta.json: $.verdict: must be one of ['PASS', 'FAIL']", msg)
        self.assertNotIn("notes.md", msg)  # optional
        self.assertIn("2 more refusal(s) allowed", msg)
        self.assertEqual(self.run.state["steps"]["draft"]["status"], "running")
        self.assertEqual(self.run.state["steps"]["draft"]["fix_failures"], 1)

        self.write("draft.md", "## Summary\nR1 is covered\n## Body\nlong enough body text")
        self.write("meta.json", {"verdict": "PASS"})
        self.assertEqual(self.run.complete("draft")["status"], "done")
        self.assertEqual(self.run.state["steps"]["draft"]["fix_failures"], 0)

    def test_step_fails_after_max_fix_attempts(self):
        self.run.start("draft")
        for _ in range(2):
            with self.assertRaises(sm.VerificationError):
                self.run.complete("draft")
        with self.assertRaises(sm.VerificationError) as cm:
            self.run.complete("draft")
        self.assertIn("try 3 of 3", str(cm.exception))
        act = self.run.next_action()
        self.assertEqual(act["action"], "failed")
        self.assertIn("failed verification 3 times", act["reason"])
        self.run.retry("draft")
        self.run.start("draft")
        self.assertEqual(self.run.state["steps"]["draft"]["fix_failures"], 0)

    def test_contract_is_described_to_the_subagent(self):
        contract = self.run.next_action()["contract"]
        self.assertIn("`draft.md`: must have the headings `## Summary`, `## Body`; must match /\\bR\\d+\\b/; at least 30 bytes",
                      contract)
        self.assertIn("`meta.json`: valid JSON matching the `verdict` schema", contract)
        self.assertIn("`notes.md`: optional", contract)


class TestChecks(FlowTestCase):
    def test_commands_must_pass_and_see_placeholders(self):
        self.inputs = {"topic": "it's quoted"}
        self.make([{"id": "build", "skill": "b.md", "artifacts": ["out.txt"], "checks": [
            {"name": "output mentions topic", "run": "grep -q -- {input.topic} {artifacts_dir}/out.txt"},
            "test -f marker"]}])
        self.run.start("build")
        self.write("out.txt", "nothing relevant")
        with self.assertRaises(sm.VerificationError) as cm:
            self.run.complete("build")
        self.assertIn("check `output mentions topic` failed (exit 1)", str(cm.exception))
        self.assertIn("check `test -f marker` failed", str(cm.exception))
        self.write("out.txt", "about it's quoted")
        (self.dir / "marker").write_text("")  # checks run in the project directory
        self.assertEqual(self.run.complete("build")["status"], "done")

    def test_checks_run_only_after_the_contract_passes(self):
        self.make([{"id": "build", "skill": "b.md", "artifacts": ["out.txt"], "checks": ["touch ran-check"]}])
        self.run.start("build")
        with self.assertRaises(sm.VerificationError):
            self.run.complete("build")
        self.assertFalse((self.dir / "ran-check").exists())


class TestWhen(FlowTestCase):
    def test_skipped_steps_count_as_done(self):
        self.make([
            {"id": "a", "skill": "a.md", "artifacts": ["kind.json"]},
            {"id": "only_bugs", "skill": "b.md", "artifacts": ["b.md"],
             "when": {"file": "kind.json", "field": "kind", "equals": "bug"}},
            {"id": "only_topic", "skill": "c.md", "artifacts": ["c.md"], "when": {"input": "topic", "in": ["x", "y"]}},
        ])
        self.step("a", {"kind.json": {"kind": "feature"}})
        act = self.run.next_action()
        self.assertEqual(act["step"], "only_topic")
        self.assertEqual(self.run.state["steps"]["only_bugs"]["status"], "skipped")
        self.step("only_topic", {"c.md": "c"})
        self.assertEqual(self.run.next_action()["action"], "done")
        self.assertIn("[-] only_bugs", self.run.render_status())

    def test_cycle_step_and_foreach_conditions(self):
        self.make([
            {"id": "plan", "skill": "p.md", "artifacts": ["items.json"], "produces_items": {"file": "items.json"}},
            {"id": "each", "kind": "foreach", "over": "plan", "cycle": [
                {"id": "do", "skill": "d.md", "artifacts": ["{item}/out.md"]},
                {"id": "extra", "skill": "e.md", "artifacts": ["{item}/extra.md"],
                 "when": {"file": "{item}/out.md", "matches": "NEEDS EXTRA"}}]},
            {"id": "never", "kind": "foreach", "over": "plan", "when": {"input": "topic", "equals": "nope"},
             "cycle": [{"id": "x", "skill": "x.md"}]},
        ])
        self.step("plan", {"items.json": [{"id": "A"}, {"id": "B"}]})
        self.step("each[A].do", {"A/out.md": "NEEDS EXTRA"})
        self.step("each[A].extra", {"A/extra.md": "x"})
        self.step("each[B].do", {"B/out.md": "fine"})
        self.assertEqual(self.run.next_action()["action"], "done")
        each = self.run.state["steps"]["each"]
        self.assertEqual(each["items"][1]["steps"]["extra"]["status"], "skipped")
        self.assertEqual(each["items"][1]["status"], "done")
        self.assertEqual(self.run.state["steps"]["never"]["status"], "skipped")


class TestRepeat(FlowTestCase):
    def flow(self, **repeat):
        return self.make([
            {"id": "write", "skill": "w.md", "artifacts": ["text.md"]},
            {"id": "critique", "skill": "c.md", "needs": ["write"],
             "artifacts": [{"path": "critique.json", "json_schema": "verdict"}],
             "repeat": {"until": {"file": "critique.json", "field": "verdict", "equals": "PASS"},
                        "back_to": "write", **repeat},
             "gate": {"id": "G1", "on_reject": "write"}},
        ])

    def test_loops_back_with_feedback_until_the_condition_holds(self):
        self.flow(max=3)
        self.step("write", {"text.md": "v1"})
        res = self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        self.assertEqual((res["status"], res["sent_back_to"], res["round"]), ("repeating", "write", 1))
        act = res["next"]
        self.assertEqual((act["step"], act["attempt"]), ("write", 2))
        self.assertIn("Automatic check not met after round 1 of 3", act["feedback"])
        self.assertIn('critique.json → verdict == "PASS"', act["feedback"])
        self.assertIn(str(self.run.artifacts_dir / "critique.json"), act["inputs"])
        self.step("write", {"text.md": "v2"})
        res = self.step("critique", {"critique.json": {"verdict": "PASS"}})
        self.assertEqual((res["status"], res["next"]["action"]), ("awaiting_gate", "await_gate"))
        self.run.approve("G1")
        self.assertEqual(self.run.next_action()["action"], "done")

    def test_exhausted_fail(self):
        self.flow(max=2)
        self.step("write", {"text.md": "v1"})
        self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        self.step("write", {"text.md": "v2"})
        res = self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        self.assertEqual(res["next"]["action"], "failed")
        self.assertIn("condition still not met after 2 rounds", res["next"]["reason"])

    def test_exhausted_gate_lets_the_human_decide_and_rejection_resets_the_budget(self):
        self.flow(max=2, on_exhausted="gate")
        self.step("write", {"text.md": "v1"})
        self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        self.step("write", {"text.md": "v2"})
        res = self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        gate = res["next"]
        self.assertEqual(gate["action"], "await_gate")
        self.assertIn("condition still not met after 2 rounds", gate["summary"])
        self.run.reject("G1", "try a different angle")
        self.assertEqual(self.run.state["steps"]["critique"]["loops"], 0)
        act = self.run.next_action()
        self.assertEqual(act["step"], "write")
        self.assertIn("try a different angle", act["feedback"])

    def test_exhausted_continue(self):
        self.flow(max=1, on_exhausted="continue")
        self.step("write", {"text.md": "v1"})
        res = self.step("critique", {"critique.json": {"verdict": "FAIL"}})
        self.assertEqual(res["status"], "awaiting_gate")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class WaitTestCase(FlowTestCase):
    def setUp(self):
        self.clock = Clock()
        saved = (sm._clock, sm._sleep)
        sm._clock, sm._sleep = self.clock, self.clock.sleep
        self.addCleanup(lambda: setattr(sm, "_clock", saved[0]) or setattr(sm, "_sleep", saved[1]))


class TestCommandWait(WaitTestCase):
    def flow(self, **wait):
        check = {"run": "test -f ready && exit 0; test -f broken && exit 3; exit 1", "failed_exit_codes": [3]}
        return self.make([
            {"id": "fix", "skill": "f.md", "artifacts": ["fix.md"],
             "when": {"file": "deploy.status.json", "field": "status", "equals": "failed"}},
            {"id": "deploy", "needs": ["fix"],
             "wait": {"check": check, "poll_every": "5m", "timeout": "1h", **wait}},
        ])

    def test_pending_then_ready(self):
        self.flow()
        act = self.run.next_action()
        self.assertEqual(self.run.state["steps"]["fix"]["status"], "skipped")
        self.assertEqual((act["action"], act["step"], act["due"]), ("wait", "deploy", True))
        res = self.run.poll("deploy")
        self.assertEqual((res["status"], res["last_status"]), ("waiting", "pending"))
        act = res["next"]
        self.assertEqual((act["due"], act["seconds_until_due"]), (False, 300))
        self.assertIn("poll_blocking", act["commands"])
        self.assertEqual(self.run.poll("deploy")["status"], "waiting")  # not due: no check
        self.assertEqual(self.run.state["steps"]["deploy"]["wait"]["polls"], 1)
        (self.dir / "ready").write_text("")
        self.assertEqual(self.run.poll("deploy", force=True)["status"], "done")
        status = json.loads((self.run.artifacts_dir / "deploy.status.json").read_text())
        self.assertEqual((status["status"], status["polls"]), ("ready", 2))
        self.assertEqual(self.run.next_action()["action"], "done")

    def test_blocking_poll_waits_through_pending_checks(self):
        self.flow()
        self.run.next_action()
        res = self.run.poll("deploy", block=sm.MAX_BLOCK_SECONDS)
        self.assertEqual(res["status"], "waiting")
        self.assertEqual(self.run.state["steps"]["deploy"]["wait"]["polls"], 2)  # at 0s and after 300s of sleep

    def test_failure_goes_back_to_the_fix_step_which_now_runs(self):
        self.flow(on_failure={"back_to": "fix"})
        self.run.next_action()
        (self.dir / "broken").write_text("")
        res = self.run.poll("deploy")
        self.assertEqual((res["status"], res["sent_back_to"]), ("failed_externally", "fix"))
        act = res["next"]
        self.assertEqual(act["step"], "fix")
        self.assertIn("External check 'deploy' failed: exit code 3", act["feedback"])
        self.assertIn(str(self.run.artifacts_dir / "deploy.status.json"), act["inputs"])
        (self.dir / "broken").unlink()
        (self.dir / "ready").write_text("")
        self.step("fix", {"fix.md": "fixed"})
        self.assertEqual(self.run.poll("deploy")["status"], "done")

    def test_failure_fails_by_default(self):
        self.flow()
        self.run.next_action()
        (self.dir / "broken").write_text("")
        act = self.run.poll("deploy")["next"]
        self.assertEqual(act["action"], "failed")
        self.assertIn("external check 'deploy' failed", act["reason"])

    def test_timeout(self):
        self.flow()
        self.run.next_action()
        self.run.poll("deploy")
        self.clock.sleep(3601)
        act = self.run.next_action()
        self.assertEqual(act["action"], "failed")
        self.assertIn("timed out after 1h", act["reason"])

    def test_timeout_continue(self):
        self.flow(on_timeout="continue")
        self.run.next_action()
        self.clock.sleep(3601)
        self.assertEqual(self.run.next_action()["action"], "done")
        self.assertIn("continued after it timed out", self.run.state["steps"]["deploy"]["summary"])


class TestAgentWait(WaitTestCase):
    def setUp(self):
        super().setUp()
        self.make([{"id": "review", "skill": "check-review.md",
                    "wait": {"check": "agent", "poll_every": "10m", "timeout": "1d"}}])

    def report(self, status):
        act = self.run.next_action()
        self.assertEqual(act["action"], "run_step")
        self.assertIn("wait_check", act)
        self.run.start("review")
        self.write("review.status.json", {"status": status, "summary": f"it is {status}"})
        return self.run.complete("review")

    def test_subagent_checks_until_ready(self):
        res = self.report("pending")
        self.assertEqual(res["status"], "waiting")
        act = res["next"]
        self.assertEqual((act["action"], act["due"]), ("wait", False))
        self.clock.sleep(600)
        res = self.report("ready")
        self.assertEqual(res["next"]["action"], "done")
        self.assertEqual(self.run.state["steps"]["review"]["attempt"], 1)
        self.assertEqual(self.run.state["steps"]["review"]["wait"]["polls"], 2)

    def test_status_file_is_verified(self):
        self.run.start("review")
        self.write("review.status.json", {"status": "maybe"})
        with self.assertRaises(sm.VerificationError) as cm:
            self.run.complete("review")
        self.assertIn("must be one of ['ready', 'pending', 'failed']", str(cm.exception))

    def test_blocking_poll_on_agent_check_returns_when_due(self):
        self.report("pending")
        res = self.run.poll("review", block=sm.MAX_BLOCK_SECONDS)
        self.assertEqual(res["next"]["action"], "wait")  # 10 minutes is beyond one blocking poll
        res = self.run.poll("review", force=True)
        self.assertEqual(res["next"]["action"], "run_step")


class TestPreflight(unittest.TestCase):
    def flow(self, **requires):
        return {"name": "demo", "requires": requires,
                "steps": [{"id": "a", "skill": "a.md", "checks": ["definitely-not-a-command-xyz --check"],
                           "requires": {"mcp": ["no-such-server"]}}]}

    def test_reports_missing_commands_variables_and_unconfirmed_mcp(self):
        os.environ["AGENTIC_TEST_SET"] = "1"
        self.addCleanup(os.environ.pop, "AGENTIC_TEST_SET")
        report = sm.preflight(self.flow(commands=["python3"], env=["AGENTIC_TEST_UNSET", "AGENTIC_TEST_UNSET|AGENTIC_TEST_SET"]))
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"], [
            "command `definitely-not-a-command-xyz` is not on PATH",
            "environment variable AGENTIC_TEST_UNSET is not set",
        ])
        self.assertIn("could not confirm MCP server 'no-such-server'", report["warnings"][0])

    def test_init_refuses_unless_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            flow_path = Path(d) / "flow.json"
            flow_path.write_text(json.dumps(self.flow()))
            base = [sys.executable, str(BUILDER / "state_manager.py"), "--flow", str(flow_path), "--runs-dir", d, "--run", "R"]
            p = subprocess.run(base + ["init"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("the environment is not ready", json.loads(p.stdout)["error"])
            p = subprocess.run(base + ["init", "--skip-preflight"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout)
            p = subprocess.run(base + ["preflight"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertEqual(json.loads(p.stdout)["requirements"]["commands"], ["definitely-not-a-command-xyz"])

    def test_command_words(self):
        self.assertEqual(sm._command_words('test -z "$(git status)" && npm test | tee x; FOO=1 make check'),
                         ["npm", "tee", "make"])


class TestValidation(unittest.TestCase):
    def errors(self, *steps, **extra):
        return "\n".join(sm.validate_flow({"name": "demo", "steps": list(steps), **extra}))

    def test_unknown_keys_suggest_the_right_one(self):
        self.assertIn("unknown key 'repaet' (did you mean 'repeat'?)",
                      self.errors({"id": "a", "skill": "a.md", "repaet": {}}))
        self.assertIn("cycle step 'b': unknown key 'chekcs' (did you mean 'checks'?)", self.errors(
            {"id": "a", "skill": "a.md", "artifacts": ["i.json"], "produces_items": {"file": "i.json"}},
            {"id": "f", "kind": "foreach", "over": "a", "cycle": [{"id": "b", "skill": "b.md", "chekcs": []}]}))

    def test_conditions(self):
        bad = [
            ({"file": "x.json"}, "use exactly one test"),
            ({"file": "x.json", "input": "y", "equals": 1}, "exactly one of 'file' or 'input'"),
            ({"file": "/abs", "exists": True}, "relative to the run's artifacts"),
            ({"input": "t", "in": "abc"}, "'in' takes a list"),
            ({"input": "t", "gte": "3"}, "'gte' takes a number"),
            ({"input": "t", "matches": "("}, "not a valid regex"),
            ({"all": []}, "non-empty list"),
            ({"not": {"input": "t", "equals": 1}, "x": 1}, "'not' takes one condition"),
        ]
        for cond, expected in bad:
            self.assertIn(expected, self.errors({"id": "a", "skill": "a.md", "when": cond}), cond)

    def test_loops_waits_and_checks(self):
        cases = [
            ({"repeat": {"back_to": "a"}}, "needs an 'until' condition"),
            ({"repeat": {"until": {"input": "t", "equals": 1}, "on_exhausted": "gate"}}, "needs an 'after' gate"),
            ({"repeat": {"until": {"input": "t", "equals": 1}, "back_to": "later"}}, "repeat back_to 'later' must be"),
            ({"wait": {"check": "maybe"}}, "wait.check must be"),
            ({"wait": {"check": {"run": "x"}, "poll_every": "soon"}}, "bad duration"),
            ({"wait": {"check": {"run": "x"}, "on_failure": "retry"}}, "wait.on_failure must be"),
            ({"checks": [{"cmd": "x"}]}, "each check is a command"),
            ({"artifacts": [{"path": "a.md", "sections": "## A"}]}, "'sections' must be a list"),
            ({"artifacts": [{"path": "a.json", "json_schema": "nope"}]}, "neither a schema object nor a name"),
            ({"artifacts": [{"path": "a.md", "colour": 1}]}, "unknown keys ['colour']"),
            ({"requires": {"binaries": ["x"]}}, "unknown requires key 'binaries'"),
            ({"max_fix_attempts": 0}, "'max_fix_attempts' must be a positive integer"),
        ]
        for extra, expected in cases:
            self.assertIn(expected, self.errors({"id": "a", "skill": "a.md", **extra}), extra)

    def test_wait_without_skill_and_cycle_status_file(self):
        self.assertEqual(self.errors({"id": "ci", "wait": {"check": {"run": "true"}}}), "")
        self.assertIn("'skill' (path of the step's instructions) is required",
                      self.errors({"id": "ci", "wait": {"check": "agent"}}))
        self.assertIn("must contain '{item}'", self.errors(
            {"id": "a", "skill": "a.md", "artifacts": ["i.json"], "produces_items": {"file": "i.json"}},
            {"id": "f", "kind": "foreach", "over": "a",
             "cycle": [{"id": "w", "wait": {"check": {"run": "true"}, "status_file": "w.json"}}]}))

    def test_parallel_agents_cannot_have_checks(self):
        self.assertIn("cannot have 'checks'", self.errors(
            {"id": "a", "skill": "a.md", "artifacts": ["r-{agent}.md"], "agents": [{"id": "x"}], "checks": ["true"]}))


class TestConditionsAndSimulation(FlowTestCase):
    def test_evaluate(self):
        self.make([{"id": "a", "skill": "a.md"}])
        self.write("r.json", {"score": 4, "tags": ["x"], "nested": {"ok": True}})
        self.write("t.md", "Verdict: PASS")
        ev = self.run.evaluate
        self.assertTrue(ev({"file": "r.json", "field": "score", "gte": 4}))
        self.assertFalse(ev({"file": "r.json", "field": "score", "lte": 3}))
        self.assertTrue(ev({"file": "r.json", "field": "tags.0", "equals": "x"}))
        self.assertTrue(ev({"file": "r.json", "field": "nested.ok", "equals": True}))
        self.assertTrue(ev({"file": "t.md", "matches": "Verdict: PASS"}))
        self.assertFalse(ev({"file": "missing.json", "field": "x", "not_equals": 1}))  # missing fails all but exists
        self.assertTrue(ev({"file": "missing.json", "exists": False}))
        self.assertTrue(ev({"any": [{"input": "topic", "equals": "nope"}, {"not": {"input": "topic", "in": ["y"]}}]}))
        self.assertEqual(sm.describe_condition({"all": [{"file": "r.json", "field": "score", "gte": 4},
                                                        {"input": "topic", "exists": True}]}),
                         "(r.json → score >= 4 and input 'topic' exists)")

    def test_simulate_shows_skips_loops_and_waits(self):
        self.make([
            {"id": "write", "skill": "w.md", "artifacts": ["t.md"]},
            {"id": "critique", "skill": "c.md", "artifacts": [{"path": "c.json", "json_schema": "verdict"}],
             "repeat": {"until": {"file": "c.json", "field": "verdict", "equals": "PASS"}, "back_to": "write"}},
            {"id": "fix", "skill": "f.md", "artifacts": ["f.md"],
             "when": {"file": "ship.status.json", "field": "status", "equals": "failed"}},
            {"id": "ship", "wait": {"check": {"run": "false"}, "on_failure": {"back_to": "fix"}}},
        ])
        trace = sm.simulate(self.flow_path, until={"critique": 1}, waits={"ship": 1}, wait_fails={"ship": 1})
        self.assertEqual(trace, [
            "run   write  [attempt 1, subagent]",
            "run   critique  [attempt 1, subagent]",
            'loop  critique -> back to write  (round 1: c.json → verdict == "PASS" not met)',
            "run   write  [attempt 2, subagent] (with feedback)",
            "run   critique  [attempt 2, subagent]",
            'skip  fix  (condition false: ship.status.json → status == "failed")',
            "wait  ship: failed",
            "wait  ship failed -> back to fix",
            "run   fix  [attempt 1, subagent] (with feedback)",
            "wait  ship: pending",
            "wait  ship: ready",
            "done",
        ])

    def test_simulate_detects_endless_loops(self):
        self.make([{"id": "a", "skill": "a.md", "artifacts": ["a.md"], "max_iterations": 10000, "gate": {"id": "G"}}])
        self.assertTrue(sm.simulate(self.flow_path, {"G": 10000})[-1].startswith("stopped after"))


class TestCliPoll(unittest.TestCase):
    def test_poll_and_verification_refusals_are_saved(self):
        with tempfile.TemporaryDirectory() as d:
            flow = {"name": "demo", "steps": [
                {"id": "a", "skill": "a.md", "artifacts": [{"path": "a.md", "sections": ["## A"]}]},
                {"id": "w", "wait": {"check": {"run": "true"}}}]}
            (Path(d) / "flow.json").write_text(json.dumps(flow))
            base = [sys.executable, str(BUILDER / "state_manager.py"), "--flow", str(Path(d) / "flow.json"),
                    "--runs-dir", d, "--run", "R"]

            def cli(*args):
                p = subprocess.run(base + list(args), capture_output=True, text=True, cwd=d)
                return p.returncode, json.loads(p.stdout)

            cli("init")
            cli("start", "a")
            (Path(d) / "demo" / "R" / "artifacts" / "a.md").write_text("no heading")
            code, out = cli("complete", "a")
            self.assertEqual(code, 1)
            self.assertIn("missing the heading `## A`", out["error"])
            self.assertEqual(cli("show")[1]["steps"]["a"]["fix_failures"], 1)  # the refusal was saved
            (Path(d) / "demo" / "R" / "artifacts" / "a.md").write_text("## A\n")
            code, out = cli("complete", "a")
            self.assertEqual(out["next"]["action"], "wait")
            code, out = cli("poll", "w")
            self.assertEqual((code, out["status"], out["next"]["action"]), (0, "done", "done"))


if __name__ == "__main__":
    unittest.main()
