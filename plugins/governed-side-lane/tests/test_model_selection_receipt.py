from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from side_lane import model_selection as ms  # noqa: E402
from side_lane.model_selection import receipt as rc  # noqa: E402
from side_lane.model_selection.contract import ContractError  # noqa: E402

NOW = 1_800_000_000.0
TABLE = ms.load()


def selection(**over):
    base = {"caller": "side-lane-local", "thread_id": "T", "task": {"objective": "x", "kind": "coding"},
            "owner": {"id": "me", "kind": "human"}, "allow_metered": True,
            "bindings": [{"ref": "b-c", "provider": "claude", "owner_id": "me"}, {"ref": "b-or", "provider": "openrouter"}],
            "availability": [{"provider": "claude", "state": "available", "checked_at": NOW}]}
    base.update(over)
    return ms.select(ms.Request.parse(base), TABLE, now=NOW).to_dict()


def parse(*argv):
    p = argparse.ArgumentParser()
    rc.add_arguments(p)
    return p.parse_args(argv)


def run(selection_obj, *argv):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "sel.json"
        f.write_text(json.dumps(selection_obj))
        out = io.StringIO()
        code = rc.run(parse("--selection", str(f), *argv), out=out)
    return code, (json.loads(out.getvalue()) if out.getvalue() else None)


NATIVE = ("--provider", "claude", "--model", "claude-opus-5-5", "--route", "native_cli")


class BuildTests(unittest.TestCase):
    def test_a_successful_run_copies_the_selected_route_and_defaults_billing_to_unverified(self):
        sel = selection()
        code, r = run(sel, *NATIVE, "--success", "--usage", '{"input_tokens": 120, "output_tokens": 30}')
        self.assertEqual(code, 0)
        self.assertEqual((r["provider"], r["model"], r["route"], r["success"]), ("claude", "claude-opus-5-5", "native_cli", True))
        self.assertEqual(r["billing_evidence"], "unverified")  # a successful run is not billing evidence
        self.assertEqual(r["selection_id"], sel["selection_id"])
        self.assertEqual(r["selected"], {"provider": "claude", "model": "claude-opus-5-5", "route": "native_cli",
                                         "tier": "high", "effort": None})
        self.assertEqual(r["observed_usage"], {"input_tokens": 120, "output_tokens": 30})

    def test_a_failure_is_recorded_as_a_failure(self):
        code, r = run(selection(), *NATIVE, "--failure")
        self.assertEqual((code, r["success"]), (0, False))

    def test_the_outcome_is_required(self):
        with self.assertRaises(SystemExit):
            parse("--selection", "x", *NATIVE)

    def test_a_different_route_than_selected_needs_a_reason(self):
        sel = selection()
        other = ("--provider", "codex", "--model", "gpt-6.1-sol", "--route", "native_cli", "--success")
        self.assertEqual(run(sel, *other)[0], 2)
        code, r = run(sel, *other, "--switch-reason", "claude exhausted mid-task")
        self.assertEqual((code, r["switch_reason"]), (0, "claude exhausted mid-task"))

    def test_billing_claims_must_match_the_route(self):
        sel = selection()
        metered = ("--provider", "openrouter", "--model", "anthropic/claude-sonnet-5.5", "--route", "metered_openrouter",
                   "--success", "--switch-reason", "fell back")
        self.assertEqual(run(sel, *metered, "--billing", "included_confirmed")[0], 2)
        self.assertEqual(run(sel, *metered, "--billing", "metered_confirmed")[0], 0)

    def test_only_a_selected_selection_can_be_receipted(self):
        none = selection(availability=[{"provider": "claude", "state": "exhausted", "checked_at": NOW}], allow_metered=False)
        self.assertEqual(none["status"], "no_eligible")
        self.assertEqual(run(none, *NATIVE, "--success")[0], 2)

    def test_a_malformed_selection_is_refused(self):
        sel = selection()
        for mutate in (lambda d: d.pop("provider"), lambda d: d.update(contract="x"), lambda d: d.update(tier="max")):
            bad = dict(sel)
            mutate(bad)
            self.assertEqual(run(bad, *NATIVE, "--success")[0], 2)

    def test_effort_that_ran_is_recorded(self):
        code, r = run(selection(), *NATIVE, "--success", "--effort", "high")
        self.assertEqual((code, r["effort"]), (0, "high"))


class ReviewTests(unittest.TestCase):
    def test_credential_like_free_text_is_refused(self):
        sel = selection()
        other = ("--provider", "codex", "--model", "gpt-6.1-sol", "--route", "native_cli", "--success")
        for reason in ("used key sk-abcdef123456 to retry", "Authorization: abc", "A" * 40, "x" * 300):
            self.assertEqual(run(sel, *other, "--switch-reason", reason)[0], 2, reason[:20])
        self.assertEqual(run(sel, *NATIVE, "--success", "--effort", "sk-abcdef123456")[0], 2)
        self.assertEqual(run(sel, "--provider", "claude", "--model", "sk-abcdef123456", "--route", "native_cli", "--success")[0], 2)
        self.assertEqual(run(sel, *other, "--switch-reason", "claude exhausted, moved to codex")[0], 0)

    def test_non_finite_numbers_are_refused_everywhere(self):
        for bad in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", bad)[0], 2, bad)
        with self.assertRaises(ContractError):
            rc.clean_usage({"x": float("inf")})
        with self.assertRaises(ContractError):
            rc.clean_usage({"x": float("nan")})
        code, r = run(selection(), *NATIVE, "--success", "--usage", '{"x": 1.5}')
        self.assertEqual((code, r["observed_usage"]["x"]), (0, 1.5))

    def metered(self, **over):
        down = [{"provider": "claude", "state": "exhausted", "checked_at": NOW}]
        return selection(availability=down, tier="medium", **over)

    def test_an_auto_served_model_is_the_selected_route_not_a_switch(self):
        sel = self.metered()
        self.assertEqual((sel["route"], sel["model"]), ("metered_openrouter", "openrouter/auto"))
        served = "anthropic/claude-sonnet-5.5"
        self.assertIn(served, sel["openrouter"]["allowed_models"])
        code, r = run(sel, "--provider", "openrouter", "--model", served, "--route", "metered_openrouter", "--success",
                      "--billing", "metered_confirmed")
        self.assertEqual(code, 0)
        self.assertTrue(r["auto_resolved"])
        self.assertEqual(r["model"], served)
        self.assertEqual(r["selected"]["model"], "openrouter/auto")  # the selection is kept as it was

    def test_an_auto_served_model_outside_the_allowed_set_still_needs_a_reason(self):
        sel = self.metered()
        args = ("--provider", "openrouter", "--model", "mystery/model", "--route", "metered_openrouter", "--success")
        self.assertEqual(run(sel, *args)[0], 2)
        code, r = run(sel, *args, "--switch-reason", "Auto served a model outside the sent allow-list")
        self.assertEqual((code, r["auto_resolved"]), (0, False))

    def test_auto_resolved_cannot_be_claimed_for_a_native_selection(self):
        from side_lane.model_selection.contract import ExecutionReceipt
        bad = ExecutionReceipt("s1", "claude", "claude-opus-5-5", "native_cli", True, auto_resolved=True,
                               selected_provider="claude", selected_model="claude-opus-5-5", selected_route="native_cli")
        with self.assertRaises(ContractError):
            bad.validate()
        wrong_provider = ExecutionReceipt("s1", "claude", "m", "native_cli", True, auto_resolved=True,
                                          selected_provider="openrouter", selected_model="openrouter/auto",
                                          selected_route="metered_openrouter")
        with self.assertRaises(ContractError):
            wrong_provider.validate()


class SecondReviewTests(unittest.TestCase):
    def auto_selection(self):
        down = [{"provider": "claude", "state": "exhausted", "checked_at": NOW}]
        return selection(availability=down, tier="medium")

    def args(self, model):
        return ("--provider", "openrouter", "--model", model, "--route", "metered_openrouter", "--success")

    def test_auto_resolution_needs_a_validated_non_empty_allow_list(self):
        for bad in (None, [], "anthropic/*", [""], [1], {"a": 1}):
            sel = self.auto_selection()
            sel["openrouter"]["allowed_models"] = bad
            self.assertEqual(run(sel, *self.args("anthropic/claude-sonnet-5.5"))[0], 2, bad)  # no reason, not resolved
        sel = self.auto_selection()
        del sel["openrouter"]
        self.assertEqual(run(sel, *self.args("anthropic/claude-sonnet-5.5"))[0], 2)

    def test_the_served_model_must_look_like_an_openrouter_id(self):
        sel = self.auto_selection()
        sel["openrouter"]["allowed_models"] = ["*"]
        for bad in ("claude", "a/b/c d", "a/", "/b", "sk-abcdef123456/x", "a b/c"):
            self.assertEqual(run(sel, *self.args(bad))[0], 2, bad)
        self.assertEqual(run(sel, *self.args("anthropic/claude-sonnet-5.5"))[0], 0)

    def test_huge_integers_do_not_crash_and_are_out_of_range(self):
        for bad in (10 ** 400, -(10 ** 400), 2 ** 63):
            self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", json.dumps({"n": bad}))[0], 2)
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", '{"n": 9223372036854775807}')[0], 0)
        with self.assertRaises(ContractError):
            rc.clean_usage({"n": 10 ** 400})

    def test_a_non_string_selection_id_is_rejected_not_coerced(self):
        for bad in (123, 1.5, ["x"], None, ""):
            sel = selection()
            sel["selection_id"] = bad
            self.assertEqual(run(sel, *NATIVE, "--success")[0], 2, bad)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_appending_tightens_an_existing_world_readable_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sel.json"
            f.write_text(json.dumps(selection()))
            log = Path(d) / "receipts.jsonl"
            log.write_text("")
            os.chmod(log, 0o644)
            self.assertEqual(rc.run(parse("--selection", str(f), *NATIVE, "--success", "--append", str(log)),
                                    out=io.StringIO()), 0)
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)


class ThirdReviewTests(unittest.TestCase):
    def test_a_long_token_embedded_in_prose_is_refused_everywhere(self):
        sel = selection()
        prose = "adapter returned " + "A" * 40 + " at the end"
        other = ("--provider", "codex", "--model", "gpt-6.1-sol", "--route", "native_cli", "--success")
        self.assertEqual(run(sel, *NATIVE, "--success", "--usage", json.dumps({"note": prose}))[0], 2)
        self.assertEqual(run(sel, *other, "--switch-reason", prose)[0], 2)
        self.assertEqual(run(sel, *NATIVE, "--success", "--effort", prose)[0], 2)
        for ok in ("local_key_route_launch finished", "claude exhausted, moved to codex", "anthropic/claude-sonnet-5.5"):
            self.assertEqual(run(sel, *other, "--switch-reason", ok)[0], 0, ok)

    def test_a_direct_auto_resolved_receipt_needs_a_concrete_served_model(self):
        from side_lane.model_selection.contract import ExecutionReceipt
        for model in ("not a model", "openrouter/auto", "openrouter/auto-beta", "claude", "", None):
            bad = ExecutionReceipt("s1", "openrouter", model, "metered_openrouter", True, auto_resolved=True,
                                   selected_provider="openrouter", selected_model="openrouter/auto",
                                   selected_route="metered_openrouter")
            with self.assertRaises(ContractError):
                bad.validate()
        ExecutionReceipt("s1", "openrouter", "anthropic/claude-sonnet-5.5", "metered_openrouter", True, auto_resolved=True,
                         selected_provider="openrouter", selected_model="openrouter/auto",
                         selected_route="metered_openrouter").validate()

    def test_non_object_openrouter_metadata_is_refused_not_a_crash(self):
        for bad in ("x", 5, ["a"], True):
            sel = selection(availability=[{"provider": "claude", "state": "exhausted", "checked_at": NOW}], tier="medium")
            sel["openrouter"] = bad
            self.assertEqual(run(sel, "--provider", "openrouter", "--model", "anthropic/claude-sonnet-5.5", "--route",
                                 "metered_openrouter", "--success")[0], 2, bad)


class FourthReviewTests(unittest.TestCase):
    def test_credential_shaped_usage_keys_are_refused_and_never_echoed(self):
        secret = "sk-abcdef123456"
        for key in (secret, "A" * 40, "has space", "", "bearer abc", "x" * 65, "ghp_abcdefghij"):
            out, err = io.StringIO(), io.StringIO()
            old = sys.stderr
            sys.stderr = err
            try:
                code, _ = run(selection(), *NATIVE, "--success", "--usage", json.dumps({key: 1}))
            finally:
                sys.stderr = old
            self.assertEqual(code, 2, key)
            if key:
                self.assertNotIn(key, err.getvalue())
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", '{"cache_read.tokens": 1, "ttft_ms": 3}')[0], 0)

    def test_copied_selection_fields_are_sanitized_too(self):
        secret = "sk-abcdef123456"
        for field, value in (("effort", secret), ("provider", secret), ("model", secret), ("selection_id", secret),
                             ("selection_id", "has space"), ("selection_id", "x" * 65)):
            sel = selection()
            sel[field] = value
            self.assertEqual(run(sel, *NATIVE, "--success")[0], 2, field)

    def test_a_long_valid_model_id_can_be_receipted(self):
        long_id = "vendor/" + "model-name-" * 8 + "x"
        self.assertGreater(len(long_id), 32)
        sel = selection(availability=[{"provider": "claude", "state": "exhausted", "checked_at": NOW}], tier="medium")
        sel["openrouter"]["allowed_models"] = ["*"]
        code, r = run(sel, "--provider", "openrouter", "--model", long_id, "--route", "metered_openrouter", "--success")
        self.assertEqual((code, r["model"]), (0, long_id))
        self.assertEqual(run(selection(), *NATIVE[:2], "--model", "m" * 129, *NATIVE[4:], "--success")[0], 2)
        self.assertEqual(run(selection(), "--provider", "claude", "--model", "sk-abcdef123456", "--route", "native_cli",
                             "--success")[0], 2)

    def test_a_successful_auto_run_cannot_be_receipted_as_the_auto_alias(self):
        sel = selection(availability=[{"provider": "claude", "state": "exhausted", "checked_at": NOW}], tier="medium")
        for alias in ("openrouter/auto", "openrouter/auto-beta"):
            args = ("--provider", "openrouter", "--model", alias, "--route", "metered_openrouter")
            self.assertEqual(run(sel, *args, "--success")[0], 2, alias)
            if alias == "openrouter/auto":
                self.assertEqual(run(sel, *args, "--failure")[0], 0, alias)  # nothing was served on a failure
            else:  # auto-beta is not the selected model: that is a switch and needs its reason
                self.assertEqual(run(sel, *args, "--failure")[0], 2, alias)
                self.assertEqual(run(sel, *args, "--failure", "--switch-reason", "beta router used")[0], 0, alias)


class UsageTests(unittest.TestCase):
    def test_credential_like_fields_and_values_are_refused(self):
        for bad in ('{"api_key": "x"}', '{"Authorization": "x"}', '{"session_token": 1}', '{"note": "sk-abcdef123456"}',
                    '{"note": "' + "A" * 40 + '"}', '{"note": "Bearer abc"}', '{"nested": {"a": 1}}', '{"list": [1]}',
                    '{"note": "' + "x " * 60 + '"}', "[1, 2]", '"text"'):
            code, _ = run(selection(), *NATIVE, "--success", "--usage", bad)
            self.assertEqual(code, 2, bad)

    def test_token_counts_are_allowed_but_tokens_are_not(self):
        ok = '{"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "cache_read_tokens": 4}'
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", ok)[0], 0)
        for bad in ('{"token": "x"}', '{"session_token": 1}', '{"accesstoken": 1}', '{"id_token": "x"}'):
            self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", bad)[0], 2, bad)

    def test_plain_words_that_contain_key_or_token_are_fine_as_values(self):
        ok = '{"worker": "local_key_route_launch", "note": "token counts only", "plan": "included"}'
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", ok)[0], 0)
        for bad in ('{"note": "password=hunter2"}', '{"note": "Authorization: x"}', '{"note": "bearer xyz"}'):
            self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", bad)[0], 2, bad)

    def test_too_many_fields_are_refused(self):
        many = json.dumps({f"f{i}": i for i in range(30)})
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", many)[0], 2)

    def test_plain_numbers_booleans_and_short_strings_pass(self):
        ok = '{"input_tokens": 1, "cost_usd": 0.01, "cached": true, "plan": "included"}'
        code, r = run(selection(), *NATIVE, "--success", "--usage", ok)
        self.assertEqual(code, 0)
        self.assertEqual(r["observed_usage"]["plan"], "included")

    def test_malformed_usage_json_exits_two(self):
        self.assertEqual(run(selection(), *NATIVE, "--success", "--usage", "{not json")[0], 2)


class GraphTests(unittest.TestCase):
    def test_a_node_is_picked_out_of_graph_output(self):
        a, b = selection(thread_id="T:a"), selection(thread_id="T:b")
        graph = {"thread_id": "T", "nodes": [{"id": "a", "selection": a}, {"id": "b", "selection": b}]}
        code, r = run(graph, *NATIVE, "--success", "--node-id", "b")
        self.assertEqual((code, r["selection_id"]), (0, b["selection_id"]))
        self.assertEqual(run(graph, *NATIVE, "--success", "--node-id", "zzz")[0], 2)
        self.assertEqual(run(graph, *NATIVE, "--success")[0], 2)  # graph output needs --node-id
        self.assertEqual(run(selection(), *NATIVE, "--success", "--node-id", "a")[0], 2)


class AppendTests(unittest.TestCase):
    def test_receipts_append_as_lines_with_private_permissions(self):
        sel = selection()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sel.json"
            f.write_text(json.dumps(sel))
            log = Path(d) / "sub" / "receipts.jsonl"
            for _ in range(2):
                self.assertEqual(rc.run(parse("--selection", str(f), *NATIVE, "--success", "--append", str(log)),
                                        out=io.StringIO()), 0)
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["selection_id"], sel["selection_id"])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_a_refused_receipt_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sel.json"
            f.write_text(json.dumps(selection()))
            log = Path(d) / "receipts.jsonl"
            code = rc.run(parse("--selection", str(f), "--provider", "codex", "--model", "m", "--route", "native_cli",
                                "--success", "--append", str(log)), out=io.StringIO())
            self.assertEqual(code, 2)
            self.assertFalse(log.exists())


class CliTests(unittest.TestCase):
    def test_the_cli_exposes_receipt_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "sel.json"
            f.write_text(json.dumps(selection()))
            proc = subprocess.run([sys.executable, "-m", "side_lane.cli", "receipt", "--selection", str(f), *NATIVE,
                                   "--success"], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["billing_evidence"], "unverified")
            bad = subprocess.run([sys.executable, "-m", "side_lane.cli", "receipt", "--selection", str(f), "--provider",
                                  "codex", "--model", "m", "--route", "native_cli", "--success"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(bad.returncode, 2)
            self.assertIn("switch_reason", bad.stderr)


if __name__ == "__main__":
    unittest.main()
