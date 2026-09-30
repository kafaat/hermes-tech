"""Prove the validator catches the defects found in review (mutation tests)."""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_with(mutate):
    tmp = Path(tempfile.mkdtemp())
    for d in ("contracts", "runtime", "policies", "db", "tools", "docs", "evals"):
        shutil.copytree(ROOT / d, tmp / d)
    mutate(tmp)
    p = subprocess.run([sys.executable, str(tmp / "tools/validate.py")], capture_output=True, text=True)
    shutil.rmtree(tmp)
    return p.returncode, p.stdout


def edit_json(tmp, rel, fn):
    p = tmp / rel
    d = json.loads(p.read_text(encoding="utf-8"))
    fn(d)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


class TestValidatorNegative(unittest.TestCase):
    def test_baseline_passes(self):
        code, out = run_with(lambda t: None)
        self.assertEqual(code, 0, out)

    def test_proposal_also_denied(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/site_builder.contract.json",
                                                 lambda d: d["permissions"]["deny"].append("site:deploy_prod")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL [semantic] agent_site_builder: no proposal is also denied", out)

    def test_unbudgeted_model(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/content.contract.json",
                                                 lambda d: d["model"].__setitem__("primary", "claude-sonnet-4-6")))
        self.assertEqual(code, 1)
        self.assertIn("is in registry.approved_models", out)

    def test_per_call_cap_below_worst_case(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/replies.contract.json",
                                                 lambda d: d["budget"].__setitem__("per_call_usd", 0.005)))
        self.assertEqual(code, 1)
        self.assertIn("FAIL [semantic] agent_replies: worst-case call cost", out)

    def test_monthly_caps_exceed_ai_cap(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/competitor.contract.json",
                                                 lambda d: d["budget"].__setitem__("class_cap_usd", 0.60)))
        self.assertEqual(code, 1)
        self.assertIn("FAIL [semantic] sum of monthly agent caps", out)

    def test_weekly_checks_above_package(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/competitor.contract.json",
                                                 lambda d: d["feature_flags"].__setitem__("max_monthly_checks", 40)))
        self.assertEqual(code, 1)
        self.assertIn("FAIL [semantic] competitor checks within package limit", out)

    def test_missing_force_rls(self):
        def m(t):
            p = t / "db/migrations/0002_rls.sql"
            p.write_text(p.read_text(encoding="utf-8").replace("alter table app.kb_facts force row level security;", ""), encoding="utf-8")
        code, out = run_with(m)
        self.assertEqual(code, 1)
        self.assertIn("FAIL [sql] kb_facts: RLS forced", out)

    def test_schema_invalid_contract_reports_not_crashes(self):
        def m(t):
            edit_json(t, "contracts/billing.contract.json", lambda d: d["budget"].pop("class_cap_usd"))
        code, out = run_with(m)
        self.assertEqual(code, 1)
        self.assertIn("semantic checks skipped until the schema errors are fixed", out)
        self.assertIn("TOTAL", out)

    def test_unlisted_security_definer_function(self):
        def m(t):
            p = t / "docs/security_definer_inventory.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text((ROOT / "docs/security_definer_inventory.md").read_text(encoding="utf-8").replace("`app.is_operator()`", "`app.removed()`"), encoding="utf-8")
        code, out = run_with(m)
        self.assertEqual(code, 1)
        self.assertIn("FAIL [sql] app.is_operator: security definer function listed", out)

    def test_public_execute_not_revoked(self):
        def m(t):
            p = t / "db/migrations/0006_v12_hardening.sql"
            p.write_text(p.read_text(encoding="utf-8").replace("revoke execute on all functions in schema app from public;", ""), encoding="utf-8")
        code, out = run_with(m)
        self.assertEqual(code, 1)
        self.assertIn("FAIL [sql] EXECUTE on app functions revoked from PUBLIC", out)

    def test_schema_scoped_default_privileges_do_not_count(self):
        # "in schema app" cannot revoke the built-in PUBLIC EXECUTE; only the global form closes new functions
        def m(t):
            p = t / "db/migrations/0006_v12_hardening.sql"
            p.write_text(p.read_text(encoding="utf-8").replace(
                "alter default privileges revoke execute on functions from public;",
                "alter default privileges in schema app revoke execute on functions from public;"), encoding="utf-8")
        code, out = run_with(m)
        self.assertEqual(code, 1)
        self.assertIn("FAIL [sql] future app functions start without PUBLIC EXECUTE", out)

    def test_fallback_without_admission_record(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/content.contract.json",
                                                 lambda d: d["model"].__setitem__("fallback", "gpt-5.4-nano")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL [semantic] agent_content: fallback 'gpt-5.4-nano' admitted by measurement", out)

    def test_schema_rejects_legacy_field(self):
        code, out = run_with(lambda t: edit_json(t, "contracts/replies.contract.json",
                                                 lambda d: d["permissions"].__setitem__("requires_human_approval", ["reply:send"])))
        self.assertEqual(code, 1)
        # wording differs between jsonschema (CI) and the built-in fallback: check the verdict, not the phrase
        self.assertRegex(out, r"FAIL \[schema\] replies\.contract\.json matches agent_contract\.schema\.json -> .*requires_human_approval")


if __name__ == "__main__":
    unittest.main()
