#!/usr/bin/env python3
"""External monitor for /deps: runs OUTSIDE Railway (GitHub Actions, .github/workflows/monitor.yml), because Railway
checks /healthz only at deploy time and never reports a skipped or hung cron run (spec 28.7).

    HERMES_DEPS_URL=https://<app>/deps HERMES_MONITOR_TOKEN=... python ops/monitor/check_deps.py

Exit 0: the service answered "ok". Exit 1: degraded (the failing signal names are printed), unreachable, or an answer
that is not what /deps gives; the run turns red and GitHub notifies the repository owner. Exit 0 with a warning
when the two settings are absent (a fork, or before the owner sets them), so an unconfigured monitor is visible,
not silent and not a daily false alarm. The token is sent in X-Monitor-Token and never printed; numbers only.
"""
from __future__ import annotations
import json, os, sys, urllib.error, urllib.request
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 20
EXPECTED = ("status", "failing", "retention_stale", "overdue_bodies", "outbox_attention", "webhook_backlog")


def check(url: str, token: str, opener=urllib.request.urlopen) -> tuple[int, str]:
    """(exit code, one line for the log)."""
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.path != "/deps" or u.query:
        return 1, "HERMES_DEPS_URL must be https://<host>/deps"
    req = urllib.request.Request(url, headers={"X-Monitor-Token": token, "User-Agent": "hermes-deps-monitor/1.8"})
    try:
        with opener(req, timeout=TIMEOUT_SECONDS) as r:
            code, raw = r.status, r.read(65536)
    except urllib.error.HTTPError as e:                    # 503 carries the body too
        code, raw = e.code, e.read(65536)
    except Exception as exc:                               # noqa: BLE001 - DNS, TLS, timeout: the service is not reachable
        return 1, f"unreachable: {type(exc).__name__}"
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return 1, f"HTTP {code}: not the /deps answer (is the monitor token right?)"
    if not isinstance(body, dict) or any(k not in body for k in EXPECTED):
        return 1, f"HTTP {code}: not the /deps answer (is the monitor token right?)"
    numbers = " ".join(f"{k}={body[k]}" for k in EXPECTED[2:])
    if code == 200 and body["status"] == "ok":
        return 0, f"ok · {numbers} · purge due {body.get('retention_due_at')}"
    failing = ",".join(str(x) for x in body.get("failing") or []) or body["status"]
    return 1, f"DEGRADED ({failing}) · HTTP {code} · {numbers}"


def main(env=os.environ) -> int:
    url, token = env.get("HERMES_DEPS_URL", ""), env.get("HERMES_MONITOR_TOKEN", "")
    if not url or not token:
        print("::warning::the /deps monitor is not configured: set the repository secrets HERMES_DEPS_URL and "
              "HERMES_MONITOR_TOKEN (spec 28.15)")
        return 0
    code, line = check(url, token)
    print(("::error::" if code else "") + line)
    return code


if __name__ == "__main__":
    sys.exit(main())
