#!/usr/bin/env python3
"""Generate derived artefacts from contracts and SLOs. Never edit derived/ by hand.

  derived/policy_matrix.md       human review of every agent's permissions
  derived/agent_capabilities.json API payload for the admin portal
  derived/alerts.yaml            alert rules (SLO thresholds + per-agent budget alerts)

CI runs `python tools/derive.py --check` and fails if derived/ is stale.
"""
import json, sys, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build():
    reg = json.loads((ROOT / "contracts/registry.json").read_text(encoding="utf-8"))
    cons = [json.loads((ROOT / "contracts" / e["contract"]).read_text(encoding="utf-8")) for e in reg["agents"]]
    enabled = {e["id"]: e["enabled"] for e in reg["agents"]}
    out = {}

    # policy matrix
    actions = sorted({a for c in cons for k in ("allow", "deny", "proposals") for a in c["permissions"][k]})
    head = "| action | " + " | ".join(c["agent_id"].replace("agent_", "") for c in cons) + " |"
    lines = ["# Policy matrix (generated — do not edit)", "",
             "A = allow · D = deny · P = proposal (queued for a human who executes it) · blank = not permitted", "",
             head, "|" + "---|" * (len(cons) + 1)]
    for a in actions:
        row = []
        for c in cons:
            p = c["permissions"]
            row.append("P" if a in p["proposals"] else "A" if a in p["allow"] else "D" if a in p["deny"] else "")
        lines.append(f"| `{a}` | " + " | ".join(row) + " |")
    lines += ["", "| agent | enabled | budget class | per call | per task | class cap |", "|---|---|---|---|---|---|"]
    for c in cons:
        b = c["budget"]
        lines.append(f"| {c['agent_id']} | {'yes' if enabled[c['agent_id']] else 'no'} | {b['budget_class']} | {b['per_call_usd']} | {b['per_task_usd']} | {b['class_cap_usd']} |")
    out["derived/policy_matrix.md"] = "\n".join(lines) + "\n"

    # capabilities
    caps = {"generated_from": reg["registry_version"], "agents": [
        {"agent_id": c["agent_id"], "version": c["version"], "enabled": enabled[c["agent_id"]], "purpose": c["purpose"],
         "tools": [t["name"] for t in c["tools"]], "allow": c["permissions"]["allow"], "proposals": c["permissions"]["proposals"],
         "budget_class": c["budget"]["budget_class"], "queue_priority": c["runtime"]["queue_priority"]} for c in cons]}
    out["derived/agent_capabilities.json"] = json.dumps(caps, ensure_ascii=False, indent=2) + "\n"

    # alerts
    slo = yaml.safe_load((ROOT / "ops/slo.yaml").read_text(encoding="utf-8"))
    rules = []
    for s in slo["slos"]:
        for level in ("warn", "critical"):
            sev = slo["severity_map"][level]
            rules.append({"id": f"{s['id']}_{level}", "metric": s["id"], "op": ">", "threshold": s[level],
                          "severity": sev, "channels": list(slo["channels"][sev]), "runbook": "ops/runbook.md"})
    for c in cons:
        b, e = c["budget"], c["escalation"]
        rules.append({"id": f"{c['agent_id']}_budget_alert", "metric": f"{c['agent_id']}.spent_ratio", "op": ">=",
                      "threshold": b["alert_threshold_pct"] / 100, "severity": "P3", "channels": [e["notification_channel"]],
                      "runbook": "ops/runbook.md#rb-03"})
        rules.append({"id": f"{c['agent_id']}_circuit_open", "metric": f"{c['agent_id']}.circuit_open", "op": "==",
                      "threshold": 1, "severity": "P1", "channels": ["founder_sms", "founder_slack"], "runbook": "ops/runbook.md#rb-01"})
    out["derived/alerts.yaml"] = "# generated — do not edit\n" + yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False)
    return out


if __name__ == "__main__":
    files = build()
    if "--check" in sys.argv:
        stale = [p for p, txt in files.items() if not (ROOT / p).exists() or (ROOT / p).read_text(encoding="utf-8") != txt]
        print("derived files up to date" if not stale else f"STALE: {stale}")
        sys.exit(1 if stale else 0)
    for p, txt in files.items():
        (ROOT / p).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / p).write_text(txt, encoding="utf-8")
        print("wrote", p)
