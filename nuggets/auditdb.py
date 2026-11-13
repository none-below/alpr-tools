"""The audit DuckDB as the store sees it: a read-only attach of truth (and of derived, the views and linking cache
built beside it, when present), the loader's commit (`code`) and a state id over `truth.releases` (`state`), which
together pin what a query counted (README → Audit logs and queries).

`state` is a sha256 over every release's id, container and member hashes and row count, sorted; it stands in for
the bundle id until assembly exists (PLAN-audit-db.md, step 3). DuckDB is an optional dependency: anything here
raises ImportError when it is missing, and callers say so.

Derived: `<audit_db>/derived.duckdb` is attached as `derived` when it exists beside the truth in use — never beside
a `$NUGGETS_TRUTH` candidate, which has no derived of its own, unless `$NUGGETS_DERIVED` names one. Its views read
`truth.*` by catalog name, so truth is attached first. A query that reads `derived.` carries the linking cache's
build record in its pin (`derived_build`), because that cache is a build of its own: the same truth under a newer
linking code links sightings into events differently.
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


def derived_path(cfg: Config) -> Path | None:
    """`$NUGGETS_DERIVED` when set; else `<audit_db>/derived.duckdb` when it exists and no candidate truth is in use."""
    env = os.environ.get("NUGGETS_DERIVED")
    if env:
        return Path(env).expanduser()
    if os.environ.get("NUGGETS_TRUTH"):
        return None
    p = (cfg.audit_db or Path(".")) / "derived.duckdb"
    return p if p.exists() else None


def connect(cfg: Config, facts_db: Path | None = None, threads: int = 4, memory_limit: str = "4GB", derived: bool = True):
    import duckdb  # optional dependency

    truth = truth_path(cfg)
    if not truth.exists():
        raise FileNotFoundError(f"no audit DB at {truth} (nuggets.toml audit_db)")
    con = duckdb.connect(config={"threads": threads, "memory_limit": memory_limit})
    con.execute(f"ATTACH '{truth}' AS truth (READ_ONLY)")
    d = derived_path(cfg) if derived else None
    if d and d.exists():
        con.execute(f"ATTACH '{d}' AS derived (READ_ONLY)")
    if facts_db and facts_db.exists():
        con.execute(f"ATTACH '{facts_db}' AS facts (READ_ONLY)")
    return con


def has_derived(con) -> bool:
    return any(r[0] == "derived" for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall())


def derived_build(con) -> str | None:
    """The linking cache's build record (`derived.cache.builds`): code fingerprint, build time, the truth fingerprint
    it was built over and the DuckDB version — or None when derived is not attached."""
    if not has_derived(con):
        return None
    try:
        rows = con.execute("SELECT name, built_at, truth_fingerprint, code_sha, duckdb_version FROM derived.cache.builds "
                           "ORDER BY built_at DESC").fetchall()
    except Exception:  # noqa: BLE001  a derived without the cache schema (views only)
        return "attached, no cache build record"
    if not rows:
        return "attached, cache not built"
    _name, built, fp, code, ver = rows[0]
    return f"linking {code} built {built} over truth md5:{fp} (duckdb {ver})"


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
