from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane.model_selection import MemoryState  # noqa: E402
from side_lane.model_selection import local  # noqa: E402

NOW = 1_800_000_000.0
HOSTS = {"claude": True, "codex": True, "devin": False}


def parse(*argv):
    p = argparse.ArgumentParser()
    local.add_arguments(p)
    return p.parse_args(argv)


def run(*argv, **kw):
    out = io.StringIO()
    kw.setdefault("hosts", HOSTS)
    kw.setdefault("host_usage", {})
    kw.setdefault("openrouter", False)
    kw.setdefault("state", MemoryState())
    code = local.run(parse(*argv), now=NOW, out=out, **kw)
    return code, (json.loads(out.getvalue()) if out.getvalue() else None)


class AdapterTests(unittest.TestCase):
    def test_bindings_are_opaque_refs_for_signed_in_hosts_only(self):
        b = local.bindings_for(HOSTS, "me", openrouter=True, extra_providers=("glm",))
        self.assertEqual({(x["provider"], x["ref"]) for x in b},
                         {("claude", "local-oauth:claude"), ("codex", "local-oauth:codex"),
                          ("glm", "local-key:glm"), ("openrouter", "local-key:openrouter")})
        self.assertNotIn("devin", {x["provider"] for x in b})

    def test_usage_declarations_map_to_states(self):
        obs = local.availability_from_usage({"claude": "included-oauth", "codex": "extra-usage"}, HOSTS, NOW)
        self.assertEqual({o["provider"]: o["state"] for o in obs}, {"claude": "available", "codex": "exhausted"})
        private = local.availability_from_usage({}, HOSTS, NOW, unknown_usage="excluded")
        self.assertEqual({o["state"] for o in private}, {"unknown"})

    def test_probe_results_map_to_five_states(self):
        probes = {"claude": {"state": "available", "detail": "ok", "checked_at": NOW},
                  "codex": {"state": "unknown", "detail": "login needed", "checked_at": NOW},
                  "glm": {"state": "unknown", "detail": "timeout", "checked_at": NOW},
                  "gemini": {"state": "unknown", "detail": "unexpected result", "checked_at": NOW},
                  "grok": {"state": "unknown", "detail": "not pinned", "checked_at": NOW},
                  "devin": {"state": "exhausted", "detail": "limit reached", "checked_at": NOW},
                  "x": {"state": "absent", "detail": "", "checked_at": NOW}}
        got = {o["provider"]: o["state"] for o in local.availability_from_probes(probes, NOW)}
        self.assertEqual(got, {"claude": "available", "codex": "auth_failed", "glm": "transient",
                               "gemini": "transient", "grok": "transient", "devin": "exhausted"})


class RunTests(unittest.TestCase):
    def test_single_task_selects_and_exits_zero(self):
        code, sel = run("--task", "fix it", "--kind", "coding")
        self.assertEqual((code, sel["status"], sel["tier"]), (0, "selected", "high"))
        self.assertIn(sel["provider"], {"claude", "codex"})
        self.assertTrue(sel["binding_ref"].startswith("local-oauth:"))

    def test_nodes_are_routed_one_by_one_and_spread(self):
        with tempfile.TemporaryDirectory() as d:
            nodes = Path(d) / "n.json"
            nodes.write_text(json.dumps([{"id": f"n{i}", "task": "t", "tier": "medium"} for i in range(4)]))
            code, out = run("--nodes", str(nodes), "--thread-id", "T")
        self.assertEqual(code, 0)
        providers = [n["selection"]["provider"] for n in out["nodes"]]
        self.assertEqual((providers.count("claude"), providers.count("codex")), (2, 2))
        self.assertEqual(out["nodes"][0]["selection"]["thread_id"], "T:n0")

    def test_pin_and_no_metered_default_are_respected(self):
        code, sel = run("--task", "x", "--tier", "high", "--pin-provider", "codex")
        self.assertEqual((code, sel["provider"], sel["model"]), (0, "codex", "gpt-6.1-sol"))
        code, sel = run("--task", "x", "--tier", "high", host_usage={"claude": "extra-usage", "codex": "extra-usage"})
        self.assertEqual((code, sel["status"]), (3, "no_eligible"))
        code, sel = run("--task", "x", "--tier", "high", host_usage={"claude": "extra-usage", "codex": "extra-usage"},
                        openrouter=True, allow_metered_default=True)
        self.assertEqual((code, sel["route"]), (0, "metered_openrouter"))

    def test_previous_selection_keeps_the_route_on_follow_up(self):
        state = MemoryState()
        _, first = run("--task", "x", "--tier", "medium", "--thread-id", "T", state=state)
        with tempfile.TemporaryDirectory() as d:
            prev = Path(d) / "p.json"
            prev.write_text(json.dumps(first))
            _, again = run("--task", "x", "--tier", "medium", "--thread-id", "T", "--previous", str(prev), state=state)
        self.assertTrue(again["affinity"]["sticky"])
        self.assertEqual(again["provider"], first["provider"])

    def test_a_declared_glm_plan_makes_glm_an_automatic_included_candidate(self):
        self.assertEqual(local.included_plans_from({"glm": "included-oauth", "claude": "included-oauth"}), ["glm"])
        self.assertEqual(local.included_plans_from({"glm": "extra-usage", "codex": "included-oauth"}), [])
        hosts = {"claude": True, "codex": True}
        usage = {"glm": "included-oauth"}
        probes = {p: {"state": "available", "detail": "ok", "checked_at": NOW} for p in ("claude", "codex", "glm")}
        state = MemoryState()
        seen = set()
        for i in range(6):
            code, sel = run("--task", "x", "--tier", "medium", "--thread-id", f"T{i}", hosts=hosts, host_usage=usage,
                            probes=probes, extra_providers=("glm",), state=state)
            self.assertEqual(code, 0)
            seen.add(sel["provider"])
        self.assertEqual(seen, {"claude", "codex", "glm"})
        for i in range(6):  # no declaration (public default): GLM is never chosen automatically
            _, sel = run("--task", "x", "--tier", "medium", "--thread-id", f"U{i}", hosts=hosts, host_usage={},
                         probes=probes, extra_providers=("glm",), state=MemoryState())
            self.assertNotEqual(sel["provider"], "glm")

    def test_extra_provider_availability_follows_the_declaration_not_unknown(self):
        out = local._extra_availability(("glm",), {"glm": "included-oauth"}, set(), NOW)
        self.assertEqual((out[0]["state"], out[0]["source"]), ("available", "declared-usage"))
        self.assertEqual(local._extra_availability(("glm",), {"glm": "extra-usage"}, set(), NOW)[0]["state"], "exhausted")
        self.assertEqual(local._extra_availability(("glm",), {}, set(), NOW)[0]["state"], "unknown")
        self.assertEqual(local._extra_availability(("glm",), {"glm": "included-oauth"}, {"glm"}, NOW), [])

    def test_the_public_command_with_a_declaration_and_no_probe_puts_glm_in_the_spread(self):
        hosts = {"claude": True, "codex": True}
        seen = set()
        state = MemoryState()
        for i in range(6):
            _, sel = run("--task", "x", "--tier", "medium", "--thread-id", f"P{i}", hosts=hosts,
                         host_usage={"glm": "included-oauth"}, extra_providers=("glm",), state=state)
            seen.add(sel["provider"])
        self.assertEqual(seen, {"claude", "codex", "glm"})  # no probes: the declaration is the evidence

    def test_the_declarable_set_equals_the_tier_table(self):
        from side_lane.model_selection import load
        self.assertEqual(set(local.DECLARABLE_PLANS), {c.provider for c in load().entries if c.declarable_included})

    def test_a_declared_gemini_plan_joins_the_low_spread_only_with_a_live_probe(self):
        self.assertEqual(local.included_plans_from({"gemini": "included-oauth", "glm": "included-oauth"}), ["gemini", "glm"])
        hosts = {"claude": True, "codex": True, "gemini": True}
        state = MemoryState()
        for i in range(8):  # declared but never probed (the public path): not selected automatically
            _, sel = run("--task", "x", "--tier", "low", "--thread-id", f"D{i}", hosts=hosts,
                         host_usage={"gemini": "included-oauth"}, state=state)
            self.assertNotEqual(sel["provider"], "gemini")
        probes = {p: {"state": "available", "detail": "ok", "checked_at": NOW} for p in ("claude", "codex", "gemini")}
        seen = set()
        state = MemoryState()
        for i in range(6):  # declared and live-probed available: joins the spread
            _, sel = run("--task", "x", "--tier", "low", "--thread-id", f"G{i}", hosts=hosts,
                         host_usage={"gemini": "included-oauth"}, probes=probes, state=state)
            seen.add(sel["provider"])
        self.assertEqual(seen, {"claude", "codex", "gemini"})
        for i in range(6):  # probed but not declared: still never automatic
            _, sel = run("--task", "x", "--tier", "low", "--thread-id", f"H{i}", hosts=hosts, host_usage={},
                         probes=probes, state=MemoryState())
            self.assertNotEqual(sel["provider"], "gemini")
        failed = dict(probes, gemini={"state": "unknown", "detail": "ModelNotFound", "checked_at": NOW})
        for i in range(6):  # a failed probe (no access, wrong model) is not routable
            _, sel = run("--task", "x", "--tier", "low", "--thread-id", f"F{i}", hosts=hosts,
                         host_usage={"gemini": "included-oauth"}, probes=failed, state=MemoryState())
            self.assertNotEqual(sel["provider"], "gemini")
    def test_a_failed_probe_is_never_routable(self):
        probes = {"claude": {"state": "unknown", "detail": "not pinned", "checked_at": NOW},
                  "codex": {"state": "available", "detail": "ok", "checked_at": NOW}}
        code, sel = run("--task", "x", "--tier", "medium", probes=probes, host_usage={})
        self.assertEqual((code, sel["provider"]), (0, "codex"))
        down = {p: {"state": "unknown", "detail": "network", "checked_at": NOW} for p in ("claude", "codex")}
        code, sel = run("--task", "x", "--tier", "medium", probes=down, host_usage={})
        self.assertEqual((code, sel["status"]), (3, "no_eligible"))

    def test_node_ids_must_be_unique_non_empty_strings(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ([{"id": "a", "task": "t"}, {"id": "a", "task": "t"}], [{"id": 1, "task": "t"}],
                        [{"id": " ", "task": "t"}], [{"id": "a", "task": ""}], [{"id": "a"}], []):
                f = Path(d) / "n.json"
                f.write_text(json.dumps(bad))
                self.assertEqual(run("--nodes", str(f))[0], 2, bad)

    def test_previous_graph_output_keeps_each_nodes_route(self):
        state = MemoryState()
        with tempfile.TemporaryDirectory() as d:
            nodes = Path(d) / "n.json"
            nodes.write_text(json.dumps([{"id": "a", "task": "t", "tier": "medium"}, {"id": "b", "task": "t", "tier": "medium"}]))
            _, first = run("--nodes", str(nodes), "--thread-id", "T", state=state)
            prev = Path(d) / "p.json"
            prev.write_text(json.dumps(first))
            _, again = run("--nodes", str(nodes), "--previous", str(prev), state=state)
            self.assertTrue(all(n["selection"]["affinity"]["sticky"] for n in again["nodes"]))
            self.assertEqual([n["selection"]["provider"] for n in again["nodes"]],
                             [n["selection"]["provider"] for n in first["nodes"]])
            self.assertEqual(again["thread_id"], "T")
            prev.write_text(json.dumps([1]))
            self.assertEqual(run("--nodes", str(nodes), "--previous", str(prev), state=state)[0], 2)

    def test_signed_in_hosts_gate_on_the_auth_preflight_not_presence(self):
        gated = local.signed_in_hosts({"claude": True, "codex": True, "devin": False},
                                      status_fn=lambda h: h == "codex")
        self.assertEqual(gated, {"claude": False, "codex": True, "devin": False})

        def boom(host):
            raise RuntimeError("status exploded")

        self.assertEqual(local.signed_in_hosts({"claude": True}, status_fn=boom), {"claude": False})

    def test_global_pins_apply_to_every_graph_node_unless_a_node_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            nodes = Path(d) / "n.json"
            nodes.write_text(json.dumps([{"id": "a", "task": "t"}, {"id": "b", "task": "t", "pin_provider": "claude"}]))
            code, out = run("--nodes", str(nodes), "--tier", "high", "--pin-provider", "codex")
        self.assertEqual(code, 0)
        self.assertEqual([n["selection"]["provider"] for n in out["nodes"]], ["codex", "claude"])

    def test_an_invalid_node_leaves_no_partial_routing_state(self):
        state = MemoryState()
        with tempfile.TemporaryDirectory() as d:
            nodes = Path(d) / "n.json"
            nodes.write_text(json.dumps([{"id": "a", "task": "t", "tier": "medium"}, {"id": "b", "task": "t", "tier": "max"}]))
            self.assertEqual(run("--nodes", str(nodes), state=state)[0], 2)
        self.assertEqual(dict(state.counts(local.owner_id())), {})

    def test_pin_only_bindings_let_gemini_and_glm_be_pinned_but_never_auto_selected(self):
        from unittest import mock
        with mock.patch("side_lane.hosts.resolve_host_executable", side_effect=lambda h, **k: "/bin/x" if h == "gemini" else None), \
                mock.patch("side_lane.credentials.credential_present", side_effect=lambda svc, *a: svc == local.GLM_SERVICE):
            hosts, extra = local._pin_only_bindings({"claude": True, "codex": False}, ())
        self.assertEqual((hosts.get("gemini"), extra), (True, ("glm",)))
        bound = dict(hosts)
        code, sel = run("--task", "x", "--tier", "low", "--pin-provider", "gemini", "--allow-metered", hosts=bound)
        self.assertEqual((code, sel["provider"]), (0, "gemini"))
        code, sel = run("--task", "x", "--tier", "medium", "--pin-provider", "glm", "--allow-metered",
                        hosts=bound, extra_providers=("glm",))
        self.assertEqual((code, sel["provider"]), (0, "glm"))
        for tier in ("low", "medium"):  # undeclared: never chosen automatically
            for i in range(4):
                code, sel = run("--task", "x", "--tier", tier, "--thread-id", f"{tier}{i}", hosts=bound,
                                extra_providers=("glm",))
                self.assertNotIn(sel["provider"], ("gemini", "glm"))
    def test_bad_input_exits_two(self):
        self.assertEqual(run()[0], 2)
        self.assertEqual(run("--task", "x", "--nodes", "n.json")[0], 2)

    def test_cli_exposes_select(self):
        import subprocess
        proc = subprocess.run([sys.executable, "-m", "side_lane.cli", "select", "--help"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--thread-id", proc.stdout)

    def test_output_never_contains_secrets(self):
        _, sel = run("--task", "x", "--tier", "medium")
        text = json.dumps(sel).lower()
        for bad in ("api_key", "bearer", "password", "sk-"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
