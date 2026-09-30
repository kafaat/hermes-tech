"""Reference implementation of the orchestrator enforcement layer (v1.5, concurrency-safe).

Every agent invocation passes through `Orchestrator.invoke`, in this order:
  1. agent enabled in registry                 -> AGENT_DISABLED
  2. tool declared in the contract             -> TOOL_NOT_IN_CONTRACT
  3. action is a proposal                      -> queued for a human (APPROVAL_QUEUED)
  4. action allowed and not denied             -> PERMISSION_DENIED
  --- critical section (one lock; production: one transaction with row locks) ---
  5. idempotency: finished key returns the stored result; a key already in flight
     returns IN_PROGRESS without executing (no double execution)
  6. circuit: open -> CIRCUIT_OPEN; after cooldown exactly ONE probe is admitted
  7. budget RESERVATION of the call's upper bound against call, task, agent and
     customer caps (check and reserve are one atomic step, so concurrent calls
     cannot both pass on the same headroom)
  --- end of critical section ---
  8. execute with bounded retry; every retry re-reserves
  9. settle: replace the reservation by the actual cost, store the result,
     update or trip the circuit, release the probe

Idempotency keys are scoped by agent, action and tool plus the contract's key fields,
so two different effects inside one task (text and image) never share a key.

v1.6 (review of 1.5): every claim that can outlive its holder has a LEASE
  - an in-flight key: timeout x attempts + 60 s; after that another worker may take over
  - a half-open probe: timeout + 60 s (< cooldown); after that a new probe is admitted
Failures are classified: retryable kinds release the key; terminal kinds keep it FAILED
until an operator releases it (audited). A second RESERVATION_EXCEEDED for an agent
within 24 h pauses that agent (AGENT_PAUSED) until an operator unpauses it.
Protocol for callers: docs/delivery_protocol.md.
"""
from __future__ import annotations
import json, threading, time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPS = 1e-9


class Denied(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code} {detail}".strip())
        self.code = code


class ToolError(Exception):
    def __init__(self, kind, retryable=True, cost=0.0):
        super().__init__(kind)
        self.kind, self.retryable, self.cost = kind, retryable, cost


def covered(action, patterns):
    dom = action.split(":")[0]
    return any(p == action or (p.endswith(":*") and p.split(":")[0] == dom) for p in patterns)


@dataclass
class AgentState:
    spent: float = 0.0
    reserved: float = 0.0
    calls: int = 0
    consecutive_failures: int = 0
    circuit: str = "closed"
    opened_at: float | None = None
    probe_in_flight: bool = False
    probe_until: float | None = None


@dataclass
class State:
    agents: dict = field(default_factory=dict)
    ai_spent_total: float = 0.0
    ai_reserved: float = 0.0

    def agent(self, aid):
        return self.agents.setdefault(aid, AgentState())


class Orchestrator:
    def __init__(self, root=ROOT, clock=time.time):
        root = Path(root)
        self.registry = json.loads((root / "contracts/registry.json").read_text(encoding="utf-8"))
        self.contracts = {}
        for e in self.registry["agents"]:
            c = json.loads((root / "contracts" / e["contract"]).read_text(encoding="utf-8"))
            self.contracts[c["agent_id"]] = c
        self.enabled = {e["id"]: e["enabled"] for e in self.registry["agents"]}
        self.limits = self.registry["limits"]
        self.states: dict[str, State] = {}
        self.idem: dict[str, dict] = {}
        self.task_spend: dict[tuple, list] = {}       # (agent, task_id) -> [spent, reserved]
        self.approvals: list[dict] = []
        self.calls: list[dict] = []
        self.flags: list[dict] = []
        self.audit: list[dict] = []
        self.paused: dict[str, str] = {}
        self.exceeded_at: dict[str, list] = {}
        self.clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def month_id(self):
        return time.strftime("%Y-%m", time.gmtime(self.clock()))

    def state(self, customer_id, month_id=None):
        """Budget state is per customer AND per month (v1.7): a new month starts from zero."""
        return self.states.setdefault(f"{customer_id or '__acquisition__'}|{month_id or self.month_id()}", State())

    def _log(self, task, agent_id, tool, outcome, cost=0.0, error=None, key=""):
        self.calls.append({"task_id": task["task_id"], "customer_id": task.get("customer_id"), "agent_id": agent_id,
                           "tool": tool, "outcome": outcome, "cost_usd": round(cost, 6), "error_code": error,
                           "idempotency_key": key})

    def idempotency_key(self, contract, task, action, tool):
        fields = "|".join(str(task[f]) for f in contract["runtime"]["idempotency_key_fields"])
        return f"{contract['agent_id']}|{action}|{tool}|{fields}"

    INPUT_MARGIN = 0.10          # tokenizer drift, SDK-added system text and tool definitions

    def call_upper_bound(self, agent_id, input_tokens, tool):
        """Worst-case cost of one call: counted input (+10%) + max output (reasoning included) at list
        price + the tool's declared maximum unit cost."""
        c = self.contracts[agent_id]
        price = self.registry["approved_models"][c["model"]["primary"]]
        unit = next((t.get("unit_cost_usd", 0.0) for t in c["tools"] if t["name"] == tool), 0.0)
        tin = input_tokens * (1 + self.INPUT_MARGIN)
        return (tin * price["input_usd_per_mtok"] + c["model"]["params"]["max_output_tokens"] * price["output_usd_per_mtok"]) / 1e6 + unit

    def leases(self, c):
        t = c["runtime"]["timeout_seconds"]
        return t * max(1, c["retry"]["max_attempts"]) + 60, t + 60      # (key lease, probe lease)

    def unpause(self, agent_id, operator, reason):
        with self._lock:
            self.paused.pop(agent_id, None)
            self.exceeded_at.pop(agent_id, None)
            self.audit.append({"event": "agent.unpaused", "agent_id": agent_id, "by": operator, "reason": reason})

    def release_terminal(self, key, operator, reason):
        """Only path out of a terminal failure: an audited operator decision."""
        with self._lock:
            prev = self.idem.get(key)
            if prev and prev.get("status") == "FAILED":
                self.idem.pop(key)
                self.audit.append({"event": "idempotency.released", "key": key, "by": operator, "reason": reason})

    def _reserve(self, c, task, st, ag, amount, tool, key):
        """Check every cap and reserve in one step. Caller holds the lock."""
        bud, aid = c["budget"], c["agent_id"]
        if amount > bud["per_call_usd"] + EPS:
            raise Denied("BUDGET_EXCEEDED_CALL", f"{amount} > {bud['per_call_usd']}")
        ts = self.task_spend.setdefault((aid, task["task_id"]), [0.0, 0.0])
        if ts[0] + ts[1] + amount > bud["per_task_usd"] + EPS:
            self._log(task, aid, tool, "budget_exceeded", error="BUDGET_EXCEEDED_TASK", key=key)
            raise Denied("BUDGET_EXCEEDED_TASK", f"{task['task_id']} {ts[0] + ts[1]:.4f}+{amount}")
        if ag.spent + ag.reserved + amount > bud["class_cap_usd"] + EPS:
            self._log(task, aid, tool, "budget_exceeded", error="BUDGET_EXCEEDED_AGENT", key=key)
            raise Denied("BUDGET_EXCEEDED_AGENT", f"{aid} {ag.spent + ag.reserved:.4f}+{amount}")
        monthly = bud["budget_class"] == "monthly_active"
        if monthly and st.ai_spent_total + st.ai_reserved + amount > self.limits["ai_cap_per_active_customer_month_usd"] + EPS:
            self._log(task, aid, tool, "budget_exceeded", error="BUDGET_EXCEEDED_CUSTOMER", key=key)
            raise Denied("BUDGET_EXCEEDED_CUSTOMER", f"{st.ai_spent_total + st.ai_reserved:.4f}+{amount}")
        ts[1] += amount
        ag.reserved += amount
        if monthly:
            st.ai_reserved += amount
        return ts

    def _settle(self, c, st, ag, ts, reserved, actual):
        """Replace a reservation by the actual cost. Caller holds the lock."""
        monthly = c["budget"]["budget_class"] == "monthly_active"
        ts[1] -= reserved
        ts[0] += actual
        ag.reserved -= reserved
        ag.spent += actual
        if monthly:
            st.ai_reserved -= reserved
            st.ai_spent_total += actual
        if actual > reserved + EPS:
            # the overshoot IS charged (spent includes it), so every later call sees it: the cap stays hard
            # for admission; the only overshoot possible is one mis-bounded call. A second one within 24 h
            # means the bound itself is wrong (price change, tokenizer, variable tool cost): pause the agent.
            aid, now = c["agent_id"], self.clock()
            self.flags.append({"agent_id": aid, "error_code": "RESERVATION_EXCEEDED", "reserved": reserved, "actual": actual})
            if actual > c["budget"]["per_call_usd"] + EPS:       # v1.7: a call costing more than the contract allows is a breach
                self.paused[aid] = "PER_CALL_BREACH"
                return
            hist = [t for t in self.exceeded_at.get(aid, []) if now - t < 86400] + [now]
            self.exceeded_at[aid] = hist
            if len(hist) >= 2:
                self.paused[aid] = "RESERVATION_EXCEEDED"

    # ------------------------------------------------------------------ main entry
    def invoke(self, agent_id, action, tool, task, execute, estimated_cost, input_tokens=None):
        """The reservation is max(estimated_cost, call_upper_bound(input_tokens)) when input_tokens is given:
        a caller's low estimate can never shrink the reservation below the computed bound (v1.7)."""
        if not self.enabled.get(agent_id, False):
            raise Denied("AGENT_DISABLED", agent_id)
        if agent_id in self.paused:
            raise Denied("AGENT_PAUSED", self.paused[agent_id])
        c = self.contracts[agent_id]
        if tool not in {t["name"] for t in c["tools"]}:
            raise Denied("TOOL_NOT_IN_CONTRACT", tool)
        perm = c["permissions"]
        if action in perm["proposals"]:
            with self._lock:
                self.approvals.append({"agent_id": agent_id, "action": action, "task": task, "decision": "pending"})
                self._log(task, agent_id, tool, "escalated", error="APPROVAL_QUEUED")
            return {"status": "APPROVAL_QUEUED"}
        if not covered(action, perm["allow"]) or covered(action, perm["deny"]):
            with self._lock:
                self._log(task, agent_id, tool, "rejected", error="PERMISSION_DENIED")
            raise Denied("PERMISSION_DENIED", action)

        if input_tokens is not None:
            estimated_cost = max(estimated_cost, self.call_upper_bound(agent_id, input_tokens, tool))
        key = self.idempotency_key(c, task, action, tool)
        st = self.state(task.get("customer_id"))
        key_lease, probe_lease = self.leases(c)
        with self._lock:
            ag = st.agent(agent_id)
            now = self.clock()
            prev = self.idem.get(key)
            if prev is not None:
                if prev["status"] != "IN_PROGRESS":
                    return prev                               # OK result, or terminal FAILED
                if now < prev["lease_until"]:
                    return {"status": "IN_PROGRESS", "retry_after": min(30, max(1, int(prev["lease_until"] - now)))}
                self.flags.append({"agent_id": agent_id, "error_code": "LEASE_TAKEOVER", "key": key})
            probe = False
            if ag.circuit in ("open", "half_open"):
                cooled = ag.opened_at is not None and now - ag.opened_at >= self.limits["circuit_cooldown_seconds"]
                probe_alive = ag.probe_in_flight and ag.probe_until is not None and now < ag.probe_until
                if probe_alive or not cooled:
                    self._log(task, agent_id, tool, "circuit_open", error="CIRCUIT_OPEN", key=key)
                    raise Denied("CIRCUIT_OPEN", agent_id)
                if ag.probe_in_flight:                        # holder died: its lease expired
                    self.flags.append({"agent_id": agent_id, "error_code": "PROBE_LEASE_EXPIRED"})
                ag.circuit, ag.probe_in_flight, ag.probe_until, probe = "half_open", True, now + probe_lease, True
            try:
                ts = self._reserve(c, task, st, ag, estimated_cost, tool, key)
            except Denied:
                if probe:
                    ag.probe_in_flight, ag.probe_until = False, None
                raise
            self.idem[key] = {"status": "IN_PROGRESS", "lease_until": now + key_lease}

        r = c["retry"]
        attempts = 1 if probe else max(1, r["max_attempts"])
        last = None
        for i in range(attempts):
            if i > 0:
                with self._lock:
                    try:
                        ts = self._reserve(c, task, st, ag, estimated_cost, tool, key)
                    except Denied:
                        self.idem.pop(key, None)
                        raise
            try:
                result, cost = execute()
            except ToolError as e:
                last = e
                with self._lock:
                    self._settle(c, st, ag, ts, estimated_cost, e.cost)
                    self._log(task, agent_id, tool, "failure", e.cost, error=e.kind, key=key)
                if not e.retryable or e.kind not in r["retryable_errors"]:
                    break
                continue
            except BaseException:
                with self._lock:                               # unexpected crash: never leave a key or a reservation stuck
                    self._settle(c, st, ag, ts, estimated_cost, 0.0)
                    self.idem.pop(key, None)
                    ag.probe_in_flight = False
                raise
            with self._lock:
                self._settle(c, st, ag, ts, estimated_cost, cost)
                ag.calls += 1
                ag.consecutive_failures, ag.circuit, ag.opened_at, ag.probe_in_flight, ag.probe_until = 0, "closed", None, False, None
                self._log(task, agent_id, tool, "success", cost, key=key)
                out = {"status": "OK", "result": result}
                self.idem[key] = out
            return out

        with self._lock:
            transient = last is not None and last.retryable and last.kind in r["retryable_errors"]
            if transient or last is None:
                self.idem.pop(key, None)                      # transient: the key may be claimed again
            else:                                             # terminal: duplicates see FAILED, never re-run
                self.idem[key] = {"status": "FAILED", "error": last.kind, "terminal": True}
            ag.consecutive_failures += 1
            if probe or ag.consecutive_failures >= self.limits["circuit_failure_threshold"]:
                ag.circuit, ag.opened_at = "open", self.clock()
            ag.probe_in_flight, ag.probe_until = False, None
        raise Denied("EXECUTION_FAILED", last.kind if last else "")
