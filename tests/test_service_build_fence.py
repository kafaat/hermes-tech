"""The build fence (spec 28.29): while a newer build is live and seen, a worker of the previous build claims nothing; if
the newer build stops beating for 60 s, the previous one carries on, so a crashed deploy never stalls the queue."""
import contextlib, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.worker import BUILD_STALE_SECONDS, Worker  # noqa: E402


class Db:
    """app.service_build and app.claim_task, as far as the fence uses them; `now` is moved by the test."""

    def __init__(self):
        self.now, self.row, self.claims = 0.0, None, 0

    @contextlib.contextmanager
    def tx(self, *a, **k):
        yield Cursor(self)


class Cursor:
    def __init__(self, db):
        self.db, self.result = db, None

    def execute(self, sql, args=()):
        d = self.db
        if sql.startswith("insert into app.service_build"):
            d.row = {"commit": args[0], "seen": d.now}
        elif sql.startswith("update app.service_build"):
            if d.row and d.row["commit"] == args[0]:
                d.row["seen"] = d.now
        elif sql.startswith("select commit <> %s"):
            self.result = None if d.row is None else (d.row["commit"] != args[0] and d.now - d.row["seen"] < args[1],)
        elif "app.claim_task" in sql:
            d.claims += 1
            self.result = None
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result


class TestBuildFence(unittest.TestCase):
    def test_the_previous_build_stops_claiming_while_the_new_one_is_seen(self):
        db = Db()
        old, new = Worker(db, "old", {}, build="aaaa"), Worker(db, "new", {}, build="bbbb")
        old.register_build()
        old.claim()
        self.assertEqual(db.claims, 1)
        new.register_build()                                     # the deploy: the new build is live
        old.claim()
        self.assertEqual(db.claims, 1)                           # the old worker took nothing
        new.claim()
        self.assertEqual(db.claims, 2)
        old.heartbeat(force=True)                                # the old build's beat never takes the fence back
        self.assertEqual(db.row["commit"], "bbbb")

    def test_a_new_build_that_stops_beating_no_longer_fences(self):
        db = Db()
        old, new = Worker(db, "old", {}, build="aaaa"), Worker(db, "new", {}, build="bbbb")
        new.register_build()
        db.now += BUILD_STALE_SECONDS - 1
        old.claim()
        self.assertEqual(db.claims, 0)
        new.heartbeat(force=True)
        db.now += BUILD_STALE_SECONDS - 1
        old.claim()
        self.assertEqual(db.claims, 0)                           # still beating: still fenced
        db.now += 2                                              # the new build crashed: no beat for 60 s
        old.claim()
        self.assertEqual(db.claims, 1)

    def test_no_build_no_fence(self):
        db = Db()
        Worker(db, "w", {}).claim()                              # tests and tools: no build, no fence
        self.assertEqual(db.claims, 1)
        Worker(db, "w", {}, build="aaaa").claim()                # no live build recorded yet
        self.assertEqual(db.claims, 2)


if __name__ == "__main__":
    unittest.main()
