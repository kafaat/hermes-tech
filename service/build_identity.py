"""Dependency-free build identity and explicit deployment preflight."""
import os
import re
import sys

def build_identity(env):
    commit = env.get("RAILWAY_GIT_COMMIT_SHA", "")
    generation = env.get("HERMES_BUILD_GENERATION", "")
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
        require_rollout(os.environ)
    except ValueError as exc:
        sys.exit(str(exc))
