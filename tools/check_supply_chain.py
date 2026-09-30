#!/usr/bin/env python3
"""Supply-chain hygiene report for CI workflows and dependency files.

WARN: GitHub Action not pinned to a full commit SHA; requirements without --hash.
FAIL: pull_request_target trigger; workflow without a top-level permissions block.
Exit 1 on FAIL, or on WARN with --strict.
"""
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
warn, fail = [], []
for wf in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
    txt = wf.read_text(encoding="utf-8")
    if "pull_request_target" in txt:
        fail.append(f"{wf.name}: uses pull_request_target")
    if not re.search(r"^permissions:", txt, re.M):
        fail.append(f"{wf.name}: no top-level permissions block")
    for m in re.finditer(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", txt, re.M):   # YAML keys only, never comments
        ref = m.group(1)
        if not re.search(r"@[0-9a-f]{40}$", ref):
            warn.append(f"{wf.name}: action not pinned to a commit SHA: {ref}")
for req in ROOT.glob("**/requirements*.txt"):
    lines = [l for l in req.read_text().splitlines() if l.strip() and not l.startswith("#")]
    if lines and not all("--hash=" in l for l in lines):
        warn.append(f"{req.relative_to(ROOT)}: dependencies without --hash")
co = ROOT / ".github/CODEOWNERS"
if not co.exists() or not re.search(r"^/\.github/\s+@", co.read_text(encoding="utf-8"), re.M):
    fail.append("CODEOWNERS does not protect /.github/ (the gate cannot protect itself)")
elif "@OWNER_HANDLE" in co.read_text(encoding="utf-8"):
    warn.append("CODEOWNERS still has the @OWNER_HANDLE placeholder")
for l in fail:
    print("FAIL", l)
for l in warn:
    print("WARN", l)
print(f"supply chain: {len(fail)} fail · {len(warn)} warn")
sys.exit(1 if fail or ("--strict" in sys.argv and warn) else 0)
