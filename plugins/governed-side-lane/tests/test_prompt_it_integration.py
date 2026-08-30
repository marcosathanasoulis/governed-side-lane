from __future__ import annotations

from pathlib import Path
import unittest


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "prompt-it-side-lane-routing"
    / "SKILL.md"
)


class PromptItIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_optional_runner_preserves_normal_prompt_it_flow(self) -> None:
        self.assertIn("normal Prompt it workflow remains fully usable", self.text)
        self.assertIn("continue with ordinary in-host", self.text)
        self.assertIn("Do not block the brief", self.text)
        self.assertIn("not install or configure the runner", self.text)

    def test_recommendation_is_presence_only_and_never_auto_dispatches(self) -> None:
        self.assertIn("check-capabilities", self.text)
        self.assertIn("recommend", self.text)
        self.assertIn("presence-only", self.text)
        self.assertIn("never read or infer", self.text)
        self.assertIn("Prompt it approval remains required", self.text)
        self.assertIn("never as authorization", self.text)

    def test_coordinator_stays_fixed_and_each_worker_host_is_hard_gated(self) -> None:
        self.assertIn("keeps its Codex coordinator", self.text)
        self.assertIn("keeps its Claude coordinator", self.text)
        self.assertIn("moves the coordinator or borrows connector identity", self.text)
        self.assertIn("worker host satisfies every required connector/MCP", self.text)
        self.assertIn("GLM is a stricter explicit gate", self.text)
        self.assertIn("never use GLM or another", self.text)

    def test_routing_uses_task_evidence_not_tier_equivalence_or_quota_inference(
        self,
    ) -> None:
        self.assertIn("facts discovered during Prompt it research", self.text)
        self.assertIn("Never state or imply", self.text)
        self.assertIn("quality floor", self.text)
        self.assertIn("best-fit", self.text)
        self.assertIn("cost-optimized", self.text)
        self.assertIn("extra-usage statement", self.text)
        self.assertIn("Community consensus alone never activates", self.text)


if __name__ == "__main__":
    unittest.main()
