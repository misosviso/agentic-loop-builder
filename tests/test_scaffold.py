import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "skills" / "agentic-loop-builder"
EXAMPLE = ROOT / "examples" / "development-loop"
sys.path.insert(0, str(BUILDER))

import scaffold  # noqa: E402
import state_manager as sm  # noqa: E402


class TestGenerate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "loop"

    def test_generates_a_runnable_loop(self):
        res = scaffold.generate(EXAMPLE / "flow.json", self.out)
        for rel in ["SKILL.md", "flow.json", "state.schema.json", "state_manager.py",
                    "steps/jira-context.md", "steps/requirements.md", "steps/decompose.md",
                    "steps/implement.md", "steps/review.md", "steps/open-pr.md"]:
            self.assertTrue((self.out / rel).is_file(), rel)
        self.assertIn("hook_settings", res)
        skill = (self.out / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: development-loop\ndescription: Take a Jira story"))
        self.assertIn("python3 .claude/skills/development-loop/state_manager.py --run <RUN_ID>", skill)
        self.assertIn("**G4** before, hook-enforced", skill)
        self.assertIn("init --input story=<Jira issue key, e.g. PROJ-123>", skill)
        # the copied engine runs against the copied flow with no --flow argument
        self.assertEqual(sm.simulate(self.out / "flow.json")[-1], "done")

    def test_keeps_edited_files_unless_forced(self):
        scaffold.generate(EXAMPLE / "flow.json", self.out)
        (self.out / "steps/review.md").write_text("mine")
        (self.out / "SKILL.md").write_text("mine")
        res = scaffold.generate(EXAMPLE / "flow.json", self.out)
        self.assertIn("SKILL.md", res["kept"])
        self.assertEqual((self.out / "steps/review.md").read_text(), "mine")
        self.assertEqual((self.out / "SKILL.md").read_text(), "mine")
        scaffold.generate(EXAMPLE / "flow.json", self.out, force=True)
        self.assertNotEqual((self.out / "SKILL.md").read_text(), "mine")
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


class TestExampleIsInSync(unittest.TestCase):
    """examples/development-loop is generator output plus hand-written steps."""

    def test_engine_copy_matches(self):
        self.assertEqual((EXAMPLE / "state_manager.py").read_text(), (BUILDER / "state_manager.py").read_text(),
                         "run: python3 skills/agentic-loop-builder/scaffold.py generate "
                         "examples/development-loop/flow.json --out examples/development-loop --force")

    def test_generated_files_match(self):
        flow = sm.load_flow(EXAMPLE / "flow.json")
        self.assertEqual(json.loads((EXAMPLE / "state.schema.json").read_text()), scaffold.state_schema(flow))
        self.assertEqual((EXAMPLE / "SKILL.md").read_text(), scaffold.orchestrator_md(flow, ".claude/skills/development-loop"))

    def test_step_skills_are_written(self):
        for path in (EXAMPLE / "steps").glob("*.md"):
            self.assertNotIn("TODO", path.read_text(), path.name)


if __name__ == "__main__":
    unittest.main()
