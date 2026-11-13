#!/usr/bin/env python3
"""One writer per file across open pull requests (CC-SINGLE-WRITER, CC-NO-AGE-RELEASE).

An open pull request reserves the files it changes. This check fails a pull request while another
open pull request in the same repository changes any of the same files.

- CC-SINGLE-WRITER: two open pull requests never write the same file. The one exception is a
  stacked pair, where one pull request's base branch is the other's head branch: its writes land
  on top of the other's, one after the other, not side by side.
- CC-NO-AGE-RELEASE: nothing about the other pull request's age, draft state or last activity
  releases its files. The decision never reads those fields. Only the other pull request's
  closure or merge, read from GitHub now, releases them.
- UNKNOWN is not PASS: if GitHub cannot be read completely (an error, or a file list shorter
  than the pull request's own count), the check exits 2 and fails.

Scope: files in open pull requests of one repository. Environments, databases, local worktrees
and other repositories are outside it. It grants nothing: no label or comment turns it green.

Exit codes: 0 clear, 1 conflict, 2 unknown.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class Unknown(Exception):
    """GitHub could not be read completely; the answer is UNKNOWN, never PASS."""


def summarize(pr: dict, files: list[dict]) -> dict:
    """What the decision may use: identity, branches and paths. Age, draft and activity are left out."""
    if "changed_files" not in pr:
        raise Unknown(f"#{pr['number']}: GitHub gave no changed-file count to check the list against")
    if len(files) != pr["changed_files"]:
        raise Unknown(f"#{pr['number']}: read {len(files)} of {pr['changed_files']} changed files")
    paths = set()
    for f in files:
        paths.add(f["filename"])
        if f.get("previous_filename"):
            paths.add(f["previous_filename"])
    return {"number": pr["number"], "head": pr["head"]["label"], "base": pr["base"]["label"], "paths": paths}


def stacked(a: dict, b: dict) -> bool:
    return a["base"] == b["head"] or b["base"] == a["head"]


def conflicts(current: dict, others: list[dict]) -> list[dict]:
    found = []
    for other in others:
        if other["number"] == current["number"] or stacked(current, other):
            continue
        shared = sorted(current["paths"] & other["paths"])
        if shared:
            found.append({"number": other["number"], "paths": shared})
    return found


class GitHub:
    def __init__(self, repo: str, token: str):
        self.repo, self.token = repo, token

    def get(self, url: str) -> tuple[object, str | None]:
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kafaat-single-writer-guard",
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
                match = NEXT.search(response.headers.get("Link") or "")
        # OSError covers URLError, timeouts and a connection reset while the body is read;
        # HTTPException covers a truncated body (IncompleteRead). Either way the answer is UNKNOWN.
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise Unknown(f"GET {url}: {error}") from error
        return body, match.group(1) if match else None

    def one(self, path: str) -> dict:
        body, _ = self.get(f"{API}/repos/{self.repo}/{path}")
        if not isinstance(body, dict):
            raise Unknown(f"{path}: expected an object")
        return body

    def all(self, path: str) -> list[dict]:
        url, items = f"{API}/repos/{self.repo}/{path}", []
        while url:
            body, url = self.get(url)
            if not isinstance(body, list):
                raise Unknown(f"{path}: expected a list")
            items += body
        return items


def check(client: GitHub, number: int) -> dict:
    pr = client.one(f"pulls/{number}")
    if pr.get("state") != "open":
        return {"status": "CLEAR", "pr": number, "note": f"#{number} is {pr.get('state')}", "conflicts": []}
    current = summarize(pr, client.all(f"pulls/{number}/files?per_page=100"))
    open_prs = client.all("pulls?state=open&per_page=100")
    others, opened = [], {}
    for other in open_prs:
        if other["number"] == number:
            continue
        full = client.one(f"pulls/{other['number']}")  # The list omits changed_files; this has it.
        others.append(summarize(full, client.all(f"pulls/{other['number']}/files?per_page=100")))
        opened[other["number"]] = (other.get("title", ""), other.get("created_at", ""), other.get("draft", False))
    found = conflicts(current, others)
    for item in found:  # Reported for the reader only; the decision above never saw these fields.
        item["title"], item["created_at"], item["draft"] = opened[item["number"]]
    return {"status": "CONFLICT" if found else "CLEAR", "pr": number, "conflicts": found,
            "open_pull_requests_read": len(open_prs)}


def summary_markdown(report: dict) -> str:
    lines = [f"### single-writer: **{report['status']}**", ""]
    if report.get("note"):
        lines.append(report["note"])
    now = datetime.now(timezone.utc)
    for item in report.get("conflicts", []):
        age = ""
        if item.get("created_at"):
            days = (now - datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))).days
            age = f", opened {days} days ago"
        draft = ", draft" if item.get("draft") else ""
        lines.append(f"- #{item['number']} {item.get('title', '')}{draft}{age}: "
                     f"{len(item['paths'])} shared file(s)")
        lines += [f"  - `{path}`" for path in item["paths"][:50]]
    if report.get("conflicts"):
        lines += ["", "Age, draft state and inactivity never release a file (CC-NO-AGE-RELEASE). "
                  "Wait for the other pull request to merge or close, or coordinate through it, then re-run this job."]
    if report["status"] == "UNKNOWN":
        lines.append(f"GitHub could not be read completely, so this is not a pass: {report.get('error')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    try:
        if not token:
            raise Unknown("GH_TOKEN is not set")
        report = check(GitHub(args.repo, token), args.pr)
    except (Unknown, KeyError, TypeError) as error:  # A response of an unexpected shape is UNKNOWN too.
        report = {"status": "UNKNOWN", "pr": args.pr, "error": str(error), "conflicts": []}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as out:
            out.write(summary_markdown(report))
    return {"CLEAR": 0, "CONFLICT": 1}.get(report["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
