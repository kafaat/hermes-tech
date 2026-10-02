"""Final state of the schema after ALL migrations, computed statically (v1.8).

Grants and policies accumulate: a policy created in 0002 and dropped in 0009 is gone; a table-level
REVOKE also removes column grants; CREATE OR REPLACE keeps a function, DROP FUNCTION removes it. Checks on
the concatenated text of all migrations cannot see any of that (the 1.7 validator still required a policy
0009 had dropped). This model applies the statements in order; db/tests case 46 asserts the same facts
against the real catalog in CI.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def statements(sql: str) -> list[str]:
    """Split on ';' outside quotes, dollar-quoted bodies and comments."""
    out, buf, i, n = [], [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and not sql.startswith("''", j):
                    break
                j += 2 if sql.startswith("''", j) else 1
            buf.append(sql[i:j + 1]); i = j + 1
            continue
        m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
        if m:
            tag = m.group(0)
            j = sql.find(tag, i + len(tag))
            j = n if j < 0 else j + len(tag)
            buf.append(sql[i:j]); i = j
            continue
        if c == ";":
            s = " ".join("".join(buf).split())
            if s:
                out.append(s)
            buf = []
        else:
            buf.append(c)
        i += 1
    s = " ".join("".join(buf).split())
    if s:
        out.append(s)
    return out


def _split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


@dataclass
class Policy:
    table: str
    name: str
    command: str
    roles: list
    using: str
    check: str
    migration: str


@dataclass
class State:
    tables: dict = field(default_factory=dict)          # name -> body
    rls: dict = field(default_factory=dict)             # name -> {"enabled": bool, "force": bool}
    policies: dict = field(default_factory=dict)        # (table, name) -> Policy
    privs: dict = field(default_factory=dict)           # (role, table, priv) -> "table" | set(columns)
    functions: dict = field(default_factory=dict)       # name -> {"definer": bool, "head": str, "migration": str}

    def table_priv(self, role, table, priv):
        return self.privs.get((role, table, priv)) == "table"

    def column_priv(self, role, table, priv, column):
        v = self.privs.get((role, table, priv))
        return v == "table" or (isinstance(v, set) and column in v)

    def force_exceptions(self):
        return sorted(t for t, r in self.rls.items() if r["enabled"] and not r["force"])


def _apply_grant(st: State, stmt: str, revoke: bool):
    m = re.match(r"(?:grant|revoke) (.+?) on (?:table )?((?:app\.\w+)(?:\s*,\s*app\.\w+)*) (?:to|from) (.+)$", stmt, re.I)
    if not m or " function" in m.group(1).lower() or "schema" in m.group(2).lower():
        return
    tables = [t.strip().split(".", 1)[1] for t in m.group(2).split(",")]
    roles = [r.strip().lower() for r in m.group(3).split(",")]
    for item in _split_top(m.group(1)):
        pm = re.match(r"(\w+)\s*(?:\((.*)\))?$", item.strip())
        if not pm:
            continue
        priv = pm.group(1).lower()
        cols = {c.strip() for c in pm.group(2).split(",")} if pm.group(2) else None
        privs = ["select", "insert", "update", "delete", "truncate", "references", "trigger"] if priv == "all" else [priv]
        for role in roles:
            for t in tables:
                for p in privs:
                    key = (role, t, p)
                    if not revoke:
                        if cols is None:
                            st.privs[key] = "table"
                        elif st.privs.get(key) != "table":
                            st.privs[key] = set(st.privs.get(key) or set()) | cols
                    else:
                        if cols is None:
                            st.privs.pop(key, None)          # a table-level REVOKE removes the column grants too
                        elif isinstance(st.privs.get(key), set):
                            st.privs[key] -= cols            # a column REVOKE never touches a table-level grant


def load(migrations=None) -> State:
    st = State()
    files = migrations or sorted((ROOT / "db/migrations").glob("*.sql"))
    for f in files:
        for s in statements(Path(f).read_text(encoding="utf-8")):
            low = s.lower()
            m = re.match(r"create table (?:if not exists )?app\.(\w+) \((.*)\)$", s, re.I | re.S)
            if m:
                st.tables[m.group(1)] = m.group(2)
                continue
            m = re.match(r"alter table app\.(\w+) (enable|no force|force) row level security", low)
            if m:
                r = st.rls.setdefault(m.group(1), {"enabled": False, "force": False})
                if m.group(2) == "enable":
                    r["enabled"] = True
                else:
                    r["force"] = m.group(2) == "force"
                continue
            m = re.match(r"create policy (\w+) on app\.(\w+)(?: as (permissive|restrictive))? for (\w+) to ([\w\s,]+?)(?: using \((.*?)\))?(?: with check \((.*)\))?$", s, re.I | re.S)
            if m:
                using, check = m.group(6) or "", m.group(7) or ""
                if not using and not check:
                    um = re.search(r" using \((.*)\)$", s, re.S)
                    using = um.group(1) if um else ""
                st.policies[(m.group(2), m.group(1))] = Policy(m.group(2), m.group(1), m.group(4).lower(),
                                                                [r.strip().lower() for r in m.group(5).split(",")],
                                                                using, check, Path(f).name)
                continue
            m = re.match(r"drop policy (?:if exists )?(\w+) on app\.(\w+)", low)
            if m:
                st.policies.pop((m.group(2), m.group(1)), None)
                continue
            if re.match(r"(grant|revoke) ", low):
                _apply_grant(st, s, low.startswith("revoke"))
                continue
            m = re.match(r"create (?:or replace )?function app\.(\w+)\((.*?)\)(.*?)as \$", s, re.I | re.S)
            if m:
                st.functions[m.group(1)] = {"definer": "security definer" in m.group(3).lower(), "head": m.group(0),
                                            "migration": Path(f).name}
                continue
            m = re.match(r"drop function (?:if exists )?app\.(\w+)", low)
            if m:
                st.functions.pop(m.group(1), None)
    return st


LEASE_BOUND = re.compile(r"worker_(customer_id|context)")
WORKER_REFERENCE_POLICIES = {"templates_read", "outbox_topics_read", "agent_pauses_worker_read",   # reference data, no tenant rows
                             "service_build_worker"}             # 0027: the live build (one commit row), spec 28.29


def worker_policies_not_lease_bound(st: State) -> list[str]:
    return sorted(f"{p.table}.{p.name}" for p in st.policies.values()
                  if "hermes_worker" in p.roles and p.name not in WORKER_REFERENCE_POLICIES
                  and not LEASE_BOUND.search(p.using + " " + p.check))


if __name__ == "__main__":
    s = load()
    print("FORCE exceptions:", ", ".join(s.force_exceptions()))
    print("definer functions:", ", ".join(sorted(k for k, v in s.functions.items() if v["definer"])))
    print("worker policies not lease-bound:", worker_policies_not_lease_bound(s) or "none")
    for key in sorted(k for k in s.privs if k[0] == "hermes_worker" and k[2] in ("insert", "update")):
        print("worker", key[2], key[1], "table" if s.privs[key] == "table" else sorted(s.privs[key]))
