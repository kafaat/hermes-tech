import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.build_identity import build_identity

class BuildIdentityTests(unittest.TestCase):
    def test_exact_identity(self):
        self.assertEqual(build_identity({'RAILWAY_GIT_COMMIT_SHA': 'A'*40, 'HERMES_BUILD_GENERATION': '2'}), ('a'*40, 2))

    def test_missing_partial_or_invalid_identity_rejected(self):
        for commit, generation in [('', '1'), ('abcd', '1'), ('a'*40, ''), ('a'*40, '0'), ('a'*40, '-1'), ('a'*40, '9223372036854775808')]:
            with self.subTest(commit=commit, generation=generation), self.assertRaises(ValueError):
                build_identity({'RAILWAY_GIT_COMMIT_SHA': commit, 'HERMES_BUILD_GENERATION': generation})

    def test_rollout_cli_refuses_default_and_invalid_identity(self):
        import subprocess
        base = {'RAILWAY_GIT_COMMIT_SHA': 'a'*40, 'HERMES_BUILD_GENERATION': '1'}
        for extra in [{}, {'HERMES_BUILD_AUTHORITY_ROLLOUT':'0'}, {'HERMES_BUILD_AUTHORITY_ROLLOUT':'1','HERMES_BUILD_GENERATION':'0'}]:
            p = subprocess.run([sys.executable, '-m', 'service.build_identity'], env={**base, **extra}, cwd=Path(__file__).resolve().parents[1], capture_output=True)
            self.assertNotEqual(p.returncode, 0)
        p = subprocess.run([sys.executable, '-m', 'service.build_identity'], env={**base,'HERMES_BUILD_AUTHORITY_ROLLOUT':'1'}, cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_migration_script_holds_unapproved_cutover(self):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'psql'
            fake.write_text('#!' + sys.executable + '\n' + '''import os,sys
from pathlib import Path
args=sys.argv[1:]
if args == ['-qAt']:
    for line in sys.stdin: pass
elif '-f' in args:
    with open(os.environ['TEST_CALLS'],'a') as f: f.write(args[args.index('-f')+1]+'\\n')
elif '-tAc' in args:
    sql=args[args.index('-tAc')+1]
    print(0 if '0031_atomic_build_authority.sql' in sql and os.environ['TEST_PENDING']=='1' else 1)
''')
            fake.chmod(0o700)
            calls = root / 'calls'
            base = {**os.environ, 'PATH': str(root)+os.pathsep+os.environ['PATH'], 'DATABASE_URL': 'synthetic-only',
                    'TEST_CALLS': str(calls), 'MIGRATE_ONLY': '1', 'RAILWAY_GIT_COMMIT_SHA':'a'*40,
                    'HERMES_BUILD_GENERATION':'1', 'HERMES_BUILD_AUTHORITY_ROLLOUT':'0'}
            for pending, approved, expected in [('1','0',False),('1','1',True),('0','0',True)]:
                calls.write_text('')
                p = subprocess.run(['bash','ops/railway/migrate.sh'], env={**base,'TEST_PENDING':pending,'HERMES_BUILD_AUTHORITY_ROLLOUT':approved},
                                   cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
                self.assertEqual(p.returncode == 0, expected, p.stderr)
                self.assertEqual('db/migrations/0031_atomic_build_authority.sql' in calls.read_text(), pending=='1' and approved=='1')
