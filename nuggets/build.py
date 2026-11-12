"""`build [--for gj|public]`: the store as tables.

Writes `_build/facts.json` and `_build/sources.json` (the resolved view every other command reads), `_build/stamp.json`
(data-repo, site-repo and tools commits, and the audit DB's code/state pin when it can be opened), and, when DuckDB
is importable, `_build/facts.duckdb` with `facts`, `fact_parties`, `fact_sources`, `fact_values` and the
`contracts` table (one row per contract-profile fact with typed start/end and the profile's typed keys), so a
query joins a contract's dates to audit rows. `--for <audience>` also writes `_build/for-<audience>/facts.json`:
verified/observed facts only, holds applied, and for `public` every cited node reachable as public or repo; the
pointers whose targets fell out are listed. Every build ends by printing what is newly public since the last one.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .grab import PUBLIC_ACCESS, barring_hold, holds_on, node_access
from .lint import run_lint
from .store import Fact, Store, load_store

POINTER_KEYS = ("related", "resolved_by", "amends", "replaces", "moved_to", "merged_into")


def _git(p: Path | None, *args: str) -> str | None:
    if not p or not p.exists():
        return None
    try:
        return subprocess.run(["git", "-C", str(p), *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def repo_stamp(p: Path | None) -> dict:
    h = _git(p, "rev-parse", "HEAD")
    dirty = bool(_git(p, "status", "--porcelain", "--untracked-files=no")) if h else None
    return {"path": str(p) if p else None, "commit": h, "dirty": dirty}


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def _word(basis) -> str | None:
    """The edge word an `end_basis`/`start_basis` opens with (`row:`, `open:`, `nominal:` …), or None."""
    if not basis:
        return None
    head = str(basis).split(":", 1)[0].strip()
    return head if head and " " not in head and len(head) <= 12 else None


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def fact_record(f: Fact, store: Store, errors: list[str]) -> dict:
    d = f.data
    vals = d.get("values", {})
    return {
        "id": f.id, "path": str(f.path.relative_to(store.cfg.store)), "kind": d.get("kind"), "status": d.get("status"),
        "title": d.get("title", ""), "profile": d.get("profile"), "topics": d.get("topics", []),
        "date": _jsonable(d.get("date")), "start": _jsonable(d.get("start")), "end": _jsonable(d.get("end")),
        "start_basis": d.get("start_basis"), "end_basis": d.get("end_basis"),
        "start_word": _word(d.get("start_basis")), "end_word": _word(d.get("end_basis")),
        "window": _jsonable(d.get("window")), "effective": _jsonable(d.get("effective")),
        "parties": d.get("parties", []), "in": d.get("in", []),
        "sources": [{"alias": a, **{k: v for k, v in s.items()}} for a, s in d.get("sources", {}).items()],
        "values": _jsonable(vals), "rows": _jsonable(d.get("rows", [])),
        "hold": d.get("hold"), "amends": vals.get("amends") or d.get("amends"),
        "pointers": {k: d[k] for k in POINTER_KEYS if d.get(k)},
        "verification": _jsonable(d.get("verification", {})),
        "content_sha256": hashlib.sha256(f.text.encode()).hexdigest(),
        "lint_errors": errors,
    }


def root_record(r) -> dict:
    g = r.merged()
    nodes = []
    for n in r.nodes:
        v = r.node_view(n)
        nodes.append({**_jsonable({k: x for k, x in v.items() if k != "files"}), "parent": n.parent.id if n.parent else None})
    return {**_jsonable({k: v for k, v in g.items() if k not in ("files",)}), "hold": r.hold, "nodes": nodes}


def audience_tree(store: Store, facts: list[dict], audience: str) -> tuple[list[dict], list[str]]:
    kept, dropped = [], []
    for rec in facts:
        f = store.facts[rec["id"]]
        if rec["status"] not in ("verified", "observed"):
            dropped.append(f"{rec['id']}: {rec['status']}")
            continue
        if rec["lint_errors"]:
            dropped.append(f"{rec['id']}: lint errors")
            continue
        bad = None
        for s in rec["sources"]:
            node = store.nodes.get(s.get("source", ""))
            if not node:
                bad = f"{s['alias']}: unregistered"
                break
            h = barring_hold(holds_on(node.root, node, f), audience)
            if h:
                bad = f"{s['alias']}: held ({h.get('reason')})"
                break
            if audience == "public" and node_access(node.root, node) not in PUBLIC_ACCESS:
                bad = f"{s['alias']}: {node_access(node.root, node)} only"
                break
        if bad:
            dropped.append(f"{rec['id']}: {bad}")
            continue
        kept.append(rec)
    ids = {k["id"] for k in kept}
    for k in kept:  # pointers whose target fell out of this tree
        for key, v in list(k["pointers"].items()):
            targets = v if isinstance(v, list) else [v]
            for t in targets:
                if isinstance(t, str) and t.startswith("fact:") and t[5:] not in ids:
                    dropped.append(f"{k['id']}: pointer {key} → {t} absent from this tree")
        for c in store.facts[k["id"]].data.get("contradicts", []):
            if c.get("fact") not in ids:
                dropped.append(f"{k['id']}: contradicts {c.get('fact')} absent from this tree")
    return kept, dropped


def write_duckdb(path: Path, facts: list[dict]) -> bool:
    try:
        import duckdb
    except ImportError:
        return False
    tmp = path.with_suffix(".tmp.duckdb")
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    con.execute("""CREATE TABLE facts (id VARCHAR PRIMARY KEY, kind VARCHAR, status VARCHAR, title VARCHAR, profile VARCHAR,
        date DATE, start DATE, end_date DATE, end_text VARCHAR, start_word VARCHAR, end_word VARCHAR,
        start_basis VARCHAR, end_basis VARCHAR, topics VARCHAR[], path VARCHAR, content_sha256 VARCHAR, lint_errors INTEGER)""")
    con.execute("CREATE TABLE fact_parties (fact_id VARCHAR, party_id VARCHAR, name VARCHAR, role VARCHAR)")
    con.execute("CREATE TABLE fact_sources (fact_id VARCHAR, alias VARCHAR, node_id VARCHAR, root_id VARCHAR, locator VARCHAR, note VARCHAR)")
    con.execute("CREATE TABLE fact_values (fact_id VARCHAR, key VARCHAR, value VARCHAR)")
    con.execute("""CREATE TABLE contracts (fact_id VARCHAR PRIMARY KEY, status VARCHAR, title VARCHAR,
        customer_id VARCHAR, customer VARCHAR, vendor_id VARCHAR, vendor VARCHAR,
        start DATE, start_word VARCHAR, end_date DATE, end_text VARCHAR, end_word VARCHAR,
        instrument VARCHAR, signed VARCHAR, effective VARCHAR, term VARCHAR, renewal VARCHAR,
        units_n INTEGER, units_added INTEGER, annual_fee_usd DOUBLE, amends VARCHAR, agency_signer VARCHAR, approval VARCHAR,
        content_sha256 VARCHAR)""")
    for r in facts:
        end = _as_date(r["end"])
        con.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            r["id"], r["kind"], r["status"], r["title"], r["profile"], _as_date(r["date"]), _as_date(r["start"]), end,
            None if end else (str(r["end"]) if r["end"] is not None else None), r["start_word"], r["end_word"],
            r["start_basis"], r["end_basis"], r["topics"], r["path"], r["content_sha256"], len(r["lint_errors"])])
        for p in r["parties"]:
            con.execute("INSERT INTO fact_parties VALUES (?,?,?,?)", [r["id"], p.get("id"), p.get("name"), p.get("role")])
        for s in r["sources"]:
            con.execute("INSERT INTO fact_sources VALUES (?,?,?,?,?,?)", [r["id"], s["alias"], s.get("source"), s.get("root"), s.get("locator"), s.get("note")])
        for k, v in r["values"].items():
            con.execute("INSERT INTO fact_values VALUES (?,?,?)", [r["id"], k, json.dumps(v) if isinstance(v, (list, dict)) else str(v)])
        if r["profile"] == "contract":
            v = r["values"]
            cust = next((p for p in r["parties"] if p.get("role") == "customer"), {})
            vend = next((p for p in r["parties"] if p.get("role") == "vendor"), {})

            def num(x):
                try:
                    return float(x) if x is not None else None
                except (TypeError, ValueError):
                    return None
            con.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                r["id"], r["status"], r["title"], cust.get("id"), cust.get("name"), vend.get("id"), vend.get("name"),
                _as_date(r["start"]), r["start_word"], end, None if end else (str(r["end"]) if r["end"] is not None else None), r["end_word"],
                v.get("instrument"), v.get("signed"), v.get("effective"), v.get("term"), v.get("renewal"),
                int(v["units_n"]) if str(v.get("units_n", "")).lstrip("-").isdigit() else None,
                int(v["units_added"]) if str(v.get("units_added", "")).lstrip("-").isdigit() else None,
                num(v.get("annual_fee_usd")), v.get("amends"), v.get("agency_signer"), v.get("approval"), r["content_sha256"]])
    con.close()
    os.replace(tmp, path)
    return True


def build(cfg: Config, audience: str | None = None) -> int:
    store = load_store(cfg)
    findings = run_lint(store)
    by_fact: dict[str, list[str]] = {}
    for f in findings:
        if f.level == "error":
            for fid, fact in store.facts.items():
                if fid in f.where or str(fact.path) in f.where:
                    by_fact.setdefault(fid, []).append(str(f))
    bd = cfg.build_dir
    bd.mkdir(parents=True, exist_ok=True)
    stamp = {
        "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_repo": repo_stamp(cfg.store), "site_repo": repo_stamp(cfg.site_repo), "tools_repo": repo_stamp(cfg.tools_repo),
        "audit_db": None,
    }
    try:
        from .auditdb import connect, loader_code, state_id
        con = connect(cfg)
        stamp["audit_db"] = {"code": loader_code(con), "state": state_id(con)}
        con.close()
    except Exception as e:  # noqa: BLE001 — the audit DB is optional at build time
        stamp["audit_db"] = {"error": str(e)[:200]}
    facts = [fact_record(f, store, by_fact.get(f.id, [])) for f in store.facts.values()]
    roots = [root_record(r) for r in store.roots.values()]
    (bd / "stamp.json").write_text(json.dumps(stamp, indent=1))
    (bd / "facts.json").write_text(json.dumps({"stamp": stamp, "facts": facts}, indent=1, default=str))
    (bd / "sources.json").write_text(json.dumps({"stamp": stamp, "roots": roots}, indent=1, default=str))
    db_ok = write_duckdb(bd / "facts.duckdb", facts)
    print(f"build: {len(facts)} facts, {len(roots)} roots → {bd}/facts.json, sources.json, stamp.json"
          + (", facts.duckdb" if db_ok else " (duckdb not importable: no facts.duckdb)"))
    n_err = sum(1 for f in findings if f.level == "error")
    if n_err:
        print(f"build: {n_err} lint error(s); those facts are left out of every audience tree")
    for aud in ([audience] if audience else ["gj", "public"]):
        kept, dropped = audience_tree(store, facts, aud)
        adir = bd / f"for-{aud}"
        adir.mkdir(exist_ok=True)
        prev = set()
        pf = adir / "facts.json"
        if pf.exists():
            try:
                prev = {x["id"] for x in json.loads(pf.read_text()).get("facts", [])}
            except (ValueError, KeyError):
                prev = set()
        pf.write_text(json.dumps({"stamp": stamp, "audience": aud, "facts": kept}, indent=1, default=str))
        (adir / "dropped.txt").write_text("\n".join(dropped) + ("\n" if dropped else ""))
        new = sorted({k["id"] for k in kept} - prev)
        print(f"build --for {aud}: {len(kept)} facts kept, {len(dropped)} dropped (see {adir}/dropped.txt)")
        if aud == "public":
            print("newly public since the last build: " + (", ".join(new) if new else "nothing"))
    return 0
