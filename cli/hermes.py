"""hermes: the operator's command line (spec 14, cli/hermes_cli_spec.md).

    python -m cli.hermes login --url https://<hermes-app> --email you@example.com     # Supabase code, then TOTP (aal2)
    python -m cli.hermes login --url https://<hermes-app> --staging                   # staging: HERMES_STAGING_LOGIN_CODE
    python -m cli.hermes whoami
    python -m cli.hermes customers add --name "..." --sector restaurant --city Sanaa --zone zone_a --reason "pilot 1"
    python -m cli.hermes --json outbox attention

It holds no database credential: every command is a POST to hermes-app's operator API (service/ops_api.py) with
the operator's own session token, and the server runs it in that operator's database session. The commands and
their arguments come from service/ops_api.py COMMANDS, the one list the server checks.

The session lives in $HERMES_SESSION_FILE or ~/.config/hermes/session.json, readable by this user alone (0600).
A Supabase session is renewed with its refresh token when the access token is within a minute of expiry
(HERMES_SUPABASE_URL and HERMES_SUPABASE_ANON_KEY, the project's public values, are needed for that and for login).

Exit codes (spec 14): 0 done, 2 invalid input, 3 refused (no session, not an operator, not this role, not aal2),
4 not found, 5 the state refuses it, 10 server or network.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from service.ops_api import COMMANDS, _bool, _customers
from service.supabase_auth import AuthFailed, SupabaseAuth, project_host

EXIT = {200: 0, 400: 2, 413: 2, 401: 3, 403: 3, 404: 4, 409: 5}
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
TOP = {"whoami", "calls", "cost", "freeze", "unfreeze"}       # one word on the command line; the rest are "group action"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the session token (Authorization) to wherever it points: answered as an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


class CliError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def session_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_FILE") or Path.home() / ".config" / "hermes" / "session.json")


def save_session(data: dict) -> None:
    p = session_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)                                  # an older file made with a wider mode is narrowed too


def load_session() -> dict:
    p = session_path()
    try:
        if p.stat().st_mode & 0o077:
            raise CliError(f"{p} is readable by others; run `hermes logout` and sign in again", 3)
        return json.loads(p.read_text())
    except FileNotFoundError:
        raise CliError("no session: run `hermes login` first", 3) from None
    except ValueError:
        raise CliError(f"{p} is not a session file", 3) from None


def check_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return f"https://{u.netloc}"
    if u.scheme == "http" and u.hostname in LOOPBACK:   # a local run of service.app (tests, development)
        return f"http://{u.netloc}"
    raise CliError("--url must be https://<host> (http only for localhost)", 2)


def post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": "server"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise CliError(f"cannot reach {base}: {type(e).__name__}", 10) from None


class _Api:
    """service/supabase_auth.py's port, for this command line: the Supabase project host and nothing else."""

    def __init__(self, host: str):
        self.host = host

    def request(self, method, url, body, headers):
        if urlsplit(url).hostname != self.host or not url.startswith("https://"):
            raise ValueError("host")
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                     headers={**headers, "Content-Type": "application/json"})
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}


def supabase(env=os.environ) -> SupabaseAuth:
    url, key = env.get("HERMES_SUPABASE_URL", ""), env.get("HERMES_SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise CliError("HERMES_SUPABASE_URL and HERMES_SUPABASE_ANON_KEY are needed to sign in", 2)
    return SupabaseAuth(url, key, _Api(project_host(url)))


def _expiry(token: str) -> float:
    try:
        body = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("exp", 0))
    except (IndexError, ValueError):
        return 0.0


def token_for(sess: dict, now=time.time) -> str:
    """The access token, renewed first when it is within a minute of expiry and a refresh token exists."""
    if _expiry(sess["access_token"]) - now() > 60 or not sess.get("refresh_token"):
        return sess["access_token"]
    try:
        new = supabase().refresh(sess["refresh_token"])
    except AuthFailed as e:
        raise CliError(f"session renewal failed ({e.code}): run `hermes login` again", 3) from None
    sess.update(access_token=new.access_token, refresh_token=new.refresh_token)
    save_session(sess)
    return new.access_token


# ------------------------------------------------------------------ login / logout
def login(args) -> int:
    base = check_url(args.url)
    if args.staging:
        status, doc = post(base, "/ops/api/session/staging", {"code": getpass.getpass("staging login code: ")})
        if status != 200:
            raise CliError(f"staging login refused ({doc.get('error', status)})", EXIT.get(status, 10))
        save_session({"url": base, "access_token": doc["access_token"]})
        print("signed in (staging operator)")
        return 0
    if not args.email:
        raise CliError("--email or --staging", 2)
    sb = supabase()
    try:
        sb.send_code(args.email)
        first = sb.verify_code(args.email, input("the 6-digit code sent by email: ").strip())
        factor = sb.mfa_factor(first.access_token)
        if factor is None or not factor["verified"]:
            raise CliError("no verified authenticator: enrol one once in the portal (/portal/mfa)", 3)
        final = sb.mfa_verify(first.access_token, factor["id"], getpass.getpass("the 6-digit authenticator code: ").strip())
    except AuthFailed as e:
        raise CliError(f"sign-in failed ({e.code})", 3) from None
    save_session({"url": base, "access_token": final.access_token, "refresh_token": final.refresh_token})
    print("signed in (aal2)")
    return 0


def logout(args) -> int:
    p = session_path()
    try:
        sess = json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        sess = None
    if sess and sess.get("refresh_token"):
        try:
            supabase().sign_out(sess["access_token"])
        except CliError:
            pass                                        # the local session goes either way
    p.unlink(missing_ok=True)
    print("signed out")
    return 0


# ------------------------------------------------------------------ the commands, from the server's own list
def _kind(check) -> str:
    if check is _bool:
        return "flag"
    if check is _customers:
        return "list"
    if getattr(check, "__qualname__", "").startswith("_int."):
        return "int"
    return "str"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes", description="Hermes operator command line (spec 14)")
    p.add_argument("--json", action="store_true", help="print the server's answer as JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="sign in: Supabase code + authenticator (aal2), or the staging code")
    lg.add_argument("--url", required=True, help="https://<hermes-app>")
    who = lg.add_mutually_exclusive_group(required=True)
    who.add_argument("--email")
    who.add_argument("--staging", action="store_true")
    sub.add_parser("logout", help="forget the local session")
    groups = {}
    for name, cmd in COMMANDS.items():
        if name in TOP:
            sp = sub.add_parser(name)
        else:
            group, action = name.split(".", 1)
            if group not in groups:
                groups[group] = sub.add_parser(group).add_subparsers(dest="action", required=True)
            sp = groups[group].add_parser(action.replace("_", "-"))
        sp.set_defaults(command=name)
        for arg, (check, required) in cmd.args.items():
            flag, kind = "--" + arg.replace("_", "-"), _kind(check)
            if kind == "flag":
                sp.add_argument(flag, dest=arg, action="store_true", default=None)
            elif kind == "int":
                sp.add_argument(flag, dest=arg, type=int, required=required)
            elif kind == "list":
                sp.add_argument(flag, dest=arg, type=lambda s: [x for x in s.split(",") if x], required=required,
                                help="comma-separated")
            else:
                sp.add_argument(flag, dest=arg, required=required)
        if cmd.write:
            sp.add_argument("--reason", required=cmd.reason_required,
                            help="why (5+ characters); required unless an incident is open")
        if name == "logs.export":
            sp.add_argument("--to", required=True, help="the file the evidence is written to (0600)")
    return p


def show(data, as_json: bool, out=None) -> None:
    out = out or sys.stdout
    if as_json:
        json.dump(data, out, ensure_ascii=False, indent=2, default=str)
        out.write("\n")
        return
    for key, value in (data or {}).items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            cols = list(value[0])
            out.write(f"{key}:\n" if key != "rows" else "")
            out.write("\t".join(cols) + "\n")
            for row in value:
                out.write("\t".join("" if row.get(c) is None else str(row[c]) for c in cols) + "\n")
        elif isinstance(value, list):
            out.write(f"{key}: {len(value)} rows\n" if not value else f"{key}: {', '.join(map(str, value))}\n")
        else:
            out.write(f"{key}: {value}\n")


def run(args) -> int:
    name = args.command
    sess = load_session()
    cmd = COMMANDS[name]
    body = {k: getattr(args, k) for k in [*cmd.args, *(["reason"] if cmd.write else [])] if getattr(args, k, None) is not None}
    status, doc = post(sess["url"], "/ops/api/" + name, {"args": body}, token_for(sess))
    if status != 200:
        where = f" ({doc['field']})" if doc.get("field") else ""
        raise CliError(f"{name}: {doc.get('error', 'server')}{where}", EXIT.get(status, 10))
    data = doc.get("data")
    if name == "logs.export":
        fd = os.open(args.to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)    # evidence never overwrites a file
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        data = {"written": args.to, "audit_rows": len(data["audit"]), "agent_calls": len(data["agent_calls"])}
    show(data, args.json)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "login":
            return login(args)
        if args.cmd == "logout":
            return logout(args)
        return run(args)
    except CliError as e:
        print(f"hermes: {e}", file=sys.stderr)
        return e.code
    except FileExistsError as e:
        print(f"hermes: {e.filename} exists; evidence is never overwritten", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
