"""Dependency-free build identity and explicit deployment preflight."""
import os
import re
import sys

def staging_auto(env) -> bool:
    """Staging only (spec 28.38): the deployment's own pre-deploy step (ops/railway/migrate.sh) activates each new build
    with the next generation, and the process reads its generation back from app.service_build. Refused anywhere the
    Graph API is live: production activates by hand, as README describes."""
    mode = env.get("HERMES_BUILD_AUTO_ACTIVATE", "")
    if not mode:
        return False
    if mode != "staging" or env.get("HERMES_GRAPH") != "simulate":
        raise ValueError("HERMES_BUILD_AUTO_ACTIVATE=staging is for staging only (HERMES_GRAPH=simulate)")
    return True


def build_identity(env):
    """(commit, generation); generation None means 'auto': the active generation is read from the database."""
    commit = env.get("RAILWAY_GIT_COMMIT_SHA", "")
    generation = env.get("HERMES_BUILD_GENERATION", "")
    if generation == "auto":
        if not staging_auto(env):
            raise ValueError("HERMES_BUILD_GENERATION=auto needs HERMES_BUILD_AUTO_ACTIVATE=staging")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ValueError("Full commit and positive HERMES_BUILD_GENERATION are required")
        return commit.lower(), None
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit) or not re.fullmatch(r"[1-9][0-9]{0,18}", generation):
        raise ValueError("Full commit and positive HERMES_BUILD_GENERATION are required")
    value = int(generation)
    if value > 9223372036854775807:
        raise ValueError("HERMES_BUILD_GENERATION exceeds PostgreSQL bigint")
    return commit.lower(), value


def require_rollout(env):
    if env.get("HERMES_BUILD_AUTHORITY_ROLLOUT") != "1":
        raise ValueError("0031 held: explicit HERMES_BUILD_AUTHORITY_ROLLOUT=1 required for coordinated cutover")
    return build_identity(env)

if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--staging-auto"]:          # migrate.sh, pre-deploy: the commit to activate, or a refusal
            if not staging_auto(os.environ):
                raise ValueError("HERMES_BUILD_AUTO_ACTIVATE is not set")
            print(build_identity({**os.environ, "HERMES_BUILD_GENERATION": "auto"})[0])
        else:
            require_rollout(os.environ)
    except ValueError as exc:
        sys.exit(str(exc))
