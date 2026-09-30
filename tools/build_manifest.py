#!/usr/bin/env python3
"""Release manifest: binds the specification version, every package file and the reports to one source id.

  python tools/build_manifest.py          write MANIFEST.json
  python tools/build_manifest.py --check  exit 1 if any file differs from the manifest (a report or file
                                           from another version cannot slip in unnoticed)
The source id is the SHA-256 of the sorted "path sha256" lines of every file except MANIFEST.json.
"""
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".git"}


def files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and p.name != "MANIFEST.json" and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS):
            yield p


def compute():
    entries = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files()}
    source_id = hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(entries.items())).encode()).hexdigest()
    reg = json.loads((ROOT / "contracts/registry.json").read_text(encoding="utf-8"))
    return {"package_version": (ROOT / "VERSION").read_text().strip(), "registry_version": reg["registry_version"],
            "source_id": source_id, "file_count": len(entries), "files": entries}


if __name__ == "__main__":
    m = compute()
    if "--check" in sys.argv:
        old = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
        diff = sorted(set(old["files"].items()) ^ set(m["files"].items()))
        if diff or old["source_id"] != m["source_id"]:
            print("MANIFEST MISMATCH:", sorted({d[0] for d in diff})[:20])
            sys.exit(1)
        print(f"manifest ok · {m['package_version']} · source {m['source_id'][:16]} · {m['file_count']} files")
        sys.exit(0)
    (ROOT / "MANIFEST.json").write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"manifest written · {m['package_version']} · source {m['source_id'][:16]} · {m['file_count']} files")
