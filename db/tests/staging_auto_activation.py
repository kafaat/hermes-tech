#!/usr/bin/env python3
"""Staging auto-activation (spec 28.38) on a real Postgres: ops/railway/migrate.sh as hermes-app's pre-deploy step
activates each new build with the next generation, and the process reads exactly that generation back.

    DATABASE_URL=... python db/tests/staging_auto_activation.py      (a scratch database is created beside it)

  1. live Graph (production): the pre-deploy step refuses auto-activation and activates nothing
  2. staging, first build A: migrations applied, A active at generation 1; A's process reads 1
  3. staging, next build B: B active at generation 2; B reads 2, A (the previous build) is refused at start
  4. a redeploy of B: generation 3, so the container it replaces holds no authority any more
"""
from __future__ import annotations
import os, subprocess, sys, urllib.parse, uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from service.app import active_generation  # noqa: E402
from service.pg import Database  # noqa: E402

failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def predeploy(url, commit, graph="simulate"):
    env = {**os.environ, "DATABASE_URL": url, "MIGRATE_ONLY": "1", "RAILWAY_GIT_COMMIT_SHA": commit, "HERMES_GRAPH": graph,
           "HERMES_BUILD_AUTO_ACTIVATE": "staging", "HERMES_BUILD_GENERATION": "auto", "HERMES_BUILD_AUTHORITY_ROLLOUT": "1"}
    return subprocess.run(["bash", "ops/railway/migrate.sh"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def main():
    base = os.environ["DATABASE_URL"]
    name = f"hermes_auto_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')
    url = urllib.parse.urlunsplit(urllib.parse.urlsplit(base)._replace(path="/" + name))

    def active():
        with psycopg.connect(url) as conn:
            return conn.execute("select commit, generation from app.service_build where id = 1").fetchone()
    a, b = "a1" * 20, "b2" * 20
    try:
        p = predeploy(url, a, graph="live")
        check(p.returncode != 0 and "staging only" in (p.stdout + p.stderr), "live Graph: auto-activation refused")
        p = predeploy(url, a)
        check(p.returncode == 0 and active() == (a, 1), f"first staging build activated at generation 1 {active()}")
        db = Database(url, "hermes_worker")
        check(active_generation(db, a) == 1, "its process reads generation 1 back")
        p = predeploy(url, b)
        check(p.returncode == 0 and active() == (b, 2) and "generation 2" in p.stdout, f"the next build takes generation 2 {active()}")
        check(active_generation(db, b) == 2, "the new build's process reads 2")
        try:
            active_generation(db, a)
            check(False, "the previous build is refused at start")
        except RuntimeError:
            check(True, "the previous build is refused at start")
        p = predeploy(url, b)
        check(p.returncode == 0 and active() == (b, 3), f"a redeploy of the same commit takes a new generation {active()}")
    finally:
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(f'drop database if exists "{name}" with (force)')
    if failures:
        print(f"staging auto-activation: {len(failures)} FAILED")
        sys.exit(1)
    print("staging auto-activation: all PASS")


if __name__ == "__main__":
    main()
