"""The audit DuckDB as the store sees it: a read-only attach, the loader's commit (`code`) and a state id over
`truth.releases` (`state`), which together pin what a query counted (README → Audit logs and queries).

`state` is a sha256 over every release's id, container and member hashes and row count, sorted; it stands in for
the bundle id until assembly exists (PLAN-audit-db.md, step 3). DuckDB is an optional dependency: anything here
raises ImportError when it is missing, and callers say so.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import Config


def truth_path(cfg: Config) -> Path:
    """`$NUGGETS_TRUTH` when set (a candidate build such as truth_new.duckdb), else `<audit_db>/truth.duckdb`."""
    env = os.environ.get("NUGGETS_TRUTH")
    return Path(env).expanduser() if env else (cfg.audit_db or Path(".")) / "truth.duckdb"


def connect(cfg: Config, facts_db: Path | None = None, threads: int = 4, memory_limit: str = "4GB"):
    import duckdb  # optional dependency

    truth = truth_path(cfg)
    if not truth.exists():
        raise FileNotFoundError(f"no audit DB at {truth} (nuggets.toml audit_db)")
    con = duckdb.connect(config={"threads": threads, "memory_limit": memory_limit})
    con.execute(f"ATTACH '{truth}' AS truth (READ_ONLY)")
    if facts_db and facts_db.exists():
        con.execute(f"ATTACH '{facts_db}' AS facts (READ_ONLY)")
    return con


def loader_code(con) -> str:
    """`<repo>@<commit>` of the loader that built truth (truth.build_info)."""
    rows = dict(con.execute("SELECT key, value FROM truth.build_info").fetchall())
    url = rows.get("repo_web_url", "")
    repo = url.rstrip("/").rsplit("/", 1)[-1] if url else "sm-alpr"
    return f"{repo}@{rows.get('repo_commit', '?')}" + ("+dirty" if rows.get("repo_inputs_dirty") == "True" else "")


def state_id(con) -> str:
    rows = con.execute(
        "SELECT release_id, coalesce(container_sha256, ''), coalesce(member_sha256, ''), coalesce(n_rows, -1) "
        "FROM truth.releases ORDER BY 1, 2, 3").fetchall()
    h = hashlib.sha256()
    for r in rows:
        h.update(("\t".join(str(x) for x in r) + "\n").encode())
    return "sha256:" + h.hexdigest()
