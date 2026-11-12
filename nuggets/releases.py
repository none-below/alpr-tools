"""`releases twins`: the same released table loaded twice through two of our own paths, compared cell by cell.

A twin pair is two releases in `truth.releases` with the same request number, workbook and sheet (and audit
kind) under different path prefixes — a committed NDJSON conversion beside the portal-harvested workbook, for
instance. Hash dedup cannot see the pair (our conversion's bytes against the staged cells), and a count by rows
doubles it. This command finds the pairs and, for each, joins the two row sets on `row_no` and counts, per
column, the cells that differ raw and the cells that still differ after trimming whitespace and treating an
empty cell as a missing one (what a faithful converter is allowed to change). The verdict per pair is
`same` (nothing beyond trimming), `trimmed` (differences that vanish under normalization) or `differs` (cells
that do not), with the columns named. The report is the basis a removal judgment cites; the command itself
removes nothing (judgments are data files with a basis, never code).

Writes `_build/releases/twins.md` and `twins.json`. Read-only on the audit DB; four threads.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import Config

TYPED = ["ID", "Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
         "Reason", "Case #", "Filters", "Search Time", "Search Type", "Text Prompt", "Moderation"]

PAIRS_SQL = """
WITH r AS (
  SELECT release_id, split_part(release_id, ':', 1) AS path,
         regexp_extract(coalesce(pra_id, ''), '([0-9]+-[0-9]+)$', 1) AS req,
         lower(regexp_replace(coalesce(source_file, ''), '\\s+', ' ', 'g')) AS wb,
         lower(trim(coalesce(sheet, ''))) AS sh, audit, n_rows, header, container_sha256
  FROM truth.releases WHERE producer ILIKE $producer)
SELECT a.release_id, b.release_id, a.n_rows, b.n_rows, a.audit, len(a.header), len(b.header), a.path, b.path
FROM r a JOIN r b ON a.req = b.req AND a.wb = b.wb AND a.sh = b.sh AND a.audit = b.audit AND a.path < b.path
WHERE a.req <> ''
ORDER BY 1
"""


def _compare_sql(table: str) -> str:
    sel = ", ".join(f'"{c}"' for c in TYPED)
    # DuckDB's trim() strips spaces only; a converter's str.strip() also removes tabs and line ends
    norm = ", ".join(f'nullif(trim("{c}", \' \\t\\r\\n\'), \'\') AS "n_{c}"' for c in TYPED)
    sums = []
    for c in TYPED:
        sums.append(f'sum(CASE WHEN a."{c}" IS NOT DISTINCT FROM b."{c}" THEN 0 ELSE 1 END) AS "raw:{c}"')
        sums.append(f'sum(CASE WHEN a."n_{c}" IS NOT DISTINCT FROM b."n_{c}" THEN 0 ELSE 1 END) AS "norm:{c}"')
    sums.append("sum(CASE WHEN CAST(a.extra AS VARCHAR) IS NOT DISTINCT FROM CAST(b.extra AS VARCHAR) THEN 0 ELSE 1 END) AS \"raw:extra\"")
    sums.append("sum(CASE WHEN a.row_no IS NULL OR b.row_no IS NULL THEN 1 ELSE 0 END) AS unmatched_rows")
    return f"""
WITH a AS (SELECT row_no, {sel}, {norm}, extra FROM {table} WHERE release_id = $a),
     b AS (SELECT row_no, {sel}, {norm}, extra FROM {table} WHERE release_id = $b)
SELECT count(*) AS joined, {", ".join(sums)}
FROM a FULL JOIN b USING (row_no)
"""


def twins(cfg: Config, producer_like: str, table: str = "truth.flock_audit_rows") -> int:
    try:
        from .auditdb import connect, loader_code, state_id
        con = connect(cfg)
    except ImportError:
        print("releases twins: duckdb is not importable")
        return 2
    except FileNotFoundError as e:
        print(f"releases twins: {e}")
        return 2
    pairs = con.execute(PAIRS_SQL, {"producer": producer_like}).fetchall()
    out_dir = cfg.build_dir / "releases"
    out_dir.mkdir(parents=True, exist_ok=True)
    sql = _compare_sql(table)
    results = []
    for i, (ra, rb, na, nb, audit, ha, hb, pa, pb) in enumerate(pairs, 1):
        row = con.execute(sql, {"a": ra, "b": rb}).fetchone()
        cols = [d[0] for d in con.description]
        m = dict(zip(cols, row))
        raw = {c: int(m[f"raw:{c}"] or 0) for c in TYPED}
        norm = {c: int(m[f"norm:{c}"] or 0) for c in TYPED}
        residual = {c: n for c, n in norm.items() if n}
        trimmed_only = {c: raw[c] - norm[c] for c in TYPED if raw[c] - norm[c]}
        verdict = "differs" if residual or m["unmatched_rows"] else ("trimmed" if trimmed_only or m["raw:extra"] else "same")
        results.append({"a": ra, "b": rb, "rows_a": na, "rows_b": nb, "audit": audit, "header_len_a": ha, "header_len_b": hb,
                        "joined": int(m["joined"]), "unmatched_rows": int(m["unmatched_rows"] or 0),
                        "raw_diffs": {c: n for c, n in raw.items() if n}, "residual_after_trim": residual,
                        "trimmed_only": trimmed_only, "extra_json_diffs": int(m["raw:extra"] or 0), "verdict": verdict})
        print(f"[{i}/{len(pairs)}] {verdict:8} {na:>7} rows  {ra[:70]}" + (f"  residual {residual}" if residual else ""))
    code, state = loader_code(con), state_id(con)
    con.close()
    ran = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = {"ran_at_utc": ran, "producer_like": producer_like, "table": table, "code": code, "state": state,
               "pairs": len(results), "same": sum(r["verdict"] == "same" for r in results),
               "trimmed": sum(r["verdict"] == "trimmed" for r in results), "differs": sum(r["verdict"] == "differs" for r in results),
               "rows_doubled": sum(r["rows_a"] for r in results), "results": results}
    (out_dir / "twins.json").write_text(json.dumps(summary, indent=1))
    lines = [f"# Twin releases for producer like {producer_like!r} — {ran}", "",
             f"pin: code = {code}; state = {state}; table = {table}",
             f"pairs: {summary['pairs']} (same {summary['same']}, trimmed {summary['trimmed']}, differs {summary['differs']}); rows loaded twice: {summary['rows_doubled']}",
             "", "verdict: same = every cell equal; trimmed = equal after trimming whitespace and treating '' as missing (and/or only the `extra` JSON differs); differs = cells that still differ, or rows one side lacks", "",
             "| verdict | rows | a | b | hdr a/b | raw diffs | residual after trim | extra json diffs |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['verdict']} | {r['rows_a']} | {r['a']} | {r['b']} | {r['header_len_a']}/{r['header_len_b']} | {r['raw_diffs'] or ''} | {r['residual_after_trim'] or ''} | {r['extra_json_diffs'] or ''} |")
    (out_dir / "twins.md").write_text("\n".join(lines) + "\n")
    print(f"releases twins: {summary['pairs']} pairs → {out_dir}/twins.md (same {summary['same']}, trimmed {summary['trimmed']}, differs {summary['differs']})")
    return 0
