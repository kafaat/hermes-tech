"""CC-SINGLE-WRITER and CC-NO-AGE-RELEASE (spec 28.48): one writer per file across open pull requests. The guard runs
against a fake GitHub here (no network); in CI the BASE copy runs against the real one on every pull request."""
import contextlib, hashlib, http.client, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github/scripts"))
import single_writer_guard as guard  # noqa: E402

# The copy pinned in kafaat/- engineering-skills/skills.lock.json (both critical rules of collaboration-coordination).
# A change here is a change to the rule: update the lock there first, then this digest.
PINNED_SHA256 = "57731d5b8a964414352681c76ced900f03db92ee62d7fa32384a803b8793339c"
REPO = "kafaat/hermes-tech"
BASE = f"{guard.API}/repos/{REPO}"


def pr(number, files, *, head=None, base="kafaat:main", created_at="2026-11-12T00:00:00Z", draft=False,
       state="open", renamed=None):
    file_list = [{"filename": f} for f in files]
    file_list += [{"filename": new, "previous_filename": old, "status": "renamed"} for old, new in (renamed or {}).items()]
    body = {"number": number, "state": state, "title": f"PR {number}", "draft": draft, "created_at": created_at,
            "updated_at": created_at, "head": {"label": head or f"kafaat:branch-{number}"}, "base": {"label": base},
            "changed_files": len(file_list)}
    return body, file_list


class FakeGitHub(guard.GitHub):
    def __init__(self, pages):
        super().__init__(REPO, "token")
        self.pages = pages

    def get(self, url):
        if url not in self.pages:
            raise guard.Unknown(f"GET {url}: 404")
        return self.pages[url]


def site(*prs, page_size=None, list_page_size=None):
    """page_size splits a file list over two pages; list_page_size splits the open pull request list the same way."""
    pages = {}
    for body, files in prs:
        n = body["number"]
        pages[f"{BASE}/pulls/{n}"] = (body, None)
        url = f"{BASE}/pulls/{n}/files?per_page=100"
        if page_size and len(files) > page_size:
            pages[url], pages[f"{url}&page=2"] = (files[:page_size], f"{url}&page=2"), (files[page_size:], None)
        else:
            pages[url] = (files, None)
    listing = [{k: v for k, v in body.items() if k != "changed_files"} for body, _ in prs if body["state"] == "open"]
    url = f"{BASE}/pulls?state=open&per_page=100"
    if list_page_size and len(listing) > list_page_size:
        pages[url], pages[f"{url}&page=2"] = (listing[:list_page_size], f"{url}&page=2"), (listing[list_page_size:], None)
    else:
        pages[url] = (listing, None)
    return pages


def run(pages, number):
    out = io.StringIO()
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"} | {"GH_TOKEN": "token"}
    with mock.patch.object(guard, "GitHub", lambda repo, token: FakeGitHub(pages)), \
            mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
        code = guard.main(["--repo", REPO, "--pr", str(number)])
    return code, json.loads(out.getvalue())


class TestSingleWriter(unittest.TestCase):
    def test_a_shared_file_in_another_open_pull_request_fails(self):
        code, report = run(site(pr(1, ["MANIFEST.json", "a.py"]), pr(2, ["MANIFEST.json", "b.py"])), 1)
        self.assertEqual((code, report["status"], report["conflicts"][0]["paths"]), (1, "CONFLICT", ["MANIFEST.json"]))
        self.assertEqual(run(site(pr(1, ["a.py"]), pr(2, ["b.py"])), 1)[0], 0)                   # disjoint: clear
        self.assertEqual(run(site(pr(1, ["old.py"]), pr(2, [], renamed={"old.py": "new.py"})), 1)[0], 1)  # a rename's old path
        many = [f"f{i}.py" for i in range(5)]
        self.assertEqual(run(site(pr(1, ["f4.py"]), pr(2, many), page_size=3), 1)[0], 1)         # the second page is read
        code, report = run(site(pr(1, ["s.py"]), pr(2, ["o.py"]), pr(3, ["s.py"]), list_page_size=2), 1)
        self.assertEqual((code, [c["number"] for c in report["conflicts"]]), (1, [3]))          # and of the open list
        self.assertEqual(run(site(pr(1, ["a.py"], state="closed"), pr(2, ["a.py"])), 1)[0], 0)  # a closed PR guards nothing

    def test_a_stacked_pair_is_not_a_conflict(self):
        lower, upper = pr(1, ["a.py"], head="kafaat:lower"), pr(2, ["a.py"], head="kafaat:upper", base="kafaat:lower")
        self.assertEqual((run(site(lower, upper), 1)[0], run(site(lower, upper), 2)[0]), (0, 0))
        same_base = site(pr(1, ["a.py"], base="kafaat:feature"), pr(2, ["a.py"], base="kafaat:feature"))
        self.assertEqual(run(same_base, 1)[0], 1)                                                 # siblings are not stacked

    def test_age_draft_and_inactivity_never_release_a_file(self):
        codes = {run(site(pr(1, ["a.py"]), pr(2, ["a.py"], created_at=when, draft=draft)), 1)[0]
                 for when in ("2019-05-05T00:00:00Z", "2026-11-12T00:00:00Z") for draft in (True, False)}
        self.assertEqual(codes, {1})
        body, files = pr(2, ["a.py"], created_at="2019-05-05T00:00:00Z", draft=True)
        self.assertEqual(set(guard.summarize(body, files)), {"number", "head", "base", "paths"})  # the decision never sees them

    def test_an_incomplete_or_unreadable_answer_is_unknown(self):
        body, files = pr(2, ["a.py", "b.py"])
        body["changed_files"] = 3001                                                              # a list shorter than its count
        code, report = run(site(pr(1, ["z.py"]), (body, files)), 1)
        self.assertEqual((code, report["status"]), (2, "UNKNOWN"))
        gone = site(pr(1, ["a.py"]), pr(2, ["a.py"]))
        del gone[f"{BASE}/pulls/2/files?per_page=100"]
        self.assertEqual(run(gone, 1)[0], 2)                                                      # unreadable
        malformed = site(pr(1, ["a.py"]), pr(2, ["b.py"]))
        malformed[f"{BASE}/pulls/2"] = ({"number": 2, "changed_files": 1}, None)
        self.assertEqual(run(malformed, 1)[0], 2)                                                 # no head or base
        paged = site(pr(1, ["a.py"]), pr(2, ["b.py"]), pr(3, ["a.py"]), list_page_size=2)
        del paged[f"{BASE}/pulls?state=open&per_page=100&page=2"]
        self.assertEqual(run(paged, 1)[0], 2)                                                     # open list, page 2 unreadable
        paged = site(pr(1, ["a.py"]), pr(2, ["a.py"]))
        paged[f"{BASE}/pulls?state=open&per_page=100"] = ({"message": "rate limited"}, None)
        self.assertEqual(run(paged, 1)[0], 2)                                                     # open list not a list
        for error in (ConnectionResetError("reset"), http.client.IncompleteRead(b"{")):        # dropped mid-read
            with self.subTest(error=type(error).__name__), mock.patch.object(guard.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(guard.Unknown):
                    guard.GitHub(REPO, "token").get(f"{BASE}/pulls/1")
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_STEP_SUMMARY")}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            self.assertEqual(guard.main(["--repo", REPO, "--pr", "1"]), 2)                     # no token

    def test_the_guard_is_the_copy_pinned_in_kafaat(self):
        digest = hashlib.sha256((ROOT / ".github/scripts/single_writer_guard.py").read_bytes()).hexdigest()
        self.assertEqual(digest, PINNED_SHA256)

    def test_ci_runs_the_base_single_writer_guard_on_pull_requests(self):
        job = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))["jobs"]["workflow-guard"]
        steps = [s for s in job["steps"] if "single_writer_guard.py" in s.get("run", "")]
        self.assertEqual(len(steps), 1, "workflow-guard runs the single-writer guard in a step of its own")
        run_ = steps[0]["run"]
        self.assertIn('git show "$base:.github/scripts/single_writer_guard.py"', run_)          # the program comes from the base
        self.assertIn('--pr "$NUMBER"', run_)
        self.assertIn("exit 1", run_)                                                             # no base guard is not a pass
        self.assertEqual(steps[0].get("if"), "github.event_name == 'pull_request'")
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "read"})
        gate = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))["jobs"]["gate"]
        self.assertIn("workflow-guard", gate["needs"])                                            # and gate waits for it


    def test_the_step_runs_the_base_copy_and_fails_without_one(self):
        """The workflow step itself, run in a scratch repository: the guard that runs is the base commit's, never the
        pull request's own copy, its exit code is the step's, and a base without a guard fails (28.48)."""
        job = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))["jobs"]["workflow-guard"]
        run_ = next(s["run"] for s in job["steps"] if "single_writer_guard.py" in s.get("run", ""))
        with tempfile.TemporaryDirectory() as d:
            repo, runner = Path(d) / "repo", Path(d) / "runner"
            repo.mkdir(), runner.mkdir()
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "GIT_CONFIG_NOSYSTEM": "1",
                   "RUNNER_TEMP": str(runner), "REPO": REPO, "NUMBER": "7"}
            guard_file = repo / ".github/scripts/single_writer_guard.py"

            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                       "-c", "commit.gpgsign=false", *args], cwd=repo, env=env, check=True,
                                      capture_output=True, text=True).stdout.strip()

            def commit(guard_source=None):
                if guard_source is not None:
                    guard_file.parent.mkdir(parents=True, exist_ok=True)
                    guard_file.write_text(guard_source, encoding="utf-8")
                (repo / "n").write_text(str(len(list(repo.iterdir()))), encoding="utf-8")
                git("add", "-A")
                git("commit", "-q", "-m", "c")
                return git("rev-parse", "HEAD")

            def step(base):
                script = run_.replace("${{ steps.base.outputs.base }}", base)
                return subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env, capture_output=True, text=True)

            git("init", "-q")
            no_guard = commit()
            passing = commit('print("BASE COPY")\n')
            failing = commit('print("BASE COPY")\nraise SystemExit(1)\n')
            guard_file.write_text('print("HEAD COPY")\n', encoding="utf-8")       # the pull request's own, weakened copy

            r = step(passing)
            self.assertEqual((r.returncode, "BASE COPY" in r.stdout, "HEAD COPY" in r.stdout), (0, True, False))
            self.assertEqual(step(failing).returncode, 1)                           # the base guard's failure is the step's
            for base in (no_guard, "", "0" * 40):                                   # no guard at base, no base, not a commit
                with self.subTest(base=base[:7] or "empty"):
                    r = step(base)
                    self.assertEqual(r.returncode, 1)
                    self.assertIn("unknown, not a pass", r.stdout)


if __name__ == "__main__":
    unittest.main()
