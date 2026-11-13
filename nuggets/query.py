"""`query <name> [--since <date>] [--param k=v]… [--summary]`: a named query, read-only, against the audit DB
(truth, and derived when it is built beside it) with the store's facts attached as tables.

A query is `queries/<name>.toml` — looked up in the store first (investigation queries, private), then in this
repo's `queries/` (public ones). It holds the SQL, a plain-words definition (with the time zone of any date
window), `unit` (`rows` | `searches (distinct ID)` | `searches (linked events)`), the vocabulary it depends on,
`since`, and which edge words a bound contract date may carry. Besides `sql`, every key starting with `sql_` is a
further block run with the same parameters and written beside the main result, in the file's order
(`sql_periods` → `<stem>.periods.csv`). `[params]` are the defaults; `--param k=v` overrides one for the run, and
the output stem then carries the override (`<name>--org=example-pd`), so runs over different agencies sit side by
side instead of overwriting each other.

The run writes `_build/queries/<stem>.csv` (plus one CSV per extra block) and `<stem>.summary.md` with the pin —
`code` (the loader's commit, from truth.build_info), `state` (the state id over truth.releases), the derived
linking build when the query reads `derived.`, the query's and the runner's commits, the parameters — and the
releases the count rests on, each with its node id, whether the store has registered it, and whether truth flags
it (a second row count that disagrees with the loader's). The number becomes a fact only by hand (README → Audit
logs and queries).

Plates: a `--plate` run is not implemented yet; the CSV of a query that selects a plate column is internal
output and is never pasted anywhere (rule 6).
"""

from __future__ import annotations

import csv
import re
import subprocess
import tomllib
from datetime import datetime, timezone
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


def _slug(v) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(v).lower()).strip("-")
    return s or "x"


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


def run_query(cfg: Config, name: str, since: str | None = None, summary: bool = False, overrides: dict | None = None) -> int:
    q, qpath, qrepo = load_query(cfg, name)
    try:
        from .auditdb import connect, derived_build, has_derived, loader_code, state_id
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

    def alias(v):
        return aliases.get(v, v) if isinstance(v, str) else v

    overrides = overrides or {}
    params = {k: alias(v) for k, v in q.get("params", {}).items()}
    params.update({k: alias(v) for k, v in overrides.items()})
    params["since"] = since or "1900-01-01"
    blocks = {k: v for k, v in q.items() if k.startswith("sql_") and isinstance(v, str) and v.strip()}
    uses_derived = any("derived." in s for s in (q["sql"], *blocks.values()))
    if uses_derived and not has_derived(con):
        print("query: this query reads derived.*, and no derived.duckdb is attached (none beside the truth in use; "
              "set NUGGETS_DERIVED to name one)")
        return 2
    stem = name + "".join(f"--{k}={_slug(overrides[k])}" for k in sorted(overrides))
    out_dir = cfg.build_dir / "queries"
    out_dir.mkdir(parents=True, exist_ok=True)
    ran_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    main_csv = out_dir / f"{stem}.csv"
    n_main = _write_csv(main_csv, con.execute(q["sql"], _params_for(q["sql"], params)))
    extra = {}
    for key, sql in blocks.items():
        p = out_dir / f"{stem}.{key.removeprefix('sql_')}.csv"
        extra[key] = (p, _write_csv(p, con.execute(sql, _params_for(sql, params))))
    code, state = loader_code(con), state_id(con)
    dbuild = derived_build(con) if uses_derived else None
    releases, flagged, flag_col = [], {}, True
    if q.get("releases_sql"):
        for rid, sha, n in con.execute(q["releases_sql"], _params_for(q["releases_sql"], params)).fetchall():
            nid = ids.node_id(sha) if sha else None
            releases.append((rid, sha, n, nid, "registered" if nid in store.nodes else "not registered"))
        try:
            flags = dict(con.execute("SELECT release_id, count_mismatch_at FROM truth.releases WHERE count_mismatch_at IS NOT NULL").fetchall())
        except Exception:  # noqa: BLE001  a truth built before the second row count has no such column
            flags, flag_col = {}, False
        flagged = {r[0]: flags[r[0]] for r in releases if r[0] in flags}
    binds = []
    if q.get("binds_sql"):
        binds = con.execute(q["binds_sql"], _params_for(q["binds_sql"], params)).fetchall()
    con.close()
    tools_commit = _commit_of(Path(__file__).resolve())
    shown = {k: v for k, v in params.items() if k != "since"}
    lines = [f"# {stem} — run {ran_at}", "", f"definition: {q.get('definition', '')}", f"unit: {q.get('unit', 'rows')}",
             f"since: {params['since']} (query window; the logs' own zone is UTC)",
             "params: " + (", ".join(f"{k} = {v!r}" + (" (--param)" if k in overrides else "") for k, v in shown.items()) or "none"),
             f"pin: code = {code}; state = {state}; query = {qrepo}/queries/{name}.toml@{_commit_of(qpath)}; runner = alpr-tools@{tools_commit}"
             + (f"; derived = {dbuild}" if dbuild else ""),
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
        lines += [f"## {key.removeprefix('sql_').replace('_', ' ').capitalize()} ({n} rows; {p.name})", "", _md_table(p)]
    if binds:
        lines += ["## Facts this query joined to (id, content sha256, status)", ""]
        lines += [f"- {b[0]} @ {b[1]} ({b[2]})" for b in binds] + [""]
    if releases:
        lines += [f"## Releases this count rests on ({len(releases)})", "", "| release_id | container sha256 | n_rows | node id | store |", "|---|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} |" for r in releases]
        if not flag_col:
            lines += ["", "flagged: not checkable — this truth records no second row count (truth.releases.count_mismatch_at), so a loader drop is invisible here", ""]
        elif flagged:
            lines += ["", f"flagged: {len(flagged)} of these releases carry a second row count that disagrees with the loader's; a count "
                          "resting on them stays draft until the difference is explained (README → Audit logs and queries):"]
            lines += [f"- {rid}: diverges at {at}" for rid, at in sorted(flagged.items())] + [""]
        else:
            lines += ["", "flagged: none — every release here has a second row count that agrees with the loader's, or none that truth could take", ""]
    smd = out_dir / f"{stem}.summary.md"
    smd.write_text("\n".join(lines))
    print(f"query {stem}: {n_main} rows → {main_csv}" + "".join(f", {p}" for p, _ in extra.values()) + f"; summary → {smd}")
    if summary:
        print()
        print(smd.read_text())
    return 0
