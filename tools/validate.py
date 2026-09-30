#!/usr/bin/env python3
"""Hermes repository validator.

Runs three layers of checks and exits non-zero on any failure:
  1. JSON Schema (draft 2020-12 subset) for contracts, registry, runtime files, policy.
     Uses the `jsonschema` package when installed; otherwise a built-in validator that
     covers every keyword used by the Hermes schemas.
  2. Semantic rules that a schema cannot express (budgets, permissions, models, costs).
  3. Static checks on SQL migrations (row-level security coverage, append-only audit).

Usage:  python tools/validate.py            (from repository root)
"""
import yaml, json, re, sys, glob, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []  # (ok, layer, message)


def check(ok, layer, msg):
    results.append((bool(ok), layer, msg))
    return ok


# --------------------------------------------------------------------------- schema
class MiniValidator:
    """Subset of JSON Schema 2020-12 sufficient for the Hermes schemas."""

    TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}

    def __init__(self, schema):
        self.root = schema

    def _resolve(self, ref):
        assert ref.startswith("#/"), ref
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def _is_type(self, v, t):
        if t == "integer":
            return isinstance(v, int) and not isinstance(v, bool)
        if t == "number":
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        return isinstance(v, self.TYPES[t])

    def errors(self, inst, sch=None, path="$"):
        sch = self.root if sch is None else sch
        if "$ref" in sch:
            yield from self.errors(inst, self._resolve(sch["$ref"]), path)
            return
        if "type" in sch:
            types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
            if not any(self._is_type(inst, t) for t in types):
                yield f"{path}: expected {types}, got {type(inst).__name__}"
                return
        if "const" in sch and inst != sch["const"]:
            yield f"{path}: must equal {sch['const']!r}"
        if "enum" in sch and inst not in sch["enum"]:
            yield f"{path}: {inst!r} not in {sch['enum']}"
        if isinstance(inst, str):
            if "pattern" in sch and not re.search(sch["pattern"], inst):
                yield f"{path}: {inst!r} does not match {sch['pattern']}"
            if "maxLength" in sch and len(inst) > sch["maxLength"]:
                yield f"{path}: longer than {sch['maxLength']}"
            if "minLength" in sch and len(inst) < sch["minLength"]:
                yield f"{path}: shorter than {sch['minLength']}"
        if isinstance(inst, (int, float)) and not isinstance(inst, bool):
            if "minimum" in sch and inst < sch["minimum"]:
                yield f"{path}: {inst} < {sch['minimum']}"
            if "maximum" in sch and inst > sch["maximum"]:
                yield f"{path}: {inst} > {sch['maximum']}"
        if isinstance(inst, list):
            if "minItems" in sch and len(inst) < sch["minItems"]:
                yield f"{path}: fewer than {sch['minItems']} items"
            if "maxItems" in sch and len(inst) > sch["maxItems"]:
                yield f"{path}: more than {sch['maxItems']} items"
            if sch.get("uniqueItems") and len({json.dumps(i, sort_keys=True) for i in inst}) != len(inst):
                yield f"{path}: items not unique"
            if "items" in sch:
                for i, item in enumerate(inst):
                    yield from self.errors(item, sch["items"], f"{path}[{i}]")
        if isinstance(inst, dict):
            for req in sch.get("required", []):
                if req not in inst:
                    yield f"{path}: missing required '{req}'"
            if "minProperties" in sch and len(inst) < sch["minProperties"]:
                yield f"{path}: fewer than {sch['minProperties']} properties"
            props = sch.get("properties", {})
            for k, v in inst.items():
                if k in props:
                    yield from self.errors(v, props[k], f"{path}.{k}")
                elif sch.get("additionalProperties") is False:
                    yield f"{path}: unexpected property '{k}'"
                elif isinstance(sch.get("additionalProperties"), dict):
                    yield from self.errors(v, sch["additionalProperties"], f"{path}.{k}")


def schema_errors(instance, schema):
    try:
        import jsonschema  # type: ignore
        v = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in v.iter_errors(instance)]
    except ImportError:
        return list(MiniValidator(schema).errors(instance))


def load(rel):
    with open(ROOT / rel, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- layer 1
contract_schema = load("contracts/agent_contract.schema.json")
registry = load("contracts/registry.json")
errs = schema_errors(registry, load("contracts/registry.schema.json"))
check(not errs, "schema", "registry.json matches registry.schema.json" + ("" if not errs else f" -> {errs}"))

contracts = {}
for entry in registry["agents"]:
    path = ROOT / "contracts" / entry["contract"]
    if not check(path.exists(), "schema", f"{entry['contract']} exists"):
        continue
    c = load(f"contracts/{entry['contract']}")
    errs = schema_errors(c, contract_schema)
    check(not errs, "schema", f"{entry['contract']} matches agent_contract.schema.json" + ("" if not errs else f" -> {errs}"))
    check(c["agent_id"] == entry["id"], "schema", f"{entry['contract']}: agent_id equals registry id")
    if errs:   # semantic rules assume a schema-valid contract; report instead of crashing
        check(False, "semantic", f"{entry['contract']}: semantic checks skipped until the schema errors are fixed")
        continue
    contracts[c["agent_id"]] = c

orphans = {Path(p).name for p in glob.glob(str(ROOT / "contracts/*.contract.json"))} - {e["contract"] for e in registry["agents"]}
check(not orphans, "schema", "no contract file outside the registry" + ("" if not orphans else f" -> {orphans}"))

st = load("runtime/agent_runtime_state.example.json")
errs = schema_errors(st, load("runtime/agent_runtime_state.schema.json"))
check(not errs, "schema", "agent_runtime_state.example.json matches its schema" + ("" if not errs else f" -> {errs}"))

call_schema = load("runtime/agent_call.schema.json")
with open(ROOT / "runtime/agent_calls.example.jsonl", encoding="utf-8") as f:
    for n, line in enumerate(f, 1):
        errs = schema_errors(json.loads(line), call_schema)
        check(not errs, "schema", f"agent_calls.example.jsonl line {n} matches agent_call.schema.json" + ("" if not errs else f" -> {errs}"))

policy = load("policies/complaint_keywords.json")
errs = schema_errors(policy, load("policies/complaint_keywords.schema.json"))
check(not errs, "schema", "complaint_keywords.json matches its schema" + ("" if not errs else f" -> {errs}"))


crules = load("policies/content_rules.json")
errs = schema_errors(crules, load("policies/content_rules.schema.json"))
check(not errs, "schema", "content_rules.json matches its schema" + ("" if not errs else f" -> {errs}"))

# --------------------------------------------------------------------------- layer 2
def covered(action, patterns):
    dom, verb = action.split(":")
    return any(p == action or (p.split(":")[1] == "*" and p.split(":")[0] == dom) for p in patterns)


# idempotency keys are scoped by agent, action and tool (defect R3, v1.5)
seen = {}
for n, line in enumerate((ROOT / "runtime/agent_calls.example.jsonl").read_text(encoding="utf-8").splitlines(), 1):
    if not line.strip():
        continue
    row = json.loads(line)
    k = row["idempotency_key"]
    check(k.startswith(f"{row['agent_id']}|") and f"|{row['tool']}|" in k, "semantic",
          f"agent_calls example line {n}: key scoped by agent and tool")
    ca = contracts.get(row["agent_id"])
    if ca:
        act_ = k.split("|")[1]
        check(covered(act_, ca["permissions"]["allow"]) or act_ in ca["permissions"]["proposals"], "semantic",
              f"agent_calls example line {n}: action '{act_}' permitted for {row['agent_id']}")
    prev = seen.get((row["task_id"], k))
    check(prev is None or prev == row["tool"], "semantic", f"agent_calls example line {n}: no key shared by two tools in one task")
    seen[(row["task_id"], k)] = row["tool"]

limits, models = registry["limits"], registry["approved_models"]
monthly_sum = 0.0
for aid, c in contracts.items():
    perm, bud, rt = c["permissions"], c["budget"], c["runtime"]
    check(not [a for a in perm["proposals"] if covered(a, perm["deny"])], "semantic",
          f"{aid}: no proposal is also denied (approval path reachable)")
    check(not set(perm["proposals"]) & set(perm["allow"]), "semantic",
          f"{aid}: proposals are not directly allowed (a human executes them)")
    check(not [a for a in perm["allow"] if covered(a, perm["deny"])], "semantic", f"{aid}: allow and deny do not overlap")
    check(not [t for t in c["tools"] if t["side_effects"] == "financial"], "semantic",
          f"{aid}: no tool with financial side effects")
    for m in (c["model"]["primary"], c["model"]["fallback"]):
        if m is not None:
            check(m in models, "semantic", f"{aid}: model '{m}' is in registry.approved_models (budgeted and vendor-confirmed)")
    check(bud["per_call_usd"] <= bud["per_task_usd"] <= bud["class_cap_usd"], "semantic",
          f"{aid}: per_call <= per_task <= class_cap ({bud['per_call_usd']} <= {bud['per_task_usd']} <= {bud['class_cap_usd']})")
    price = models.get(c["model"]["primary"])
    if price is None:
        continue  # unbudgeted model already reported above; cost checks need a price
    llm_max = rt["max_input_tokens"] * price["input_usd_per_mtok"] / 1e6 + c["model"]["params"]["max_output_tokens"] * price["output_usd_per_mtok"] / 1e6
    tool_max = max([t.get("unit_cost_usd", 0.0) for t in c["tools"]] + [0.0])
    check(llm_max + tool_max <= bud["per_call_usd"] + 1e-9, "semantic",
          f"{aid}: worst-case call cost {llm_max + tool_max:.4f} fits per_call_usd {bud['per_call_usd']}")
    if bud["budget_class"] == "monthly_active":
        monthly_sum += bud["class_cap_usd"]
        check("customer_id" in rt["idempotency_key_fields"] or aid == "agent_billing", "semantic",
              f"{aid}: idempotency key is tenant-scoped")
        check(rt["max_input_tokens"] <= 16000, "semantic", f"{aid}: context <= 16k tokens for monthly agents")
    if bud["budget_class"] == "onboarding":
        check(bud["class_cap_usd"] <= limits["onboarding_cap_per_new_customer_usd"], "semantic",
              f"{aid}: onboarding cap within registry limit")
    if bud["budget_class"] == "acquisition":
        check("customer_id" not in rt["idempotency_key_fields"], "semantic", f"{aid}: acquisition key is not per customer")
        check(bud["class_cap_usd"] <= limits["acquisition_pool_month_usd"], "semantic", f"{aid}: acquisition cap within pool")
    if c["retry"]["backoff"] == "none":
        check(c["retry"]["max_attempts"] <= 1, "semantic", f"{aid}: no backoff implies at most one attempt")
    check(c["audit"]["pii_redaction"] is True, "semantic", f"{aid}: PII redaction on")
    check(c["retry"]["max_delay_ms"] >= c["retry"]["initial_delay_ms"], "semantic", f"{aid}: max_delay >= initial_delay")
    if c.get("feature_flags", {}).get("silent_mode"):
        check(not perm["proposals"] and not [t for t in c["tools"] if t["scope"] in ("write_commit", "write_external")],
              "semantic", f"{aid}: silent-mode auditor has no external or committing effects")
        ff = c["feature_flags"]
        check(all(k in ff for k in ("promotion_min_days", "promotion_min_resolved_flags", "promotion_min_precision_pct")),
              "semantic", f"{aid}: silent mode declares measurable promotion criteria")
        check(all(k in ff for k in ("promotion_unflagged_audit_min_samples", "promotion_unflagged_severe_upper95_max_pct")),
              "semantic", f"{aid}: promotion also audits UNFLAGGED output (precision alone cannot see what was missed)")

check(monthly_sum <= limits["ai_cap_per_active_customer_month_usd"] + 1e-9, "semantic",
      f"sum of monthly agent caps {monthly_sum:.2f} <= AI cap per active customer {limits['ai_cap_per_active_customer_month_usd']}")
comp = contracts.get("agent_competitor")
if comp:
    check(comp["feature_flags"]["max_monthly_checks"] <= registry["package_limits"]["competitor_checks_per_month"], "semantic",
          "competitor checks within package limit")
check(any(not e["enabled"] for e in registry["agents"] if e["id"] == "agent_billing"), "semantic",
      "agent_billing disabled until gate 0.1")

# policy rules
cats = {c["id"] for c in policy["categories"]}
check(cats == {"pricing", "quality", "legal", "safety", "refund"}, "semantic", "policy defines the five categories")
all_kw = [k for c in policy["categories"] for k in c["keywords"] + c["keywords_ye"]]
check(len(all_kw) == len(set(all_kw)), "semantic", "no keyword listed twice across categories")
check(not set(all_kw) & set(policy["owner_inquiry_terms"]), "semantic", "owner inquiry terms are not complaint keywords")
check(set(policy["channels"]["patron"]["copy_to_founder"]) <= {"legal", "safety"}, "semantic",
      "patron messages reach the founder only for legal and safety")
check(all(len(c["keywords_ye"]) >= 2 for c in policy["categories"]), "semantic", "every category has Yemeni-dialect forms")


for r in crules["rules"]:
    check(bool(r["severity_by_kind"]), "semantic", f"content rule {r['id']}: applies to at least one output kind")
    if r["kind"] == "regex":
        try:
            [re.compile(p) for p in r["patterns"]]
            check(True, "semantic", f"content rule {r['id']}: regex patterns compile")
        except re.error as e:
            check(False, "semantic", f"content rule {r['id']}: regex patterns compile -> {e}")
check(all(r["severity_by_kind"].get("reply") == "block" for r in crules["rules"] if r["id"] in ("price_mention", "price_word")),
      "semantic", "price mentions are blocked in agent replies (matches agent_replies price:quote deny)")
check(set(crules["allow_link_domains"]) and all("." in d for d in crules["allow_link_domains"]), "semantic", "link allowlist holds domains only")

sys.path.insert(0, str(ROOT / "tools"))
from validate_schema import free_text_fields  # noqa: E402
ops_schema = load("runtime/ops_summary.schema.json")
check(not free_text_fields(ops_schema), "semantic", "Hermes Agent data door (ops_summary) has no free-text field (ADR-0002)")
vocab = set(ops_schema["properties"]["failures"]["items"]["properties"]["error_code"]["enum"])
used = set(re.findall(r'Denied\("([A-Z_0-9]+)"', (ROOT / "tools/enforce.py").read_text(encoding="utf-8")))
used |= set(re.findall(r"raise exception '([A-Z_0-9]+)", "".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "db/migrations").glob("*.sql")))))
used |= {k.upper() for k in contract_schema["properties"]["retry"]["properties"]["retryable_errors"]["items"]["enum"]}
used |= set(re.findall(r'FetchRefused\("([A-Z_0-9]+)"', "".join(p.read_text(encoding="utf-8") for p in
                                                                   [ROOT / "tools/safe_fetch.py", *sorted((ROOT / "service").glob("*.py"))])))
check("UNKNOWN_CODE" in vocab and "unknown_codes" in ops_schema["required"], "semantic",
      "unknown failure codes are quarantined as UNKNOWN_CODE, never rejected (incident path)")
lexicon = yaml.safe_load((ROOT / "docs/error_codes.yaml").read_text(encoding="utf-8"))["codes"]
check(vocab == {k for k, v in lexicon.items() if v["kind"] != "state"} and all(v.get("ar") for v in lexicon.values()), "semantic",
      "failure vocabulary is generated from docs/error_codes.yaml; every code has a meaning")
check(used <= vocab, "semantic", "failure vocabulary covers every error code the platform can raise" + ("" if used <= vocab else f" -> missing {sorted(used - vocab)}"))
adm = load("evals/model_admission.json")
fl = adm["floors"]
acc = load("policies/acceptance_policy.json")
errs = schema_errors(acc, load("policies/acceptance_policy.schema.json"))
check(not errs, "schema", "acceptance_policy.json matches its schema" + ("" if not errs else f" -> {errs}"))
import acceptance as _acc  # noqa: E402
from stats import acceptance_probability_zero_misses as _pz  # noqa: E402
check(set(acc["critical_categories"]) >= {"legal", "safety"} and acc["critical_misses_max"] == 0, "semantic",
      "acceptance policy: zero misses in the critical categories (legal, safety)")
check(_acc.minimum_complaints_consistent(), "semantic",
      f"minimum complaint count can meet the miss-rate bound at the per-attempt confidence ({acc['min_complaints']} complaints, "
      f"one-sided {_acc.attempt_confidence():.1%})")
check(_pz(acc["max_miss_rate_upper"], acc["min_complaints"], acc["max_attempts_per_version"]) <= 1 - acc["path_confidence"] + 1e-9, "semantic",
      "a system at the threshold miss rate passes a zero-miss attempt within the allowed attempts with probability <= 1 - path confidence")
check(adm["retest_policy"]["max_consecutive_failures_per_version"] <= acc["max_attempts_per_version"], "semantic",
      "model retest policy stays inside the acceptance family (attempts per version)")
check(not any(k in fl for k in ("complaints_miss_upper95_max", "critical_missed_max", "min_dataset_complaints", "complaints_precision_min"))
      and "acceptance_test" not in policy, "semantic", "no second copy of the complaint acceptance rule (admission floors, keyword policy)")
check("evals/complaints_seed.jsonl" in acc["development_datasets"], "semantic", "the development seed set can never be an acceptance attempt")
q = contracts.get("agent_quality")
if q:
    check(fl["quality_precision_min"] * 100 == q["feature_flags"]["promotion_min_precision_pct"], "semantic",
          "model admission quality floor matches the quality agent promotion criterion")
from admission import evaluate  # noqa: E402
statuses = {}
for cand in adm["candidates"]:
    st, probs = evaluate(cand, fl, adm["retest_policy"])
    statuses[(cand["model"], cand["version"])] = (st, cand)
    check(st != "invalid", "semantic", f"admission record {cand['model']}@{cand['version']} follows the retest policy" + ("" if st != "invalid" else f" -> {probs}"))
for aid, c in contracts.items():
    fb = c["model"]["fallback"]
    if fb is None:
        continue
    ok = [cand for (m, v), (st, cand) in statuses.items() if m == fb and st == "admitted" and aid in cand["agent_ids"]]
    check(bool(ok), "semantic", f"{aid}: fallback '{fb}' admitted by measurement (evals/model_admission.json, ADR-0009)")

# --------------------------------------------------------------------------- layer 3
sql = "\n".join(open(p, encoding="utf-8").read() for p in sorted(glob.glob(str(ROOT / "db/migrations/*.sql"))))
tables = {}
for m in re.finditer(r"create table app\.(\w+)\s*\((.*?)\n\);", sql, re.S):
    tables[m.group(1)] = m.group(2)
import sql_state  # noqa: E402
final = sql_state.load()
inventory = (ROOT / "docs/security_definer_inventory.md").read_text(encoding="utf-8")
inv_force = re.search(r"^FORCE-EXCEPTIONS:\s*(.+)$", inventory, re.M)
inv_force = sorted(x.strip() for x in inv_force.group(1).split(",")) if inv_force else []
check(final.force_exceptions() == inv_force, "sql", "FORCE exceptions after all migrations equal the published inventory"
      + ("" if final.force_exceptions() == inv_force else f" -> final {final.force_exceptions()} vs inventory {inv_force}"))
ENABLE_ONLY = set(inv_force)                                # the published list; anything else must be FORCE
NO_WORKER_TABLES = {"task_leases"}                          # v1.7: tenant authority lives here; worker never touches it directly
for t, body in tables.items():
    has_tenant = re.search(r"^\s*customer_id\s+uuid", body, re.M) is not None
    r = final.rls.get(t, {"enabled": False, "force": False})
    check(r["enabled"], "sql", f"{t}: RLS enabled")
    if t not in ENABLE_ONLY:
        check(r["force"], "sql", f"{t}: RLS forced")
    check(any(k[0] == t for k in final.policies), "sql", f"{t}: at least one policy")
    wpols = [p_ for p_ in final.policies.values() if p_.table == t and "hermes_worker" in p_.roles]
    if t in NO_WORKER_TABLES:
        check(not any(k[0] == "hermes_worker" and k[1] == t for k in final.privs), "sql", f"{t}: no direct worker grant (changed only through functions)")
        continue
    if has_tenant and wpols:
        check(all(sql_state.LEASE_BOUND.search(p_.using + " " + p_.check) for p_ in wpols)
              and not any("is not distinct from (select app.worker_customer_id())" in (p_.using + p_.check) for p_ in wpols),
              "sql", f"{t}: worker access bound to a live lease (no NULL-tenant match without one)")
bad = sql_state.worker_policies_not_lease_bound(final)
check(not bad, "sql", "every final worker policy is bound to a lease, except reference data" + ("" if not bad else f" -> {bad}"))
narrow = [("tasks", "update"), ("tasks", "insert"), ("approvals", "update"), ("approvals", "insert"), ("outbox", "update"),
          ("kb_facts", "update"), ("webhook_events", "update"), ("webhook_events", "insert")]
wide = [f"{t}:{pv}" for t, pv in narrow if final.table_priv("hermes_worker", t, pv)]
check(not wide, "sql", "worker holds column grants only where the authority layer needs them (final grants, after revokes)"
      + ("" if not wide else f" -> {wide}"))
check(not final.column_priv("hermes_worker", "outbox", "update", "resolution")
      and not final.column_priv("hermes_worker", "outbox", "update", "resolved_by")
      and not final.column_priv("authenticated", "approvals", "update", "consumed_at"), "sql",
      "no application role can write a resolution or a consumption directly")
check(not final.column_priv("hermes_jobs", "inquiries", "select", "body") and final.column_priv("hermes_jobs", "inquiries", "update", "body"),
      "sql", "retention job can clear an inquiry body but never read one")
check(not final.column_priv("hermes_ingest", "webhook_events", "insert", "customer_id"), "sql",
      "webhook ingest cannot choose a tenant (routing from channel_accounts only)")
check("create trigger audit_no_update before update or delete on app.audit_log" in sql, "sql", "audit_log blocks update/delete")
check("revoke update, delete, truncate on app.audit_log" in sql, "sql", "audit_log privileges revoked")
check(not re.search(r"grant[^;]*delete[^;]*to (authenticated|hermes_worker)", sql, re.I), "sql", "no delete grants to application roles")
check("bypassrls" not in sql.split("0000_supabase_shim")[0].lower(), "sql", "migrations create no bypassrls role")
check("security definer" not in re.search(r"function app\.assert_approved.*?\$\$", sql, re.S).group(0), "sql",
      "approval guard runs as invoker inside the tenant scope")
check("unique (kind, external_event_id)" in sql, "sql", "webhook_events are idempotent per provider event id")
check("check (signature_valid or processed_at is null)" in sql, "sql", "unsigned webhook events are never processed")
check("for update skip locked" in sql, "sql", "queue claim uses FOR UPDATE SKIP LOCKED")
check(re.search(r"topic not in \('content\.publish','reply\.send','site\.deploy_prod'\) or approval_id is not null", sql) is not None,
      "sql", "outbox refuses external effects without an approval")
unwrapped = [st.split(" on ")[0] for st in re.findall(r"create policy .*?;", sql, re.S)
             if re.search(r"(?<!select )app\.(is_operator|worker_customer_id)\(\)", st)]
check(not unwrapped, "sql", "policy helper calls wrapped as (select fn()) for per-statement evaluation" + ("" if not unwrapped else f" -> {unwrapped[:2]}"))
for name, fn in sorted(final.functions.items()):
    if fn["definer"]:
        check(f"`app.{name}(" in inventory, "sql", f"app.{name}: security definer function listed in docs/security_definer_inventory.md")
        check("set search_path" in fn["head"].lower(), "sql", f"app.{name}: security definer function pins search_path")
check("revoke execute on all functions in schema app from public" in sql, "sql", "EXECUTE on app functions revoked from PUBLIC")
# the global form: a per-schema default cannot revoke the built-in PUBLIC EXECUTE (it only adds)
check(re.search(r"alter default privileges\s+revoke execute on functions from public", sql) is not None, "sql", "future app functions start without PUBLIC EXECUTE")
check("grant execute on function app.assert_approved(uuid, uuid, text) to hermes_worker, authenticated" in sql, "sql",
      "guard helper executable by the roles whose writes fire the guard triggers")
check("leads_name_source_required" in sql, "sql", "lead names require a non-Google source (Maps caching terms)")
check("create table app.restore_drills" in sql and "v_restore_drill_status" in sql, "sql", "restore drills recorded and surfaced")
check("v_secrets_rotation_due" in sql, "sql", "secret rotation age surfaced (90 days)")
# v1.5: concurrency-safe budget, idempotency and probe in the database
for fn in ("reserve_budget", "settle_budget", "claim_idempotency", "complete_idempotency", "release_idempotency", "claim_probe", "record_outcome"):
    check(f"create or replace function app.{fn}(" in sql, "sql", f"app.{fn} present (v1.5)")
rbs = re.findall(r"function app\.reserve_budget\(.*?end \$\$;", sql, re.S)
if rbs:
    body = rbs[-1]                                    # the definition in force after all migrations
    check("agent_is_paused(p_agent)" in body and body.index("agent_is_paused") < body.index("for update"), "sql",
          "reserve_budget refuses a paused agent before taking any lock (v1.6)")
    pos = [body.find(t) for t in ("from app.acquisition_budget where", "from app.customer_ai_budget where", "from app.agent_state where", "from app.task_budget where")]
    check(body.count("for update") >= 4, "sql", "reserve_budget locks every budget row it checks (FOR UPDATE)")
    check(pos[1] < pos[2] < pos[3], "sql", "reserve_budget lock order: customer -> agent -> task (deadlock-free)")
    check(body.index("return 'OK'") > body.rindex("reserved_usd = reserved_usd + p_amount"), "sql", "reserve_budget reserves before reporting OK")
check("on conflict (key) do update" in sql and "where app.idempotency_keys.status = 'failed'" in sql, "sql",
      "idempotency claim is one conflict-safe statement; only failed keys are re-claimed")
check("idempotency_keys" in sql and "^agent_[a-z0-9_]+\\|" in sql, "sql", "idempotency keys must start with agent|action|tool (format check)")
# v1.6: leases, terminal state, lock order doc
check("app.idempotency_keys.lease_until < now()" in sql, "sql", "a dead key holder is taken over only after its lease")
check("probe_until > now()" in sql and "PROBE_LEASE_MUST_BE_BELOW_COOLDOWN" in sql, "sql", "probe lease exists and is shorter than the cooldown")
check("status = 'rejected'" in sql and "operator_release_rejected" in sql, "sql", "terminal failures stay rejected until an audited operator release")
check("run_after <= now()" in sql and "function app.requeue_task(" in sql, "sql", "IN_PROGRESS duplicates are re-queued with a delay, not dropped")
lock_doc = (ROOT / "docs/lock_order.md").read_text(encoding="utf-8") if (ROOT / "docs/lock_order.md").exists() else ""
check("tasks → acquisition_budget → customer_ai_budget → agent_state → task_budget" in lock_doc, "sql", "global lock order written in docs/lock_order.md")
if rbs:
    b = rbs[-1]
    check(b.find("from app.acquisition_budget where") < b.find("from app.customer_ai_budget where"), "sql",
          "reserve_budget follows the documented order (acquisition before customer)")
# v1.7 authority layer
check("alter table app.tasks no force row level security" in sql and "alter table app.approvals no force row level security" in sql, "sql",
      "tables managed by definer functions drop FORCE explicitly, with the reason written")
wcid = re.findall(r"function app\.worker_customer_id\(\).*?\$\$(.*?)\$\$", sql, re.S)
check(bool(wcid) and "task_leases" in wcid[-1] and "app.customer_id" not in wcid[-1], "sql",
      "worker tenant comes only from a live task lease, never from a GUC the worker chooses")
iso = re.findall(r"function app\.is_operator\(\).*?\$\$(.*?)\$\$", sql, re.S)
check(bool(iso) and "aal2" in iso[-1], "sql", "operator authority requires an aal2 session (MFA per session, not enrolment)")
check(re.search(r"grant insert \(customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at\) on app\.approvals to hermes_worker", sql) is not None
      and "revoke insert, update on app.approvals from hermes_worker" in sql, "sql", "the worker can only propose: no decision, decider or consumption columns")
check("CUSTOMER_APPROVAL_OWNER_ONLY" in sql and "DECIDER_MUST_BE_SESSION_USER" in sql, "sql", "a business's approval is decided by one of its owners, as the session user")
check("APPROVAL_PAYLOAD_MISMATCH" in sql and "APPROVAL_TARGET_MISMATCH" in sql and "outbox_approval_single_use" in sql and "OUTBOX_IMMUTABLE" in sql,
      "sql", "outbox checks target and payload hash, consumes once, and freezes the payload")
check("references app.outbox_topics (topic)" in sql, "sql", "outbox topics are a closed list")
check("PUBLISHED_CONTENT_IMMUTABLE" in sql and "before insert or update on app.deployments" in sql and "ROLLBACK_MUST_RESTORE_A_DEPLOYED_APPROVED_ARTIFACT" in sql,
      "sql", "published content immutable; prod deploys guarded on insert and update; rollback restores an approved artifact")
check("KB_APPROVAL_OWNER_ONLY" in sql and "grant update (topic, fact, valid_until) on app.kb_facts to hermes_worker" in sql, "sql",
      "knowledge approval is owner-only and survives no edit")
for child, parent in (("deployments", "sites"), ("payments", "invoices"), ("invoices", "subscriptions"), ("quality_flags", "content_items"),
                      ("channel_accounts", "secret_refs"), ("competitor_snapshots", "competitors")):
    check(re.search(rf"alter table app\.{child}\s+add constraint \w+ foreign key \(\w+, customer_id\) references app\.{parent} \(id, customer_id\)", sql) is not None,
          "sql", f"{child} -> {parent}: composite (id, customer_id) ownership key")
check("fencing" in sql and "DEAD_LETTER" in sql and "function app.complete_task(" in sql, "sql", "task leases with fencing, recovery and dead letter")
check("to_char(r.ts at time zone 'UTC'" in sql and "order by chain_seq" in sql, "sql", "audit chain hashed with a UTC timestamp and ordered by chain_seq")
for g in ("content_publish_guard", "prod_deploy_guard", "subscription_activate_guard", "competitor_limit_guard"):
    check(f"create trigger {g}" in sql, "sql", f"trigger {g} present")

# v1.8 (0010): closure of the 1.7 review, checked on the LAST definition of each function
def last_def(name):
    defs = re.findall(rf"create (?:or replace )?function app\.{name}\(.*?\$\$(.*?)\$\$", sql, re.S)
    return defs[-1] if defs else ""
check(all("clock_timestamp()" in last_def(f) and "now()" not in last_def(f).replace("clock_timestamp()", "")
          for f in ("worker_customer_id", "worker_context", "bind_task", "extend_task_lease")), "sql",
      "lease checks use the wall clock (clock_timestamp), not the transaction start")
ct = last_def("complete_task")
check("lease_until > clock_timestamp()" in ct and "status = 'running'" in ct and "TASK_STATUS_NOT_TERMINAL" in ct, "sql",
      "completion needs a live lease on a running task and a terminal status")
check("token = gen_random_uuid()" in last_def("claim_task") and "LEASE_SECONDS_OUT_OF_RANGE" in last_def("claim_task"), "sql",
      "recovery rotates the expired holder's token; lease length bounded")
co = last_def("claim_outbox_dispatch")
check("AMBIGUOUS_LEASE_EXPIRED" in co and "provider_idempotent" in co and "MAX_ATTEMPTS" in co, "sql",
      "an expired unconfirmed send is not re-claimed unless the provider de-duplicates")
ob = last_def("outbox_before_write")
check("OUTBOX_NEEDS_HUMAN" in ob and "app.is_operator()" in ob and "OUTBOX_FINAL" in ob and "OUTBOX_AMBIGUOUS_NEEDS_HUMAN" in ob, "sql",
      "outbox state machine: human check cleared only by an aal2 operator; outcomes final")
check("APPROVAL_ALREADY_CONSUMED" in last_def("enqueue_outbox") and "return o.id" in last_def("enqueue_outbox"), "sql",
      "a redelivered request returns the existing outbox row")
ab = last_def("approvals_before_write")
check("pg_get_userbyid" in ab and "app.consuming" not in ab, "sql", "approval consumption guarded by ownership, not by a session setting")
check(all(f"create trigger {t}_audit after" in sql for t in ("approvals", "outbox", "content_items", "deployments")), "sql",
      "effects audited by the database inside their own transaction (approvals, outbox, content, prod deploys)")
aia = final.policies.get(("audit_log", "audit_insert_authenticated"))
check(bool(aia) and "auth.uid()" in aia.check and "actor_type = 'operator'" in aia.check, "sql",
      "audit rows written by a session carry that session's own identity and role")
check("create trigger webhook_route before insert on app.webhook_events" in sql and "new.customer_id := null" in last_def("webhook_route"),
      "sql", "webhook events are routed from channel_accounts at ingest, never from the caller")
check("create table app.retention_runs" in sql and "function app.purge_inquiry_bodies(" in sql and "v_retention_status" in sql, "sql",
      "inquiry bodies purged after 30 days, each run recorded and surfaced")

# --------------------------------------------------------------------------- report
failed = [r for r in results if not r[0]]
by_layer = {}
for ok, layer, _ in results:
    by_layer.setdefault(layer, [0, 0])[0 if ok else 1] += 1
lines = [f"{'PASS' if ok else 'FAIL'} [{layer}] {msg}" for ok, layer, msg in results]
lines.append("")
lines.append("summary: " + ", ".join(f"{k} {v[0]} passed / {v[1]} failed" for k, v in by_layer.items()))
lines.append(f"TOTAL {len(results) - len(failed)}/{len(results)} checks passed")
print("\n".join(lines))
sys.exit(1 if failed else 0)
