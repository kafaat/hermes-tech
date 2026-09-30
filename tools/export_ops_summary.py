#!/usr/bin/env python3
"""Build the ONLY document Hermes Agent may read about the business.

Technical control for ADR-0002: the founder's agent never receives customer rows,
message text, names or phone numbers. This exporter whitelists aggregate fields,
drops everything else, and refuses to emit anything that fails
runtime/ops_summary.schema.json (which contains no free-text string fields).
"""
from __future__ import annotations
import json, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def build(month_id: str, totals: dict, agent_rows: list[dict], alert_rows: list[dict], failure_rows: list[dict] | None = None) -> dict:
    t_keys = ("active_customers", "new_customers", "ai_cost_usd", "human_minutes", "open_incidents", "queue_depth_p95")
    doc = {
        "month_id": month_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {k: totals[k] for k in t_keys},
        "agents": [{"agent_id": r["agent_id"], "calls": int(r["calls"]), "success_rate": round(float(r["success_rate"]), 4),
                    "cost_usd": round(float(r["cost_usd"]), 4)} for r in agent_rows],
        "alerts": [{"rule_id": r["rule_id"], "severity": r["severity"], "count": int(r["count"])} for r in alert_rows],
        "failures": [],
        "unknown_codes": 0,
    }
    schema = json.loads((ROOT / "runtime/ops_summary.schema.json").read_text(encoding="utf-8"))
    vocab = set(schema["properties"]["failures"]["items"]["properties"]["error_code"]["enum"])
    for r in (failure_rows or []):
        code = r["error_code"]
        if code not in vocab:                  # quarantine: a new code during an incident must not break the export
            doc["unknown_codes"] += int(r["count"])
            code = "UNKNOWN_CODE"
        doc["failures"].append({"customer_ref": r["customer_ref"], "agent_id": r["agent_id"], "error_code": code, "count": int(r["count"])})
    doc["failures"] = doc["failures"][:50]
    from validate_schema import errors_for
    errs = errors_for(doc, schema)
    if errs:
        raise ValueError(f"ops summary rejected: {errs}")
    return doc


if __name__ == "__main__":
    src = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(build(src["month_id"], src["totals"], src.get("agents", []), src.get("alerts", []), src.get("failures", [])),
                     ensure_ascii=False, indent=2))
