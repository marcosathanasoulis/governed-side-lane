from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane import model_selection as ms  # noqa: E402
from side_lane.model_selection import availability as av  # noqa: E402
from side_lane.model_selection import openrouter as orr  # noqa: E402
from side_lane.model_selection.contract import Availability, ContractError  # noqa: E402

NOW = 1_800_000_000.0
TABLE = ms.load()


def obs(provider, state="available", at=NOW, **kw):
    return {"provider": provider, "state": state, "checked_at": at, "source": "probe", **kw}


def request(**over):
    base = {
        "caller": "prompt-it", "thread_id": "t1", "task": {"objective": "fix it", "kind": "coding"},
        "owner": {"id": "marcos", "kind": "human"}, "coordinator": "claude", "allow_metered": True,
        "bindings": [{"ref": "b-claude", "provider": "claude", "owner_id": "marcos"},
                     {"ref": "b-codex", "provider": "codex", "owner_id": "marcos"},
                     {"ref": "b-devin", "provider": "devin", "owner_id": "marcos"},
                     {"ref": "b-or", "provider": "openrouter"}],
        "availability": [obs("claude"), obs("codex"), obs("devin")],
    }
    base.update(over)
    return ms.Request.parse(base)


class TierAndRotationTests(unittest.TestCase):
    def test_tiers_are_exactly_low_medium_high_xhigh(self):
        self.assertEqual(ms.TIERS, ("low", "medium", "high", "xhigh"))
        with self.assertRaises(ContractError):
            ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"}, "tier": "max"})

    def test_selector_assigns_tier_with_reason_when_caller_gives_none(self):
        s = ms.select(request(), TABLE, now=NOW)
        self.assertEqual((s.tier, s.tier_assigned_by), ("high", "selector"))
        self.assertIn("coding", s.tier_reason)
        s = ms.select(request(task={"objective": "x"}), TABLE, now=NOW)
        self.assertEqual(s.tier, "medium")

    def test_caller_tier_is_kept(self):
        s = ms.select(request(tier="xhigh"), TABLE, now=NOW)
        self.assertEqual((s.tier, s.tier_assigned_by), ("xhigh", "caller"))
        self.assertEqual(s.model, "claude-fable-5-1")

    def test_work_spreads_evenly_across_providers(self):
        state = ms.MemoryState()
        picks = [ms.select(request(thread_id=f"t{i}", tier="medium"), TABLE, now=NOW, state=state).provider
                 for i in range(60)]
        self.assertEqual({p: picks.count(p) for p in set(picks)}, {"claude": 20, "codex": 20, "devin": 20})

    def test_two_providers_split_evenly_whatever_the_coordinator(self):
        for coordinator in ("claude", "codex"):
            state = ms.MemoryState()
            picks = [ms.select(request(thread_id=f"t{i}", coordinator=coordinator, tier="high",
                                       availability=[obs("claude"), obs("codex")]), TABLE, now=NOW, state=state).provider
                     for i in range(100)]
            self.assertEqual((picks.count("claude"), picks.count("codex")), (50, 50))

    def test_follow_up_does_not_advance_the_spread(self):
        state = ms.MemoryState()
        first = ms.select(request(), TABLE, now=NOW, state=state)
        before = dict(state.counts("marcos"))
        again = ms.select(request(previous_selection=first.to_dict()), TABLE, now=NOW, state=state)
        self.assertTrue(again.affinity["sticky"])
        self.assertEqual((again.provider, again.model), (first.provider, first.model))
        self.assertEqual(dict(state.counts("marcos")), before)

    def test_json_state_persists_between_instances(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spread.json"
            with ms.JsonFileState(path).transaction("marcos") as tx:
                tx.record("claude")
            self.assertEqual(ms.JsonFileState(path).counts("marcos"), {"claude": 1})


class ConsistencyTests(unittest.TestCase):
    def test_prompt_it_and_side_lane_agree_on_identical_input(self):
        a = ms.select(request(caller="prompt-it"), TABLE, now=NOW, state=ms.MemoryState()).to_dict()
        b = ms.select(request(caller="side-lane"), TABLE, now=NOW, state=ms.MemoryState()).to_dict()
        for key in ("provider", "model", "route", "tier", "binding_ref", "selection_id", "fallbacks", "exclusions"):
            self.assertEqual(a[key], b[key], key)


class OwnerTests(unittest.TestCase):
    def test_bot_identity_is_never_the_plan_owner(self):
        s = ms.select(request(owner={"id": "slack-bot", "kind": "service"}), TABLE, now=NOW)
        self.assertEqual((s.route, s.owner_source), ("metered_openrouter", "request"))
        self.assertIn("owner_unresolved", s.uncertainty)
        self.assertEqual(ms.select(request(owner=None, allow_metered=False), TABLE, now=NOW).status, "owner_unresolved")

    def test_owner_survives_follow_up_even_if_requester_changes(self):
        first = ms.select(request(), TABLE, now=NOW)
        follow = ms.select(request(owner={"id": "someone-else", "kind": "human"}, previous_selection=first.to_dict()),
                           TABLE, now=NOW)
        self.assertEqual((follow.owner.id, follow.owner_source), ("marcos", "thread"))
        self.assertEqual(follow.binding_ref, first.binding_ref)
        self.assertTrue(any("plan owner retained" in e["reason"] for e in follow.exclusions))

    def test_binding_must_belong_to_the_owner(self):
        bindings = [{"ref": "b-claude", "provider": "claude", "owner_id": "other"}, {"ref": "b-or", "provider": "openrouter"}]
        s = ms.select(request(bindings=bindings, tier="medium"), TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")
        self.assertTrue(any(e["reason"] == "no_binding" for e in s.exclusions))


class AvailabilityTests(unittest.TestCase):
    def test_available_included_candidates_reach_selection(self):
        s = ms.select(request(tier="medium"), TABLE, now=NOW)
        self.assertIn(s.provider, {"claude", "codex", "devin"})
        self.assertEqual(s.availability_evidence["state"], "available")

    def test_five_states_are_distinct_and_unknown_is_not_exhausted(self):
        self.assertEqual(set(ms.AVAILABILITY_STATES), {"available", "exhausted", "auth_failed", "transient", "unknown"})
        state, _ = av.effective_state(None, NOW)
        self.assertEqual(state, "unknown")
        stale = Availability("claude", "available", NOW - 5000)
        self.assertEqual(av.effective_state(stale, NOW)[0], "unknown")
        self.assertEqual(av.effective_state(Availability("claude", "auth_failed", 0), NOW)[0], "auth_failed")
        reset = Availability("claude", "exhausted", NOW - 10, reset_at=NOW - 1)
        self.assertEqual(av.effective_state(reset, NOW)[0], "unknown")

    def test_unknown_only_candidates_are_used_with_uncertainty_before_metered(self):
        s = ms.select(request(availability=[], tier="high"), TABLE, now=NOW)
        self.assertIn(s.route, {"native_cli", "included_endpoint"})
        self.assertIn("unknown", s.uncertainty)

    def test_exhausted_auth_failed_and_transient_are_excluded_with_reasons(self):
        avail = [obs("claude", "exhausted"), obs("codex", "auth_failed"), obs("devin", "transient")]
        s = ms.select(request(availability=avail, tier="medium"), TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")
        reasons = {e["candidate"].split("/")[0]: e["reason"] for e in s.exclusions if "/" in e["candidate"]}
        self.assertEqual((reasons["claude"], reasons["codex"], reasons["devin"]), ("exhausted", "auth_failed", "transient"))

    def test_unverified_plan_is_excluded_unless_pinned(self):
        hosted = [{"ref": "b-glm", "provider": "glm-coding-plan", "owner_id": "marcos"}, {"ref": "b-or", "provider": "openrouter"}]
        s = ms.select(request(tier="medium", availability=[obs("glm-hosted")], bindings=hosted), TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")
        self.assertTrue(any("plan_unverified" in e["reason"] for e in s.exclusions))


class PinTests(unittest.TestCase):
    def pin(self, provider, model=None, **over):
        pref = {"provider": provider, **({"model": model} if model else {})}
        return ms.select(request(preference=pref, **over), TABLE, now=NOW)

    def test_explicit_native_route_is_honored(self):
        s = self.pin("codex", "gpt-6.1-sol", tier="high")
        self.assertEqual((s.provider, s.model, s.route, s.binding_ref), ("codex", "gpt-6.1-sol", "native_cli", "b-codex"))

    def test_pinned_gemini_needs_metered_allowed_or_a_declaration(self):
        gem = [{"ref": "b-gem", "provider": "gemini", "owner_id": "marcos"}]
        s = self.pin("gemini", bindings=gem, availability=[obs("gemini")], tier="low", allow_metered=False)
        self.assertEqual(s.status, "no_eligible")
        self.assertTrue(any("metered_not_allowed" in e["reason"] for e in s.exclusions))
        self.assertEqual(self.pin("gemini", bindings=gem, availability=[obs("gemini")], tier="low").provider, "gemini")
        declared = self.pin("gemini", bindings=gem, availability=[obs("gemini")], tier="low", allow_metered=False,
                            included_plans=["gemini"])
        self.assertEqual((declared.provider, declared.status), ("gemini", "selected"))
    def test_unusable_pin_is_not_silently_replaced(self):
        s = self.pin("claude", availability=[obs("claude", "exhausted"), obs("codex")], tier="medium")
        self.assertEqual(s.status, "no_eligible")
        self.assertIsNone(s.provider)

    def test_unsupported_pin_is_reported(self):
        self.assertEqual(self.pin("claude", "claude-imaginary").status, "no_eligible")


class AffinityTests(unittest.TestCase):
    def test_exhaustion_reselects_and_records_why(self):
        first = ms.select(request(tier="medium"), TABLE, now=NOW)
        avail = [obs(first.provider, "exhausted")] + [obs(p) for p in ("claude", "codex", "devin") if p != first.provider]
        follow = ms.select(request(tier="medium", availability=avail, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertNotEqual(follow.provider, first.provider)
        self.assertFalse(follow.affinity["sticky"])
        self.assertIn("exhausted", follow.affinity["reselect_reason"])

    def test_materially_changed_task_reselects(self):
        first = ms.select(request(tier="medium"), TABLE, now=NOW)
        follow = ms.select(request(tier="medium", task_changed=True, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertFalse(follow.affinity["sticky"])
        self.assertEqual(follow.affinity["reselect_reason"], "task materially changed")

    def test_explicit_change_overrides_stickiness(self):
        first = ms.select(request(tier="medium"), TABLE, now=NOW)
        other = "codex" if first.provider != "codex" else "claude"
        follow = ms.select(request(tier="medium", preference={"provider": other}, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertEqual(follow.provider, other)

    def test_metered_route_is_sticky_for_follow_ups(self):
        first = ms.select(request(availability=[obs("claude", "exhausted"), obs("codex", "exhausted"), obs("devin", "exhausted")], tier="medium"), TABLE, now=NOW)
        self.assertEqual(first.route, "metered_openrouter")
        follow = ms.select(request(tier="medium", previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertEqual((follow.route, follow.affinity["sticky"]), ("metered_openrouter", True))


class MeteredAndOpenRouterTests(unittest.TestCase):
    def test_metered_fallback_keeps_the_same_tier_and_is_labelled_billed(self):
        s = ms.select(request(availability=[obs(p, "exhausted") for p in ("claude", "codex", "devin")], tier="xhigh"), TABLE, now=NOW)
        self.assertEqual((s.route, s.tier), ("metered_openrouter", "xhigh"))
        self.assertTrue(s.openrouter["billed"])
        self.assertEqual(s.openrouter["cost_tier"], "xhigh")
        self.assertNotIn("allowed_models", s.openrouter)  # no fixed pool: Auto picks any model for the tier

    def test_metered_not_allowed_means_no_eligible_not_silent_spend(self):
        s = ms.select(request(allow_metered=False, availability=[obs(p, "exhausted") for p in ("claude", "codex", "devin")], tier="medium"), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertIsNone(s.route)

    def test_tier_reaches_the_outgoing_auto_request(self):
        for tier in ms.TIERS:
            body = orr.build_auto_request(tier, ["anthropic/*"], [{"role": "user", "content": "task summary"}])
            self.assertEqual(body["model"], "openrouter/auto")
            self.assertEqual(body["plugins"], [{"id": "auto-router", "allowed_models": ["anthropic/*"], "cost_tier": tier}])
        beta = orr.build_auto_request("high", ["a/b"], [], beta=True)
        self.assertEqual((beta["model"], beta["plugins"][0]["id"]), ("openrouter/auto-beta", "auto-beta-router"))

    def test_empty_allowed_list_is_not_widened(self):
        with self.assertRaises(ContractError):
            orr.build_auto_request("medium", [], [])
        self.assertNotIn("allowed_models", orr.build_auto_request("medium", None, [])["plugins"][0])

    def test_response_check_flags_out_of_list_and_unmapped_models(self):
        pool = TABLE.for_tier("medium")
        sent = orr.build_auto_request("medium", orr.allowed_for_tier(pool), [])
        ok = orr.check_response({"model": "anthropic/claude-sonnet-5.5"}, sent, pool)
        self.assertTrue(ok["in_allowed"])
        self.assertEqual(ok["native"], "claude/claude-sonnet-5-5@medium")
        odd = orr.check_response({"model": "mystery/model"}, sent, pool)
        self.assertFalse(odd["in_allowed"])
        self.assertIsNone(odd["native"])
        self.assertIn("no_native_mapping", odd["note"])


class ReviewFindingTests(unittest.TestCase):
    def test_metered_never_hands_out_another_owners_openrouter_binding(self):
        bindings = [{"ref": "or-other", "provider": "openrouter", "owner_id": "someone-else"}]
        s = ms.select(request(bindings=bindings, availability=[], tier="medium",
                              owner={"id": "slack-bot", "kind": "service"}), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertNotIn("or-other", str(s.to_dict()))
        own = [{"ref": "or-mine", "provider": "openrouter", "owner_id": "marcos"}]
        s = ms.select(request(bindings=own, availability=[obs("claude", "exhausted")], tier="medium"), TABLE, now=NOW)
        self.assertEqual(s.binding_ref, "or-mine")

    def test_string_booleans_are_rejected(self):
        for field in ("allow_metered", "task_changed"):
            with self.assertRaises(ContractError):
                ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"}, field: "false"})

    def test_follow_up_keeps_the_original_tier_audit_fields(self):
        first = ms.select(request(), TABLE, now=NOW)
        self.assertEqual(first.tier_assigned_by, "selector")
        follow = ms.select(request(previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertEqual((follow.tier_assigned_by, follow.tier_reason), ("selector", first.tier_reason))

    def test_pin_outside_the_callers_tier_is_not_silently_upgraded(self):
        s = ms.select(request(tier="medium", preference={"provider": "claude", "model": "claude-opus-5-5"}), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertEqual(s.tier, "medium")
        self.assertTrue(any("pin_tier_mismatch" in e["reason"] for e in s.exclusions))

    def test_pin_defines_the_tier_only_when_the_tier_was_a_default(self):
        s = ms.select(request(task={"objective": "x"}, preference={"provider": "claude", "model": "claude-opus-5-5"}), TABLE, now=NOW)
        self.assertEqual((s.status, s.tier, s.tier_assigned_by), ("selected", "high", "preference"))

    def test_concurrent_selections_spread_evenly_through_the_file_state(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            state = ms.JsonFileState(Path(d) / "spread.json")
            picks: list[str] = []
            lock = threading.Lock()

            def work(i):
                sel = ms.select(request(thread_id=f"t{i}", tier="medium", availability=[obs("claude"), obs("codex")]),
                                TABLE, now=NOW, state=state)
                with lock:
                    picks.append(sel.provider)

            threads = [threading.Thread(target=work, args=(i,)) for i in range(24)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual((picks.count("claude"), picks.count("codex")), (12, 12))

    def test_a_held_lock_fails_closed_and_is_not_stolen(self):
        from side_lane.model_selection import rotation
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spread.json"
            holder = ms.JsonFileState(path)
            state = ms.JsonFileState(path, lock_timeout=0.2)
            with holder.transaction("marcos"):
                with self.assertRaises(rotation.LockTimeout):
                    ms.select(request(tier="medium"), TABLE, now=NOW, state=state)
            # released when the holder finishes: the same state now works, nothing was left behind to steal
            self.assertEqual(ms.select(request(tier="medium"), TABLE, now=NOW, state=state).status, "selected")

    def test_the_os_lock_is_released_when_the_holder_dies(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spread.json"
            code = ("import os,sys,time\nsys.path.insert(0, %r)\n"
                    "from side_lane.model_selection import rotation\n"
                    "fd=os.open(%r, os.O_CREAT|os.O_RDWR)\nassert rotation._try_lock(fd)\nprint('held',flush=True)\ntime.sleep(60)\n"
                    % (str(ROOT), str(path) + ".lock"))
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "held")
                with self.assertRaises(__import__("side_lane.model_selection.rotation", fromlist=["x"]).LockTimeout):
                    ms.select(request(tier="medium"), TABLE, now=NOW, state=ms.JsonFileState(path, lock_timeout=0.2))
            finally:
                proc.kill()
                proc.wait()
                proc.stdout.close()
            self.assertEqual(ms.select(request(tier="medium"), TABLE, now=NOW, state=ms.JsonFileState(path)).status, "selected")

    def test_a_lock_that_cannot_be_opened_fails_closed(self):
        from unittest import mock
        from side_lane.model_selection.rotation import StateError
        with tempfile.TemporaryDirectory() as d:
            state = ms.JsonFileState(Path(d) / "spread.json")
            with mock.patch("os.open", side_effect=PermissionError("nope")):
                with self.assertRaises(StateError):
                    state.transaction("marcos").__enter__()


class SecondReviewTests(unittest.TestCase):
    def test_corrupt_or_unreadable_state_fails_closed_and_is_never_overwritten(self):
        from side_lane.model_selection.rotation import StateError
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "spread.json"
            path.write_text("{not json")
            state = ms.JsonFileState(path)
            with self.assertRaises(StateError):
                ms.select(request(tier="medium"), TABLE, now=NOW, state=state)
            self.assertEqual(path.read_text(), "{not json")
            path.write_text("[1, 2]")
            with self.assertRaises(StateError):
                state.counts("marcos")

    def test_absent_state_file_is_an_empty_state(self):
        with tempfile.TemporaryDirectory() as d:
            state = ms.JsonFileState(Path(d) / "new" / "spread.json")
            self.assertEqual(dict(state.counts("marcos")), {})
            s = ms.select(request(tier="medium"), TABLE, now=NOW, state=state)
            self.assertEqual(s.status, "selected")

    def test_previous_selection_is_validated(self):
        good = ms.select(request(), TABLE, now=NOW).to_dict()
        for mutate in (lambda d: d.pop("caller"), lambda d: d.update(tier="max"), lambda d: d.pop("provider"), lambda d: d.update(contract="x"),
                       lambda d: d.update(route="teleport"), lambda d: d.update(status="maybe")):
            bad = dict(good)
            mutate(bad)
            with self.assertRaises(ContractError):
                request(previous_selection=bad)
        with self.assertRaises(ContractError):
            request(previous_selection=["not", "an", "object"])
        self.assertIsNotNone(request(previous_selection=good).previous_selection)

    def test_service_identity_is_never_serialized_as_owner(self):
        s = ms.select(request(owner={"id": "slack-bot", "kind": "service"}), TABLE, now=NOW)
        self.assertIsNone(s.to_dict()["owner"])
        self.assertEqual(s.route, "metered_openrouter")
        self.assertNotIn("slack-bot", str(s.to_dict()))


class ReproducedGapTests(unittest.TestCase):
    """Offline counterexamples reproduced against 7a39f669 (pr292-gap-reproductions.json)."""

    def test_explicit_tier_change_leaves_a_sticky_route(self):
        first = ms.select(request(tier="medium"), TABLE, now=NOW)
        follow = ms.select(request(tier="xhigh", previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertEqual(follow.tier, "xhigh")
        self.assertFalse(follow.affinity["sticky"])
        self.assertIn("changed the tier", follow.affinity["reselect_reason"])

    def test_provider_pin_without_an_entry_in_the_requested_tier_is_not_moved(self):
        s = ms.select(request(tier="xhigh", preference={"provider": "glm"}), TABLE, now=NOW)
        self.assertEqual((s.status, s.tier), ("no_eligible", "xhigh"))
    def test_model_only_preference_is_honored(self):
        s = ms.select(request(tier="high", preference={"model": "gpt-6.1-sol"}), TABLE, now=NOW)
        self.assertEqual((s.provider, s.model, s.tier), ("codex", "gpt-6.1-sol", "high"))
        s = ms.select(request(tier="high", preference={"model": "no-such-model"}), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")

    def test_newer_provider_exhaustion_beats_an_older_model_available(self):
        avail = [obs("claude", at=NOW - 60, model="claude-sonnet-5-5"), obs("claude", "exhausted", at=NOW - 5),
                 obs("codex")]
        s = ms.select(request(tier="medium", availability=avail), TABLE, now=NOW)
        self.assertNotEqual(s.provider, "claude")
        self.assertTrue(any(e["reason"] == "exhausted" for e in s.exclusions))

    def test_native_api_route_can_truthfully_report_metered_billing(self):
        ms.ExecutionReceipt("s1", "codex-api", "gpt-6-astra", "native_cli", True, "metered_confirmed").validate()

    def test_configured_effort_reaches_the_selection_and_receipt(self):
        s = ms.select(request(tier="high", preference={"provider": "codex"}), TABLE, now=NOW)
        self.assertEqual((s.effort, s.to_dict()["effort"]), ("high", "high"))
        r = ms.ExecutionReceipt(s.selection_id, "codex", "gpt-6.1-sol", "native_cli", True, effort="high",
                                selected_provider=s.provider, selected_model=s.model, selected_route=s.route,
                                selected_tier=s.tier, selected_effort=s.effort).validate().to_dict()
        self.assertEqual(r["selected"], {"provider": "codex", "model": "gpt-6.1-sol", "route": "native_cli",
                                         "tier": "high", "effort": "high"})

    def test_receipt_route_difference_needs_a_reason(self):
        bad = ms.ExecutionReceipt("s1", "codex", "gpt-6.1-sol", "metered_openrouter", True,
                                  selected_provider="codex", selected_model="gpt-6.1-sol", selected_route="native_cli")
        with self.assertRaises(ContractError):
            bad.validate()

    def test_every_tier_has_reviewed_slugs_so_the_metered_fallback_exists(self):
        for tier in ms.TIERS:
            self.assertTrue(orr.allowed_for_tier(TABLE.for_tier(tier)), tier)

    def test_metered_fallback_needs_no_reviewed_slugs(self):
        bare = ms.TierTable(TABLE.version, tuple(c.__class__(**{**c.__dict__, "openrouter_slug": None}) for c in TABLE.entries))
        s = ms.select(request(tier="high", availability=[obs(p, "exhausted") for p in ("claude", "codex", "devin")]),
                      bare, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")
        self.assertNotIn("allowed_models", s.openrouter)

    def test_model_only_pin_evaluates_every_same_tier_match(self):
        # glm-5.3 has a direct (metered) and a hosted entry; with only metered use allowed and the direct
        # key bound, the second match is evaluated rather than the first one failing the pin.
        bindings = [{"ref": "b-key", "provider": "glm", "owner_id": "marcos"},
                    {"ref": "b-glm", "provider": "glm-coding-plan", "owner_id": "marcos"}]
        s = ms.select(request(tier="medium", preference={"model": "glm-5.3"}, bindings=bindings, allow_metered=False,
                              availability=[obs("glm"), obs("glm-hosted")]), TABLE, now=NOW)
        self.assertEqual((s.status, s.provider, s.binding_ref), ("selected", "glm-hosted", "b-glm"))
        self.assertTrue(any("metered_not_allowed" in e["reason"] for e in s.exclusions))

    def test_follow_up_drops_a_metered_route_when_metered_use_is_revoked(self):
        bindings = [{"ref": "b-key", "provider": "glm", "owner_id": "marcos"}]
        first = ms.select(request(tier="medium", preference={"provider": "glm"}, bindings=bindings,
                                  availability=[obs("glm")]), TABLE, now=NOW)
        self.assertEqual(first.provider, "glm")
        follow = ms.select(request(tier="medium", allow_metered=False, previous_selection=first.to_dict(),
                                   bindings=bindings + [{"ref": "b-claude", "provider": "claude", "owner_id": "marcos"},
                                                        {"ref": "b-codex", "provider": "codex", "owner_id": "marcos"}],
                                   availability=[obs("claude"), obs("codex")]), TABLE, now=NOW)
        self.assertNotEqual(follow.provider, "glm")
        self.assertEqual(follow.affinity["reselect_reason"], "metered use is no longer allowed")

    def test_partial_selected_identity_in_a_receipt_is_rejected(self):
        for kw in ({"selected_model": "m"}, {"selected_route": "native_cli"}, {"selected_provider": "claude"},
                   {"selected_provider": "claude", "selected_model": "m"}):
            with self.assertRaises(ContractError):
                ms.ExecutionReceipt("s1", "claude", "m2", "native_cli", True, **kw).validate()

    def test_non_finite_freshness_timestamps_are_rejected_or_unknown(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ContractError):
                request(availability=[obs("claude", at=bad)])
        self.assertEqual(av.effective_state(Availability("claude", "available", float("nan")), NOW)[0], "unknown")

    def test_a_metered_candidate_cannot_bypass_allow_metered_false(self):
        pin = {"provider": "glm"}
        bindings = [{"ref": "b-key", "provider": "glm", "owner_id": "marcos"}]
        s = ms.select(request(tier="medium", preference=pin, allow_metered=False, bindings=bindings,
                              availability=[obs("glm")]), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertTrue(any("metered_not_allowed" in e["reason"] for e in s.exclusions))
        s = ms.select(request(tier="medium", preference=pin, allow_metered=True, bindings=bindings,
                              availability=[obs("glm")]), TABLE, now=NOW)
        self.assertEqual((s.provider, s.status), ("glm", "selected"))

    def test_local_glm_is_paid_and_never_auto_selected_hosted_is_distinct(self):
        glm = [c for c in TABLE.entries if c.provider == "glm"][0]
        hosted = [c for c in TABLE.entries if c.provider == "glm-hosted"][0]
        self.assertEqual((glm.plan, hosted.plan, hosted.route), ("metered", "unverified", "included_endpoint"))
        bindings = [{"ref": "b-key", "provider": "glm", "owner_id": "marcos"}]
        s = ms.select(request(tier="medium", bindings=bindings, availability=[obs("glm")]), TABLE, now=NOW)
        self.assertNotEqual(s.provider, "glm")

    def test_a_different_caller_cannot_inherit_a_threads_owner_or_route(self):
        first = ms.select(request(caller="side-lane"), TABLE, now=NOW)
        other = ms.select(request(caller="prompt-it", owner={"id": "intruder", "kind": "human"},
                                  previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertEqual((other.owner.id, other.owner_source), ("intruder", "request"))
        self.assertFalse(other.affinity["sticky"])

    def test_retries_are_idempotent_for_the_spread(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        counts = dict(state.counts("marcos"))
        for _ in range(3):
            again = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
            self.assertEqual((again.provider, again.selection_id), (first.provider, first.selection_id))
        self.assertEqual(dict(state.counts("marcos")), counts)
        other = ms.select(request(thread_id="T2", tier="medium"), TABLE, now=NOW, state=state)
        self.assertNotEqual(other.provider, first.provider)

    def test_retry_through_the_file_state_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            state = ms.JsonFileState(Path(d) / "spread.json")
            a = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
            b = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
            self.assertEqual((a.provider, a.selection_id), (b.provider, b.selection_id))
            self.assertEqual(sum(state.counts("marcos").values()), 1)


class ThirdReviewTests(unittest.TestCase):
    def test_exact_openrouter_model_pins_are_not_routed_through_auto(self):
        s = ms.select(request(preference={"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"}), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertIsNone(s.route)
        s = ms.select(request(preference={"provider": "openrouter", "model": "openrouter/auto"}), TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")

    def test_hosted_glm_is_never_auto_selected_until_cataloged(self):
        bindings = [{"ref": "b-glm", "provider": "glm-coding-plan", "owner_id": "marcos"}]
        state = ms.MemoryState()
        for i in range(6):
            s = ms.select(request(thread_id=f"t{i}", tier="medium", bindings=bindings, availability=[obs("glm-hosted")]),
                          TABLE, now=NOW, state=state)
            self.assertNotEqual(s.provider, "glm-hosted")

    def test_future_dated_observations_are_not_evidence(self):
        future = Availability("claude", "available", NOW + 10_000)
        self.assertEqual(av.effective_state(future, NOW)[0], "unknown")
        exhausted = Availability("claude", "exhausted", NOW + 10_000, reset_at=NOW + 20_000)
        self.assertEqual(av.effective_state(exhausted, NOW)[0], "unknown")
        s = ms.select(request(tier="medium", availability=[obs("claude", at=NOW + 10_000), obs("codex")]), TABLE, now=NOW)
        self.assertEqual(s.availability_evidence["state"], "available")

    def test_completed_effects_must_be_a_list_of_strings(self):
        for bad in ("pr-comment-posted:123", ["ok", 3], [""], {"a": 1}, 5):
            with self.assertRaises(ContractError):
                request(completed_effects=bad)
        self.assertEqual(request(completed_effects=["a:1"]).completed_effects, ("a:1",))

    def test_receipt_requires_identity_and_a_real_boolean(self):
        for kw in ({"provider": ""}, {"model": "  "}, {"selection_id": ""}, {"success": "false"}, {"success": 1}):
            args = {"selection_id": "s1", "provider": "claude", "model": "m", "route": "native_cli", "success": True, **kw}
            with self.assertRaises(ContractError):
                ms.ExecutionReceipt(**args).validate()

    def test_persisted_state_types_are_validated(self):
        from side_lane.model_selection.rotation import StateError
        shapes = [{"marcos": {"counts": {"claude": -1}, "reservations": {}}},
                  {"marcos": {"counts": {"claude": "2"}, "reservations": {}}},
                  {"marcos": {"counts": {"claude": True}, "reservations": {}}},
                  {"marcos": {"counts": {}, "reservations": {"k": 5}}}]
        with tempfile.TemporaryDirectory() as d:
            for shape in shapes:
                path = Path(d) / "s.json"
                path.write_text(__import__("json").dumps(shape))
                with self.assertRaises(StateError):
                    ms.JsonFileState(path).counts("marcos")

    def test_reselecting_a_thread_does_not_count_it_twice(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual(sum(state.counts("marcos").values()), 1)
        avail = [obs(first.provider, "exhausted")] + [obs(p) for p in ("claude", "codex", "devin") if p != first.provider]
        again = ms.select(request(thread_id="T", tier="medium", availability=avail, previous_selection=first.to_dict()),
                          TABLE, now=NOW, state=state)
        self.assertNotEqual(again.provider, first.provider)
        self.assertEqual(sum(state.counts("marcos").values()), 1)
        self.assertEqual({k: v for k, v in state.counts("marcos").items() if v}, {again.provider: 1})  # it moved with the thread


class FourthReviewTests(unittest.TestCase):
    def test_receipt_identity_must_be_real_strings_and_a_known_route(self):
        base = {"selection_id": "s1", "provider": "claude", "model": "m", "route": "native_cli", "success": True}
        for kw in ({"selected_provider": "", "selected_model": "", "selected_route": ""},
                   {"selected_provider": 5, "selected_model": "m", "selected_route": "native_cli"},
                   {"selected_provider": "claude", "selected_model": "m", "selected_route": "teleport"},
                   {"switch_reason": 7}, {"switch_reason": "  "}):
            with self.assertRaises(ContractError):
                ms.ExecutionReceipt(**base, **kw).validate()
        ms.ExecutionReceipt(**base, selected_provider="claude", selected_model="m", selected_route="native_cli").validate()
        with self.assertRaises(ContractError):
            ms.ExecutionReceipt(**{**base, "model": "m2"}, selected_provider="claude", selected_model="m",
                                selected_route="native_cli", switch_reason="").validate()

    def test_a_served_model_with_several_native_routes_is_not_attributed_to_one(self):
        pool = TABLE.for_tier("medium")
        sent = orr.build_auto_request("medium", orr.allowed_for_tier(pool), [])
        r = orr.check_response({"model": "z-ai/glm-5.3"}, sent, pool)
        self.assertIsNone(r["native"])
        self.assertIn("several reviewed routes", r["note"])

    def test_a_pin_defined_tier_reaches_the_fallbacks_and_failures(self):
        task = {"objective": "x"}  # default tier is medium; the pin is high-only
        ok = ms.select(request(task=task, preference={"provider": "claude", "model": "claude-opus-5-5"}), TABLE, now=NOW)
        self.assertEqual((ok.tier, ok.tier_assigned_by), ("high", "preference"))
        self.assertTrue(all("medium" not in f["reason"] for f in ok.fallbacks))
        self.assertIn("(high)", ok.fallbacks[-1]["reason"])
        bad = ms.select(request(task=task, preference={"provider": "claude", "model": "claude-opus-5-5"},
                                availability=[obs("claude", "exhausted")]), TABLE, now=NOW)
        self.assertEqual((bad.status, bad.tier, bad.tier_assigned_by), ("no_eligible", "high", "preference"))


class FifthReviewTests(unittest.TestCase):
    def test_availability_requires_a_real_checked_at(self):
        for bad in (None, False, "now"):
            with self.assertRaises(ContractError):
                ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"},
                                  "availability": [{"provider": "claude", "state": "auth_failed", "checked_at": bad}]})
        with self.assertRaises(ContractError):
            ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"},
                              "availability": [{"provider": "claude", "state": "auth_failed"}]})

    def test_task_metadata_is_validated_not_coerced(self):
        for task in ({"objective": "x", "tools": "bash"}, {"objective": "x", "modalities": ["a", 3]},
                     {"objective": "x", "context_size": True}, {"objective": "x", "context_size": "10"},
                     {"objective": "x", "context_size": 1.5}, {"objective": "x", "context_size": -1}):
            with self.assertRaises(ContractError):
                ms.Request.parse({"caller": "c", "thread_id": "t", "task": task})
        ok = ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x", "tools": ["bash"], "context_size": 10}})
        self.assertEqual((ok.task.tools, ok.task.context_size), (("bash",), 10))

    def test_receipt_selected_tier_is_validated_and_needs_the_identity(self):
        base = {"selection_id": "s1", "provider": "claude", "model": "m", "route": "native_cli", "success": True}
        ident = {"selected_provider": "claude", "selected_model": "m", "selected_route": "native_cli"}
        for kw in ({"selected_tier": "high"}, {**ident, "selected_tier": "max"}, {**ident, "selected_tier": 3}):
            with self.assertRaises(ContractError):
                ms.ExecutionReceipt(**base, **kw).validate()
        ms.ExecutionReceipt(**base, **ident, selected_tier="high").validate()

    def test_a_retry_that_selects_nothing_keeps_the_committed_reservation(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        before = dict(state.counts("marcos"))
        every_provider_down = [obs(p, "exhausted") for p in ("claude", "codex", "devin")]
        nothing = ms.select(request(thread_id="T", tier="medium", allow_metered=False, availability=every_provider_down),
                            TABLE, now=NOW, state=state)
        self.assertEqual(nothing.status, "no_eligible")
        self.assertEqual(dict(state.counts("marcos")), before)
        again = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual((again.provider, dict(state.counts("marcos"))), (first.provider, before))

    def test_a_sticky_retry_does_not_erase_the_reservation(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        before = dict(state.counts("marcos"))
        for _ in range(2):
            ms.select(request(thread_id="T", tier="medium", previous_selection=first.to_dict()), TABLE, now=NOW, state=state)
        self.assertEqual(dict(state.counts("marcos")), before)

    def test_metered_route_respects_openrouter_availability(self):
        down = [obs(p, "exhausted") for p in ("claude", "codex", "devin")]
        for state_name in ("auth_failed", "transient", "exhausted"):
            s = ms.select(request(tier="medium", availability=down + [obs("openrouter", state_name, model="openrouter/auto")]),
                          TABLE, now=NOW)
            self.assertEqual(s.status, "no_eligible", state_name)
            self.assertIn(f"openrouter_{state_name}", s.reason)
        s = ms.select(request(tier="medium", availability=down + [obs("openrouter", "available", model="openrouter/auto")]),
                      TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")

    def test_ownerless_pins_are_not_silently_replaced_by_auto(self):
        for pref in ({"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"}, {"model": "gpt-6.1-sol"},
                     {"provider": "claude"}):
            s = ms.select(request(owner=None, preference=pref), TABLE, now=NOW)
            self.assertNotEqual(s.route, "metered_openrouter", pref)
            self.assertIn(s.status, ("owner_unresolved", "no_eligible"))
        s = ms.select(request(owner=None, preference={"provider": "openrouter", "model": "openrouter/auto"}), TABLE, now=NOW)
        self.assertEqual(s.route, "metered_openrouter")


class SixthReviewTests(unittest.TestCase):
    def test_owner_scoped_openrouter_failure_is_honored(self):
        down = [obs(p, "exhausted") for p in ("claude", "codex", "devin")]
        s = ms.select(request(tier="medium", availability=down + [obs("openrouter", "auth_failed", owner_id="marcos",
                                                                      model="openrouter/auto")]), TABLE, now=NOW)
        self.assertEqual(s.status, "no_eligible")
        self.assertIn("openrouter_auth_failed", s.reason)
        other = ms.select(request(tier="medium", availability=down + [obs("openrouter", "auth_failed", owner_id="someone-else",
                                                                          model="openrouter/auto")]), TABLE, now=NOW)
        self.assertEqual(other.route, "metered_openrouter")

    def test_metered_selection_carries_the_availability_evidence(self):
        down = [obs(p, "exhausted") for p in ("claude", "codex", "devin")]
        known = ms.select(request(tier="medium", availability=down + [obs("openrouter", "available", model="openrouter/auto")]),
                          TABLE, now=NOW)
        self.assertEqual(known.availability_evidence["state"], "available")
        self.assertIsNone(known.uncertainty)
        unknown = ms.select(request(tier="medium", availability=down), TABLE, now=NOW)
        self.assertEqual(unknown.availability_evidence["state"], "unknown")
        self.assertIn("unknown", unknown.uncertainty)

    def test_selection_id_is_stable_across_delayed_retries(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        ms.select(request(thread_id="T2", tier="medium"), TABLE, now=NOW, state=state)
        retry = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual((retry.provider, retry.selection_id), (first.provider, first.selection_id))
        other_caller = ms.select(request(thread_id="T", tier="medium", caller="side-lane"), TABLE, now=NOW,
                                 state=ms.MemoryState())
        self.assertEqual(other_caller.selection_id, first.selection_id)


class SeventhReviewTests(unittest.TestCase):
    def test_falsey_malformed_owner_values_are_rejected_not_treated_as_absent(self):
        for bad in ([], "", 0, False, "marcos"):
            with self.assertRaises(ContractError):
                request(owner=bad)
        self.assertIsNone(ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"}}).owner)
        good = ms.select(request(), TABLE, now=NOW).to_dict()
        good["owner"] = []
        with self.assertRaises(ContractError):
            request(previous_selection=good)

    def test_delayed_retry_identity_holds_across_hundreds_of_threads(self):
        state = ms.MemoryState()
        first = ms.select(request(thread_id="T0", tier="medium"), TABLE, now=NOW, state=state)
        for i in range(1, 300):
            ms.select(request(thread_id=f"T{i}", tier="medium"), TABLE, now=NOW, state=state)
        total = sum(state.counts("marcos").values())
        retry = ms.select(request(thread_id="T0", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual((retry.provider, retry.selection_id), (first.provider, first.selection_id))
        self.assertEqual(sum(state.counts("marcos").values()), total)


class EighthReviewTests(unittest.TestCase):
    def test_malformed_non_object_preference_is_rejected(self):
        for bad in (False, "", [], 0, "codex"):
            with self.assertRaises(ContractError):
                request(preference=bad)
        self.assertEqual(request(preference=None).preference_provider, None)

    def test_selected_effort_alone_is_not_a_valid_selected_record(self):
        base = {"selection_id": "s1", "provider": "claude", "model": "m", "route": "native_cli", "success": True}
        with self.assertRaises(ContractError):
            ms.ExecutionReceipt(**base, selected_effort="high").validate()

    def test_file_state_keeps_reservation_order_so_the_oldest_is_evicted(self):
        from unittest import mock
        from side_lane.model_selection import rotation
        with tempfile.TemporaryDirectory() as d:
            state = ms.JsonFileState(Path(d) / "spread.json")
            with mock.patch.object(rotation, "MAX_RESERVATIONS", 3):
                for name in ("z-thread", "y-thread", "x-thread", "a-thread"):  # a-thread sorts first alphabetically
                    ms.select(request(thread_id=name, tier="medium"), TABLE, now=NOW, state=state)
                data = __import__("json").loads((Path(d) / "spread.json").read_text())
            keys = list(data["marcos"]["reservations"])
            self.assertEqual(len(keys), 3)
            self.assertNotIn(ms.selector._thread_key(request(thread_id="z-thread", tier="medium")), keys)  # oldest gone
            self.assertIn(ms.selector._thread_key(request(thread_id="a-thread", tier="medium")), keys)     # newest kept


class NinthReviewTests(unittest.TestCase):
    def test_task_must_be_an_object(self):
        for bad in (None, [], "x", 0, [1]):
            with self.assertRaises(ContractError):
                ms.Request.parse({"caller": "c", "thread_id": "t", "task": bad})

    def test_collections_must_be_arrays_of_objects(self):
        for field in ("bindings", "availability"):
            for bad in ("x", {}, 5, ["x"], [1]):
                with self.assertRaises(ContractError):
                    ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"}, field: bad})
            ms.Request.parse({"caller": "c", "thread_id": "t", "task": {"objective": "x"}, field: []})

    def test_one_thread_is_counted_once_however_its_inputs_change(self):
        state = ms.MemoryState()
        ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        ms.select(request(thread_id="T", tier="high"), TABLE, now=NOW, state=state)
        ms.select(request(thread_id="T", tier="xhigh"), TABLE, now=NOW, state=state)
        self.assertEqual(sum(state.counts("marcos").values()), 1)
        ms.select(request(thread_id="T2", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual(sum(state.counts("marcos").values()), 2)

    def test_changed_pin_records_a_reselect_reason_and_a_repeated_pin_stays_sticky(self):
        first = ms.select(request(tier="medium", preference={"provider": "claude"}), TABLE, now=NOW)
        same = ms.select(request(tier="medium", preference={"provider": "claude"}, previous_selection=first.to_dict()),
                         TABLE, now=NOW)
        self.assertTrue(same.affinity["sticky"])
        self.assertNotIn("reselect_reason", same.affinity)
        changed = ms.select(request(tier="medium", preference={"provider": "codex"}, previous_selection=first.to_dict()),
                            TABLE, now=NOW)
        self.assertFalse(changed.affinity["sticky"])
        self.assertIn("explicit request changed the route", changed.affinity["reselect_reason"])


class TenthReviewTests(unittest.TestCase):
    def test_repeating_an_openrouter_pin_is_sticky_and_switching_to_it_records_why(self):
        pin = {"provider": "openrouter", "model": "openrouter/auto"}
        first = ms.select(request(tier="medium", preference=pin), TABLE, now=NOW)
        same = ms.select(request(tier="medium", preference=pin, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertTrue(same.affinity["sticky"])
        self.assertEqual(same.selection_id, first.selection_id)
        higher = ms.select(request(tier="high", preference=pin, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertFalse(higher.affinity["sticky"])
        self.assertIn("changed the tier", higher.affinity["reselect_reason"])
        native = ms.select(request(tier="medium", preference={"provider": "claude"}), TABLE, now=NOW)
        moved = ms.select(request(tier="medium", preference=pin, previous_selection=native.to_dict()), TABLE, now=NOW)
        self.assertFalse(moved.affinity["sticky"])
        self.assertIn("explicit request changed the route to openrouter/auto", moved.affinity["reselect_reason"])


class EleventhReviewTests(unittest.TestCase):
    def test_moving_a_thread_to_metered_releases_its_native_reservation(self):
        state = ms.MemoryState()
        native = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        self.assertEqual(sum(state.counts("marcos").values()), 1)
        moved = ms.select(request(thread_id="T", tier="medium", preference={"provider": "openrouter"},
                                  previous_selection=native.to_dict()), TABLE, now=NOW, state=state)
        self.assertEqual(moved.route, "metered_openrouter")
        self.assertEqual(sum(state.counts("marcos").values()), 0)
        again = ms.select(request(thread_id="T", tier="medium", preference={"provider": "openrouter"},
                                  previous_selection=moved.to_dict()), TABLE, now=NOW, state=state)
        self.assertTrue(again.affinity["sticky"])
        self.assertEqual(sum(state.counts("marcos").values()), 0)

    def test_automatic_metered_failover_releases_too(self):
        state = ms.MemoryState()
        native = ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        down = [obs(p, "exhausted") for p in ("claude", "codex", "devin")]
        moved = ms.select(request(thread_id="T", tier="medium", availability=down, previous_selection=native.to_dict()),
                          TABLE, now=NOW, state=state)
        self.assertEqual(moved.route, "metered_openrouter")
        self.assertEqual(sum(state.counts("marcos").values()), 0)

    def test_a_failed_metered_attempt_keeps_the_existing_reservation(self):
        state = ms.MemoryState()
        ms.select(request(thread_id="T", tier="medium"), TABLE, now=NOW, state=state)
        ms.select(request(thread_id="T", tier="medium", allow_metered=False, preference={"provider": "openrouter"}),
                  TABLE, now=NOW, state=state)
        self.assertEqual(sum(state.counts("marcos").values()), 1)


class GlmIncludedTests(unittest.TestCase):
    GLM = [{"ref": "b-key", "provider": "glm", "owner_id": "marcos"}]

    def glm_request(self, **over):
        base = {"tier": "medium", "bindings": request().bindings and [
            {"ref": "b-claude", "provider": "claude", "owner_id": "marcos"},
            {"ref": "b-codex", "provider": "codex", "owner_id": "marcos"}] + self.GLM,
            "availability": [obs("claude"), obs("codex"), obs("glm")]}
        base.update(over)
        return request(**base)

    def test_an_entitled_owner_gets_glm_in_the_automatic_spread(self):
        state = ms.MemoryState()
        picks = [ms.select(self.glm_request(thread_id=f"t{i}", included_plans=["glm"], allow_metered=False),
                           TABLE, now=NOW, state=state) for i in range(30)]
        self.assertEqual({p.provider for p in picks}, {"claude", "codex", "glm"})
        glm = [p for p in picks if p.provider == "glm"][0]
        self.assertEqual((glm.route, glm.status), ("native_cli", "selected"))
        self.assertIn("declared included", glm.availability_evidence["entitlement"])
        self.assertEqual([sum(1 for p in picks if p.provider == x) for x in ("claude", "codex", "glm")], [10, 10, 10])

    def test_without_the_declaration_glm_stays_pin_only_and_paid(self):
        state = ms.MemoryState()
        for i in range(9):
            s = ms.select(self.glm_request(thread_id=f"t{i}"), TABLE, now=NOW, state=state)
            self.assertNotEqual(s.provider, "glm")
        pinned = ms.select(self.glm_request(preference={"provider": "glm"}, allow_metered=False), TABLE, now=NOW)
        self.assertEqual(pinned.status, "no_eligible")
        entitled = ms.select(self.glm_request(preference={"provider": "glm"}, allow_metered=False, included_plans=["glm"]),
                             TABLE, now=NOW)
        self.assertEqual((entitled.status, entitled.provider), ("selected", "glm"))

    def test_an_exhausted_or_failed_glm_is_excluded_even_when_entitled(self):
        for state_name in ("exhausted", "auth_failed", "transient"):
            avail = [obs("claude"), obs("codex"), obs("glm", state_name)]
            for i in range(6):
                s = ms.select(self.glm_request(thread_id=f"t{i}", included_plans=["glm"], availability=avail),
                              TABLE, now=NOW)
                self.assertNotEqual(s.provider, "glm", state_name)

    def test_off_vpn_no_binding_means_no_glm(self):
        no_key = [b for b in self.glm_request().bindings if b.provider != "glm"]
        s = ms.select(request(tier="medium", included_plans=["glm"],
                              bindings=[b.__dict__ for b in no_key], availability=[obs("claude"), obs("codex")]),
                      TABLE, now=NOW)
        self.assertNotEqual(s.provider, "glm")
        self.assertTrue(any(e["candidate"].startswith("glm/") and e["reason"] == "no_binding" for e in s.exclusions))

    def test_only_owner_declarable_routes_can_be_promoted(self):
        for provider, tier, binding in (("codex-api", "xhigh", "openai-api-key"), ("claude-api", "xhigh", "anthropic"),
                                        ("gemini", "medium", "gemini"), ("glm-hosted", "medium", "glm-coding-plan")):
            s = ms.select(request(tier=tier, included_plans=[provider], allow_metered=False,
                                  bindings=[{"ref": "b", "provider": binding, "owner_id": "marcos"}],
                                  availability=[obs(provider)]), TABLE, now=NOW)
            self.assertNotEqual(s.provider, provider, provider)

    def test_included_plans_must_be_a_list_of_strings(self):
        for bad in ("glm", [1], {"glm": True}):
            with self.assertRaises(ContractError):
                request(included_plans=bad)

    def test_revoking_the_declaration_drops_a_sticky_glm_route(self):
        first = ms.select(self.glm_request(included_plans=["glm"], preference={"provider": "glm"}, allow_metered=False),
                          TABLE, now=NOW)
        self.assertEqual(first.provider, "glm")
        follow = ms.select(self.glm_request(allow_metered=False, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertNotEqual(follow.provider, "glm")
        self.assertEqual(follow.affinity["reselect_reason"], "metered use is no longer allowed")

    def test_a_non_boolean_declarable_flag_fails_closed(self):
        import json as _json
        for bad in ("false", "true", 1, "yes"):
            raw = _json.loads((ROOT / "config" / "model-tiers.json").read_text())
            glm = next(e for e in raw["entries"] if e["provider"] == "glm")
            glm["declarable_included"] = bad
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "t.json"
                path.write_text(_json.dumps(raw))
                with self.assertRaises(ContractError):
                    ms.load(path)

    def test_a_promoted_route_is_dropped_when_the_declaration_disappears_even_with_metered_allowed(self):
        first = ms.select(self.glm_request(included_plans=["glm"], preference={"provider": "glm"}, allow_metered=False),
                          TABLE, now=NOW)
        self.assertIn("entitlement", first.availability_evidence)
        follow = ms.select(self.glm_request(allow_metered=True, previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertFalse(follow.affinity["sticky"])
        self.assertIn("declaration", follow.affinity["reselect_reason"])
        still = ms.select(self.glm_request(included_plans=["glm"], previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertTrue(still.affinity["sticky"])

    def test_the_table_flag_is_only_valid_on_metered_entries(self):
        import json as _json
        raw = _json.loads((ROOT / "config" / "model-tiers.json").read_text())
        raw["entries"][0]["declarable_included"] = True  # a claude/included entry
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.json"
            path.write_text(_json.dumps(raw))
            with self.assertRaises(ContractError):
                ms.load(path)


class ProbeRequiredTests(unittest.TestCase):
    GEM = [{"ref": "b-gem", "provider": "gemini", "owner_id": "marcos"}]

    def gem_request(self, availability, **over):
        base = {"tier": "low", "included_plans": ["gemini"], "allow_metered": False,
                "bindings": [{"ref": "b-claude", "provider": "claude", "owner_id": "marcos"}, *self.GEM],
                "availability": availability}
        base.update(over)
        return request(**base)

    def test_a_declared_plan_without_a_live_probe_is_never_automatic(self):
        state = ms.MemoryState()
        for i in range(8):
            s = ms.select(self.gem_request([obs("claude"), obs("gemini")], thread_id=f"t{i}"), TABLE, now=NOW, state=state)
            self.assertEqual(s.provider, "claude")
            self.assertTrue(any("probe_required" in e["reason"] for e in s.exclusions))

    def test_a_live_available_probe_makes_the_declared_plan_automatic(self):
        state = ms.MemoryState()
        picks = [ms.select(self.gem_request([obs("claude"), obs("gemini", live=True)], thread_id=f"t{i}"),
                           TABLE, now=NOW, state=state).provider for i in range(6)]
        self.assertEqual({p: picks.count(p) for p in set(picks)}, {"claude": 3, "gemini": 3})

    def test_an_inconclusive_live_probe_cannot_route_even_when_nothing_else_is_available(self):
        for st in ("unknown", "transient", "exhausted", "auth_failed"):
            s = ms.select(self.gem_request([obs("claude", "exhausted"), obs("gemini", st, live=True)]), TABLE, now=NOW)
            self.assertEqual(s.status, "no_eligible", st)
            self.assertIsNone(s.provider)
        ok = ms.select(self.gem_request([obs("claude", "exhausted"), obs("gemini", live=True)]), TABLE, now=NOW)
        self.assertEqual(ok.provider, "gemini")

    def test_a_sticky_gemini_route_is_dropped_when_its_probe_stops_succeeding(self):
        first = ms.select(self.gem_request([obs("claude", "exhausted"), obs("gemini", live=True)]), TABLE, now=NOW)
        self.assertEqual(first.provider, "gemini")
        same = ms.select(self.gem_request([obs("claude", "exhausted"), obs("gemini", live=True)],
                                          previous_selection=first.to_dict()), TABLE, now=NOW)
        self.assertTrue(same.affinity["sticky"])
        for avail in ([obs("claude"), obs("gemini", "unknown", live=True)], [obs("claude"), obs("gemini")]):
            follow = ms.select(self.gem_request(avail, previous_selection=first.to_dict()), TABLE, now=NOW)
            self.assertNotEqual(follow.provider, "gemini")
            self.assertIn("live probe", follow.affinity["reselect_reason"])

    def test_a_live_failed_probe_still_excludes_it(self):
        for st in ("exhausted", "auth_failed", "transient", "unknown"):
            s = ms.select(self.gem_request([obs("claude"), obs("gemini", st, live=True)]), TABLE, now=NOW)
            self.assertEqual(s.provider, "claude", st)

    def test_a_pin_is_not_blocked_by_the_probe_requirement(self):
        s = ms.select(self.gem_request([obs("gemini")], preference={"provider": "gemini"}), TABLE, now=NOW)
        self.assertEqual((s.provider, s.status), ("gemini", "selected"))

    def test_live_must_be_a_real_boolean_and_probe_required_needs_the_declaration_flag(self):
        with self.assertRaises(ContractError):
            request(availability=[obs("claude", live="yes")])
        import json as _json
        raw = _json.loads((ROOT / "config" / "model-tiers.json").read_text())
        raw["entries"][0]["probe_required"] = True  # claude haiku: not a declarable entry
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.json"
            path.write_text(_json.dumps(raw))
            with self.assertRaises(ContractError):
                ms.load(path)
        raw["entries"][0].pop("probe_required")
        glm = next(e for e in raw["entries"] if e["provider"] == "glm")
        glm["probe_required"] = "false"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.json"
            path.write_text(_json.dumps(raw))
            with self.assertRaises(ContractError):
                ms.load(path)

    def test_the_table_marks_gemini_and_only_gemini_probe_required(self):
        self.assertEqual({c.provider for c in TABLE.entries if c.probe_required}, {"gemini"})


class ReceiptTests(unittest.TestCase):
    def test_native_and_metered_receipts_are_distinguished(self):
        ok = ms.ExecutionReceipt("s1", "claude", "claude-sonnet-5-5", "native_cli", True).validate()
        self.assertEqual(ok.billing_evidence, "unverified")  # auth success is not billing proof
        with self.assertRaises(ContractError):
            ms.ExecutionReceipt("s1", "openrouter", "x", "metered_openrouter", True, "included_confirmed").validate()

    def test_a_switch_requires_a_reason(self):
        bad = ms.ExecutionReceipt("s1", "codex", "gpt-6.1-sol", "native_cli", True,
                                  selected_provider="claude", selected_model="claude-sonnet-5-5", selected_route="native_cli")
        with self.assertRaises(ContractError):
            bad.validate()
        bad2 = ms.ExecutionReceipt("s1", "codex", "gpt-6.1-sol", "native_cli", True, switch_reason="claude exhausted",
                                   selected_provider="claude", selected_model="claude-sonnet-5-5", selected_route="native_cli")
        bad2.validate()

    def test_completed_effects_travel_with_the_selection_for_failover(self):
        s = ms.select(request(completed_effects=("pr-comment-posted:123",), tier="medium"), TABLE, now=NOW)
        self.assertEqual(s.completed_effects, ("pr-comment-posted:123",))
        self.assertEqual(s.to_dict()["completed_effects"], ["pr-comment-posted:123"])


class TableTests(unittest.TestCase):
    def test_table_is_valid_and_versioned(self):
        self.assertTrue(TABLE.version)
        for tier in ms.TIERS:
            self.assertGreaterEqual(len({c.provider for c in TABLE.for_tier(tier)}), 3)

    def test_xhigh_is_fable_and_astra_class(self):
        models = {c.model for c in TABLE.for_tier("xhigh")}
        self.assertTrue({"claude-fable-5-1", "gpt-6-astra"} <= models)

    def test_there_are_no_paid_api_key_native_routes_and_astra_is_an_included_codex_route(self):
        self.assertFalse([c for c in TABLE.entries if c.binding_provider in ("openai-api-key", "anthropic")])
        astra = [c for c in TABLE.for_tier("xhigh") if c.model == "gpt-6-astra"][0]
        self.assertEqual((astra.provider, astra.plan, astra.route), ("codex", "included", "native_cli"))
    def test_no_credentials_or_secrets_in_selection_output(self):
        text = str(ms.select(request(), TABLE, now=NOW).to_dict()).lower()
        for bad in ("api_key", "sk-", "bearer", "password"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
