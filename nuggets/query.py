"""`query <name> [--since <date>] [--summary]`: a named query, read-only, against the audit DB with the store's
facts attached as tables.

A query is `queries/<name>.toml` — looked up in the store first (investigation queries, private), then in this
repo's `queries/` (public ones). It holds the SQL, a plain-words definition (with the time zone of any date
window), `unit` (`rows` | `searches (distinct ID)`), the vocabulary it depends on, `since`, and which edge
words a bound contract date may carry. The run writes `_build/queries/<name>.csv` (plus `<name>.periods.csv`
when the query has a `sql_periods`) and `<name>.summary.md` with the pin — `code` (the loader's commit, from
truth.build_info), `state` (the state id over truth.releases), the query's and the runner's commits — and the
releases the count rests on, each with its node id and whether the store has registered it. The number becomes
a fact only by hand (README → Audit logs and queries).

Plates: a `--plate` run is not implemented yet; the CSV of a query that selects a plate column is internal
output and is never pasted anywhere (rule 6).
"""

from __future__ import annotations

import csv
import re
import subprocess
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

from . import ids
from .config import Config
from .store import load_store

TOOLS_QUERIES = Path(__file__).resolve().parent.parent / "queries"


def load_query(cfg: Config, name: str) -> tuple[dict, Path, str]:
    for base, label in ((cfg.store / "queries", "alpr-data"), (TOOLS_QUERIES, "alpr-tools")):
        p = base / f"{name}.toml"
        if p.exists():
            return tomllib.loads(p.read_text()), p, label
    raise SystemExit(f"no query named {name!r} in {cfg.store / 'queries'} or {TOOLS_QUERIES}")


def _commit_of(p: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "log", "-1", "--format=%H", "--", str(p)], capture_output=True, text=True, check=True)
        h = r.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(p.parent), "status", "--porcelain", "--", str(p)], capture_output=True, text=True).stdout.strip()
        return (h[:12] if h else "uncommitted") + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "?"


def _params_for(sql: str, params: dict) -> dict:
    """Only the named parameters the SQL mentions: DuckDB refuses a bound name the statement does not use."""
    return {k: v for k, v in params.items() if re.search(rf"\${k}\b", sql)}


def _write_csv(path: Path, cur) -> int:
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return len(rows)


def _md_table(path: Path, max_rows: int = 200) -> str:
    with path.open() as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return "(empty)\n"
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in body[:max_rows]:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    if len(body) > max_rows:
        out.append(f"| … {len(body) - max_rows} more rows in the CSV |" + " |" * (len(head) - 1))
    return "\n".join(out) + "\n"


def run_query(cfg: Config, name: str, since: str | None = None, summary: bool = False) -> int:
    q, qpath, qrepo = load_query(cfg, name)
    try:
        from .auditdb import connect, loader_code, state_id
        con = connect(cfg, facts_db=cfg.build_dir / "facts.duckdb")
    except ImportError:
        print("query: duckdb is not importable; run under an environment with duckdb (the audit DB's pinned version)")
        return 2
    except FileNotFoundError as e:
        print(f"query: {e}")
        return 2
    if not (cfg.build_dir / "facts.duckdb").exists():
        print("query: no _build/facts.duckdb; run `nuggets build` first")
        return 2
    store = load_store(cfg)
    aliases = store.parties.get("aliases", {})
    params = {}
    for k, v in q.get("params", {}).items():
        params[k] = aliases.get(v, v) if isinstance(v, str) else v
    params["since"] = since or "1900-01-01"
    out_dir = cfg.build_dir / "queries"
    out_dir.mkdir(parents=True, exist_ok=True)
    ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    main_csv = out_dir / f"{name}.csv"
    n_main = _write_csv(main_csv, con.execute(q["sql"], _params_for(q["sql"], params)))
    extra = {}
    for key in ("sql_periods",):
        if q.get(key):
            p = out_dir / f"{name}.{key.removeprefix('sql_')}.csv"
            extra[key] = (p, _write_csv(p, con.execute(q[key], _params_for(q[key], params))))
    code, state = loader_code(con), state_id(con)
    releases = []
    if q.get("releases_sql"):
        for rid, sha, n in con.execute(q["releases_sql"], _params_for(q["releases_sql"], params)).fetchall():
            nid = ids.node_id(sha) if sha else None
            releases.append((rid, sha, n, nid, "registered" if nid in store.nodes else "not registered"))
    binds = []
    if q.get("binds_sql"):
        binds = con.execute(q["binds_sql"], _params_for(q["binds_sql"], params)).fetchall()
    con.close()
    tools_commit = _commit_of(Path(__file__).resolve())
    lines = [f"# {name} — run {ran_at}", "", f"definition: {q.get('definition', '')}", f"unit: {q.get('unit', 'rows')}",
             f"since: {params['since']} (query window; the logs' own zone is UTC)",
             f"pin: code = {code}; state = {state}; query = {qrepo}/queries/{name}.toml@{_commit_of(qpath)}; runner = alpr-tools@{tools_commit}",
             f"accepted edge words for a bound start: {q.get('accept_start', [])}; a nominal: or draft edge never drives a count", ""]
    cov = q.get("coverage")
    if cov:
        from .coverage import coverage_lines
        months = []
        with main_csv.open() as fh:
            rd = csv.DictReader(fh)
            col = cov.get("month_column", "month_utc")
            months = sorted({r[col] for r in rd if r.get(col)}) if rd.fieldnames and col in rd.fieldnames else []
        if months:   # the full span, holes included, not only the months with rows
            y, m = int(months[0][:4]), int(months[0][5:])
            span = []
            while f"{y:04d}-{m:02d}" <= months[-1]:
                span.append(f"{y:04d}-{m:02d}")
                m += 1
                if m > 12:
                    y, m = y + 1, 1
            months = span
        lines += ["## Coverage of the window", ""] + coverage_lines(cfg, cov.get("producer_like", "%"), cov.get("audit"), months, state) + [""]
    lines += [f"## Result ({n_main} rows; {main_csv.name})", "", _md_table(main_csv)]
    for key, (p, n) in extra.items():
        lines += [f"## {key.removeprefix('sql_').capitalize()} ({n} rows; {p.name})", "", _md_table(p)]
    if binds:
        lines += ["## Facts this query joined to (id, content sha256, status)", ""]
        lines += [f"- {b[0]} @ {b[1]} ({b[2]})" for b in binds] + [""]
    if releases:
        lines += [f"## Releases this count rests on ({len(releases)})", "", "| release_id | container sha256 | n_rows | node id | store |", "|---|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |" for r in releases]
        lines += ["", "flagged: none recorded — the parser-free second row count is not in truth.releases yet (PLAN slice 2), so loader drops are invisible here", ""]
    smd = out_dir / f"{name}.summary.md"
    smd.write_text("\n".join(lines))
    print(f"query {name}: {n_main} rows → {main_csv}" + "".join(f", {p}" for p, _ in extra.values()) + f"; summary → {smd}")
    if summary:
        print()
        print(smd.read_text())
    return 0
