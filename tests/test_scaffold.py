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
                         ["jira_context", "requirements", "decomposition", "subtasks", "open_pr"])
        item = schema["properties"]["steps"]["properties"]["subtasks"]["properties"]["items"]["items"]
        self.assertEqual(item["properties"]["steps"]["required"], ["implement", "review"])
        self.assertIn("agents", schema["$defs"]["unit"]["properties"])


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
