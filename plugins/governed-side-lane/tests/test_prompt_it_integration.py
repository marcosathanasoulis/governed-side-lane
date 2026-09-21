from __future__ import annotations

from pathlib import Path
import unittest


SKILL = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "prompt-it-side-lane-routing"
    / "SKILL.md"
)
GOVERNANCE = Path(__file__).resolve().parents[1] / "config" / "lane-governance.md"
README = Path(__file__).resolve().parents[3] / "README.md"


class PromptItIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = " ".join(SKILL.read_text(encoding="utf-8").split())
        cls.governance = " ".join(GOVERNANCE.read_text(encoding="utf-8").split())
        cls.readme = " ".join(README.read_text(encoding="utf-8").split())

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
        self.assertIn("generic Prompt it invocation never authorizes dispatch", self.text)
        self.assertIn("never as authorization", self.text)

    def test_preapproval_dispatch_requires_bounded_external_research_authority(
        self,
    ) -> None:
        self.assertIn("explicitly authorized a bounded external research team", self.text)
        self.assertIn("qualified `review` route", self.text)
        self.assertIn("All implementation dispatch waits for execution-brief approval", self.text)
        self.assertNotIn(
            "Prompt it approval remains required before any external lane is dispatched",
            self.text,
        )

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

    def test_preapproved_backup_is_a_bounded_visible_reassignment(self) -> None:
        self.assertIn("one preapproved backup", self.text)
        self.assertIn("availability failure", self.text)
        self.assertIn("without another permission pause", self.text)
        self.assertIn("primary is terminal or stopped", self.governance)
        self.assertIn("No third route, cycle, or parallel writer", self.governance)
        self.assertIn("GLM remains fixed to `glm-5.3`", self.governance)

    def test_native_oauth_does_not_generically_fall_back_to_api_key(self) -> None:
        self.assertIn("never automatically or generically fall back to this route", self.readme)
        self.assertIn("exact preapproved backup", self.readme)
        self.assertNotIn("unchanged and never fall back to this route", self.readme)

    def test_authorized_execute_mode_source_research_preserves_review_contract(self) -> None:
        self.assertIn("authorized bounded source-research task", self.text)
        self.assertIn("execute harness", self.text)
        self.assertIn("brief or report-only output", self.text)
        self.assertIn("Read-only scope does not mean strict review-mode-only", self.governance)
        self.assertIn("strict review no-secret/no-MCP contract", self.governance)

    def test_omniroute_add_on_is_optional_and_linked(self) -> None:
        self.assertIn("OmniRoute add-on guide", self.readme)
        self.assertIn("native/direct routes remain fully usable without it", self.text)


if __name__ == "__main__":
    unittest.main()
