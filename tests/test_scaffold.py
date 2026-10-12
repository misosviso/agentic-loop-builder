import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "skills" / "agentic-loop-builder"
EXAMPLE_ROOT = ROOT / "examples" / "development-loop" / ".claude"
EXAMPLE = EXAMPLE_ROOT / "skills" / "development-loop"
EXAMPLE_AGENT = EXAMPLE_ROOT / "agents" / "development-loop.md"
sys.path.insert(0, str(BUILDER))

import scaffold  # noqa: E402
import state_manager as sm  # noqa: E402


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / ".claude" / "skills" / "loop"
        self.agent = Path(self.tmp.name) / ".claude" / "agents" / "development-loop.md"

    def test_generates_a_runnable_loop(self):
        res = scaffold.generate(EXAMPLE / "flow.json", self.out)
        for rel in ["SKILL.md", "flow.json", "state.schema.json", "state_manager.py",
                    "steps/jira-context.md", "steps/requirements.md", "steps/decompose.md",
                    "steps/implement.md", "steps/review.md", "steps/open-pr.md"]:
            self.assertTrue((self.out / rel).is_file(), rel)
        self.assertIn("hook_settings", res)
        self.assertTrue(self.agent.is_file())
        self.assertEqual(Path(res["agent"]).resolve(), self.agent.resolve())
        orchestrator = self.agent.read_text()
        self.assertIn("2 in parallel: `correctness` (general-purpose), `verification` (general-purpose)", orchestrator)
        self.assertIn("| `development-loop-implementer` |", orchestrator)
        self.assertIn("python3 .claude/skills/development-loop/state_manager.py --run <RUN_ID>", orchestrator)
        self.assertIn("**G4** before, hook-enforced", orchestrator)
        self.assertIn("init --input story=<Jira issue key, e.g. PROJ-123>", orchestrator)
        # the copied engine runs against the copied flow with no --flow argument
        self.assertEqual(sm.simulate(self.out / "flow.json")[-1], "done")

    def test_keeps_edited_files_unless_forced(self):
        scaffold.generate(EXAMPLE / "flow.json", self.out)
        (self.out / "steps/review.md").write_text("mine")
        (self.out / "SKILL.md").write_text("mine")
        self.agent.write_text("mine")
        res = scaffold.generate(EXAMPLE / "flow.json", self.out)
        self.assertIn("SKILL.md", res["kept"])
        self.assertEqual(self.agent.read_text(), "mine")
        self.assertEqual((self.out / "steps/review.md").read_text(), "mine")
        self.assertEqual((self.out / "SKILL.md").read_text(), "mine")
        scaffold.generate(EXAMPLE / "flow.json", self.out, force=True)
        self.assertNotEqual((self.out / "SKILL.md").read_text(), "mine")
        self.assertNotEqual(self.agent.read_text(), "mine")
        self.assertEqual((self.out / "steps/review.md").read_text(), "mine")

    def test_stub_mentions_inputs_outputs_and_schema(self):
        scaffold.generate(EXAMPLE / "flow.json", self.out)
        stub = (self.out / "steps/decompose.md").read_text()
        self.assertIn("From `requirements`: requirements.md", stub)
        self.assertIn("a JSON object with a `subtasks` list", stub)
        self.assertIn('"verification"', stub)
        self.assertIn("gate **G2**", stub)
        review = (self.out / "steps/review.md").read_text()
        self.assertIn("From `implement` for the same item: subtasks/{item}/implementation.md", review)

    def test_orchestrator_agent(self):
        scaffold.generate(EXAMPLE / "flow.json", self.out)
        text = self.agent.read_text()
        front, body = text.split("\n---\n", 1)
        fields = dict(line.split(": ", 1) for line in front.splitlines()[1:])
        self.assertEqual(fields["name"], "development-loop")
        self.assertEqual(fields["tools"], "Agent(development-loop-jira, general-purpose, development-loop-implementer), "
                                          "Bash, Read, Glob, Grep, AskUserQuestion")
        self.assertEqual((fields["model"], fields["color"]), ("inherit", "blue"))
        self.assertLessEqual(len(json.loads(fields["description"])), scaffold.MAX_DESCRIPTION)
        self.assertIn("Started by /development-loop", json.loads(fields["description"]))
        self.assertIn("Session mode", json.loads(fields["initialPrompt"]))
        self.assertIn("orchestrator **only**", body)
        self.assertIn("### Command mode", body)
        self.assertIn("/development-loop <RUN_ID> reject <GATE> <what must change>", body)
        self.assertIn("Never approve or reject a gate that the arguments didn't decide.", body)
        self.assertIn("all of them in parallel", body)
        self.assertNotIn("do the step yourself", body)
        self.assertIn("](../skills/development-loop/steps/review.md)", body)

    def test_agent_options(self):
        custom = Path(self.tmp.name) / "elsewhere" / "orch.md"
        res = scaffold.generate(EXAMPLE / "flow.json", self.out, agent_path=custom)
        self.assertTrue(custom.is_file())
        self.assertFalse(self.agent.exists())
        other = Path(self.tmp.name) / "plain"
        res = scaffold.generate(EXAMPLE / "flow.json", other, with_agent=False)
        self.assertIsNone(res["agent"])
        self.assertFalse((other / "agents" / "development-loop.md").exists())
        self.assertTrue((other / "agents" / "development-loop-jira.md").exists())
        # without an agent the command orchestrates inline instead of forking
        skill = (other / "SKILL.md").read_text()
        self.assertNotIn("context: fork", skill)
        self.assertIn("If `run_in_subagent` is false, do the step yourself", skill)
        self.assertIn("## Procedure", skill)

    def test_command_forks_into_the_agent(self):
        scaffold.generate(EXAMPLE / "flow.json", self.out)
        skill = (self.out / "SKILL.md").read_text()
        front, body = skill.split("\n---\n", 1)
        fields = dict(line.split(": ", 1) for line in front.splitlines()[1:])
        self.assertEqual((fields["context"], fields["agent"]), ("fork", "development-loop"))
        self.assertEqual((fields["background"], fields["disable-model-invocation"]), ("false", "true"))
        self.assertLessEqual(len(json.loads(fields["description"])), scaffold.MAX_DESCRIPTION)
        self.assertIn("$ARGUMENTS", body)
        self.assertIn("command mode", body)
        self.assertLess(len(body), 800)  # the procedure lives in the agent, not here

    def test_subagent_types_and_agent_skill_stubs(self):
        flow = json.loads((EXAMPLE / "flow.json").read_text())
        flow["steps"][3]["cycle"][1]["agents"][1].update(skill="steps/review-tests.md", subagent_type="test-runner")
        path = Path(self.tmp.name) / "flow.json"
        path.write_text(json.dumps(flow))
        scaffold.generate(path, self.out)
        self.assertIn("tools: Agent(development-loop-jira, general-purpose, development-loop-implementer, test-runner),",
                      self.agent.read_text())
        self.assertIn("`verification` (test-runner)", self.agent.read_text())
        stub = (self.out / "steps/review-tests.md").read_text()
        self.assertIn("# Step: review (verification)", stub)
        self.assertIn("`subtasks/{item}/review-verification.md`", stub)
        self.assertIn("used by: `verification`", stub)

    def test_custom_subagents(self):
        res = scaffold.generate(EXAMPLE / "flow.json", self.out)
        agents_dir = self.agent.parent
        impl = agents_dir / "development-loop-implementer.md"
        self.assertEqual(set(res["subagents"]), {"development-loop-jira", "development-loop-implementer"})
        front, body = impl.read_text().split("\n---\n", 1)
        self.assertIn("name: development-loop-implementer", front)
        self.assertIn("tools: Read, Write, Edit, Glob, Grep, Bash", front)
        self.assertIn("launches you for these steps: `implement`", body)
        self.assertIn("## Role", body)
        jira = (agents_dir / "development-loop-jira.md").read_text()
        self.assertIn("model: haiku", jira)
        self.assertIn("disallowedTools: Edit, NotebookEdit, Agent", jira)

        # the prompt body is the user's; frontmatter follows flow.json
        impl.write_text(front + "\n---\nMy own prompt.\n")
        flow = json.loads((EXAMPLE / "flow.json").read_text())
        flow["subagents"]["development-loop-implementer"]["model"] = "opus"
        path = Path(self.tmp.name) / "flow.json"
        path.write_text(json.dumps(flow))
        res = scaffold.generate(path, self.out)
        text = impl.read_text()
        self.assertIn("model: opus", text)
        self.assertTrue(text.endswith("\n---\nMy own prompt.\n"))
        self.assertIn(scaffold._rel(impl), res["written"])
        res = scaffold.generate(path, self.out)
        self.assertIn(scaffold._rel(impl), res["kept"])

    def test_validate_warnings(self):
        flow = json.loads((EXAMPLE / "flow.json").read_text())
        flow["description"] = "x" * 250
        flow["steps"][1]["subagent_type"] = "my-existing-agent"
        flow["subagents"]["unused-helper"] = {"description": "helps"}
        path = Path(self.tmp.name) / "flow.json"
        path.write_text(json.dumps(flow))
        _, warnings = scaffold.validate(path)
        text = "\n".join(warnings)
        self.assertIn("250 characters", text)
        self.assertIn("'my-existing-agent' is not defined in 'subagents'", text)
        self.assertIn("'unused-helper' is defined but no step uses it", text)
        self.assertNotIn("general-purpose", text)

    def test_invalid_flow_is_refused(self):
        bad = Path(self.tmp.name) / "flow.json"
        bad.write_text(json.dumps({"name": "x", "steps": [{"id": "a", "skill": "a.md", "needs": ["b"]}]}))
        with self.assertRaises(sm.EngineError):
            scaffold.generate(bad, self.out)
        self.assertFalse(self.out.exists())

    def test_schema_describes_every_step(self):
        flow = sm.load_flow(EXAMPLE / "flow.json")
        schema = scaffold.state_schema(flow)
        self.assertEqual(schema["properties"]["steps"]["required"],
                         ["jira_context", "requirements", "decomposition", "subtasks", "open_pr", "fix_ci", "ci"])
        item = schema["properties"]["steps"]["properties"]["subtasks"]["properties"]["items"]["items"]
        self.assertEqual(item["properties"]["steps"]["required"], ["implement", "review"])
        self.assertIn("agents", schema["$defs"]["unit"]["properties"])


class TestCapabilities(unittest.TestCase):
    """Can each step's subagent actually do the step?"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agents = Path(self.tmp.name) / "agents"
        self.agents.mkdir()

    def report(self, steps, subagents=None):
        flow = {"name": "demo", "steps": steps, "subagents": subagents or {}}
        return scaffold.capability_report(flow, [self.agents])

    def test_read_only_reviewer_cannot_write_its_review(self):
        errors, _ = self.report(
            [{"id": "review", "skill": "r.md", "artifacts": ["review.md"], "subagent_type": "demo-reviewer"}],
            {"demo-reviewer": {"description": "reviews", "tools": ["Read", "Grep"]}})
        self.assertEqual(len(errors), 1)
        self.assertIn("step 'review' (subagent `demo-reviewer`) must write its output files but has no Write, Edit or Bash tool",
                      errors[0])

    def test_shell_only_writer_is_a_warning(self):
        errors, warnings = self.report(
            [{"id": "a", "skill": "a.md", "artifacts": ["a.md"], "subagent_type": "demo-shell"}],
            {"demo-shell": {"description": "x", "tools": "Read, Bash"}})
        self.assertEqual(errors, [])
        self.assertIn("would have to use the shell", warnings[0])

    def test_required_tools_commands_and_mcp(self):
        errors, _ = self.report(
            [{"id": "fetch", "skill": "f.md", "artifacts": ["f.md"], "subagent_type": "demo-fetch",
              "requires": {"commands": ["acli"], "mcp": ["atlassian"], "tools": ["WebFetch"]}}],
            {"demo-fetch": {"description": "x", "tools": ["Read", "Write"]}})
        text = "\n".join(errors)
        self.assertIn("needs the `WebFetch` tool", text)
        self.assertIn("needs to run acli but has no Bash tool", text)
        self.assertIn("needs the `atlassian` MCP server's tools", text)
        errors, _ = self.report(
            [{"id": "fetch", "skill": "f.md", "artifacts": ["f.md"], "subagent_type": "demo-fetch",
              "requires": {"commands": ["acli"], "mcp": ["atlassian", "slack"], "tools": ["WebFetch"]}}],
            {"demo-fetch": {"description": "x",
                            "tools": ["Read", "Write", "Bash(acli:*)", "WebFetch", "mcp__atlassian__*", "mcp__slack"]}})
        self.assertEqual(errors, [])

    def test_disallowed_tools_and_builtins(self):
        errors, _ = self.report(
            [{"id": "a", "skill": "a.md", "artifacts": ["a.md"], "subagent_type": "demo-x"},
             {"id": "b", "skill": "b.md", "artifacts": ["b.md"], "subagent_type": "Explore"},
             {"id": "c", "skill": "c.md", "artifacts": ["c.md"]}],
            {"demo-x": {"description": "x", "disallowedTools": ["Write", "Edit", "Bash"]}})
        self.assertEqual([e.split(" (subagent")[0] for e in errors], ["step 'a'", "step 'b'"])

    def test_existing_agent_files_are_read(self):
        (self.agents / "my-writer.md").write_text("---\nname: my-writer\ntools:\n  - Read\n  - Grep\n---\nbody\n")
        errors, warnings = self.report([
            {"id": "a", "skill": "a.md", "artifacts": ["a.md"], "subagent_type": "my-writer"},
            {"id": "b", "skill": "b.md", "artifacts": ["b.md"], "subagent_type": "nobody-knows"}])
        self.assertIn("my-writer.md", errors[0])
        self.assertIn("agent not found", warnings[0])

    def test_parallel_agents_and_engine_run_waits(self):
        errors, _ = self.report([
            {"id": "review", "skill": "r.md", "artifacts": ["r-{agent}.md"],
             "agents": [{"id": "ok"}, {"id": "ro", "subagent_type": "Plan"}]},
            {"id": "ci", "wait": {"check": {"run": "true"}}}])
        self.assertEqual(len(errors), 1)
        self.assertIn("step 'review' agent 'ro' (subagent `Plan`)", errors[0])

    def test_validate_raises_on_capability_errors(self):
        flow = json.loads((EXAMPLE / "flow.json").read_text())
        flow["subagents"]["development-loop-jira"]["tools"] = ["Read"]
        path = Path(self.tmp.name) / "flow.json"
        path.write_text(json.dumps(flow))
        with self.assertRaises(sm.EngineError) as cm:
            scaffold.validate(path, self.agents)
        self.assertIn("step 'jira_context' (subagent `development-loop-jira`) must write its output files", str(cm.exception))


class TestReadinessCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_flow(self, flow):
        path = Path(self.tmp.name) / "flow.json"
        path.write_text(json.dumps(flow))
        return path

    def test_example_is_ready(self):
        report = scaffold.check(EXAMPLE / "flow.json", EXAMPLE_ROOT / "agents", environment=False)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual(report["gaps"], [])
        scenarios = {p["scenario"]: p["end"] for p in report["paths"]}
        self.assertEqual(scenarios["happy path"], "done")
        self.assertEqual(scenarios["'review' repeat condition never passes"], "done")  # on_exhausted: gate
        self.assertEqual(scenarios["wait 'ci' reports failure once"], "done")
        self.assertTrue(scenarios["wait 'ci' fails every time"].startswith("FAILED fix_ci"))
        self.assertTrue(scenarios["gate G3 rejected every time"].startswith("FAILED"))
        text = scaffold.render_check(report)
        self.assertTrue(text.startswith("READY for a first run"))

    def test_gaps_endless_loops_and_environment(self):
        path = self.write_flow({"name": "demo", "requires": {"commands": ["definitely-not-installed-xyz"]}, "steps": [
            {"id": "a", "skill": "a.md", "artifacts": ["a.md"], "max_iterations": 100000, "gate": {"id": "G1"}}]})
        report = scaffold.check(path)
        self.assertFalse(report["ok"])
        text = "\n".join(report["errors"])
        self.assertIn("environment: command `definitely-not-installed-xyz` is not on PATH", text)
        self.assertIn("path 'gate G1 rejected every time' ends with: stopped after", text)
        self.assertIn("'a': a.md only need to exist", report["gaps"][0])
        self.assertTrue(scaffold.render_check(report).startswith("NOT READY"))

    def test_invalid_flow(self):
        report = scaffold.check(self.write_flow({"name": "demo", "steps": [{"id": "a", "skil": "a.md"}]}))
        self.assertFalse(report["ok"])
        self.assertIn("did you mean 'skill'", report["errors"][0])

    def test_cli_exit_codes(self):
        import subprocess
        ok = subprocess.run([sys.executable, str(BUILDER / "scaffold.py"), "check", str(EXAMPLE / "flow.json"), "--no-env"],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        bad = subprocess.run([sys.executable, str(BUILDER / "scaffold.py"), "check", "--json",
                              str(self.write_flow({"name": "demo", "steps": []}))], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertFalse(json.loads(bad.stdout)["ok"])


class TestGeneratedDocs(unittest.TestCase):
    def test_stub_lists_what_is_checked(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / ".claude" / "skills" / "loop"
            scaffold.generate(EXAMPLE / "flow.json", out)
            impl = (out / "steps/implement.md").read_text()
            self.assertIn("## Checked automatically", impl)
            self.assertIn("`subtasks/{item}/implementation.md`: must have the headings `## Changes`, `## Verification`, `## Commit`",
                          impl)
            self.assertIn("the command `test -z \"$(git status --porcelain --untracked-files=no)\"` must exit 0", impl)
            self.assertIn("step fails after 3 refusals", impl)
            self.assertFalse((out / "steps" / "ci.md").exists())  # the engine runs the CI check itself

    def test_plain_outputs_are_flagged_in_the_stub(self):
        flow = {"name": "demo", "steps": [{"id": "a", "skill": "steps/a.md", "artifacts": ["a.md"]}]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "flow.json"
            path.write_text(json.dumps(flow))
            scaffold.generate(path, Path(d) / "out", with_agent=False)
            self.assertIn("TODO: only that the output files exist", (Path(d) / "out/steps/a.md").read_text())

    def test_table_shows_conditions_loops_and_waits(self):
        table = scaffold.steps_table(sm.load_flow(EXAMPLE / "flow.json"))
        self.assertIn("| Artifacts (✓ = contract) | Subagents | Runs when / loops / waits |", table)
        self.assertIn("checks: changes are committed", table)
        self.assertIn('only if ci.status.json → status == "failed"', table)
        self.assertIn("max 3 rounds → `implement`; then gate", table)
        self.assertIn("waits on `sh .claude/skills/development-loop/scripts/ci_status.sh` every 3m, up to 2h; on failure → `fix_ci`",
                      table)
        self.assertIn("none (the engine runs the check)", table)


class TestExampleIsInSync(unittest.TestCase):
    """examples/development-loop is generator output plus hand-written steps."""

    def test_engine_copy_matches(self):
        self.assertEqual((EXAMPLE / "state_manager.py").read_text(), (BUILDER / "state_manager.py").read_text(),
                         "run: python3 skills/agentic-loop-builder/scaffold.py generate "
                         "examples/development-loop/.claude/skills/development-loop/flow.json "
                         "--out examples/development-loop/.claude/skills/development-loop --force")

    def test_generated_files_match(self):
        flow = sm.load_flow(EXAMPLE / "flow.json")
        self.assertEqual(json.loads((EXAMPLE / "state.schema.json").read_text()), scaffold.state_schema(flow))
        self.assertEqual((EXAMPLE / "SKILL.md").read_text(), scaffold.skill_md(flow, ".claude/skills/development-loop"))
        self.assertEqual(EXAMPLE_AGENT.read_text(), scaffold.agent_md(flow, ".claude/skills/development-loop"))
        for name, spec in flow["subagents"].items():
            text = (EXAMPLE_ROOT / "agents" / f"{name}.md").read_text()
            self.assertTrue(text.startswith(scaffold.subagent_frontmatter(name, spec)), name)

    def test_step_skills_are_written(self):
        for path in [*(EXAMPLE / "steps").glob("*.md"), *(EXAMPLE_ROOT / "agents").glob("*.md")]:
            self.assertNotIn("TODO", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
