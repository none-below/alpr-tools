"""`nuggets` command line. v1: lint, sources add, verify, grab, build, query. Each subcommand lives in its module;
this file only parses arguments and sets the exit code."""

from __future__ import annotations

import argparse
import sys

from .config import Config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nuggets", description="tools for the ALPR citable-fact store")
    ap.add_argument("--store", help="store directory (default: $NUGGETS_STORE or the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lint", help="check the store against the rules; exit 1 on any error")
    p.add_argument("--warnings", action="store_true", help="print warnings too (default: errors only)")

    p = sub.add_parser("sources", help="registry commands")
    ss = p.add_subparsers(dest="scmd", required=True)
    q = ss.add_parser("add", help="write or rewrite a root's generated side from a folder")
    q.add_argument("folder")
    q.add_argument("--kind", default="pra")
    q.add_argument("--agency", required=True, help="registry agency id (or an alias from parties.toml)")
    q.add_argument("--portal", default="govqa")
    q.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("releases", help="audit-DB release checks")
    rs = p.add_subparsers(dest="rcmd", required=True)
    q = rs.add_parser("twins", help="find the same table loaded through two of our paths and compare the cells")
    q.add_argument("--producer", required=True, help="SQL LIKE pattern on releases.producer, e.g. '%Los Altos%'")
    q.add_argument("--table", default="truth.flock_audit_rows")

    p = sub.add_parser("verify", help="resolve every locator of a fact against its file and record what was looked at")
    p.add_argument("fact")
    p.add_argument("--no-render", action="store_true", help="record structure only; skip page/row artifacts")

    p = sub.add_parser("grab", help="print a fact's citation block")
    p.add_argument("fact")
    p.add_argument("--for", dest="audience", choices=["gj", "public"])

    p = sub.add_parser("build", help="write _build/: facts.json, sources.json, and the facts database")
    p.add_argument("--for", dest="audience", choices=["gj", "public"])

    p = sub.add_parser("query", help="run a named query against the audit DB and the facts database")
    p.add_argument("name")
    p.add_argument("--since")
    p.add_argument("--summary", action="store_true")

    a = ap.parse_args(argv)
    cfg = Config.load(a.store)

    if a.cmd == "lint":
        from .lint import run_lint
        from .store import load_store
        findings = run_lint(load_store(cfg))
        errors = [f for f in findings if f.level == "error"]
        for f in findings:
            if f.level == "error" or a.warnings:
                print(f)
        n_w = sum(1 for f in findings if f.level == "warn")
        print(f"lint: {len(errors)} errors, {n_w} warnings")
        return 1 if errors else 0
    if a.cmd == "sources" and a.scmd == "add":
        from .pra import add_pra_folder
        return add_pra_folder(cfg, a.folder, agency=a.agency, portal=a.portal, dry_run=a.dry_run)
    if a.cmd == "releases" and a.rcmd == "twins":
        from .releases import twins
        return twins(cfg, a.producer, table=a.table)
    if a.cmd == "verify":
        from .verify import verify_fact
        return verify_fact(cfg, a.fact, render=not a.no_render)
    if a.cmd == "grab":
        from .grab import grab
        return grab(cfg, a.fact, audience=a.audience)
    if a.cmd == "build":
        from .build import build
        return build(cfg, audience=a.audience)
    if a.cmd == "query":
        from .query import run_query
        return run_query(cfg, a.name, since=a.since, summary=a.summary)
    return 2


if __name__ == "__main__":
    sys.exit(main())
