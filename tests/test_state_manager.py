import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "skills" / "agentic-loop-builder"
EXAMPLE_FLOW = ROOT / "examples" / "development-loop" / ".claude" / "skills" / "development-loop" / "flow.json"
sys.path.insert(0, str(BUILDER))

import state_manager as sm  # noqa: E402

SUBTASKS = {
    "subtasks": [
        # listed out of order on purpose: T2 must wait for T1
        {"id": "T2", "title": "Use it", "goal": "g", "depends_on": ["T1"], "verification": ["pytest -k use"]},
        {"id": "T1", "title": "Build it", "goal": "g", "depends_on": [], "verification": ["pytest -k build"]},
    ]
}


class EngineTestCase(unittest.TestCase):
    flow_dict = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        flow = self.flow_dict or json.loads(EXAMPLE_FLOW.read_text())
        self.flow_path = self.tmp_path / "flow.json"
        self.flow_path.write_text(json.dumps(flow))
        self.runs = self.tmp_path / "runs"
        self.run = sm.Run.create(self.flow_path, self.runs, "PROJ-1", {"story": "PROJ-1"})

    def reload(self):
        self.run.save()
        self.run = sm.Run.load(self.flow_path, self.runs, "PROJ-1")
        return self.run

    def write(self, rel, content="x"):
        path = self.run.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))

    def do_step(self, expected_step, files=None):
        act = self.run.next_action()
        self.assertEqual(act["action"], "run_step", act)
        self.assertEqual(act["step"], expected_step)
        self.run.start(expected_step)
        for out in act["outputs"]:
            rel = Path(out).relative_to(self.run.artifacts_dir).as_posix()
            self.write(rel, (files or {}).get(rel, "content"))
        if act.get("agents"):
            for agent in act["agents"]:
                res = self.run.complete(expected_step, summary=f"{agent['agent']} ok", agent=agent["agent"])
            return res
        return self.run.complete(expected_step, summary=f"did {expected_step}")

    def to_subtasks(self):
        self.do_step("jira_context")
        self.do_step("requirements")
        self.run.approve("G1")
        self.do_step("decomposition", {"subtasks.json": SUBTASKS})
        self.run.approve("G2")


class TestValidateFlow(unittest.TestCase):
    def base(self):
        return json.loads(EXAMPLE_FLOW.read_text())

    def test_example_is_valid(self):
        self.assertEqual(sm.validate_flow(self.base()), [])

    def test_reports_structural_errors(self):
        cases = [
            (lambda f: f["steps"][1].update(needs=["nope"]), "needs 'nope' is not an earlier step"),
            (lambda f: f["steps"][1]["gate"].update(id="G2"), "gate id 'G2' is used twice"),
            (lambda f: f["steps"][3]["cycle"][1]["gate"].update(on_reject="open_pr"), "earlier cycle step"),
            (lambda f: f["steps"][1].update(artifacts=[]), "declares no artifacts"),
            (lambda f: f["steps"][1]["gate"].update(guard={"pattern": "x"}), "only makes sense on a 'before' gate"),
            (lambda f: f["steps"][3].update(over="jira_context"), "has no 'produces_items'"),
            (lambda f: f["steps"][2]["produces_items"].update(item_schema="missing"), "not defined in 'item_schemas'"),
            (lambda f: f["steps"][0].update(kind="parallel"), "unknown kind"),
            (lambda f: f["steps"][4].update(id="Open-PR"), "snake_case"),
            (lambda f: f["steps"][3]["cycle"][1].update(artifacts=["subtasks/{item}/review.md"]), "would write the same file"),
            (lambda f: f["steps"][1].update(artifacts=["req-{agent}.md"]), "needs an 'agents' list"),
            (lambda f: f["steps"][3]["cycle"][1].update(run_in_subagent=False), "always run as subagents"),
            (lambda f: f["steps"][2].update(agents=[{"id": "a"}]), "cannot produce items"),
            (lambda f: f["steps"][3]["cycle"][1]["agents"].append({"id": "correctness"}), "agent 'correctness': duplicate id"),
            (lambda f: f["steps"][3]["cycle"][1].update(agents=[]), "non-empty list of parallel subagents"),
            (lambda f: f.update(orchestrator={"model": 5}), "'orchestrator' must map"),
        ]
        for mutate, expected in cases:
            flow = self.base()
            mutate(flow)
            errors = sm.validate_flow(flow)
            self.assertTrue(any(expected in e for e in errors), f"{expected!r} not in {errors}")

    def test_load_flow_raises_with_all_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "flow.json"
            p.write_text(json.dumps({"name": "Bad Name", "steps": []}))
            with self.assertRaises(sm.EngineError) as cm:
                sm.load_flow(p)
            self.assertIn("kebab-case", str(cm.exception))
            self.assertIn("non-empty list", str(cm.exception))


class TestItems(unittest.TestCase):
    flow = json.loads(EXAMPLE_FLOW.read_text())
    pi = flow["steps"][2]["produces_items"]

    def check(self, items):
        return sm.check_items(self.flow, self.pi, {"subtasks": items})

    def item(self, iid, deps=(), **kw):
        return {"id": iid, "title": "t", "goal": "g", "depends_on": list(deps), "verification": ["v"], **kw}

    def test_valid(self):
        self.assertEqual(len(self.check([self.item("T1"), self.item("T2", ["T1"])])), 2)

    def test_rejects_bad_items(self):
        cases = [
            ([self.item("T1", ["T9"])], "unknown item 'T9'"),
            ([self.item("T1", ["T1"])], "depends on itself"),
            ([self.item("T1", ["T2"]), self.item("T2", ["T1"])], "dependency cycle"),
            ([self.item("T1"), self.item("T1")], "duplicate id"),
            ([{**self.item("T1"), "verification": []}], "at least 1 entries"),
            ([{k: v for k, v in self.item("T1").items() if k != "goal"}], "missing required field 'goal'"),
            ([], "no items"),
        ]
        for items, expected in cases:
            with self.assertRaises(sm.EngineError) as cm:
                self.check(items)
            self.assertIn(expected, str(cm.exception))

    def test_wrong_container(self):
        with self.assertRaises(sm.EngineError) as cm:
            sm.check_items(self.flow, self.pi, [self.item("T1")])
        self.assertIn("a list under 'subtasks'", str(cm.exception))


class TestHappyPath(EngineTestCase):
    def test_full_run(self):
        act = self.run.next_action()
        self.assertEqual(act["action"], "run_step")
        self.assertEqual(act["step"], "jira_context")
        self.assertTrue(act["run_in_subagent"])
        self.assertEqual(act["run_inputs"], {"story": "PROJ-1"})
        self.assertTrue(act["skill"].endswith("steps/jira-context.md"))
        self.do_step("jira_context")

        res = self.do_step("requirements")
        self.assertEqual(res["status"], "awaiting_gate")
        gate = res["next"]
        self.assertEqual((gate["action"], gate["gate"], gate["when"]), ("await_gate", "G1", "after"))
        self.assertEqual(gate["review"], [str(self.run.artifacts_dir / "requirements.md")])
        self.assertEqual(gate["summary"], "did requirements")
        self.run.approve("G1", note="lgtm")

        act = self.run.next_action()
        self.assertEqual(act["step"], "decomposition")
        self.assertIn("item_schema", act)
        self.assertEqual(act["items_file"], str(self.run.artifacts_dir / "subtasks.json"))
        self.assertEqual(
            act["inputs"],
            [str(self.run.artifacts_dir / "jira-context.md"), str(self.run.artifacts_dir / "requirements.md")],
        )
        self.do_step("decomposition", {"subtasks.json": SUBTASKS})
        self.run.approve("decomposition")  # step path works as well as gate id

        # T1 first despite being listed second
        act = self.run.next_action()
        self.assertEqual(act["step"], "subtasks[T1].implement")
        self.assertEqual(act["item"]["title"], "Build it")
        self.assertIn(str(self.run.artifacts_dir / "subtasks/T1/item.json"), act["inputs"])
        self.assertEqual(act["outputs"], [str(self.run.artifacts_dir / "subtasks/T1/implementation.md")])
        self.do_step("subtasks[T1].implement")
        act = self.run.next_action()
        self.assertIn(str(self.run.artifacts_dir / "subtasks/T1/implementation.md"), act["inputs"])
        self.assertEqual([a["agent"] for a in act["agents"]], ["correctness", "verification"])
        self.do_step("subtasks[T1].review")
        self.run.approve("G3")
        self.do_step("subtasks[T2].implement")
        self.do_step("subtasks[T2].review")
        self.run.approve("G3")

        gate = self.run.next_action()
        self.assertEqual((gate["action"], gate["gate"], gate["when"], gate["step"]), ("await_gate", "G4", "before", "open_pr"))
        self.assertEqual(self.run.state["steps"]["subtasks"]["status"], "done")
        with self.assertRaises(sm.EngineError):
            self.run.start("open_pr")
        self.run.approve("G4")
        self.do_step("open_pr")

        done = self.run.next_action()
        self.assertEqual(done["action"], "done")
        self.assertIn(str(self.run.artifacts_dir / "pr.md"), done["artifacts"])

        self.reload()
        events = [json.loads(line)["event"] for line in (self.run.run_dir / "history.jsonl").read_text().splitlines()]
        self.assertEqual(events[0], "init")
        self.assertIn("expand", events)
        self.assertEqual(events.count("approve"), 5)

    def test_state_survives_reload_mid_step(self):
        self.do_step("jira_context")
        self.run.start("requirements")
        self.reload()
        act = self.run.next_action()
        self.assertEqual((act["action"], act["step"], act["resumed"], act["attempt"]), ("run_step", "requirements", True, 1))
        self.run.start("requirements")  # idempotent while running
        self.assertEqual(self.run.state["steps"]["requirements"]["attempt"], 1)

    def test_state_file_is_valid_json_and_has_no_tmp_leftovers(self):
        self.do_step("jira_context")
        self.run.save()
        json.loads(self.run.state_path.read_text())
        self.assertEqual([p.name for p in self.run.run_dir.glob("*.tmp")], [])


class TestGuards(EngineTestCase):
    def test_out_of_order_start_is_refused(self):
        with self.assertRaises(sm.EngineError) as cm:
            self.run.start("requirements")
        self.assertIn("next action is run_step jira_context", str(cm.exception))

    def test_complete_requires_start_and_artifacts(self):
        with self.assertRaises(sm.EngineError):
            self.run.complete("jira_context")
        self.run.start("jira_context")
        with self.assertRaises(sm.EngineError) as cm:
            self.run.complete("jira_context")
        self.assertIn("jira-context.md", str(cm.exception))
        self.assertEqual(self.run.state["steps"]["jira_context"]["status"], "running")

    def test_extra_artifact_paths(self):
        self.run.start("jira_context")
        self.write("jira-context.md")
        self.write("notes/extra.md")
        outside = self.tmp_path / "outside.txt"
        outside.write_text("x")
        self.run.complete("jira_context", ("notes/extra.md", str(outside)))
        self.assertEqual(
            self.run.state["steps"]["jira_context"]["artifacts"],
            ["jira-context.md", "notes/extra.md", str(outside.resolve())],
        )

    def test_invalid_items_keep_step_running(self):
        self.do_step("jira_context")
        self.do_step("requirements")
        self.run.approve("G1")
        self.run.start("decomposition")
        self.write("plan.md")
        self.write("subtasks.json", {"subtasks": [{"id": "T1", "title": "t", "goal": "g", "depends_on": ["T7"], "verification": ["v"]}]})
        with self.assertRaises(sm.EngineError) as cm:
            self.run.complete("decomposition")
        self.assertIn("unknown item 'T7'", str(cm.exception))
        self.assertEqual(self.run.state["steps"]["decomposition"]["status"], "running")

    def test_approve_only_the_waiting_gate(self):
        with self.assertRaises(sm.EngineError) as cm:
            self.run.approve("G1")
        self.assertIn("no gate is waiting", str(cm.exception))
        self.do_step("jira_context")
        self.do_step("requirements")
        with self.assertRaises(sm.EngineError) as cm:
            self.run.approve("G2")
        self.assertIn("waiting gate is G1", str(cm.exception))

    def test_reject_needs_feedback(self):
        self.do_step("jira_context")
        self.do_step("requirements")
        with self.assertRaises(sm.EngineError):
            self.run.reject("G1", "  ")

    def test_fail_and_retry(self):
        self.run.start("jira_context")
        res = self.run.fail("jira_context", "Jira unreachable")
        self.assertEqual(res["next"]["action"], "failed")
        self.assertEqual(res["next"]["reason"], "Jira unreachable")
        self.assertEqual(self.run.retry("jira_context")["next"]["step"], "jira_context")


class TestRejections(EngineTestCase):
    def test_reject_after_gate_reruns_step_with_feedback(self):
        self.do_step("jira_context")
        self.do_step("requirements")
        res = self.run.reject("G1", "R3 is missing the error case")
        self.assertEqual(res["sent_back_to"], "requirements")
        act = res["next"]
        self.assertEqual((act["action"], act["step"], act["attempt"]), ("run_step", "requirements", 2))
        self.assertEqual(act["feedback"], "Gate G1 rejected: R3 is missing the error case")
        st = self.run.state["steps"]["requirements"]
        self.assertEqual(st["previous_artifacts"], ["requirements.md"])
        self.assertEqual(st["gate"]["decisions"][-1]["decision"], "rejected")
        self.do_step("requirements")
        self.run.approve("G1")
        self.assertIsNone(self.run.state["steps"]["requirements"]["feedback"])

    def test_reject_in_cycle_goes_back_to_implement(self):
        self.to_subtasks()
        self.do_step("subtasks[T1].implement")
        self.do_step("subtasks[T1].review")
        res = self.run.reject("G3", "handle empty input")
        self.assertEqual(res["sent_back_to"], "subtasks[T1].implement")
        act = res["next"]
        self.assertEqual((act["step"], act["attempt"]), ("subtasks[T1].implement", 2))
        self.assertEqual(act["feedback"], "Gate G3 rejected: handle empty input")
        # the reviews that sent it back are offered as input
        self.assertIn(str(self.run.artifacts_dir / "subtasks/T1/review-correctness.md"), act["inputs"])
        self.assertIn(str(self.run.artifacts_dir / "subtasks/T1/review-verification.md"), act["inputs"])
        item = self.run.state["steps"]["subtasks"]["items"][1]
        self.assertEqual(item["id"], "T1")
        self.assertEqual(item["steps"]["review"]["status"], "pending")
        self.assertEqual(item["steps"]["review"]["gate"]["status"], "pending")
        self.assertEqual({a["status"] for a in item["steps"]["review"]["agents"].values()}, {"pending"})
        self.do_step("subtasks[T1].implement")
        self.do_step("subtasks[T1].review")
        self.run.approve("G3")
        self.assertEqual(self.run.next_action()["step"], "subtasks[T2].implement")

    def test_max_iterations_fails_then_retry(self):
        self.to_subtasks()
        for n in range(3):
            self.do_step("subtasks[T1].implement")
            self.do_step("subtasks[T1].review")
            res = self.run.reject("G3", f"round {n + 1}")
        act = res["next"]
        self.assertEqual((act["action"], act["step"]), ("failed", "subtasks[T1].implement"))
        self.assertIn("max_iterations=3", act["reason"])
        res = self.run.retry("subtasks[T1].implement")
        self.assertEqual((res["next"]["step"], res["next"]["attempt"]), ("subtasks[T1].implement", 1))
        self.assertEqual(res["next"]["feedback"], "Gate G3 rejected: round 3")

    def test_before_gate_reject_without_target_halts(self):
        self.to_subtasks()
        for t in ("T1", "T2"):
            self.do_step(f"subtasks[{t}].implement")
            self.do_step(f"subtasks[{t}].review")
            self.run.approve("G3")
        res = self.run.reject("G4", "not yet, wait for the release freeze")
        self.assertEqual(res["next"]["action"], "failed")
        self.run.retry("open_pr")
        self.assertEqual(self.run.next_action()["action"], "await_gate")



class TestRejectToEarlierStep(EngineTestCase):
    flow_dict = copy.deepcopy(json.loads(EXAMPLE_FLOW.read_text()))
    flow_dict["steps"][2]["gate"]["on_reject"] = "requirements"

    def test_reject_resets_target_and_dependents(self):
        self.to_subtasks_until_g2()
        res = self.run.reject("G2", "requirements are too broad")
        self.assertEqual(res["sent_back_to"], "requirements")
        steps = self.run.state["steps"]
        self.assertEqual(steps["jira_context"]["status"], "done")
        self.assertEqual(steps["requirements"]["status"], "pending")
        self.assertEqual(steps["requirements"]["feedback"], "Gate G2 rejected: requirements are too broad")
        self.assertEqual(steps["requirements"]["gate"]["status"], "pending")
        self.assertEqual(steps["decomposition"]["status"], "pending")
        self.assertIsNone(steps["subtasks"]["items"])
        self.assertEqual(res["next"]["step"], "requirements")

    def to_subtasks_until_g2(self):
        self.do_step("jira_context")
        self.do_step("requirements")
        self.run.approve("G1")
        self.do_step("decomposition", {"subtasks.json": SUBTASKS})


class TestParallelAgents(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.to_subtasks()
        self.do_step("subtasks[T1].implement")
        self.path = "subtasks[T1].review"

    def write_reviews(self, *agents):
        for a in agents:
            self.write(f"subtasks/T1/review-{a}.md", f"{a} review")

    def test_run_step_lists_each_agent(self):
        act = self.run.next_action()
        self.assertTrue(act["run_in_subagent"])
        self.assertEqual(act["outputs"], [str(self.run.artifacts_dir / f"subtasks/T1/review-{a}.md") for a in ("correctness", "verification")])
        corr, ver = act["agents"]
        self.assertEqual(corr["agent"], "correctness")
        self.assertTrue(corr["skill"].endswith("steps/review.md"))
        self.assertIn("Correctness", corr["focus"])
        self.assertEqual(corr["subagent_type"], "general-purpose")
        self.assertEqual(ver["outputs"], [str(self.run.artifacts_dir / "subtasks/T1/review-verification.md")])
        self.assertIn("--agent verification", ver["commands"]["complete"])
        self.assertNotIn("complete", act["commands"])
        self.assertEqual(self.run.start(self.path)["agents"], ["correctness", "verification"])

    def test_step_completes_when_all_agents_report(self):
        self.run.start(self.path)
        self.write_reviews("correctness", "verification")
        with self.assertRaises(sm.EngineError) as cm:
            self.run.complete(self.path)
        self.assertIn("report each with --agent", str(cm.exception))
        res = self.run.complete(self.path, summary="2 nits", agent="correctness")
        self.assertEqual((res["status"], res["waiting_for"]), ("running", ["verification"]))
        with self.assertRaises(sm.EngineError):
            self.run.complete(self.path, agent="correctness")  # already reported
        res = self.run.complete(self.path, summary="all green", agent="verification")
        self.assertEqual(res["status"], "awaiting_gate")
        gate = res["next"]
        self.assertEqual(gate["gate"], "G3")
        self.assertEqual(len(gate["review"]), 2)
        self.assertEqual(gate["summary"], "correctness: 2 nits | verification: all green")

    def test_agent_artifacts_are_checked_per_agent(self):
        self.run.start(self.path)
        self.write_reviews("verification")
        with self.assertRaises(sm.EngineError) as cm:
            self.run.complete(self.path, agent="correctness")
        self.assertIn("review-correctness.md", str(cm.exception))
        self.run.complete(self.path, agent="verification")

    def test_failed_agent_fails_step_and_retry_reruns_only_it(self):
        self.run.start(self.path)
        self.write_reviews("correctness")
        self.run.complete(self.path, agent="correctness")
        res = self.run.fail(self.path, "test env broken", agent="verification")
        self.assertEqual(res["next"]["action"], "failed")
        self.assertIn("agent 'verification' failed: test env broken", res["next"]["reason"])
        act = self.run.retry(self.path)["next"]
        self.assertEqual([a["agent"] for a in act["agents"]], ["verification"])
        self.assertEqual(self.run.start(self.path)["agents"], ["verification"])
        self.write_reviews("verification")
        res = self.run.complete(self.path, agent="verification")
        self.assertEqual(res["status"], "awaiting_gate")
        self.assertEqual(len(res["next"]["review"]), 2)

    def test_failing_agent_waits_for_running_siblings(self):
        self.run.start(self.path)
        res = self.run.fail(self.path, "crashed", agent="correctness")
        self.assertEqual((res["status"], res["waiting_for"]), ("running", ["verification"]))
        self.write_reviews("verification")
        res = self.run.complete(self.path, agent="verification")
        self.assertEqual(res["status"], "failed")

    def test_resume_lists_only_unfinished_agents(self):
        self.run.start(self.path)
        self.write_reviews("correctness")
        self.run.complete(self.path, agent="correctness")
        self.reload()
        act = self.run.next_action()
        self.assertTrue(act["resumed"])
        self.assertEqual([a["agent"] for a in act["agents"]], ["verification"])

    def test_agent_flag_refused_on_single_step(self):
        self.assertEqual(self.run.next_action()["step"], self.path)
        self.do_step(self.path)
        self.run.approve("G3")
        self.run.start("subtasks[T2].implement")
        with self.assertRaises(sm.EngineError) as cm:
            self.run.complete("subtasks[T2].implement", agent="x")
        self.assertIn("no parallel agents", str(cm.exception))

    def test_status_shows_agents(self):
        self.run.start(self.path)
        self.write_reviews("correctness")
        self.run.complete(self.path, agent="correctness")
        self.assertIn("agents: correctness done, verification running", self.run.render_status())


class TestAgentOptions(EngineTestCase):
    flow_dict = copy.deepcopy(json.loads(EXAMPLE_FLOW.read_text()))
    flow_dict["defaults"]["subagent_type"] = "worker"
    flow_dict["steps"][3]["cycle"][1]["agents"][1].update(
        skill="steps/review-tests.md", subagent_type="test-runner", artifacts=["subtasks/{item}/tests.md"])

    def test_own_skill_type_and_artifacts(self):
        self.to_subtasks()
        self.assertEqual(self.run.next_action()["subagent_type"], "worker")
        self.do_step("subtasks[T1].implement")
        corr, ver = self.run.next_action()["agents"]
        self.assertEqual((corr["subagent_type"], ver["subagent_type"]), ("worker", "test-runner"))
        self.assertTrue(ver["skill"].endswith("steps/review-tests.md"))
        self.assertEqual(ver["outputs"], [str(self.run.artifacts_dir / "subtasks/T1/tests.md")])
        self.assertEqual(corr["outputs"], [str(self.run.artifacts_dir / "subtasks/T1/review-correctness.md")])


class TestIndependentItems(EngineTestCase):
    flow_dict = copy.deepcopy(json.loads(EXAMPLE_FLOW.read_text()))
    flow_dict["steps"][3]["respect_item_dependencies"] = False

    def test_listed_order_when_dependencies_ignored(self):
        self.to_subtasks()
        self.assertEqual(self.run.next_action()["step"], "subtasks[T2].implement")


class TestSimulateAndGuard(unittest.TestCase):
    def test_simulate_trace(self):
        trace = sm.simulate(EXAMPLE_FLOW, {"G3": 1})
        self.assertEqual(trace[-1], "done")
        self.assertIn("gate  G3 (after subtasks[T1].review) REJECTED -> back to subtasks[T1].implement", trace)
        self.assertIn("run   subtasks[T1].implement  [attempt 2, subagent] (with feedback)", trace)
        self.assertIn("run   subtasks[T1].review  [attempt 1, 2 parallel subagents: correctness, verification]", trace)
        self.assertLess(trace.index("gate  G4 (before open_pr) approved"), trace.index("run   open_pr  [attempt 1, subagent]"))

    def test_simulate_reports_max_iterations(self):
        trace = sm.simulate(EXAMPLE_FLOW, {"G3": 5})
        self.assertTrue(trace[-1].startswith("FAILED subtasks[T1].implement"), trace[-1])

    def test_guard(self):
        with tempfile.TemporaryDirectory() as d:
            runs = Path(d)
            pr = {"tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}}
            self.assertIn("gate G4", sm.guard(EXAMPLE_FLOW, runs, pr))
            self.assertIsNone(sm.guard(EXAMPLE_FLOW, runs, {"tool_name": "Bash", "tool_input": {"command": "git status"}}))
            self.assertIsNone(sm.guard(EXAMPLE_FLOW, runs, {"tool_name": "Write", "tool_input": {"content": "gh pr create"}}))

            run = sm.Run.create(EXAMPLE_FLOW, runs, "PROJ-9", {"story": "PROJ-9"})
            # walk the run to G4 using the simulator's moves
            while (act := run.next_action())["action"] == "run_step" or act["gate"] != "G4":
                if act["action"] == "run_step":
                    run.start(act["step"])
                    for out in act["outputs"]:
                        content = json.dumps(SUBTASKS) if out == act.get("items_file") else "x"
                        Path(out).parent.mkdir(parents=True, exist_ok=True)
                        Path(out).write_text(content)
                    for agent in act.get("agents") or [None]:
                        run.complete(act["step"], agent=agent and agent["agent"])
                else:
                    run.approve(act["gate"])
            run.save()
            self.assertIsNotNone(sm.guard(EXAMPLE_FLOW, runs, pr))  # G4 waiting, not approved
            run.approve("G4")
            run.save()
            self.assertIsNone(sm.guard(EXAMPLE_FLOW, runs, pr))
            run.start("open_pr")
            run.save()
            self.assertIsNone(sm.guard(EXAMPLE_FLOW, runs, pr))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = [sys.executable, str(BUILDER / "state_manager.py"), "--flow", str(EXAMPLE_FLOW),
                     "--runs-dir", self.tmp.name, "--run", "PROJ-7"]

    def cli(self, *args, stdin=None):
        p = subprocess.run(self.base + list(args), capture_output=True, text=True, input=stdin)
        return p.returncode, p.stdout, p.stderr

    def test_cli_round_trip(self):
        code, out, _ = self.cli("init", "--input", "story=PROJ-7")
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertEqual(res["next"]["step"], "jira_context")
        self.assertIn("--run PROJ-7 start jira_context", res["next"]["commands"]["start"])

        code, out, _ = self.cli("init", "--input", "story=PROJ-7")
        self.assertEqual(code, 1)
        self.assertIn("already exists", json.loads(out)["error"])

        self.assertEqual(self.cli("start", "jira_context")[0], 0)
        artifact = Path(self.tmp.name) / "development-loop" / "PROJ-7" / "artifacts" / "jira-context.md"
        artifact.write_text("ctx")
        code, out, _ = self.cli("complete", "jira_context", "--summary", "fetched")
        self.assertEqual(json.loads(out)["next"]["step"], "requirements")

        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("[x] jira_context", out)
        self.assertIn("next: run_step requirements", out)

        # --run also accepted after the subcommand
        p = subprocess.run(self.base[:-2] + ["next", "--run", "PROJ-7"], capture_output=True, text=True)
        self.assertEqual(json.loads(p.stdout)["step"], "requirements")

    def test_cli_missing_input_and_bad_command(self):
        code, out, _ = self.cli("init")
        self.assertEqual(code, 1)
        self.assertIn("--input story=", json.loads(out)["error"])
        self.cli("init", "--input", "story=PROJ-7")
        code, out, _ = self.cli("approve", "G1")
        self.assertEqual(code, 1)
        self.assertIn("no gate is waiting", json.loads(out)["error"])

    def test_cli_parallel_agents(self):
        flow = json.loads(EXAMPLE_FLOW.read_text())
        flow["steps"] = [{"id": "review", "skill": "r.md", "artifacts": ["review-{agent}.md"],
                          "agents": [{"id": "a"}, {"id": "b"}]}]
        flow_path = Path(self.tmp.name) / "flow.json"
        flow_path.write_text(json.dumps(flow))
        self.base[3] = str(flow_path)
        self.cli("init", "--input", "story=PROJ-7")
        self.cli("start", "review")
        arts = Path(self.tmp.name) / "development-loop" / "PROJ-7" / "artifacts"
        (arts / "review-a.md").write_text("a")
        (arts / "review-b.md").write_text("b")
        code, out, _ = self.cli("complete", "review", "--agent", "a", "--summary", "ok")
        self.assertEqual(json.loads(out)["waiting_for"], ["b"])
        code, out, _ = self.cli("complete", "review", "--agent", "b")
        self.assertEqual(json.loads(out)["next"]["action"], "done")

    def test_cli_guard_exit_codes(self):
        hook = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create"}})
        code, _, err = self.cli("guard", stdin=hook)
        self.assertEqual(code, 2)
        self.assertIn("gate G4", err)
        code, _, _ = self.cli("guard", stdin=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
