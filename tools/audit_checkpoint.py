#!/usr/bin/env python3
"""External checkpoints of the audit chain head (claim A15b, docs/audit_checkpoints.md).

The hash chain shows that a recorded row changed; it cannot show that someone with full database rights
rebuilt the WHOLE chain. A daily checkpoint kept outside the database closes that:

  append  --store FILE --seq N --hash H        (key from $AUDIT_CHECKPOINT_KEY)  add today's head
  verify  --store FILE --hashes FILE           hashes: "seq<TAB>hash" lines from
          psql -Atc "select chain_seq, hash from app.audit_log where chain_seq in (...)"

verify fails when: a recorded seq now has another hash (chain rebuilt), the head went backwards, a
checkpoint's MAC is wrong (forged line in a store that is not append-only), or a recorded seq is missing.
The store must be append-only and outside the platform's credentials (private repo with append-only
token, or mail to the founder); this tool only formats and checks.
"""
from __future__ import annotations
import argparse, hashlib, hmac, json, os, sys
from datetime import datetime, timezone


def _mac(key: bytes, seq: int, h: str, at: str) -> str:
    return hmac.new(key, f"{seq}|{h}|{at}".encode(), hashlib.sha256).hexdigest()


def make(seq: int, h: str, key: bytes, at: str | None = None) -> dict:
    if seq < 1 or len(h) != 64:
        raise ValueError("head must be a positive seq and a sha256 hex hash")
    at = at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"seq": seq, "hash": h, "at": at, "mac": _mac(key, seq, h, at)}


def verify(checkpoints: list[dict], current: dict[int, str], key: bytes) -> list[str]:
    problems, last = [], 0
    for c in checkpoints:
        if not hmac.compare_digest(c.get("mac", ""), _mac(key, c["seq"], c["hash"], c["at"])):
            problems.append(f"seq {c['seq']}: checkpoint MAC does not match (forged or altered line)")
            continue
        if c["seq"] < last:
            problems.append(f"seq {c['seq']}: head went backwards (previous checkpoint {last})")
        last = max(last, c["seq"])
        if c["seq"] not in current:
            problems.append(f"seq {c['seq']}: recorded head no longer exists (rows removed)")
        elif current[c["seq"]] != c["hash"]:
            problems.append(f"seq {c['seq']}: hash changed since the checkpoint (chain rebuilt)")
    return problems


def _key() -> bytes:
    k = os.environ.get("AUDIT_CHECKPOINT_KEY", "")
    if len(k) < 32:
        sys.exit("AUDIT_CHECKPOINT_KEY missing or shorter than 32 characters")
    return k.encode()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("append"); a.add_argument("--store", required=True); a.add_argument("--seq", type=int, required=True); a.add_argument("--hash", required=True)
    v = sub.add_parser("verify"); v.add_argument("--store", required=True); v.add_argument("--hashes", required=True)
    args = ap.parse_args()
    if args.cmd == "append":
        with open(args.store, "a", encoding="utf-8") as f:
            f.write(json.dumps(make(args.seq, args.hash, _key())) + "\n")
        print("checkpoint appended")
    else:
        cps = [json.loads(l) for l in open(args.store, encoding="utf-8") if l.strip()]
        cur = {}
        for l in open(args.hashes, encoding="utf-8"):
            if l.strip():
                s, h = l.strip().split("\t")
                cur[int(s)] = h
        probs = verify(cps, cur, _key())
        for p in probs:
            print("FAIL", p)
        print(f"audit checkpoints: {len(cps)} checked, {len(probs)} problems")
        sys.exit(1 if probs else 0)
