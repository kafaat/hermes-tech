"""Worker transport contracts; database locking and privileges are tested in db/tests."""
import contextlib
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.worker import Worker


class Db:
    def __init__(self):
        self.row, self.claims, self.before_claim = None, 0, None

    def activate(self, commit, generation):
        if self.row and generation <= self.row[1]:
            raise ValueError('non-monotonic activation')
        self.row = (commit, generation)

    @contextlib.contextmanager
    def tx(self, *args, **kwargs):
        yield Cursor(self)


class Cursor:
    def __init__(self, db):
        self.db, self.result = db, None

    def execute(self, sql, args=()):
        d = self.db
        if 'heartbeat_service_build' in sql or 'select exists' in sql:
            self.result = (d.row == tuple(args),)
        elif 'app.claim_task' in sql:
            if d.before_claim:
                hook, d.before_claim = d.before_claim, None
                hook()
            if d.row == tuple(args[-2:]):
                d.claims += 1
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.result


class TestBuildFence(unittest.TestCase):
    def setUp(self):
        self.db = Db()
        self.old = Worker(self.db, 'old', {}, build='a' * 40, build_generation=1)
        self.new = Worker(self.db, 'new', {}, build='b' * 40, build_generation=2)
        self.db.activate(self.old.build, 1)

    def test_restart_cannot_replace_new_generation(self):
        self.old.claim()
        self.db.activate(self.new.build, 2)
        self.new.register_build()
        self.old.register_build()
        self.old.heartbeat(force=True)
        self.old.claim()
        self.new.claim()
        self.assertEqual(self.db.row, (self.new.build, 2))
        self.assertEqual(self.db.claims, 2)

    def test_activation_after_diagnostic_read_cannot_allow_old_claim(self):
        self.assertFalse(self.old.fenced())
        self.db.before_claim = lambda: self.db.activate(self.new.build, 2)
        self.old.claim()
        self.assertEqual(self.db.claims, 0)

    def test_absent_authority_or_identity_fails_closed(self):
        Worker(self.db, 'unidentified', {}).claim()
        self.db.row = None
        self.old.register_build()
        self.old.claim()
        self.assertEqual(self.db.claims, 0)
        self.assertTrue(self.old.fenced())

    def test_rollback_requires_new_generation_even_for_same_commit(self):
        self.db.activate(self.new.build, 2)
        self.db.activate(self.old.build, 3)
        self.old.claim()
        Worker(self.db, 'rollback', {}, build=self.old.build, build_generation=3).claim()
        self.assertEqual(self.db.claims, 1)


if __name__ == '__main__':
    unittest.main()
