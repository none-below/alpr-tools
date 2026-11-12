"""`coverage [--producer <LIKE>]`: which agency's logs are held for which months, from the audit DB itself.

Data arrives sporadically, with holes, overlaps and re-releases (the user's rule, 2026-11-12), so every count needs
a denominator. For each (producer agency, audit kind, month) this records the releases that hold rows in it, what
they observe (first and last instant, rows, distinct search IDs, rows with no parseable time) and what they
claim (a window read from the file or sheet name: `M_D_YYYY-M_D_YYYY`, or a month-named sheet in a year-named
workbook), and marks the month:

- `full`: rows span the month (first within its first 3 days, last within its last 3)
- `partial`: rows exist but do not span the month
- `empty`: a release claims the month and holds no rows in it (an image-only PDF, a dropped parse)
- `hole`: nothing claims it and nothing holds it, inside the span the agency's logs otherwise cover
- `+n`: n releases overlap on the month (sightings, never dropped); `dup` = rows minus distinct IDs where IDs exist

Writes `_build/coverage/coverage.csv` (one row per agency, audit, month), `coverage.releases.csv` (per release
and month) and `coverage.md` (a month grid per agency and audit), all stamped with the audit DB's code and
state. `query` prints the coverage of its window from these files. Read-only; one scan of each row table.
"""

from __future__ import annotations

import calendar
import csv
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config

FLOCK_TS = r"(\d{1,2}/\d{1,2}/\d{4}, \d{1,2}:\d{2}:\d{2} [AP]M)"
DEFAULT_TZ = "America/Los_Angeles"   # months are the agency's local months: Flock prints UTC, and a May 31 evening
                                     # search sits on June 1 UTC, which would make every monthly file "overlap" the next


def local_month(texpr: str, tz: str) -> str:
    """SQL for the YYYY-MM of a UTC instant expression in the agency's zone (DuckDB's ICU timezone())."""
    return f"strftime(date_trunc('month', timezone('{tz}', timezone('UTC', {texpr}))), '%Y-%m')"


TABLES = {
    # table: (time expression, id column, release-id filter: which releases live in this table)
    "truth.flock_audit_rows": (f"try_strptime(regexp_extract(\"Search Time\", '{FLOCK_TS}', 1), '%m/%d/%Y, %I:%M:%S %p')", '"ID"',
                               "release_id NOT LIKE 'smpd:%'"),
    "truth.smpd_pdf_rows": (f"try_strptime(regexp_extract(count_time_line, '{FLOCK_TS}', 1), '%m/%d/%Y, %I:%M:%S %p')", "id",
                            "release_id LIKE 'smpd:%'"),
}
WINDOW_RE = re.compile(r"(\d{1,2})_(\d{1,2})_(\d{4})-(\d{1,2})_(\d{1,2})_(\d{4})")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MARK = {"full": "F", "partial": "p", "empty": "0", "hole": "."}


def claimed_months(container_path: str | None, member: str | None, source_file: str | None, sheet: str | None) -> list[str]:
    """Months a release claims by its own name, as YYYY-MM strings; [] when the name says nothing."""
    for s in (member, source_file, container_path):
        m = WINDOW_RE.search(s or "")
        if m:
            try:
                a = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                b = date(int(m.group(6)), int(m.group(4)), int(m.group(5)))
            except ValueError:
                continue
            out, y, mo = [], a.year, a.month
            while (y, mo) <= (b.year, b.month):
                out.append(f"{y:04d}-{mo:02d}")
                mo += 1
                if mo > 12:
                    y, mo = y + 1, 1
            return out
    if sheet:
        words = re.findall(r"[A-Za-z]+|\d{4}", sheet)
        mon = next((MONTHS[w.lower()] for w in words if w.lower() in MONTHS), None)
        year = next((int(w) for w in words if re.fullmatch(r"\d{4}", w)), None)
        if mon and year is None:
            for s in (member, source_file, container_path):
                ys = re.findall(r"(?<!\d)(20\d{2})(?!\d)", s or "")
                if ys:
                    year = int(ys[-1])   # "through July 6 2026": the last year named is the sheet's year
                    break
        if mon and year:
            return [f"{year:04d}-{mon:02d}"]
    return []


def _month_span_mark(month: str, first: datetime | None, last: datetime | None, rows: int, claimed: bool) -> str:
    if rows == 0:
        return "empty" if claimed else "hole"
    y, m = int(month[:4]), int(month[5:])
    days = calendar.monthrange(y, m)[1]
    if first and last and first.day <= 3 and last.day >= days - 2:
        return "full"
    return "partial"


def _local(dt: datetime | None, tz: str) -> datetime | None:
    """A UTC instant from the row tables as the agency's local time (naive), for the span marks."""
    if dt is None:
        return None
    from zoneinfo import ZoneInfo
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def build_coverage(cfg: Config, producer_like: str | None = None, tz: str = DEFAULT_TZ) -> int:
    try:
        from .auditdb import connect, loader_code, state_id
        con = connect(cfg)
    except ImportError:
        print("coverage: duckdb is not importable")
        return 2
    except FileNotFoundError as e:
        print(f"coverage: {e}")
        return 2
    p = producer_like or "%"
    rel = {r[0]: r for r in con.execute(
        "SELECT release_id, producer, producer_agency_id, audit, container_path, member, source_file, sheet, n_rows "
        "FROM truth.releases WHERE producer ILIKE $p AND audit <> 'event'", {"p": p}).fetchall()}
    per_release: list[tuple] = []   # (release_id, month, rows, ids, first, last, unparsed)
    per_month: dict[tuple, dict] = {}
    for table, (texpr, idcol, lives) in TABLES.items():
        if not any(con.execute(f"SELECT 1 FROM truth.releases WHERE producer ILIKE $p AND audit <> 'event' AND {lives} LIMIT 1",
                               {"p": p}).fetchall()):
            continue   # no release of this producer lives in this table: skip the scan
        try:
            rows = con.execute(f"""
                WITH r AS (SELECT release_id FROM truth.releases WHERE producer ILIKE $p AND audit <> 'event' AND {lives}),
                     x AS (SELECT release_id, {texpr} AS t, {idcol} AS id FROM {table} WHERE release_id IN (SELECT release_id FROM r))
                SELECT release_id, {local_month('t', tz)} AS month, count(*) AS rows,
                       count(DISTINCT id) AS ids, min(t) AS first, max(t) AS last,
                       count(*) FILTER (WHERE t IS NULL) AS unparsed
                FROM x GROUP BY ALL""", {"p": p}).fetchall()
        except Exception as e:  # noqa: BLE001  a table absent from this build is not an error
            if "does not exist" in str(e) or "not found" in str(e).lower():
                continue
            raise
        per_release += rows
        # distinct ids across releases per (agency, audit, month): the undoubled count
        for aid, audit, month, ids, rows_n in con.execute(f"""
                WITH r AS (SELECT release_id, producer_agency_id, audit FROM truth.releases WHERE producer ILIKE $p AND audit <> 'event' AND {lives}),
                     x AS (SELECT release_id, {texpr} AS t, {idcol} AS id FROM {table} WHERE release_id IN (SELECT release_id FROM r))
                SELECT r.producer_agency_id, r.audit, {local_month('t', tz)} AS month,
                       count(DISTINCT id) AS ids, count(*) AS rows
                FROM x JOIN r USING (release_id) WHERE t IS NOT NULL GROUP BY ALL""", {"p": p}).fetchall():
            d = per_month.setdefault((aid, audit, month), {"ids": 0, "rows": 0})
            d["ids"] += ids
            d["rows"] += rows_n
    code, state = loader_code(con), state_id(con)
    con.close()

    # assemble per (agency, audit, month)
    cells: dict[tuple, dict] = defaultdict(lambda: {"releases": [], "rows": 0, "first": None, "last": None, "unparsed": 0, "claimed_by": []})
    names: dict[str, str] = {}
    for rid, month, rows_n, ids, first, last, unparsed in per_release:
        r = rel[rid]
        aid, audit = r[2] or r[1], r[3]
        names[aid] = r[1]
        if month is None:
            cells[(aid, audit, "unparsed")]["unparsed"] += unparsed
            continue
        first, last = _local(first, tz), _local(last, tz)
        c = cells[(aid, audit, month)]
        c["releases"].append(rid)
        c["rows"] += rows_n
        c["first"] = min(x for x in (c["first"], first) if x) if (c["first"] or first) else None
        c["last"] = max(x for x in (c["last"], last) if x) if (c["last"] or last) else None
    for rid, r in rel.items():
        aid, audit = r[2] or r[1], r[3]
        names[aid] = r[1]
        for month in claimed_months(r[4], r[5], r[6], r[7]):
            cells[(aid, audit, month)]["claimed_by"].append(rid)
    # holes: months inside an (agency, audit) span with nothing
    by_series: dict[tuple, list[str]] = defaultdict(list)
    for (aid, audit, month) in cells:
        if month != "unparsed":
            by_series[(aid, audit)].append(month)
    for (aid, audit), months in by_series.items():
        lo, hi = min(months), max(months)
        y, m = int(lo[:4]), int(lo[5:])
        while f"{y:04d}-{m:02d}" <= hi:
            cells[(aid, audit, f"{y:04d}-{m:02d}")]  # creates a hole cell when absent
            m += 1
            if m > 12:
                y, m = y + 1, 1

    out_dir = cfg.build_dir / "coverage"
    out_dir.mkdir(parents=True, exist_ok=True)
    ran = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with (out_dir / "coverage.releases.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["release_id", "producer", "audit", "month_local", "rows", "distinct_ids", "first_local", "last_local", "unparsed_rows", "claims"])
        for rid, month, rows_n, ids, first, last, unparsed in sorted(per_release, key=lambda x: (x[0], x[1] or "")):
            r = rel[rid]
            w.writerow([rid, r[1], r[3], month or "", rows_n, ids, _local(first, tz) or "", _local(last, tz) or "", unparsed,
                        ";".join(claimed_months(r[4], r[5], r[6], r[7]))])
    table_rows = []
    for (aid, audit, month), c in sorted(cells.items()):
        if month == "unparsed":
            continue
        pm = per_month.get((aid, audit, month), {"ids": 0, "rows": 0})
        mark = _month_span_mark(month, c["first"], c["last"], c["rows"], bool(c["claimed_by"]))
        dup = (pm["rows"] - pm["ids"]) if pm["ids"] else None
        table_rows.append({"agency_id": aid, "producer": names.get(aid, aid), "audit": audit, "month": month, "mark": mark,
                           "releases": len(c["releases"]), "release_ids": ";".join(c["releases"]), "claimed_by": ";".join(c["claimed_by"]),
                           "rows": c["rows"], "distinct_ids": pm["ids"] or "", "dup_rows": "" if dup is None else dup,
                           "first_local": c["first"] or "", "last_local": c["last"] or ""})
    with (out_dir / "coverage.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table_rows[0].keys()) if table_rows else ["agency_id"])
        w.writeheader()
        w.writerows(table_rows)
    # the grid
    lines = [f"# Coverage — {ran}", "", f"pin: code = {code}; state = {state}; producer filter = {p!r}; months in {tz}", "",
             "marks: F full · p partial · 0 claimed but no rows · . hole · +n overlapping releases; dup = rows beyond distinct IDs (blank where the log has no ID column)", ""]
    series: dict[tuple, list[dict]] = defaultdict(list)
    for t in table_rows:
        series[(t["producer"], t["audit"])].append(t)
    for (prod, audit), ts in sorted(series.items()):
        lines += [f"## {prod} — {audit}", "", "| year | Jan | Feb | Mar | Apr | May | Jun | Jul | Aug | Sep | Oct | Nov | Dec |", "|---|" + "---|" * 12]
        by_year: dict[str, dict[str, str]] = defaultdict(dict)
        for t in ts:
            y, m = t["month"][:4], int(t["month"][5:])
            cell = MARK[t["mark"]] + (f"+{t['releases']}" if t["releases"] > 1 else "")
            by_year[y][m] = cell
        for y in sorted(by_year):
            lines.append(f"| {y} | " + " | ".join(by_year[y].get(m, " ") for m in range(1, 13)) + " |")
        unp = cells.get((ts[0]["agency_id"], audit, "unparsed"), {}).get("unparsed", 0)
        n_dup = sum(int(t["dup_rows"]) for t in ts if t["dup_rows"] != "")
        lines += ["", f"months: {len(ts)}; full {sum(t['mark'] == 'full' for t in ts)}, partial {sum(t['mark'] == 'partial' for t in ts)}, "
                      f"claimed-empty {sum(t['mark'] == 'empty' for t in ts)}, holes {sum(t['mark'] == 'hole' for t in ts)}; "
                      f"overlapping months {sum(t['releases'] > 1 for t in ts)}; duplicate rows {n_dup}; rows with no parseable time {unp}", ""]
    (out_dir / "coverage.md").write_text("\n".join(lines) + "\n")
    print(f"coverage: {len(rel)} releases, {len(table_rows)} agency-months → {out_dir}/coverage.md, coverage.csv, coverage.releases.csv")
    return 0


def coverage_lines(cfg: Config, producer_like: str, audit: str | None, months: list[str], state: str | None = None) -> list[str]:
    """What `query` prints under a result: the marks for the months in its window, from the last coverage build."""
    p = cfg.build_dir / "coverage" / "coverage.csv"
    if not p.exists():
        return ["coverage: not built (run `nuggets coverage`); counts below have no denominator"]
    md = cfg.build_dir / "coverage" / "coverage.md"
    head = md.read_text().splitlines()[2] if md.exists() else ""
    stale = state and state not in head
    pat = re.compile("^" + re.escape(producer_like).replace("%", ".*") + "$", re.I)
    rows = [r for r in csv.DictReader(p.open()) if pat.match(r["producer"]) and (audit is None or r["audit"] == audit)]
    by_month = {r["month"]: r for r in rows}
    want = months or sorted(by_month)
    held = [m for m in want if by_month.get(m, {}).get("mark") in ("full", "partial")]
    out = [f"coverage ({producer_like}{', ' + audit if audit else ''}): {len(held)} of {len(want)} months hold rows; "
           f"full {sum(by_month.get(m, {}).get('mark') == 'full' for m in want)}, partial {sum(by_month.get(m, {}).get('mark') == 'partial' for m in want)}, "
           f"claimed-empty {sum(by_month.get(m, {}).get('mark') == 'empty' for m in want)}, holes {sum(m not in by_month or by_month[m]['mark'] == 'hole' for m in want)}"
           + ("; COVERAGE IS FROM ANOTHER STATE — rebuild it" if stale else "")]
    holes = [m for m in want if m not in by_month or by_month[m]["mark"] == "hole"]
    if holes:
        out.append("holes: " + ", ".join(holes))
    partial = [f"{m} ({by_month[m]['first_local'][:10]}…{by_month[m]['last_local'][:10]})" for m in want if by_month.get(m, {}).get("mark") == "partial"]
    if partial:
        out.append("partial: " + ", ".join(partial))
    over = [f"{m} ×{by_month[m]['releases']}" for m in want if m in by_month and int(by_month[m]["releases"]) > 1]
    if over:
        out.append("overlapping releases: " + ", ".join(over))
    return out
